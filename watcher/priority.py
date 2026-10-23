"""Make this process a polite neighbour: below-normal priority + Windows EcoQoS.

EcoQoS (power throttling) tells the scheduler this is background work, so it
runs on efficiency cores / low clocks and yields immediately when the user is
busy. This is the single most effective "don't affect my other tasks" switch.
"""
from __future__ import annotations

import ctypes
import logging
import sys

import psutil

log = logging.getLogger(__name__)

PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
ProcessPowerThrottling = 4


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


def set_dpi_aware() -> None:
    """Per-monitor DPI awareness (v2) so every coordinate we touch - work area,
    window rects, mss captures, pywebview x/y - is in physical pixels, and
    WebView2 renders crisp on 125 %/150 % displays. Must run before any window."""
    if sys.platform != "win32":
        return
    try:
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        log.info("DPI awareness: %s", "per-monitor v2" if ok else "already set")
    except Exception as e:  # pragma: no cover
        log.warning("DPI awareness failed: %s", e)


def apply(cfg: dict) -> None:
    if sys.platform != "win32":
        return
    set_dpi_aware()
    if cfg.get("below_normal", True):
        try:
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            log.info("process priority: BELOW_NORMAL")
        except Exception as e:  # pragma: no cover
            log.warning("could not lower priority: %s", e)
    if cfg.get("eco_qos", True):
        try:
            state = PROCESS_POWER_THROTTLING_STATE(
                PROCESS_POWER_THROTTLING_CURRENT_VERSION,
                PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            )
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
            k32.SetProcessInformation.restype = ctypes.c_int
            ok = k32.SetProcessInformation(
                k32.GetCurrentProcess(), ProcessPowerThrottling,
                ctypes.byref(state), ctypes.sizeof(state),
            )
            log.info("EcoQoS: %s", "enabled" if ok else "not supported")
        except Exception as e:  # pragma: no cover
            log.warning("EcoQoS failed: %s", e)
