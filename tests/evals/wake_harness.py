"""Offline 'Hey Strux' test: a fake microphone plays scripted clips in real time through the real
WakeListener (Silero VAD + whisper tiny.en + the chat STT model). No real mic, nothing sent anywhere.

  .venv/Scripts/python tests/evals/wake_harness.py [scenario ...] [--gpu]
"""
import sys
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent / "wake"            # SAPI clips (Microsoft David)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
DEV_RATE = 48000


def clip(name):
    with wave.open(str(HERE / f"{name}.wav")) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    from watcher.stt import resample
    return resample(x, 16000, DEV_RATE) * 0.6


def silence(s):
    return np.zeros(int(DEV_RATE * s), np.float32)


class FakeStream:
    timeline: np.ndarray = np.zeros(0, np.float32)

    def __init__(self, samplerate, channels, dtype, blocksize, callback):
        self.cb, self.bs = callback, blocksize

    def __enter__(self):
        self._stop = False
        self._t = threading.Thread(target=self._play, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop = True

    def _play(self):
        rng = np.random.default_rng(1)
        x, i, t0 = FakeStream.timeline, 0, time.time()
        while not self._stop:
            blk = x[i:i + self.bs] if i < len(x) else np.zeros(0, np.float32)
            blk = np.concatenate([blk, np.zeros(self.bs - len(blk), np.float32)])
            blk = blk + rng.normal(0, 0.002, self.bs).astype(np.float32)        # quiet room noise
            self.cb(blk.reshape(-1, 1), self.bs, None, None)
            i += self.bs
            time.sleep(max(0, t0 + i / DEV_RATE - time.time()))


sd = types.ModuleType("sounddevice")
sd.InputStream = FakeStream
sd.query_devices = lambda kind=None: {"name": "fake mic", "default_samplerate": DEV_RATE}
sys.modules["sounddevice"] = sd

from watcher.stt import SpeechToText  # noqa: E402
from watcher.wake import _NAME_WORD, WakeListener  # noqa: E402

SCENARIOS = {
    # name: (timeline, expected commands (lowercase substrings), slow_load_s)
    "wake_cmd": (["wake_cmd"], ["time"], 0),
    "wake_pause_cmd": (["wake_only", 1.0, "cmd"], ["calendar"], 0),
    "no_wake": (["noise_talk", 1.0, "hello"], [], 0),
    "wake_twice": (["wake_twice"], ["what's up"], 0),
    "hi_wake": (["hi_wake"], ["plan my day"], 0),                # tiny.en hears "high strokes"
    "ok_wake": (["ok_wake"], ["call mom"], 0),                   # ... and "OK, structs"
    "old_name": (["old_halo"], [], 0),                           # "Hey Halo" no longer wakes it
    "lookalikes": (["neg_stocks", 1.0, "neg_struct", 1.0, "neg_strong", 1.0, "neg_struggling"], [], 0),
    "wake_nothing": (["wake_only"], [], 0),
    # first wake while the big model is still loading; the user repeats themselves meanwhile
    "slow_load_repeat": (["wake_cmd", 4.0, "wake_cmd", 2.0, "wake_cmd"], ["time"], 15),
}


def run(name, device):
    parts, want, slow = SCENARIOS[name]
    FakeStream.timeline = np.concatenate([silence(1.0)] + [clip(p) if isinstance(p, str) else silence(p) for p in parts] + [silence(1.0)])
    cfg = {"device": device, "model": "small", "gpu_model": "large-v3-turbo", "language": "en", "beam_size": 5,
           "wake": {"enabled": True, "sound": False, "silence_ms": 1200, "listen_timeout_s": 6, "max_command_s": 30}}
    stt = SpeechToText(cfg, Path(__file__).resolve().parents[2] / "data")
    if slow:
        real = stt._load
        def slow_load():
            if stt._model is None:
                time.sleep(slow)
            return real()
        stt._load = slow_load
    got, states = [], []
    w = WakeListener(cfg, stt, on_command=got.append, on_state=states.append, suppressed=lambda: False)
    stt.keep_loaded = lambda: w.running
    t0 = time.time()
    w.start()
    total = len(FakeStream.timeline) / DEV_RATE + 6 + slow
    while time.time() - t0 < total + 40 and (time.time() - t0 < total or w.state not in ("waiting", "off")):
        time.sleep(0.2)
    w.stop()
    ok = len(got) == len(want) and all(s in g.lower() and not any(_NAME_WORD.match(w.strip(" ,.!?")) for w in g.lower().split())
                                        for s, g in zip(want, got))
    ok = ok and states.count("nothing") == (1 if name == "wake_nothing" else 0)
    print(f"{'PASS' if ok else 'FAIL'} {name}: commands={got!r} want={want} heard={w.heard} woken={w.woken} "
          f"nothing={states.count('nothing')} ({time.time() - t0:.0f}s)", flush=True)
    return ok


if __name__ == "__main__":
    dev = "cpu"
    names = [a for a in sys.argv[1:] if not a.startswith("--")] or list(SCENARIOS)
    if "--gpu" in sys.argv:
        dev = "auto"
    res = [run(n, dev) for n in names]
    print(f"{sum(res)}/{len(res)} passed")
