"""Active-window sensor. Event-driven via SetWinEventHook, zero polling.

Emits:
  window  - foreground window changed (app and/or title)
  title   - title of the current foreground window changed (tab switch etc.)
"""
from __future__ import annotations

import logging
import os
import time

from ..events import Event
from . import winapi
from .base import Sensor

log = logging.getLogger(__name__)


class WindowSensor(Sensor):
    name = "window"

    def __init__(self, bus, cfg) -> None:
        super().__init__(bus, cfg)
        self._thread_id = 0
        self._hooks: list[int] = []
        self._last_hwnd = 0
        self._last_app = ""
        self._last_title = ""
        self._last_emit = 0.0
        self._proc = None  # keep callback alive (ctypes requirement)

    def _on_event(self, hook, event, hwnd, id_object, id_child, thread, ts_ms) -> None:
        try:
            if event == winapi.EVENT_OBJECT_NAMECHANGE:
                if id_object != winapi.OBJID_WINDOW or id_child != 0:
                    return
                if hwnd != winapi.foreground_hwnd():
                    return
            self._check(hwnd, forced=(event == winapi.EVENT_SYSTEM_FOREGROUND))
        except Exception:
            log.exception("window hook callback failed")

    def _check(self, hwnd: int, forced: bool) -> None:
        pid = winapi.window_pid(hwnd)
        if pid == os.getpid():
            return  # our own overlay / dashboard windows are not "activity"
        app = winapi.process_name(pid)
        title = winapi.window_title(hwnd)
        if not app and not title:
            return
        now = time.time()
        if hwnd != self._last_hwnd or app != self._last_app:
            kind = "window"
        elif title != self._last_title:
            kind = "title"
            # debounce rapid title flicker (e.g. progress bars in titles)
            if now - self._last_emit < 0.5:
                return
        else:
            return
        self._last_hwnd, self._last_app, self._last_title, self._last_emit = hwnd, app, title, now
        self.emit(Event(kind=kind, ts=now, app=app, title=title, data={"hwnd": int(hwnd or 0)}))

    def run(self) -> None:
        self._thread_id = winapi.current_thread_id()
        # emit the initial state so the timeline never starts empty
        hwnd = winapi.foreground_hwnd()
        if hwnd:
            self._check(hwnd, forced=True)
        self._proc = winapi.WinEventProcType(self._on_event)
        self._hooks = [
            winapi.set_win_event_hook(winapi.EVENT_SYSTEM_FOREGROUND, winapi.EVENT_SYSTEM_FOREGROUND, self._proc),
            winapi.set_win_event_hook(winapi.EVENT_OBJECT_NAMECHANGE, winapi.EVENT_OBJECT_NAMECHANGE, self._proc),
        ]
        log.info("window sensor hooked (foreground + namechange)")
        winapi.pump_messages()
        for h in self._hooks:
            winapi.unhook(h)

    def on_stop(self) -> None:
        if self._thread_id:
            winapi.post_quit(self._thread_id)
