"""On-demand vision: explain what is on a stored screenshot (errors, dialogs).
Uses a small VLM through Ollama (default qwen2.5vl:3b, ~3 GB). Only one model
is resident at a time thanks to keep_alive: 0 on both calls.
"""
from __future__ import annotations

import base64
from pathlib import Path

from . import prompts



def explain_frame(llm, frame_row, vision_model: str, question: str | None = None) -> dict:
    path = frame_row["image_path"] if frame_row else None
    if not path or not Path(path).exists():
        return {"ok": False, "error": "no image stored for this frame (tier is cold/dropped or it was forgotten)"}
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    msgs = [{"role": "user", "content": (question or prompts.get("vision")) + f"\n(app: {frame_row['app']}, title: {frame_row['title'][:80]})"}]
    text = llm.chat(msgs, max_tokens=300, temperature=0.2, model=vision_model, images=[b64])
    if text is None:
        return {"ok": False, "error": llm.last_error or "vision model call failed"}
    return {"ok": True, "text": text, "model": vision_model, "latency": round(llm.last_latency, 1)}


# ---- chat attachments: read an image into text once, then the 7B text model (with tools) does the rest ----
# Why not answer with the vision model directly: the 3B vision model can't call the TODO / calendar tools and is
# weaker at reasoning, and on a 6 GB laptop GPU that also drives the display a long vision generation once crashed
# Ollama's runner (0xc0000409) and reset the display driver (nvlddmkm events 14/153: the screen went black for a
# second). So reads are short (num_predict 900), small images (<= 896 px), small batches (shorter GPU kernels,
# well under Windows' 2 s TDR watchdog), unloaded right after (keep_alive 0), and cached per image.
_read_cache: "dict[str, str]" = {}


def _resize_b64(b64: str, max_side: int) -> str:
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def read_image(llm, b64: str, model: str) -> tuple[str | None, str | None]:
    """(text read from the image, error). Same image twice (a follow-up question) = no second GPU run."""
    import hashlib
    key = hashlib.sha1(b64.encode("ascii")).hexdigest()
    if key in _read_cache:
        return _read_cache[key], None
    err = None
    for side in (896, 640):                     # second try smaller: less GPU work per step
        img = _resize_b64(b64, side)
        text = llm.chat([{"role": "user", "content": prompts.get("vision_chat")}], max_tokens=900, temperature=0.0,
                        model=model, images=[img], keep_alive=0, options={"num_ctx": 4096, "num_batch": 128})
        if text:
            if len(_read_cache) > 32:
                _read_cache.pop(next(iter(_read_cache)))
            _read_cache[key] = text
            return text, None
        err = llm.last_error
    if err and "500" in err:
        err = "the image model crashed on this picture (on this GPU that can also reset the display for a moment); try a smaller crop"
    return None, err or "the image model returned nothing"
