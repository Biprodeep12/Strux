"""Keep the UI smooth: Strux itself runs at BELOW_NORMAL priority with EcoQoS so it
never competes with your work, but the WebView2 renderer processes it spawns
inherit that and then drop frames under load. This thread finds our
msedgewebview2.exe children every few seconds and gives them NORMAL priority
with power throttling disabled. They only exist while a window is on screen.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

import psutil

log = logging.getLogger(__name__)

PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ProcessPowerThrottling = 4
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1


class _PowerState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def _unthrottle(pid: int) -> None:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return
    try:
        state = _PowerState(1, PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)   # control the bit, set it to 0 = no throttling
        k32.SetProcessInformation(h, ProcessPowerThrottling, ctypes.byref(state), ctypes.sizeof(state))
    finally:
        k32.CloseHandle(h)


class UiBoost:
    def __init__(self, every: float = 3.0) -> None:
        self.every = every
        self._done: set[int] = set()
        self._t = threading.Thread(target=self._loop, name="ui-boost", daemon=True)

    def start(self) -> None:
        self._t.start()

    def _loop(self) -> None:
        import os
        from ..sensors import winapi
        me = os.getpid()
        while True:
            try:
                # one syscall for the whole table (psutil.children() walks it per process)
                snap = winapi.process_snapshot()
                kids: dict[int, list[int]] = {}
                names = {}
                for pid, ppid, name, *_ in snap:
                    kids.setdefault(ppid, []).append(pid)
                    names[pid] = name
                live, todo = set(), list(kids.get(me, []))
                while todo:
                    pid = todo.pop()
                    todo += kids.get(pid, [])
                    if names.get(pid) == "msedgewebview2.exe":
                        live.add(pid)
                for pid in live - self._done:
                    try:
                        c = psutil.Process(pid)
                        if c.nice() != psutil.NORMAL_PRIORITY_CLASS:
                            c.nice(psutil.NORMAL_PRIORITY_CLASS)
                        _unthrottle(pid)
                        self._done.add(pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                self._done &= live
            except Exception:
                log.exception("ui boost")
            time.sleep(self.every)
