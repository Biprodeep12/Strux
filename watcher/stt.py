"""Speech to text for the chats, fully local: the microphone is recorded here (sounddevice) and
transcribed with faster-whisper, so audio never leaves the PC.

On the GPU (config `stt.device`: auto | cuda | cpu) it runs `stt.gpu_model` (large-v3-turbo, ~1.2 GB
VRAM, ~0.8 s for 10 s of speech; measured 2026-10-23: Ollama keeps the same 82 % GPU split and
speed with it resident). The CUDA runtime comes from the nvidia-cublas-cu12 / nvidia-cudnn-cu12
wheels in the venv. Any CUDA failure, at load or mid-transcribe, falls back to `stt.model` on the CPU.

  start()   open the default microphone (nothing is recorded before this)
  level()   0..1 loudness of the last ~0.1 s, for the button's meter
  stop()    close the microphone and return the text
  cancel()  close the microphone and throw the audio away

The Whisper model (config `stt.model`, default "small", ~480 MB) is downloaded once into
data/models/ on first use, loaded when you first press the mic and unloaded after
`stt.unload_minutes` without use, so it costs no memory while you are not talking.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

RATE = 16_000                  # what Whisper wants
_cuda_ok: bool | None = None


def cuda_available() -> bool:
    """Put the pip-installed CUDA DLLs (site-packages/nvidia/*/bin) on the DLL path once; True when a GPU is usable."""
    global _cuda_ok
    if _cuda_ok is None:
        import os
        import sys
        try:
            roots = [Path(getattr(sys, "_MEIPASS", "")) / "nvidia"]
            import importlib.util
            spec = importlib.util.find_spec("nvidia")
            roots += [Path(p) for p in (spec.submodule_search_locations or [])] if spec else []
            for b in {b for r in roots if r.is_dir() for b in r.glob("*/bin")}:
                os.add_dll_directory(str(b))
                os.environ["PATH"] = f"{b}{os.pathsep}{os.environ.get('PATH', '')}"
            import ctranslate2
            _cuda_ok = ctranslate2.get_cuda_device_count() > 0
        except Exception as e:
            log.info("no CUDA for speech: %s", e)
            _cuda_ok = False
    return _cuda_ok


def resample(x: np.ndarray, src: int, dst: int = RATE) -> np.ndarray:
    """Linear resampling; plenty for speech."""
    if src == dst or not len(x):
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


class SpeechToText:
    def __init__(self, cfg: dict, data_dir: str | Path, hints: Callable[[], str] = lambda: "") -> None:
        self.cfg = cfg
        self.models_dir = Path(data_dir) / "models"
        self.hints = hints                       # names / projects that Whisper should spell right
        self._lock = threading.RLock()
        self._model = None
        self._model_state = "not loaded"         # not loaded | downloading | loading | ready | error: …
        self._last_used = 0.0
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._rate = RATE
        self._started = 0.0
        self._level = 0.0
        self._unloader: threading.Timer | None = None
        self.keep_loaded: Callable[[], bool] = lambda: False   # "Hey Strux" is on: stay ready
        self._cuda_failed = False                # a CUDA error this run: stay on the CPU

    # ---- model ------------------------------------------------------------------------------
    @property
    def device(self) -> str:
        want = str(self.cfg.get("device", "auto"))
        if want == "cpu" or self._cuda_failed:
            return "cpu"
        return "cuda" if cuda_available() else "cpu"

    @property
    def model_name(self) -> str:
        if self.device == "cuda":
            return str(self.cfg.get("gpu_model") or self.cfg.get("model", "small"))
        return str(self.cfg.get("model", "small"))

    def preload(self) -> None:
        """Load the model in the background (first press of the mic), so stop() is quick."""
        if self._model is None and (self._model_state == "not loaded" or self._model_state.startswith("error")):
            threading.Thread(target=self._load, name="stt-load", daemon=True).start()

    def _load(self):
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel
            self.models_dir.mkdir(parents=True, exist_ok=True)
            device, name = self.device, self.model_name
            have = any(self.models_dir.glob(f"models--*faster-whisper-{name}*"))
            self._model_state = "loading" if have else "downloading"
            t0 = time.time()
            try:
                threads = int(self.cfg.get("cpu_threads", 0)) or max(2, min(8, (__import__("os").cpu_count() or 4) // 2))
                self._model = WhisperModel(name, device=device, compute_type="int8_float16" if device == "cuda" else "int8",
                                           cpu_threads=threads, download_root=str(self.models_dir))
            except Exception as e:
                if device == "cuda":
                    log.warning("speech model failed on the GPU, using the CPU: %s", e)
                    self._cuda_failed = True
                    return self._load()
                self._model_state = f"error: {e}"
                log.warning("speech model failed to load: %s", e)
                raise
            self._model_state = "ready"
            log.info("speech model %s ready on %s in %.1fs", name, device, time.time() - t0)
            return self._model

    def _schedule_unload(self) -> None:
        if self._unloader:
            self._unloader.cancel()
        mins = float(self.cfg.get("unload_minutes", 5))
        if mins <= 0:
            return
        self._unloader = threading.Timer(mins * 60, self._unload_if_idle)
        self._unloader.daemon = True
        self._unloader.start()

    def _unload_if_idle(self) -> None:
        with self._lock:
            if self.keep_loaded():
                return
            if self._stream is None and time.time() - self._last_used >= float(self.cfg.get("unload_minutes", 5)) * 60 - 1:
                self._model = None
                self._model_state = "not loaded"
                log.info("speech model unloaded (idle)")

    def vram_mb(self) -> int:
        """VRAM our resident model holds (NVML can't see per-process use under WDDM): ~1.2 GB for large-v3-turbo."""
        loaded_on_gpu = self._model is not None and self.device == "cuda"
        return int(self.cfg.get("gpu_vram_mb", 1300)) if loaded_on_gpu else 0

    # ---- recording ----------------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> dict:
        import sounddevice as sd
        with self._lock:
            if self._stream is not None:
                return self.status()
            dev = sd.query_devices(kind="input")
            self._rate = int(dev["default_samplerate"]) or RATE
            self._chunks, self._level = [], 0.0
            max_s = float(self.cfg.get("max_seconds", 120))

            def cb(indata, frames, t, status):
                mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
                self._chunks.append(mono.copy())
                self._level = min(1.0, float(np.sqrt(np.mean(mono ** 2))) * 6)
                if time.time() - self._started > max_s:
                    raise sd.CallbackStop

            self._stream = sd.InputStream(samplerate=self._rate, channels=1, dtype="float32", blocksize=int(self._rate * 0.1),
                                          callback=cb)
            self._started = time.time()
            self._stream.start()
        log.info("microphone on (%s, %d Hz)", dev["name"], self._rate)
        self.preload()
        return self.status()

    def _close(self) -> np.ndarray:
        with self._lock:
            s, self._stream = self._stream, None
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
                log.info("microphone off")
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks, self._level = [], 0.0
        return audio

    def cancel(self) -> None:
        self._close()

    def stop(self) -> dict:
        """Close the mic and transcribe. Returns {text, seconds, latency} (text '' when nothing was said)."""
        audio = resample(self._close(), self._rate)
        seconds = len(audio) / RATE
        if seconds < 0.3 or float(np.abs(audio).max(initial=0)) < 0.003:
            return {"text": "", "seconds": round(seconds, 1), "latency": 0.0, "note": "nothing heard"}
        return self.transcribe(audio)

    def transcribe(self, audio: np.ndarray) -> dict:
        t0 = time.time()
        lang = self.cfg.get("language") or None          # "en" by default; "" / null = detect
        try:
            model = self._load()
            segs, info = model.transcribe(audio, language=lang, beam_size=int(self.cfg.get("beam_size", 5)), vad_filter=True,
                                          initial_prompt=(self.hints() or None), condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segs).strip()      # segments are lazy: CUDA errors surface here
        except RuntimeError as e:
            if self.device != "cuda":
                raise
            log.warning("speech failed on the GPU, retrying on the CPU: %s", e)
            with self._lock:
                self._model, self._cuda_failed = None, True
            return self.transcribe(audio)
        self._last_used = time.time()
        self._schedule_unload()
        out = {"text": text, "seconds": round(len(audio) / RATE, 1), "latency": round(time.time() - t0, 2),
               "language": info.language, "model": f"whisper-{self.model_name}"}
        log.info("speech: %.1fs audio -> %d chars in %.1fs", out["seconds"], len(text), out["latency"])
        return out

    def status(self) -> dict:
        return {"recording": self.recording, "level": round(self._level, 3),
                "elapsed": round(time.time() - self._started, 1) if self.recording else 0.0,
                "model": f"whisper-{self.model_name}", "device": self.device, "model_state": self._model_state,
                "max_seconds": float(self.cfg.get("max_seconds", 120))}
