"""Screenshot sensor: capture -> dedupe -> blocklist -> (OCR) -> score -> tier.

Triggers
- a `window` event on the bus (if on_window_change) and
- a timer every interval_seconds.
Both go through the same `_maybe_capture()`; a min_gap guard stops bursts.

What is stored
- Every captured frame gets a row in `frames` with phash + score + tier.
- Only frames that pass dedupe and blocklist are saved to disk, as WebP, under
  data/frames/YYYY-MM-DD/. The retention job later shrinks or deletes them.
- Blocked apps produce a `blocked` event and nothing else (no pixels touched).

Scoring (0..100)
  +30 app changed since last frame
  +25 big visual / text change (pHash distance >= 16, or OCR diff >= 40%)
  +20 error / stack-trace keywords in title or OCR text
  +10 clipboard changed in the last 5 s
  +10 user active in the last 60 s      (-30 if idle >= 2 min)
  +40 retroactively when a suggestion in its window gets accepted (store.set_feedback)
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from .. import ocr as ocr_mod
from ..events import Event
from . import phash, winapi
from .base import Sensor

log = logging.getLogger(__name__)

ERROR_RE = re.compile(
    r"\b(error|exception|traceback|failed|fatal|panic|segfault|unhandled|stack trace|"
    r"ENOENT|TypeError|NullPointer|undefined is not|cannot read propert|exit code [1-9])\b",
    re.IGNORECASE,
)


# stricter, for OCR'd screen text: real error *output*, not the word "error" in prose or code
OCR_ERROR_RE = re.compile(
    r"(Traceback \(most recent call last\)|\b[A-Z]\w*(Error|Exception)\b:|^\s*error(\[\w+\])?:|\berror (TS|CS|C)\d+|npm ERR!|"
    r"\bFAILED\b|^fatal:|panic:|Unhandled (exception|rejection)|Segmentation fault|exited with code [1-9]|exit code [1-9]|"
    r"ENOENT|EADDRINUSE|ECONNREFUSED|Cannot read propert|is not defined\b|ModuleNotFoundError|SyntaxError)",
    re.MULTILINE,
)


# source code that merely mentions an error type: `except ValueError:`, `raise TypeError(`, `catch (IOException e)`
CODE_LINE_RE = re.compile(r"^\W{0,3}\s*(\d+\s+)?(except|catch|raise|throw|throws|class|def|import|from|assert|"
                          r"if|elif|return|new|extends|@|//|#|\*)\b|^\s*\d+\s*[|:]?\s*(except|catch|raise|throw)\b|"
                          r"\b(except|catch)\s*\(?\s*[A-Z]\w*(Error|Exception)", re.I)


def new_error_line(text: str, previous: str | None) -> str | None:
    """First error-looking line in `text` that was not already on screen last time."""
    seen = set((previous or "").splitlines())
    for line in text.splitlines():
        if "Want an explanation" in line or "Error in " in line or "Want a quick" in line:
            continue                       # Strux's own suggestion text (in the bar, or quoted in a chat / terminal)
        if CODE_LINE_RE.search(line):
            continue
        if OCR_ERROR_RE.search(line) and line not in seen:
            return line.strip()[:160]
    return None


def _own_window_rects() -> list[tuple[int, int, int, int]]:
    """Screen rects (physical px) of what Strux itself shows: the drawn part of the native bar (its
    window is mostly transparent, so not the window rect) and the drop-down pane while it is open."""
    import ctypes
    import ctypes.wintypes as w
    import sys
    out = []
    bar = sys.modules.get("watcher.ui.nativebar")
    if bar is not None:
        out += bar.visible_rects()
    u = ctypes.windll.user32
    h = u.FindWindowW(None, "Strux pane")
    if h and u.IsWindowVisible(h):
        r = w.RECT()
        if u.GetWindowRect(h, ctypes.byref(r)):
            out.append((r.left, r.top, r.right, r.bottom))
    return out


def _mask_own_windows(img, left: int, top: int, rects=None) -> None:
    """Paint over Strux's own windows with the colour just around them, so frames and OCR show the
    screen as you see it minus Strux (no big white box)."""
    from PIL import ImageDraw
    rects = _own_window_rects() if rects is None else rects
    d = ImageDraw.Draw(img)
    W, H = img.size
    for x0, y0, x1, y1 in rects:
        x0, y0, x1, y1 = max(0, x0 - left), max(0, y0 - top), min(W, x1 - left), min(H, y1 - top)
        if x1 <= x0 or y1 <= y0:
            continue
        d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=_surround_colour(img, x0, y0, x1, y1))


def _surround_colour(img, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int]:
    """Most common colour on a 2 px ring just outside the rect (the background it sits on)."""
    from collections import Counter
    W, H = img.size
    px = img.load()
    seen: Counter = Counter()
    step = max(1, (x1 - x0 + y1 - y0) // 200)
    for y in (y0 - 2, y1 + 1):
        if 0 <= y < H:
            for x in range(x0, x1, step):
                seen[px[x, y][:3]] += 1
    for x in (x0 - 2, x1 + 1):
        if 0 <= x < W:
            for y in range(y0, y1, step):
                seen[px[x, y][:3]] += 1
    return seen.most_common(1)[0][0] if seen else (255, 255, 255)


SKIP_APPS = {"lockapp.exe", "logonui.exe", "screenclippinghost.exe", "snippingtool.exe"}
SELF_APPS = {"pythonw.exe", "python.exe", "strux.exe", "halo.exe"}


def score_frame(*, app_changed: bool, visual_distance: int, text_diff: float | None,
                error_hit: bool, clipboard_recent: bool, idle_seconds: float) -> int:
    s = 0
    if app_changed:
        s += 30
    if (text_diff is not None and text_diff >= 0.40) or visual_distance >= 16:
        s += 25
    if error_hit:
        s += 20
    if clipboard_recent:
        s += 10
    if idle_seconds < 60:
        s += 10
    elif idle_seconds >= 120:
        s -= 30
    return max(0, min(100, s))


def text_diff_ratio(a: str | None, b: str | None) -> float | None:
    if a is None or b is None:
        return None
    sa, sb = set(a.split()), set(b.split())
    if not sa and not sb:
        return 0.0
    return 1.0 - len(sa & sb) / max(1, len(sa | sb))


class ScreenshotSensor(Sensor):
    name = "screenshot"

    def __init__(self, bus, cfg, store, data_dir: Path) -> None:
        super().__init__(bus, cfg)
        self.store = store
        self.frames_dir = Path(data_dir) / "frames"
        self._wake = threading.Event()
        self._last_capture = 0.0
        self._last_saved = 0.0
        self._last_hash: int | None = None
        self._last_app = ""
        self._last_text: str | None = None
        self._last_clip_ts = 0.0
        self._last_title = ""
        self._same_title_streak = 0   # consecutive visual-only changes with identical app+title (video playing)
        self._ocr_q: queue.Queue = queue.Queue(maxsize=1)
        self._ocr_thread: threading.Thread | None = None
        self._last_ocr_hash: int | None = None
        self._last_ocr_ts = 0.0
        self.ocr_done = 0
        self.ocr_skipped = 0
        self.captured = 0
        self.deduped = 0
        self.blocked = 0
        if cfg.get("on_window_change", True):
            bus.subscribe("window", lambda ev: self._wake.set())
        bus.subscribe("clipboard", self._on_clip)

    def _on_clip(self, ev: Event) -> None:
        self._last_clip_ts = ev.ts

    # ---- policy --------------------------------------------------------
    def is_blocked(self, app: str, title: str) -> bool:
        app_l, title_l = app.lower(), title.lower()
        if app_l in {a.lower() for a in self.cfg.get("blocklist_apps", [])}:
            return True
        return any(k.lower() in title_l for k in self.cfg.get("blocklist_title_keywords", []))

    # ---- loop ------------------------------------------------------------
    # ---- OCR worker (idle priority, one frame at a time) --------------------
    def _ocr_worth(self, now: float, app_changed: bool, h: int, error_hit: bool) -> bool:
        if not self.cfg.get("ocr", False) or not ocr_mod.available():
            return False
        if now - self._last_ocr_ts < float(self.cfg.get("ocr_min_gap_seconds", 20)) and not error_hit:
            return False
        if (self._last_ocr_hash is not None and not app_changed and not error_hit
                and phash.hamming(h, self._last_ocr_hash) < 16):
            return False  # screen barely changed since the last OCR
        return True

    def _ocr_loop(self) -> None:
        ocr_mod.set_thread_idle_priority()
        while not self.should_stop():
            try:
                fid, ts, img, app, title = self._ocr_q.get(timeout=1.0)
            except queue.Empty:
                continue
            self.bus.publish(Event(kind="_processing", data={"stage": "ocr"}))
            text = ocr_mod.ocr(img, max_width=int(self.cfg.get("ocr_max_width", 1280)))
            self.bus.publish(Event(kind="_processing", data={"stage": "idle"}))
            if text is None:
                continue
            tdiff = text_diff_ratio(self._last_text, text)
            err_line = new_error_line(text, self._last_text)
            err = err_line is not None
            delta = (25 if (tdiff is not None and tdiff >= 0.40) else 0) + (20 if err and not ERROR_RE.search(title) else 0)
            self.store.set_frame_text(fid, ts, text, score_delta=delta)
            self._last_text = text
            self.ocr_done += 1
            if err:
                self.emit(Event(kind="ocr_error", ts=time.time(), app=app, title=title, data={"frame_id": fid, "line": err_line}))

    def run(self) -> None:
        import mss  # created inside the thread (mss is thread-affine)

        if self.cfg.get("ocr", False) and ocr_mod.available():
            self._ocr_thread = threading.Thread(target=self._ocr_loop, name="ocr", daemon=True)
            self._ocr_thread.start()
        interval = float(self.cfg.get("interval_seconds", 15))
        min_gap = float(self.cfg.get("min_gap_seconds", 3))
        fails = 0
        sct = mss.mss()
        try:
            while not self.should_stop():
                # back off while captures keep failing (locked session, secure desktop, display change)
                self._wake.wait(timeout=interval if fails < 3 else min(300.0, interval * 2 ** min(fails - 2, 4)))
                self._wake.clear()
                if self.should_stop() or self.paused.is_set():
                    continue
                if time.time() - self._last_capture < min_gap:
                    continue
                try:
                    self._capture(sct)
                    if fails:
                        log.info("capture recovered after %d failures", fails)
                    fails = 0
                except Exception as e:
                    fails += 1
                    if fails == 1:
                        log.warning("capture failed (%s); backing off until it works again", e)
                    try:                       # the GDI handles go stale after lock / display changes
                        sct.close()
                    except Exception:
                        pass
                    sct = mss.mss()
        finally:
            sct.close()

    def _capture(self, sct) -> None:
        now = time.time()
        self._last_capture = now
        _, app, title = winapi.foreground_info()
        if not app or app.lower() in SKIP_APPS or (app.lower() in SELF_APPS and title.lower().startswith(("strux", "halo"))):
            return      # lock screen / nothing in front / Strux's own windows: nothing worth keeping
        if self.is_blocked(app, title):
            self.blocked += 1
            self.emit(Event(kind="blocked", ts=now, app=app, title="", data={"reason": "blocklist"}))
            return

        mon = sct.monitors[1]  # primary monitor
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        _mask_own_windows(img, mon["left"], mon["top"])          # Strux's bar / pane never end up in a frame or in OCR
        max_w = int(self.cfg.get("max_width", 1280))
        if img.width > max_w:
            img = img.resize((max_w, int(img.height * max_w / img.width)), Image.Resampling.BILINEAR)

        h = phash.phash(img)
        dist = phash.hamming(h, self._last_hash) if self._last_hash is not None else 64
        app_changed = app != self._last_app
        if not app_changed and dist < int(self.cfg.get("phash_min_distance", 6)):
            self.deduped += 1
            return
        # video damping: same app + same title but pixels keep changing (a playing video / animation).
        # After 3 such frames, only keep one per `video_interval_seconds` (default 90) unless the title changes.
        if not app_changed and title == self._last_title and not ERROR_RE.search(title):
            self._same_title_streak += 1
            if self._same_title_streak >= 3 and (now - self._last_saved) < float(self.cfg.get("video_interval_seconds", 90)):
                self.deduped += 1
                return
        else:
            self._same_title_streak = 0

        error_hit = bool(ERROR_RE.search(title))
        idle = winapi.idle_seconds()
        score = score_frame(
            app_changed=app_changed, visual_distance=dist, text_diff=None, error_hit=error_hit,
            clipboard_recent=(now - self._last_clip_ts) <= 5, idle_seconds=idle,
        )
        text = None

        day_dir = self.frames_dir / datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{int(now * 1000)}.webp"
        img.save(path, "WEBP", quality=int(self.cfg.get("hot_quality", 50)), method=4)

        self._last_hash, self._last_app, self._last_title = h, app, title
        self._last_saved = now
        self.captured += 1
        row = {"ts": now, "app": app, "title": title, "phash": phash.to_hex(h), "score": score,
               "tier": "hot", "image_path": str(path), "ocr_text": text}
        fid = self.store.add_frame(row)
        if self._ocr_worth(now, app_changed, h, error_hit):
            try:
                self._ocr_q.put_nowait((fid, now, img, app, title))
                self._last_ocr_hash, self._last_ocr_ts = h, now
            except queue.Full:
                self.ocr_skipped += 1
        self.emit(Event(kind="frame", ts=now, app=app, title=title,
                        data={"score": score, "dist": dist, "error": error_hit, "path": str(path), "id": fid}))
