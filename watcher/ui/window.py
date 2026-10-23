"""Window manager for the two WebView2 windows (pywebview):

  dashboard  - the full app window (1320x860, normal frame), destroyed when closed
  pane       - the drop-down under the native Strux bar (watcher/ui/nativebar.py): created once,
               hidden, shown/placed with Win32 calls so it opens instantly

Memory design: windows are created on demand and really destroyed when closed,
which ends the msedgewebview2.exe child processes. pywebview's start() blocks
while any window exists and returns when the last one closes; the main thread
then waits for the next request and starts again (verified on this machine).
While start() is running, new windows can be created from any thread.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

log = logging.getLogger(__name__)

from .. import APP_NAME

def _ui_build() -> str:
    """Changes with every UI build, so WebView2 can never reuse an index.html cached from an older one."""
    try:
        from ..api.server import UI_DIST
        return str(int((UI_DIST / "index.html").stat().st_mtime))
    except Exception:
        return str(int(time.time()))


def _icon_path() -> str | None:
    from ..config import ROOT
    p = ROOT / "assets" / "strux.ico"
    return str(p) if p.is_file() else None


def set_app_id() -> None:
    """Own taskbar identity: without it the windows group under pythonw.exe and show Python's icon."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p("Strux.Assistant"))
    except Exception as e:
        log.debug("app id not set: %s", e)


class WindowManager:
    def __init__(self, url: str, title: str = APP_NAME) -> None:
        self.url = url
        self.title = title
        self._windows: dict[str, object] = {}
        self._pending: set[str] = set()
        self._req = threading.Event()
        self._quit = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        set_app_id()

    # ---- queries ---------------------------------------------------------
    def is_open(self, kind: str) -> bool:
        return kind in self._windows

    @property
    def dashboard_visible(self) -> bool:
        return "dashboard" in self._windows

    @property
    def running(self) -> bool:
        return self._running

    # ---- pane (drop-down under the native bar) ------------------------------
    # pywebview's show/move/resize wait for the window's "shown" event, which never fires for a
    # window created hidden, so the pane is driven with plain Win32 calls (physical pixels).
    def _pane_hwnd(self) -> int:
        return ctypes.windll.user32.FindWindowW(None, "Strux pane")

    def show_pane(self, x: int, y: int, w_: int, h: int) -> None:
        """x, y, w, h in physical pixels."""
        u = ctypes.windll.user32
        hwnd = self._pane_hwnd()
        if not hwnd:
            if "pane" not in self._windows:
                self.show("pane")
            log.warning("pane window not ready yet")
            return
        self._pane_geo = (int(x), int(y), int(w_), int(h))
        # geometry with Win32 (physical px, HWND_TOPMOST, SWP_NOACTIVATE) ...
        u.SetWindowPos(hwnd, ctypes.c_void_p(-1), int(x), int(y), int(w_), int(h), 0x0010)
        # ... visibility through WinForms itself, so the WebView2 control knows it is visible and renders
        win = self._windows.get("pane")
        try:
            if win is not None and win.gui is not None:
                win.gui.show(win.uid)
            else:
                u.ShowWindow(hwnd, 5)
        except Exception as e:
            log.warning("pane show failed: %s", e)
            u.ShowWindow(hwnd, 5)
        threading.Thread(target=self._focus_pane, name="pane-focus", daemon=True).start()

    def pane_visible(self) -> bool:
        hwnd = self._pane_hwnd()
        return bool(hwnd and ctypes.windll.user32.IsWindowVisible(hwnd))

    def resize_pane(self, h: int) -> None:
        hwnd = self._pane_hwnd()
        geo = getattr(self, "_pane_geo", None)
        if not hwnd or not geo:
            return
        x, y, w_, _ = geo
        self._pane_geo = (x, y, w_, int(h))
        # SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, w_, int(h), 0x0002 | 0x0004 | 0x0010)

    def hide_pane(self) -> None:
        win = self._windows.get("pane")
        try:
            if win is not None and win.gui is not None:
                win.gui.hide(win.uid)
                return
        except Exception as e:
            log.warning("pane hide failed: %s", e)
        hwnd = self._pane_hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)

    def _focus_pane(self) -> None:
        """Bring the pane to the front with keyboard focus; round corners + no taskbar button once."""
        import ctypes.wintypes as w
        u = ctypes.windll.user32
        hwnd = 0
        for _ in range(40):
            hwnd = u.FindWindowW(None, "Strux pane")
            if hwnd and u.IsWindowVisible(hwnd):
                break
            time.sleep(0.02)
        if not hwnd:
            return
        if not getattr(self, "_pane_styled", False) or getattr(self, "_pane_styled_hwnd", 0) != hwnd:
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_uint(0x00EAE4E2)), 4)   # light border
                u.GetWindowLongPtrW.restype = ctypes.c_longlong
                u.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
                u.SetWindowLongPtrW.restype = ctypes.c_longlong
                u.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_longlong]
                ex = u.GetWindowLongPtrW(hwnd, -20)
                u.SetWindowLongPtrW(hwnd, -20, (ex | 0x80) & ~0x40000)
            except Exception as e:
                log.debug("pane style failed: %s", e)
            self._pane_styled = True
            self._pane_styled_hwnd = hwnd
        fg = u.GetForegroundWindow()
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        other = u.GetWindowThreadProcessId(fg, None) if fg else 0
        if other and other != cur:
            u.AttachThreadInput(other, cur, True)
        u.SetForegroundWindow(hwnd)
        u.SetFocus(hwnd)
        if other and other != cur:
            u.AttachThreadInput(other, cur, False)

    # ---- requests from any thread -----------------------------------------
    def show(self, kind: str = "dashboard") -> None:
        with self._lock:
            w = self._windows.get(kind)
            if w is not None:
                try:
                    if kind == "dashboard":
                        w.restore()
                    w.show()
                    return
                except Exception:
                    pass
            if self._running:
                try:
                    self._create(kind)
                    return
                except Exception as e:
                    log.debug("direct create failed (%s), queueing", e)
            self._pending.add(kind)
            self._req.set()

    def hide(self, kind: str = "dashboard") -> None:
        w = self._windows.get(kind)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def toggle(self, kind: str = "dashboard") -> None:
        (self.hide if self.is_open(kind) else self.show)(kind)

    def quit(self) -> None:
        self._quit.set()
        for k in list(self._windows):
            self.hide(k)
        self._req.set()

    # ---- window creation ---------------------------------------------------
    def _create(self, kind: str):
        import webview

        if kind == "pane":
            w = webview.create_window(
                "Strux pane", f"{self.url}/?v={_ui_build()}#/pane", width=520, height=380, x=-4000, y=-4000, hidden=True,
                min_size=(200, 100), frameless=True, on_top=True, easy_drag=False, background_color="#ffffff",
                focus=True, resizable=False, shadow=False,
            )
            self._windows[kind] = w
            w.events.closed += lambda: self._windows.pop(kind, None)
            log.info("pane window created (hidden)")
            return w
        w = webview.create_window(
            APP_NAME, f"{self.url}/?v={_ui_build()}", width=1320, height=860, min_size=(960, 640),
            background_color="#f6f6f8", text_select=True,
        )
        self._windows[kind] = w
        w.events.closed += lambda: self._windows.pop(kind, None)
        log.info("%s window opened", kind)
        return w

    # ---- main thread -----------------------------------------------------
    def run(self, start_with: tuple[str, ...] = ("dashboard",)) -> None:
        """Blocks on the main thread until quit()."""
        import webview

        for k in start_with:
            self._pending.add(k)
        if self._pending:
            self._req.set()
        while not self._quit.is_set():
            self._req.wait()
            self._req.clear()
            if self._quit.is_set():
                break
            with self._lock:
                kinds, self._pending = set(self._pending), set()
                if not kinds:
                    continue
                for k in kinds:
                    self._create(k)
                self._running = True
            try:
                webview.start(gui="edgechromium", private_mode=False, debug=False, icon=_icon_path())
            except Exception:
                log.exception("webview failed")
            finally:
                with self._lock:
                    self._running = False
                    self._windows.clear()
                log.info("all windows closed (WebView2 released)")
                time.sleep(0.2)
