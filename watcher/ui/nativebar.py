"""Strux bar, drawn natively.

Why native: the bar used to be a WebView2 page. Every state change meant resizing a
GPU-composited window, and Chromium repaints a frame or two after the resize, so hover,
collapse and drop-outs stuttered. Shaping a fixed WebView2 window does not work either:
DWM treats it as opaque and shows the desktop behind the clipped area.

This bar is a Win32 *layered* window updated with UpdateLayeredWindow (per-pixel alpha):
  - the window never resizes; the bar is drawn into a fixed transparent canvas,
  - fully transparent pixels are click-through, so only the bar itself catches the mouse,
  - shape (width/height) follows a critically-damped spring, content cross-fades,
  - frames are paced by MsgWaitForMultipleObjects on the bar's own thread (~120 Hz while
    animating, zero CPU when still),
  - it can be dragged anywhere along the top edge of any monitor (position is saved),
    double-click re-centres it, and it follows DPI / resolution changes.

Rendering is Pillow at 2x supersampling (content cached per state, shape mask per frame).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import json
import logging
import math
import threading
import weakref
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .. import config as config_mod

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- win32 ---
u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00080000, 0x00000008, 0x00000080, 0x08000000
WS_EX_ACCEPTFILES, WM_DROPFILES = 0x00000010, 0x0233
_shell32 = ctypes.WinDLL("shell32")
_shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
_shell32.DragQueryFileW.restype = ctypes.c_uint
_shell32.DragFinish.argtypes = [ctypes.c_void_p]


def _dropped_paths(hdrop) -> list[str]:
    """Paths from a WM_DROPFILES handle (then released)."""
    try:
        n = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(min(n, 12)):
            size = _shell32.DragQueryFileW(hdrop, i, None, 0) + 1
            buf = ctypes.create_unicode_buffer(size)
            _shell32.DragQueryFileW(hdrop, i, buf, size)
            out.append(buf.value)
        return out
    finally:
        _shell32.DragFinish(hdrop)
CS_DBLCLKS = 0x0008
WM_DESTROY, WM_SETCURSOR, WM_MOUSEACTIVATE = 0x0002, 0x0020, 0x0021
WM_DISPLAYCHANGE, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x007E, 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONUP, WM_MOUSELEAVE, WM_DPICHANGED, WM_APP, WM_QUIT, WM_CAPTURECHANGED = 0x0205, 0x02A3, 0x02E0, 0x8000, 0x0012, 0x0215
MA_NOACTIVATE = 3
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 2, 0, 1
TME_LEAVE = 2
PM_REMOVE, QS_ALLINPUT = 1, 0x04FF
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
IDC_ARROW, IDC_HAND, IDC_SIZEWE = 32512, 32649, 32644


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte), ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32), ("hwndTrack", w.HWND), ("dwHoverTime", ctypes.c_uint32)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", ctypes.c_uint32), ("szDevice", ctypes.c_wchar * 32)]


u32.DefWindowProcW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.restype = w.HWND
u32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
u32.LoadCursorW.restype = w.HANDLE
u32.LoadCursorW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
u32.SetCursor.argtypes = [w.HANDLE]
u32.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE), w.HDC, ctypes.POINTER(w.POINT),
                                    w.COLORREF, ctypes.POINTER(BLENDFUNCTION), w.DWORD]
u32.GetDC.restype = w.HDC
u32.ReleaseDC.argtypes = [w.HWND, w.HDC]
g32.CreateCompatibleDC.restype = w.HDC
g32.CreateCompatibleDC.argtypes = [w.HDC]
g32.CreateDIBSection.restype = w.HBITMAP
g32.CreateDIBSection.argtypes = [w.HDC, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
g32.SelectObject.restype = w.HGDIOBJ
g32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
g32.DeleteObject.argtypes = [w.HGDIOBJ]
g32.DeleteDC.argtypes = [w.HDC]
u32.MonitorFromPoint.restype = w.HMONITOR
u32.MonitorFromPoint.argtypes = [w.POINT, w.DWORD]
u32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
u32.MsgWaitForMultipleObjects.argtypes = [w.DWORD, ctypes.c_void_p, w.BOOL, w.DWORD, w.DWORD]
u32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
u32.PostMessageW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
u32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
u32.GetDpiForWindow.argtypes = [w.HWND]
u32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
u32.SetCapture.argtypes = [w.HWND]
u32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]

# ---------------------------------------------------------------- style ---
ROOT = config_mod.ROOT
FONT_TEXT = r"C:\Windows\Fonts\SegUIVar.ttf"
FONT_FALLBACK = r"C:\Windows\Fonts\segoeui.ttf"
ICON_DIR = ROOT / "assets" / "fonts"

WHITE = (255, 255, 255, 255)
BORDER = (226, 228, 234, 255)
INK, INK2, INK3 = (10, 10, 12, 255), (78, 80, 88, 255), (134, 136, 146, 255)
ACCENT = (42, 120, 214, 255)
ACCENT_HOT = (30, 100, 190, 255)
ACCENT_SOFT, ACCENT_LINE = (236, 243, 252, 255), (201, 220, 245, 255)
GOOD, WARN = (12, 163, 12, 255), (201, 133, 0, 255)
WARN_SOFT, WARN_LINE = (255, 247, 228, 255), (243, 215, 154, 255)
INFO_SOFT = (246, 247, 249, 255)
HOVER_BG, ACTIVE_BG = (241, 242, 245, 255), (227, 237, 250, 255)

# logical sizes
IDLE_H, HEAD_H = 30, 46
OPEN_W = 520
IDLE_MIN, IDLE_MAX = 150, 380
RADIUS = 14
PAD = 12                         # transparent margin around the widest shape inside the canvas
MAX_H = 270
SS = 2                           # supersampling for content and shape


def _lerp(a, b, t):
    return a + (b - a) * t


@dataclass
class Item:
    kind: str                    # "sugg" | "alert"
    key: str
    text: str
    meta: str = ""
    level: str = "info"          # alerts: info | warn
    id: int | None = None
    action: str = ""             # suggestions: label of the accept button ("Explain", "Recap"...); "" = check icon


@dataclass
class BarState:
    headline: str = "Strux"
    paused: bool = False
    stage: str | None = None     # "Thinking" / "Looking"
    pending: int = 0
    waiting: str | None = None   # Claude project waiting for you
    peek: Item | None = None
    pane: str | None = None      # "chat" | "panel" | "todos" when the drop-down is open
    emphasis: bool = False       # headline is a suggestion / alert


class _Spring:
    """Critically damped spring; settles in ~0.25 s."""

    def __init__(self, v: float, k: float = 520.0) -> None:
        self.x = self.target = v
        self.v = 0.0
        self.k = k
        self.c = 2 * math.sqrt(k)

    def step(self, dt: float) -> bool:
        a = -self.k * (self.x - self.target) - self.c * self.v
        self.v += a * dt
        self.x += self.v * dt
        if abs(self.x - self.target) < 0.25 and abs(self.v) < 2:
            self.x, self.v = self.target, 0.0
            return False
        return True


BARS: "weakref.WeakSet[NativeBar]" = weakref.WeakSet()     # live bars, for the screenshot mask


def visible_rects() -> list[tuple[int, int, int, int]]:
    return [r for b in list(BARS) if (r := b.visible_rect())]


class NativeBar:
    def __init__(self, data_dir: Path, on_action: Callable[[str, Any], None], peek_seconds: float = 14.0,
                 on_press: Callable[[], None] | None = None) -> None:
        self.on_action = on_action
        self.on_press = on_press or (lambda: None)
        self.peek_seconds = peek_seconds
        self.state_file = Path(data_dir) / "bar.json"
        self.state = BarState()
        self._lock = threading.Lock()
        self.hwnd = 0
        self.scale = 1.0
        self.mode = "idle"                       # idle | hover | peek | open
        self.hover = False
        self.hot: str | None = None              # element under the pointer
        self.peek_until = 0.0
        self.leave_at = 0.0
        self.w = _Spring(0)
        self.h = _Spring(0)
        self.fade = 1.0
        self.content: Image.Image | None = None
        self.prev_content: Image.Image | None = None
        self.content_key: tuple | None = None
        self.hits: list[tuple[str, tuple[int, int, int, int]]] = []
        self._dirty = True
        self._fonts: dict[tuple, ImageFont.FreeTypeFont] = {}
        self._corners: dict[int, tuple] = {}
        self._logos: dict[int, Image.Image] = {}
        self.drawn: tuple[int, int, int, int] | None = None   # the part of the canvas that is not transparent
        self._icons = json.loads((ICON_DIR / "phosphor-map.json").read_text(encoding="utf-8"))
        self._drag: dict | None = None
        self._tracking = False
        self.canvas = (0, 0)
        self._dib = None
        self.mon_device = ""
        self.mon_rect = (0, 0, 1920, 1080)
        self.center_x = 0                        # physical px, bar centre
        self._last_frame = time.perf_counter()
        self.frames = 0
        self._wndproc = WNDPROC(self._proc)
        self._thread = threading.Thread(target=self._run, name="strux-bar", daemon=True)
        self._ready = threading.Event()
        BARS.add(self)

    # ------------------------------------------------------------ public API
    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def update(self, **changes: Any) -> None:
        """Thread-safe state change from anywhere; the bar redraws on its own thread."""
        with self._lock:
            for k, v in changes.items():
                setattr(self.state, k, v)
            if "peek" in changes and changes["peek"] is not None:
                self.peek_until = time.monotonic() + self.peek_seconds
        self._wake()

    def geometry(self) -> dict:
        """Where the bar is (physical px) so the drop-down can sit right under it."""
        return {"center_x": self.center_x, "top": self.mon_rect[1], "bar_h": int(HEAD_H * self.scale),
                "scale": self.scale, "mon": self.mon_rect}

    def visible_rect(self) -> tuple[int, int, int, int] | None:
        """Screen rect (physical px) of what is actually drawn. The window itself is much bigger
        (room for the open card) but mostly transparent, so masking the whole window would blank
        real content in screenshots."""
        d = self.drawn
        if not d or not self.hwnd:
            return None
        r = w.RECT()
        if not u32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return None
        return (r.left + d[0], r.top + d[1], r.left + d[2], r.top + d[3])

    def stop(self) -> None:
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_QUIT, 0, 0)

    # ------------------------------------------------------------ fonts/icons
    def _font(self, px: float, weight: str = "Regular") -> ImageFont.FreeTypeFont:
        key = ("t", round(px * 2) / 2, weight)
        f = self._fonts.get(key)
        if f is None:
            try:
                f = ImageFont.truetype(FONT_TEXT, int(round(px)))
                f.set_variation_by_name(weight)
            except Exception:
                f = ImageFont.truetype(FONT_FALLBACK, int(round(px)))
            self._fonts[key] = f
        return f

    def _icon_font(self, px: float, style: str = "regular") -> ImageFont.FreeTypeFont:
        key = ("i", int(round(px)), style)
        f = self._fonts.get(key)
        if f is None:
            name = {"regular": "Phosphor.ttf", "fill": "Phosphor-Fill.ttf", "bold": "Phosphor-Bold.ttf"}[style]
            f = ImageFont.truetype(str(ICON_DIR / name), int(round(px)))
            self._fonts[key] = f
        return f

    def _glyph(self, name: str, style: str = "regular") -> str:
        return chr(self._icons[style][name])

    # ------------------------------------------------------------ layout
    def _target_mode(self) -> str:
        st = self.state
        if st.pane:
            return "open"
        if st.peek is not None and (time.monotonic() < self.peek_until or self.hover):
            return "peek"
        if self.hover or (self.mode == "hover" and time.monotonic() < self.leave_at):
            return "hover"
        return "idle"

    def _headline(self) -> tuple[str, tuple]:
        st = self.state
        if st.waiting:
            return f"Claude needs you · {st.waiting}", WARN
        if st.stage:
            return f"{st.stage}…", INK2
        return st.headline, (INK if st.emphasis else INK2)

    def _ellipsize(self, text: str, font, max_w: float) -> str:
        if font.getlength(text) <= max_w:
            return text
        while text and font.getlength(text + "…") > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    def _wrap(self, text: str, font, max_w: float, max_lines: int) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if font.getlength(t) <= max_w:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = wd
            if len(lines) == max_lines:
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if len(lines) == max_lines and " ".join(lines) != " ".join(words):
            lines[-1] = self._ellipsize(lines[-1] + " …", font, max_w)
        return lines

    def _render_content(self, mode: str) -> tuple[Image.Image, list, tuple[float, float]]:
        """Content image (RGBA, physical px) + hit rects (content coords) + logical (w, h)."""
        s = self.scale * SS
        st = self.state
        text, color = self._headline()
        hits: list[tuple[str, tuple[int, int, int, int]]] = []

        def R(*v):
            return tuple(int(round(x * s)) for x in v)

        if mode == "idle":
            f = self._font(12 * s, "Semibold Text" if st.emphasis or st.waiting else "Regular")
            badge = 22 if st.pending else 0
            tw = f.getlength(text) / s
            wl = max(IDLE_MIN, min(IDLE_MAX, 12 + 6 + 8 + tw + 12 + badge))
            hl = IDLE_H
            img = Image.new("RGBA", R(wl, hl), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            dot = GOOD if not st.paused else WARN
            d.ellipse(R(12, hl / 2 - 3, 18, hl / 2 + 3), fill=dot)
            avail = wl - 26 - 12 - badge
            d.text(R(26, hl / 2), self._ellipsize(text, f, avail * s), font=f, fill=color, anchor="lm")
            if st.pending:
                self._badge(d, R(wl - 12 - 16, hl / 2 - 8, wl - 12, hl / 2 + 8), st.pending, s, WARN if st.waiting else ACCENT)
            hits.append(("bar", R(0, 0, wl, hl)))
            return self._down(img), self._scale_hits(hits), (wl, hl)

        # header (hover / peek / open)
        wl = OPEN_W
        card_h = self._card_height(st.peek, s) if mode == "peek" and st.peek else 0
        hl = HEAD_H + (card_h + 10 if card_h else 0)
        img = Image.new("RGBA", R(wl, hl), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        self._logo(img, R(10, 10, 36, 36))
        d.ellipse(R(44, HEAD_H / 2 - 3, 50, HEAD_H / 2 + 3), fill=GOOD if not st.paused else WARN)
        icons = [("chat", "keyboard"), ("panel", "list-bullets"), ("todos", "list-checks"), ("dashboard", "squares-four"),
                 ("pause", "play" if st.paused else "pause"), ("think", "sparkle")]
        if mode == "open":
            icons.append(("close", "x"))
        x = wl - 8
        icon_rects = []
        for key, glyph in reversed(icons):
            x0 = x - 32
            icon_rects.append((key, glyph, (x0, HEAD_H / 2 - 16, x, HEAD_H / 2 + 16)))
            x = x0 - 2
        icon_rects.reverse()
        badge = 24 if st.pending else 0
        text_x, text_r = 58, x - 8 - badge
        f = self._font(13 * s, "Semibold Text" if st.emphasis or st.waiting else "Regular")
        d.text(R(text_x, HEAD_H / 2), self._ellipsize(text, f, (text_r - text_x) * s), font=f, fill=color, anchor="lm")
        if st.pending:
            self._badge(d, R(text_r + 6, HEAD_H / 2 - 9, text_r + 24, HEAD_H / 2 + 9), st.pending, s, WARN if st.waiting else ACCENT)
        hits.append(("headline", R(text_x - 6, 4, text_r + badge, HEAD_H - 4)))
        for key, glyph, (x0, y0, x1, y1) in icon_rects:
            active = (mode == "open" and key == st.pane)
            if active or self.hot == key:
                d.rounded_rectangle(R(x0, y0, x1, y1), radius=int(9 * s), fill=ACTIVE_BG if active else HOVER_BG)
            fi = self._icon_font(18 * s, "fill" if active else "regular")
            d.text(R((x0 + x1) / 2, (y0 + y1) / 2), self._glyph(glyph, "fill" if active else "regular"),
                   font=fi, fill=ACCENT if active else (INK if self.hot == key else INK2), anchor="mm")
            hits.append((key, R(x0, y0, x1, y1)))
        if card_h:
            self._card(img, d, st.peek, R, s, top=HEAD_H, wl=wl, hits=hits)
        return self._down(img), self._scale_hits(hits), (wl, hl)

    def _card_height(self, it: Item, s: float) -> float:
        f = self._font(13 * s, "Regular")
        inner = OPEN_W - 20 - 28 - (14 if it.kind == "sugg" else 40)
        lines = self._wrap(it.text, f, inner * s, 3)
        if it.kind == "sugg":
            return 12 + len(lines) * 19 + 8 + 30 + 10
        return 12 + len(lines) * 19 + 12

    def _card(self, img, d, it: Item, R, s, top: float, wl: float, hits: list) -> None:
        x0, x1 = 10, wl - 10
        h = self._card_height(it, s)
        y0, y1 = top, top + h
        if it.kind == "sugg":
            bg, line, icon, icol = ACCENT_SOFT, ACCENT_LINE, "sparkle", ACCENT
        elif it.level == "warn":
            bg, line, icon, icol = WARN_SOFT, WARN_LINE, "warning", WARN
        else:
            bg, line, icon, icol = INFO_SOFT, BORDER, "info", INK3
        d.rounded_rectangle(R(x0, y0, x1, y1), radius=int(12 * s), fill=bg, outline=line, width=max(1, int(1 * s)))
        d.text(R(x0 + 14, y0 + 21), self._glyph(icon, "regular"), font=self._icon_font(16 * s), fill=icol, anchor="mm")
        f = self._font(13 * s, "Regular")
        inner = x1 - x0 - 28 - (14 if it.kind == "sugg" else 40)
        lines = self._wrap(it.text, f, inner * s, 3)
        for i, ln in enumerate(lines):
            d.text(R(x0 + 28, y0 + 12 + i * 19), ln, font=f, fill=INK, anchor="lt")
        ty = y0 + 12 + len(lines) * 19
        if it.kind == "sugg":
            by = ty + 8
            d.text(R(x0 + 28, by + 15), it.meta, font=self._font(10.5 * s, "Regular"), fill=INK3, anchor="lm")
            bx = x1 - 12
            lf = self._font(12 * s, "Semibold Text")
            for key, glyph, style, col in reversed([("accept", "check", "bold", GOOD), ("snooze", "clock-countdown", "regular", WARN), ("dismiss", "x", "bold", INK)]):
                text_btn = key == "accept" and bool(it.action)
                bw = (d.textlength(it.action, font=lf) / s + 24) if text_btn else 36
                rect = (bx - bw, by, bx, by + 30)
                hot = self.hot == key
                if text_btn:
                    d.rounded_rectangle(R(*rect), radius=int(8 * s), fill=ACCENT_HOT if hot else ACCENT)
                    d.text(R((rect[0] + rect[2]) / 2, by + 15), it.action, font=lf, fill=WHITE, anchor="mm")
                else:
                    d.rounded_rectangle(R(*rect), radius=int(8 * s), fill=HOVER_BG if hot else WHITE, outline=BORDER, width=max(1, int(1 * s)))
                    d.text(R((rect[0] + rect[2]) / 2, by + 15), self._glyph(glyph, style), font=self._icon_font(15 * s, style), fill=col, anchor="mm")
                hits.append((key, R(*rect)))
                bx -= bw + 6
        else:
            rect = (x1 - 30, y0 + 8, x1 - 8, y0 + 30)
            if self.hot == "alert-x":
                d.rounded_rectangle(R(*rect), radius=int(6 * s), fill=HOVER_BG)
            d.text(R((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), self._glyph("x", "bold"), font=self._icon_font(13 * s, "bold"), fill=INK3, anchor="mm")
            hits.append(("alert-x", R(*rect)))
        hits.append(("card", R(x0, y0, x1, y1)))

    def _badge(self, d, rect, n: int, s: float, color) -> None:
        d.ellipse(rect, fill=color)
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        d.text((cx, cy), str(min(n, 99)), font=self._font(10.5 * s, "Semibold Text"), fill=WHITE, anchor="mm")

    def _logo(self, img: Image.Image, rect) -> None:
        x0, y0, x1, y1 = rect
        size = x1 - x0
        tile = self._logos.get(size)
        if tile is None:
            from .brand import mark
            tile = self._logos[size] = mark(size)
        img.alpha_composite(tile, (x0, y0))

    def _down(self, img: Image.Image) -> Image.Image:
        return img.resize((max(1, img.width // SS), max(1, img.height // SS)), Image.Resampling.LANCZOS)

    def _scale_hits(self, hits):
        return [(k, tuple(int(v / SS) for v in r)) for k, r in hits]

    # ------------------------------------------------------------ frame
    def _corner(self, r: int) -> tuple[Image.Image, Image.Image]:
        """Anti-aliased bottom-left / bottom-right corner sprites (cached per radius)."""
        c = self._corners.get(r)
        if c is None:
            big = Image.new("L", (r * 4, r * 4), 0)
            ImageDraw.Draw(big).pieslice((0, -r * 4, r * 8, r * 4), 90, 180, fill=255)    # bottom-left quarter
            bl = big.resize((r, r), Image.Resampling.LANCZOS)
            c = (bl, bl.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
            self._corners[r] = c
        return c

    def _shape_mask(self, pw: int, ph: int) -> Image.Image:
        r = min(int(RADIUS * self.scale), pw // 2, ph)
        m = Image.new("L", (pw, ph), 255)
        if r >= 2:
            bl, br = self._corner(r)
            m.paste(bl, (0, ph - r))
            m.paste(br, (pw - r, ph - r))
        return m

    def _compose(self) -> Image.Image:
        Wp, Hp = self.canvas
        frame = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
        sw = max(0, int(round(self.w.x * self.scale)))
        sh = max(0, int(round(self.h.x * self.scale)))
        sw, sh = min(sw, Wp), min(sh, Hp)
        if sw < 2 or sh < 2:
            self.drawn = None
            return frame
        x0 = (Wp - sw) // 2
        self.drawn = (x0, 0, x0 + sw, sh)
        outer = self._shape_mask(sw, sh)
        b = max(1, int(round(self.scale)))
        inner = Image.new("L", (sw, sh), 0)
        if sw > 2 * b and sh > b:
            inner.paste(self._shape_mask(sw - 2 * b, sh - b), (b, 0))
        layer = Image.new("RGBA", (sw, sh), BORDER)
        layer.putalpha(outer)
        fill = Image.new("RGBA", (sw, sh), WHITE)
        fill.putalpha(inner)
        layer.alpha_composite(fill)
        for img, alpha in ((self.prev_content, 1.0 - self.fade), (self.content, self.fade)):
            if img is None or alpha <= 0.01:
                continue
            cx0 = (Wp - img.width) // 2
            piece = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            piece.paste(img, (cx0 - x0, 0))
            a = ImageChops.multiply(piece.getchannel("A"), inner)
            if alpha < 0.99:
                k = alpha
                a = a.point([int(v * k) for v in range(256)])
            piece.putalpha(a)
            layer.alpha_composite(piece)
        # premultiply just the bar area; everything else in the canvas stays 0
        r_, g_, b_, a_ = layer.split()
        layer = Image.merge("RGBA", (ImageChops.multiply(r_, a_), ImageChops.multiply(g_, a_), ImageChops.multiply(b_, a_), a_))
        frame.paste(layer, (x0, 0))
        return frame

    def _present(self, frame: Image.Image) -> None:
        Wp, Hp = frame.size
        data = frame.tobytes("raw", "BGRA")                          # already premultiplied in _compose
        if self._dib is None or self._dib[2] != (Wp, Hp):
            self._free_dib()
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), Wp, -Hp, 1, 32, 0, 0, 0, 0, 0, 0)
            screen = u32.GetDC(None)
            mem = g32.CreateCompatibleDC(screen)
            bits = ctypes.c_void_p()
            hbm = g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            old = g32.SelectObject(mem, hbm)
            u32.ReleaseDC(None, screen)
            self._dib = (mem, hbm, (Wp, Hp), bits, old)
        mem, hbm, _, bits, _ = self._dib
        ctypes.memmove(bits, data, len(data))
        screen = u32.GetDC(None)
        size = w.SIZE(Wp, Hp)
        src = w.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        u32.UpdateLayeredWindow(self.hwnd, screen, None, ctypes.byref(size), mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        u32.ReleaseDC(None, screen)
        self.frames += 1

    def _free_dib(self) -> None:
        if self._dib:
            mem, hbm, _, _, old = self._dib
            g32.SelectObject(mem, old)
            g32.DeleteObject(hbm)
            g32.DeleteDC(mem)
            self._dib = None

    # ------------------------------------------------------------ placement
    def _load_pos(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_pos(self) -> None:
        mx0, _, mx1, _ = self.mon_rect
        fx = (self.center_x - mx0) / max(1, (mx1 - mx0))
        try:
            self.state_file.write_text(json.dumps({"device": self.mon_device, "fx": round(fx, 4)}), encoding="utf-8")
        except Exception as e:
            log.debug("bar position save failed: %s", e)

    def _monitor_at(self, x: int, y: int) -> tuple[str, tuple[int, int, int, int]]:
        hm = u32.MonitorFromPoint(w.POINT(x, y), 2)
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        u32.GetMonitorInfoW(hm, ctypes.byref(mi))
        r = mi.rcWork
        return mi.szDevice, (r.left, r.top, r.right, r.bottom)

    def _monitors(self) -> list[tuple[str, tuple]]:
        out = []
        MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

        def cb(hm, hdc, rc, lp):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            u32.GetMonitorInfoW(hm, ctypes.byref(mi))
            r = mi.rcWork
            out.append((mi.szDevice, (r.left, r.top, r.right, r.bottom)))
            return 1
        u32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
        return out

    def _place(self, center_x: int | None = None, device: str | None = None) -> None:
        """Pin the canvas to the top edge of a monitor with the bar centred at center_x (physical)."""
        if device is not None:
            mons = dict(self._monitors())
            if device in mons:
                self.mon_device, self.mon_rect = device, mons[device]
        mx0, my0, mx1, _ = self.mon_rect
        half = OPEN_W * self.scale / 2
        cx = (mx0 + mx1) / 2 if center_x is None else center_x
        cx = max(mx0 + half + 4, min(mx1 - half - 4, cx))
        self.center_x = int(cx)
        Wp, _ = self.canvas
        u32.SetWindowPos(self.hwnd, w.HWND(-1), int(self.center_x - Wp / 2), my0, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)

    def _setup_canvas(self) -> None:
        self.scale = (u32.GetDpiForWindow(self.hwnd) or 96) / 96.0
        self.canvas = (int((OPEN_W + 2 * PAD) * self.scale), int(MAX_H * self.scale))
        self._fonts.clear()
        self._corners.clear()
        self.content_key = None
        self._dirty = True

    # ------------------------------------------------------------ thread
    def _run(self) -> None:
        try:
            k32.SetThreadPriority(k32.GetCurrentThread(), 2)          # THREAD_PRIORITY_HIGHEST (process is below-normal)
            hinst = k32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.style = CS_DBLCLKS
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.hCursor = u32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
            wc.lpszClassName = "StruxNativeBar"
            u32.RegisterClassExW(ctypes.byref(wc))
            ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_ACCEPTFILES   # drop files on the bar
            self.hwnd = u32.CreateWindowExW(ex, "StruxNativeBar", "Strux bar", WS_POPUP, 0, 0, 10, 10, None, None, hinst, None)
            pos = self._load_pos()
            dev, rect = self._monitor_at(0, 0)
            self.mon_device, self.mon_rect = dev, rect
            if pos.get("device"):
                mons = dict(self._monitors())
                if pos["device"] in mons:
                    self.mon_device, self.mon_rect = pos["device"], mons[pos["device"]]
            # the window's DPI follows the monitor it is on: move there first, then measure
            u32.SetWindowPos(self.hwnd, w.HWND(-1), self.mon_rect[0] + 10, self.mon_rect[1], 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
            self._setup_canvas()
            mx0, _, mx1, _ = self.mon_rect
            cx = mx0 + pos["fx"] * (mx1 - mx0) if "fx" in pos else None
            self._place(cx)
            u32.ShowWindow(self.hwnd, 4)                               # SW_SHOWNOACTIVATE
            # startup: the strip grows out of the edge
            self.w.x, self.h.x = IDLE_MIN * 0.7, 0
            self._ready.set()
            log.info("native bar up: %s at x=%s on %s (scale %.2f)", self.canvas, self.center_x, self.mon_device, self.scale)
            self._loop()
        except Exception:
            log.exception("native bar crashed")
            self._ready.set()
        finally:
            self._free_dib()

    def _wake(self) -> None:
        self._dirty = True
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_APP, 0, 0)

    def _loop(self) -> None:
        msg = w.MSG()
        animating = True
        while True:
            timeout = 6 if animating else (40 if self._timers_pending() else 0xFFFFFFFF)
            u32.MsgWaitForMultipleObjects(0, None, False, timeout, QS_ALLINPUT)
            while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            animating = self._tick()

    def _timers_pending(self) -> bool:
        now = time.monotonic()
        return (self.state.peek is not None and now < self.peek_until + 0.5) or (self.mode == "hover" and not self.hover)

    def _tick(self) -> bool:
        """Advance animation; returns True while something is still moving."""
        now = time.perf_counter()
        dt = min(1 / 30, max(1 / 240, now - self._last_frame))
        self._last_frame = now
        with self._lock:
            st = self.state
            if st.peek is not None and time.monotonic() >= self.peek_until and not self.hover:
                st.peek = None
            mode = self._target_mode()
            key = (mode, self._headline(), st.pending, st.paused, st.pane, self.hot,
                   (st.peek.key, st.peek.text, st.peek.meta) if (st.peek and mode == "peek") else None, round(self.scale, 3))
        changed_content = key != self.content_key
        if changed_content:
            img, hits, (wl, hl) = self._render_content(mode)
            mode_changed = mode != self.mode
            if mode_changed or self.content is None:
                self.prev_content, self.fade = self.content, 0.0
            self.content, self.hits, self.content_key, self.mode = img, hits, key, mode
            self.w.target, self.h.target = wl, hl
            self._dirty = True
        moving = self.w.step(dt) | self.h.step(dt)
        if self.fade < 1.0:
            self.fade = min(1.0, self.fade + dt / 0.14)
            moving = True
            if self.fade >= 1.0:
                self.prev_content = None
        if moving or self._dirty:
            self._present(self._compose())
            self._dirty = False
        return moving

    # ------------------------------------------------------------ input
    def _hit(self, x: int, y: int) -> str | None:
        Wp, _ = self.canvas
        if self.content is None:
            return None
        cx0 = (Wp - self.content.width) // 2
        lx, ly = x - cx0, y
        for k, (x0, y0, x1, y1) in self.hits:
            if x0 <= lx < x1 and y0 <= ly < y1 and k not in ("bar", "headline", "card"):
                return k
        for k, (x0, y0, x1, y1) in self.hits:
            if x0 <= lx < x1 and y0 <= ly < y1:
                return k
        return None

    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                cur = IDC_SIZEWE if self._drag and self._drag.get("moving") else (
                    IDC_HAND if self.hot and self.hot not in ("bar", "headline", "card") else IDC_ARROW)
                u32.SetCursor(u32.LoadCursorW(None, ctypes.c_void_p(cur)))
                return 1
            if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP):
                x = ctypes.c_short(lp & 0xFFFF).value
                y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                return self._mouse(msg, x, y) or 0
            if msg == WM_MOUSELEAVE:
                self._tracking = False
                if not (self._drag and self._drag.get("moving")):
                    self.hover = False
                    self.hot = None
                    self.leave_at = time.monotonic() + 0.28
                    self._dirty = True
                return 0
            if msg == WM_CAPTURECHANGED and self._drag:
                self._drag = None
                return 0
            if msg == WM_DPICHANGED:
                self._setup_canvas()
                self._place(self.center_x)
                return 0
            if msg == WM_DISPLAYCHANGE:
                dev, rect = self._monitor_at(self.center_x, self.mon_rect[1] + 5)
                self.mon_device, self.mon_rect = dev, rect
                self._place(self.center_x)
                self._dirty = True
                return 0
            if msg == WM_APP:
                return 0
            if msg == WM_DROPFILES:
                paths = _dropped_paths(wp)
                if paths:
                    threading.Thread(target=self._safe_action, args=("drop_files", paths), daemon=True).start()
                return 0
        except Exception:
            log.exception("bar input")
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def _mouse(self, msg, x, y):
        if not self._tracking:
            tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, self.hwnd, 0)
            u32.TrackMouseEvent(ctypes.byref(tme))
            self._tracking = True
        if msg == WM_MOUSEMOVE:
            if self._drag:
                pt = w.POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                dx = pt.x - self._drag["sx"]
                if not self._drag.get("moving") and abs(dx) > 4 * self.scale:
                    self._drag["moving"] = True
                if self._drag.get("moving"):
                    dev, rect = self._monitor_at(pt.x, pt.y)
                    if dev != self.mon_device:                      # crossed onto another monitor
                        self.mon_device, self.mon_rect = dev, rect
                    self._place(self._drag["cx"] + dx)
                return 0
            if not self.hover:
                self.hover = True
            hot = self._hit(x, y)
            if hot != self.hot:
                self.hot = hot
            self._dirty = True
            return 0
        if msg == WM_LBUTTONDOWN:
            self.on_press()
            hot = self._hit(x, y)
            if hot in (None, "bar", "headline", "card"):
                pt = w.POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                self._drag = {"sx": pt.x, "cx": self.center_x, "target": hot}
                u32.SetCapture(self.hwnd)
            return 0
        if msg == WM_LBUTTONUP:
            if self._drag:
                d = self._drag
                self._drag = None
                u32.ReleaseCapture()
                if d.get("moving"):
                    self._save_pos()
                    return 0
                self._fire("headline" if d.get("target") in ("bar", "headline") else d.get("target") or "headline")
                return 0
            hot = self._hit(x, y)
            if hot:
                self._fire(hot)
            return 0
        if msg == WM_LBUTTONDBLCLK:
            self._place(None)                                      # re-centre
            self._save_pos()
            return 0
        if msg == WM_RBUTTONUP:
            self._fire("dashboard")
            return 0
        return 0

    def _fire(self, key: str) -> None:
        it = self.state.peek
        arg = None
        if key in ("accept", "snooze", "dismiss") and it is not None:
            arg = it.id
            with self._lock:
                self.state.peek = None
        elif key == "alert-x" and it is not None:
            arg = it.key
            with self._lock:
                self.state.peek = None
        elif key == "card" and it is not None:
            key = "panel"
        threading.Thread(target=self._safe_action, args=(key, arg), daemon=True).start()
        self._dirty = True

    def _safe_action(self, key: str, arg: Any) -> None:
        try:
            self.on_action(key, arg)
        except Exception:
            log.exception("bar action %s failed", key)
