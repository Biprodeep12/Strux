"""Optional OCR. Uses rapidocr-onnxruntime on CPU if installed
(pip install -r requirements-ocr.txt). If not installed, ocr() returns None
and everything else keeps working.

OCR is expensive (~5-10 s per 1280 px frame on this CPU), so the screenshot
sensor runs it on a dedicated worker thread at IDLE priority, one frame at a
time, and only for frames that are worth it (see ScreenshotSensor._ocr_worth).
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading

from PIL import Image

log = logging.getLogger(__name__)

_engine = None
_lock = threading.Lock()
_unavailable = False
THREADS = 2
DET_SIDE = 736   # detection input size; smaller = faster, misses tiny text


def available() -> bool:
    global _unavailable
    if _unavailable:
        return False
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        _unavailable = True
        return False


def set_thread_idle_priority() -> None:
    """Call from the OCR worker thread: THREAD_PRIORITY_IDLE so the user's work always wins."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -15)
        except Exception:
            pass


def _get_engine():
    global _engine
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR(intra_op_num_threads=THREADS, inter_op_num_threads=1, det_limit_side_len=DET_SIDE)
        return _engine


def ocr(img: Image.Image, max_width: int = 1280) -> str | None:
    if not available():
        return None
    try:
        import numpy as np
        if img.width > max_width:
            img = img.resize((max_width, int(img.height * max_width / img.width)))
        result, _ = _get_engine()(np.asarray(img.convert("RGB")))
        if not result:
            return ""
        return "\n".join(r[1] for r in result if r and len(r) > 1)
    except Exception as e:
        log.warning("ocr failed: %s", e)
        return None
