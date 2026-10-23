""""Hey Strux": hands-free chat. Off unless you switch it on (Settings -> Voice), because the microphone
then stays open. Everything runs on this PC and no audio is ever written anywhere.

  mic (16 kHz) -> Silero VAD (tiny, ~1% CPU) -> a burst of speech ended
     -> its first 3 s through whisper tiny.en: does it start with "hey Strux"?
        no  -> forget it
        yes -> the rest of that burst is the message; if there is none, record until you stop talking
               (1.2 s of silence) -> whisper small -> on_command(text) -> the bar chat sends it

The listener stands down while Strux is paused, the PC is locked, the mic button is recording, or a reply
is being read out (so Strux never wakes itself).
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from typing import Callable

import numpy as np

from .stt import RATE, resample

log = logging.getLogger(__name__)

FRAME = 512                                   # Silero's frame at 16 kHz (32 ms)
# how whisper spells "Strux" (measured: SAPI voices through tiny.en give "Strucks", "strokes", "structs"; "Hi" -> "high")
NAME = r"(?:str[uoa](?:x|cks?|ks?|cts?|ts?|kes?)|str[iy]x)"
_NAME_WORD = re.compile(NAME + r"$", re.I)
GREET_RE = re.compile(r"^\W*(hey|hi|high|hay|hei|heya|hiya|hello|okay|ok|yo)[\s,.!]+([a-z']+)\b[\s,.!?]*", re.I)
PROMPT = "Hey Strux."                         # biases tiny.en to spell the name (SAPI clips: 30/30 "Strux", 0/24 look-alikes woke)


def is_name(word: str) -> bool:
    """'Strux' as whisper may write it for other voices / accents: the known spellings, or close to 'struks'
    ('Starks', 'Stucks', 'Strugs'), but not 'stocks', 'strong', 'struggling'."""
    w = (word or "").lower().strip(" ,.!?'")
    if _NAME_WORD.match(w):
        return True
    n = w.replace("x", "ks").replace("ck", "k").replace("c", "k").replace("z", "s")
    return n.startswith("st") and len(n) <= 8 and difflib.SequenceMatcher(None, n, "struks").ratio() >= 0.8


def find_wake(text: str) -> re.Match | None:
    """'Hey Strux, what's next' -> match whose end() is where the message starts."""
    m = GREET_RE.match(text or "")                        # the phrase has to open what was said
    return m if m and is_name(m.group(2)) else None


GREETING = {"hey", "hi", "high", "hay", "hello", "okay", "ok", "yo", "oh"}


def strip_greeting(text: str) -> str:
    """'Hello, add a todo…' / 'Strux add a todo…' -> 'add a todo…': drop leading greeting words only, never real words."""
    words = (text or "").split()
    i = 0
    while i < min(3, len(words)) and ((w := words[i].strip(" ,.!?").lower()) in GREETING or is_name(w)):
        i += 1
    return " ".join(words[i:])


def command_after(text: str) -> str:
    m = find_wake(text)
    return (text[m.end():] if m else "").strip(" ,.!?") if text else ""


def message_after(text: str) -> str:
    """The message in a full transcript. Drops every wake phrase / greeting it opens with, because people repeat
    themselves while waiting: 'Hey Strux. Hey Strux, what's up' -> "what's up", 'Hey Strux. Strux, Strux' -> ''."""
    t = text or ""
    for _ in range(4):
        m = find_wake(t)
        if not m:
            break
        t = t[m.end():]
    return strip_greeting(t).strip(" ,.!?")


class Vad:
    """Streaming speech probability per 32 ms frame (Silero v6 via faster-whisper's bundled model)."""

    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model
        self.model = get_vad_model()
        self.window = np.zeros(FRAME * 8, np.float32)     # a little context for each call

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        n = len(frames) // FRAME * FRAME
        if not n:
            return np.zeros(0, np.float32)
        self.window = np.concatenate([self.window, frames[:n]])[-(FRAME * 8 + n):]
        probs = self.model(self.window[-(len(self.window) // FRAME * FRAME):]).reshape(-1)
        return probs[-(n // FRAME):]


class WakeListener:
    def __init__(self, cfg: dict, stt, on_command: Callable[[str], None], on_state: Callable[[str], None],
                 suppressed: Callable[[], bool]) -> None:
        self.cfg = cfg                       # the `stt` config block; wake settings live under stt.wake
        self.stt = stt
        self.on_command = on_command
        self.on_state = on_state
        self.suppressed = suppressed
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tiny = None
        self.state = "off"                   # off | waiting | listening | transcribing | error: …
        self.heard = 0                       # bursts of speech checked
        self.woken = 0
        self.debug_until = 0.0

    @property
    def wcfg(self) -> dict:
        return self.cfg.setdefault("wake", {})

    # ---- lifecycle ----------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._tiny = None                    # free the memory
        self._set("off")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            try:
                self.on_state(state)
            except Exception:
                log.exception("wake state callback")

    # ---- the loop ---------------------------------------------------------------------------------
    def _run(self) -> None:
        import sounddevice as sd
        try:
            from faster_whisper import WhisperModel
            self.stt.models_dir.mkdir(parents=True, exist_ok=True)
            self._tiny = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=2,
                                      download_root=str(self.stt.models_dir))
            vad = Vad()
            dev = sd.query_devices(kind="input")
            rate = int(dev["default_samplerate"]) or RATE
        except Exception as e:
            log.warning("wake listener could not start: %s", e)
            self._set(f"error: {e}")
            return
        log.info("wake listener on (%s)", dev["name"])
        self.stt.preload()                       # the chat model loads now, not on the first "Hey Strux" (took 30-45 s)
        mins = float(self.wcfg.get("debug_minutes", 0) or 0)
        if mins > 0:                             # logs what was said near the mic: on for a while only, then by itself off
            self.debug_until = time.time() + mins * 60
            log.info("wake debug on for %.0f min: bursts are logged as text", mins)
        blocks: list[np.ndarray] = []
        lock = threading.Lock()

        def cb(indata, frames, t, status):
            with lock:
                blocks.append(indata[:, 0].copy())

        try:
            with sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * 0.096), callback=cb):
                self._set("waiting")
                self._listen(blocks, lock, rate, vad)
        except Exception as e:
            log.warning("wake listener stopped: %s", e)
            self._set(f"error: {e}")
            return
        log.info("wake listener off")

    def _take(self, blocks, lock, rate) -> np.ndarray:
        with lock:
            got, blocks[:] = list(blocks), []
        return resample(np.concatenate(got), rate) if got else np.zeros(0, np.float32)

    def _listen(self, blocks, lock, rate, vad) -> None:
        pre = np.zeros(0, np.float32)            # ~0.3 s before speech starts, so the first word isn't clipped
        seg: list[np.ndarray] = []
        quiet = 0                                # frames of silence since the last speech
        carry = np.zeros(0, np.float32)
        while not self._stop.is_set():
            time.sleep(0.05)
            audio = self._take(blocks, lock, rate)
            if self.suppressed():
                seg, quiet, pre, carry = [], 0, np.zeros(0, np.float32), np.zeros(0, np.float32)
                continue
            if not len(audio):
                continue
            audio = np.concatenate([carry, audio])
            n = len(audio) // FRAME * FRAME
            audio, carry = audio[:n], audio[n:]
            if not n:
                continue
            for frame, p in zip(audio.reshape(-1, FRAME), vad(audio)):
                if seg:
                    seg.append(frame)
                    quiet = quiet + 1 if p < 0.35 else 0
                    dur = len(seg) * FRAME / RATE
                    if quiet * FRAME / RATE >= 0.35 or dur >= 8:
                        woke = self._burst(np.concatenate(seg), blocks, lock, rate, vad)
                        seg, quiet = [], 0
                        pre = np.zeros(0, np.float32)
                        if woke:                     # the rest of this chunk is older than the command: start fresh
                            carry = np.zeros(0, np.float32)
                            break
                elif p >= 0.5:
                    seg = [pre, frame]
                else:
                    pre = np.concatenate([pre, frame])[-int(RATE * 0.3):]

    def _burst(self, audio: np.ndarray, blocks, lock, rate, vad) -> bool:
        """A burst of speech ended: was it 'Hey Strux …'? True when it was (and has been handled)."""
        if len(audio) < RATE * 0.35:
            return False
        self.heard += 1
        head = audio[: RATE * 3]
        segs, _ = self._tiny.transcribe(head, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False,
                                        without_timestamps=True, initial_prompt=PROMPT)
        quick = " ".join(s.text for s in segs).strip()
        log.debug("burst %.1fs, wake=%s", len(audio) / RATE, bool(find_wake(quick)))   # never what was said
        if time.time() < self.debug_until:       # opt-in (stt.wake.debug_minutes): what the wake check heard, to tune it
            log.info("wake debug: %.1fs heard %r -> %s", len(audio) / RATE, quick[:60], "WAKE" if find_wake(quick) else "no")
        if not find_wake(quick):
            return False
        self.woken += 1
        log.info("wake phrase heard")
        self._set("listening")
        self._beep()
        # keep listening: "Hey Strux, what's next" arrives split at the comma, "Hey Strux … (pause) … what's next" later.
        # Everything until you stop talking is the message, transcribed once with the better model.
        talking = bool(command_after(quick))
        more = self._record_until_silence(blocks, lock, rate, vad, talking)
        if more is None and not talking:
            rest = ""                            # said nothing after "Hey Strux"
        else:
            text = self._small(np.concatenate([audio, more]) if more is not None else audio)
            rest = message_after(text)          # also when the big model spells the greeting differently
        # what the mic caught while we transcribed is not part of this message (often "Hey Strux?" again while the
        # model loaded): replaying it later fired a burst of stale commands, so it is dropped
        self._take(blocks, lock, rate)
        self._set("waiting")
        if len(rest.split()) >= 1 and not _junk(rest):
            log.info("voice message: %d chars", len(rest))
            try:
                self.on_command(rest)
            except Exception:
                log.exception("voice command")
        else:
            self.on_state("nothing")
        return True

    def _record_until_silence(self, blocks, lock, rate, vad, talking: bool = False) -> np.ndarray | None:
        """Record until 1.2 s of silence. `talking`: the message already started (no wait for it to begin).
        What arrived while the wake check ran is kept: it is the start of the message."""
        silence = float(self.wcfg.get("silence_ms", 1200)) / 1000
        wait = float(self.wcfg.get("listen_timeout_s", 6))
        longest = float(self.wcfg.get("max_command_s", 30))
        got: list[np.ndarray] = []
        started, last_voice, t0 = talking, time.time(), time.time()
        carry = np.zeros(0, np.float32)
        while not self._stop.is_set():
            time.sleep(0.05)
            a = np.concatenate([carry, self._take(blocks, lock, rate)])
            n = len(a) // FRAME * FRAME
            a, carry = a[:n], a[n:]
            if n:
                got.append(a)
                if (vad(a) >= 0.5).any():
                    started, last_voice = True, time.time()
            now = time.time()
            if not started and now - t0 > wait:
                return None                      # said nothing after "Hey Strux"
            if started and now - last_voice >= silence:
                break
            if now - t0 > longest:
                break
        return np.concatenate(got) if got else None

    def _small(self, audio: np.ndarray) -> str:
        self._set("transcribing")
        try:
            return self.stt.transcribe(audio)["text"]
        except Exception as e:
            log.warning("voice transcription failed: %s", e)
            return ""

    def _beep(self) -> None:
        if not self.wcfg.get("sound", True):
            return
        try:
            import winsound
            winsound.MessageBeep(0x40)          # MB_ICONASTERISK: Windows' soft "ding"
        except Exception:
            pass

    def status(self) -> dict:
        return {"enabled": bool(self.wcfg.get("enabled", False)), "state": self.state, "heard": self.heard, "woken": self.woken}


def _junk(text: str) -> bool:
    """Whisper's stock outputs for noise."""
    t = text.lower().strip(" .!?")
    return t in ("", "you", "thank you", "thanks for watching", "bye", "okay", "ok") or len(t) < 2
