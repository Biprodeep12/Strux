"""Global hotkeys via RegisterHotKey on a dedicated message-loop thread.

  Ctrl+Alt+F  -> forget last 10 minutes
  Ctrl+Alt+W  -> show / hide the dashboard window
  Ctrl+Alt+P  -> pause / resume watching
  Ctrl+Alt+Space -> bar chat;  Ctrl+Alt+V -> bar chat with the clipboard (image / copied files) attached
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class Hotkeys:
    def __init__(self) -> None:
        self._bindings: dict[int, tuple[int, int, Callable[[], None]]] = {}
        self._thread_id = 0
        self._t = threading.Thread(target=self._loop, name="hotkeys", daemon=True)

    def bind(self, key: str, fn: Callable[[], None], mods: int = MOD_CONTROL | MOD_ALT) -> None:
        self._bindings[len(self._bindings) + 1] = (mods | MOD_NOREPEAT, ord(key.upper()), fn)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        for hid, (mods, vk, _) in self._bindings.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                log.warning("hotkey %s (vk %s) could not be registered (already taken?)", hid, chr(vk))
        log.info("hotkeys registered: %s", ", ".join(f"Ctrl+Alt+{chr(vk)}" for _, vk, _ in self._bindings.values()))
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                b = self._bindings.get(int(msg.wParam))
                if b:
                    try:
                        b[2]()
                    except Exception:
                        log.exception("hotkey handler failed")
        for hid in self._bindings:
            user32.UnregisterHotKey(None, hid)
