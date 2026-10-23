"""'What's on my screen?': look at the screen NOW and answer from what is really there.

Before this, such questions went to the work log (window titles of the last hour), so Strux described
what you had been doing, not what you were looking at. Now:
  1. grab the monitor the window you were using is on (Strux's own bar / pane are painted over, like frames)
  2. OCR the text (CPU). The vision model is OFF by default: on 2026-10-22 a full-screen read (896 px, 9 image
     batches) crashed Ollama's CUDA runner and reset the display driver (nvlddmkm 13/14/153), like on 10-21.
     Switch it on with screen.vision in config.yaml; it then gets a 448 px image in batches of 64.
  3. the text model answers your question from window + URL + OCR text (+ the description, if on)
The look is kept for 5 min, so follow-ups ("what does that error mean?") reuse it without a new capture,
and it is saved as a 'screen' note in Knowledge (unless learning from screens is off). Blocklisted apps
(password managers, banking titles…) are never captured.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as w
import io
import logging
import re
import threading
import time

from . import prompts

log = logging.getLogger(__name__)

SCREEN_Q = re.compile(
    r"\b(on|in|at|of) (my|the) (screen|display|monitor|desktop)\b|\bmy screen\b|\bscreenshot (of )?(my|the) screen\b|"
    r"\bwhat('s| is| am i| are we)\s+(i'?m\s+)?(looking at|seeing|reading|watching|viewing)\b|\bwhat (am i|are we) on\b|"
    r"\b(read|see|look at|check|describe|explain|summari[sz]e|translate|what('s| is) (in|on)) (my |the |this |that )?"
    r"(screen|page|window|tab|error|code|sheet|spreadsheet|table|document|doc|email|mail|chart|dialog|popup|message)\b(?! (later|tomorrow))|"
    r"\bwhat('s| is| does) (this|that) (error|page|window|tab|sheet|dialog|popup|message|code|mean)\b|\blook (again|now)\b|"
    r"\b(can|do) you see\b", re.I)
FOLLOW_SECONDS = 300
_last: dict = {}
_lock = threading.Lock()


def wants_screen(question: str, prev_reply: str = "") -> bool:
    if SCREEN_Q.search(question):
        return True
    with _lock:
        last = dict(_last)
    # a follow-up to the screen answer just given ("and the second column?", "how do I fix it?")
    return bool(last and prev_reply and prev_reply.strip() == last.get("answer", "").strip()
                and time.time() - last["ts"] < FOLLOW_SECONDS and len(question.split()) <= 25)


# ---- capture -------------------------------------------------------------------------------------------------
_u32 = ctypes.WinDLL("user32", use_last_error=True)        # private handle: never touch shared windll argtypes


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not hwnd:
        return None
    r = w.RECT()
    if not _u32.IsWindow(w.HWND(hwnd)) or not _u32.GetWindowRect(w.HWND(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _pick_monitor(monitors: list[dict], rect) -> dict:
    """The monitor that holds the centre of the window you were using (primary if unknown)."""
    if rect:
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        for m in monitors[1:]:
            if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                return m
    return monitors[1]


def blocked(shot_cfg: dict, app: str, title: str) -> bool:
    if (app or "").lower() in {a.lower() for a in shot_cfg.get("blocklist_apps", [])}:
        return True
    t = (title or "").lower()
    return any(k.lower() in t for k in shot_cfg.get("blocklist_title_keywords", []))


def capture(hwnd: int = 0):
    """PIL image of the monitor in use, minus Strux's own windows."""
    import mss
    from PIL import Image
    from ..sensors.screenshot import _mask_own_windows
    with mss.mss() as sct:
        mon = _pick_monitor(sct.monitors, _window_rect(hwnd))
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    _mask_own_windows(img, mon["left"], mon["top"])
    return img


def _b64(img, side: int, quality: int = 80) -> str:
    im = img.copy()
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---- read ------------------------------------------------------------------------------------------------------
def read(llm, img, vision_model: str, use_vision: bool = False, side: int = 448) -> tuple[str, str, list[str]]:
    """(OCR text, vision description, problems). OCR (CPU) and vision (GPU) run at the same time."""
    from .. import ocr as ocr_mod
    out = {"ocr": "", "vision": ""}
    problems: list[str] = []

    def do_ocr():
        if not ocr_mod.available():
            problems.append("OCR isn't installed")
            return
        t = ocr_mod.ocr(img, max_width=1600)
        out["ocr"] = (t or "").strip()

    def do_vision():
        if not use_vision:
            return
        # same safety limits as image attachments (vision.py): small image, short answer, small batches, unload after
        text = llm.chat([{"role": "user", "content": prompts.get("screen_vision")}], max_tokens=320, temperature=0.0,
                        model=vision_model, images=[_b64(img, side, 85)], keep_alive=0, options={"num_ctx": 4096, "num_batch": 64})
        if text:
            out["vision"] = text.strip()
        else:
            problems.append("the image model didn't answer" + (f" ({llm.last_error[:80]})" if llm.last_error else ""))

    threads = [threading.Thread(target=do_ocr, daemon=True), threading.Thread(target=do_vision, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    return out["ocr"], out["vision"], problems


def _tidy_ocr(text: str, limit: int = 3500) -> str:
    """OCR lines, de-duplicated, bounded (the 7B model has a 4k context)."""
    seen, lines = set(), []
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) < 2 or ln in seen:
            continue
        seen.add(ln)
        lines.append(ln)
    out = "\n".join(lines)
    return out if len(out) <= limit else out[:limit] + "\n…"


def context(target: dict, ocr_text: str, vision_text: str) -> str:
    from ..overlay import app_label
    parts = [f"Window in front: {app_label(target.get('app') or '') or '?'} — “{(target.get('title') or '').strip()[:160]}”"]
    if target.get("url"):
        parts.append(f"Page URL: {target['url'][:200]}")
    if vision_text:
        parts.append("What the image model sees:\n" + vision_text[:1500])
    if ocr_text:
        parts.append("Exact text read from the screen (OCR, top to bottom; may be jumbled):\n" + _tidy_ocr(ocr_text))
    if not vision_text and not ocr_text:
        parts.append("(Nothing could be read from the pixels; only the window title is known.)")
    return "\n\n".join(parts)


def answer(llm, question: str, history: list[dict], target: dict, shot_cfg: dict, vision_model: str, name: str = "",
           knowledge=None, learn_screens: bool = True, info: dict | None = None, force: bool = False,
           use_vision: bool = False) -> str | None:
    """Answer about the screen. `target` = {hwnd, app, title, url} of the window you were using.
    `info` (optional) receives the thumbnail + what was looked at, for the pane. `force` = the eye button: always a new look."""
    info = info if info is not None else {}
    from .chat import tidy, who
    app, title = target.get("app") or "", target.get("title") or ""
    if blocked(shot_cfg, app, title):
        return "I don't look at that window: it's on your privacy blocklist (Settings → screenshots)."
    with _lock:
        last = dict(_last)
    reuse = bool(last) and not force and time.time() - last["ts"] < FOLLOW_SECONDS and not SCREEN_Q.search(question)   # a follow-up
    if reuse:
        ctx, info["thumb"], info["reused"] = last["ctx"], None, True
    else:
        try:
            img = capture(int(target.get("hwnd") or 0))
        except Exception as e:
            log.warning("screen capture failed: %s", e)
            return f"I couldn't capture the screen ({type(e).__name__}). Is the session locked?"
        t0 = time.time()
        ocr_text, vision_text, problems = read(llm, img, vision_model, use_vision)
        info["vision"] = bool(vision_text)
        log.info("screen look: ocr %d chars, vision %d chars in %.1fs%s", len(ocr_text), len(vision_text), time.time() - t0,
                 f" ({'; '.join(problems)})" if problems else "")
        ctx = context(target, ocr_text, vision_text)
        info["thumb"] = _b64(img, 520, 70)
        info["problems"] = problems
        if knowledge is not None and learn_screens and (ocr_text or vision_text):
            from ..overlay import app_label
            body = (vision_text or "")[:700] + ("\nText: " + _tidy_ocr(ocr_text, 900) if ocr_text else "")
            knowledge.upsert(f"screen:{int(time.time())}", "screen", time.time(), title[:200], body, app_label(app),
                             {"app": app, "url": target.get("url", "")})
    info["app"], info["title"] = app, title
    system = prompts.get("screen", who=who(name))
    convo = [{"role": "system", "content": prompts.with_memory(system) + "\n\nSCREEN (captured just now):\n" + ctx}] \
        + [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
    text = llm.chat(convo, max_tokens=380, temperature=0.1)
    if text:
        text = tidy(text)
        with _lock:
            _last.clear()
            _last.update({"ts": last["ts"] if reuse else time.time(), "ctx": ctx, "answer": text})
    return text


def forget() -> None:
    with _lock:
        _last.clear()
