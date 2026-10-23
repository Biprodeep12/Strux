"""Thin ctypes helpers over user32/kernel32. Kept in one place so the rest of
the code never touches raw Win32."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
from functools import lru_cache

import psutil

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---- window info ---------------------------------------------------------
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD


def foreground_hwnd() -> int:
    return user32.GetForegroundWindow() or 0


def window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = w.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


@lru_cache(maxsize=512)
def _proc_name_cached(pid: int, create_time: float) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def process_name(pid: int) -> str:
    """Process exe name (lower-case). Cached per (pid, create_time) so pid reuse is safe."""
    if pid <= 0:
        return ""
    try:
        ct = psutil.Process(pid).create_time()
    except Exception:
        return ""
    return _proc_name_cached(pid, ct)


def foreground_info() -> tuple[int, str, str]:
    hwnd = foreground_hwnd()
    return hwnd, process_name(window_pid(hwnd)), window_title(hwnd)


# ---- idle --------------------------------------------------------------
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


def idle_seconds() -> float:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


# ---- clipboard -----------------------------------------------------------
CF_UNICODETEXT = 13
user32.GetClipboardSequenceNumber.restype = w.DWORD
user32.GetClipboardData.restype = w.HANDLE
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [w.HANDLE]
kernel32.GlobalUnlock.argtypes = [w.HANDLE]


def clipboard_sequence() -> int:
    return int(user32.GetClipboardSequenceNumber())


def clipboard_text() -> str | None:
    """Return clipboard text or None if not text / unavailable. Never blocks long."""
    if not user32.OpenClipboard(None):
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


# ---- WinEvent hook -----------------------------------------------------
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0
WM_QUIT = 0x0012

WinEventProcType = ctypes.WINFUNCTYPE(
    None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD
)
user32.SetWinEventHook.restype = w.HANDLE
user32.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HMODULE, WinEventProcType, w.DWORD, w.DWORD, w.DWORD]
user32.UnhookWinEvent.argtypes = [w.HANDLE]


def set_win_event_hook(event_min: int, event_max: int, proc) -> int:
    return user32.SetWinEventHook(event_min, event_max, 0, proc, 0, 0, WINEVENT_OUTOFCONTEXT)


def unhook(h: int) -> None:
    if h:
        user32.UnhookWinEvent(h)


def pump_messages() -> None:
    """Blocking message loop; returns when WM_QUIT is posted to this thread."""
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def current_thread_id() -> int:
    return int(kernel32.GetCurrentThreadId())


def post_quit(thread_id: int) -> None:
    user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)


# ---- session / presence ------------------------------------------------------
# A private user32 handle: argtypes set here must not clash with other modules (the native bar
# declares GetMonitorInfoW with its own struct on the shared ctypes.windll.user32).
_u32 = ctypes.WinDLL("user32")
_u32.OpenInputDesktop.restype = w.HANDLE
_u32.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
_u32.CloseDesktop.argtypes = [w.HANDLE]
_u32.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]


def session_locked() -> bool:
    """True while the lock screen / secure desktop owns input (Win+L, UAC, Ctrl+Alt+Del)."""
    h = _u32.OpenInputDesktop(0, False, 0x0001)          # DESKTOP_READOBJECTS
    if not h:
        return True                                         # no access to the input desktop = Winlogon has it
    try:
        buf = ctypes.create_unicode_buffer(64)
        n = w.DWORD()
        if _u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(n)):   # UOI_NAME
            return buf.value.lower() != "default"
        return False
    finally:
        _u32.CloseDesktop(h)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


_u32.MonitorFromWindow.restype = w.HANDLE
_u32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
_u32.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(_MONITORINFO)]
_u32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
_u32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]


def foreground_fullscreen() -> bool:
    """True if the foreground window covers its whole monitor (video, game, slideshow), desktop excluded."""
    hwnd = _u32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    _u32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    r = w.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    if not _u32.GetMonitorInfoW(_u32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


# ---- whole process table in one syscall ------------------------------------------
_ntdll = ctypes.windll.ntdll
_ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_EPOCH_100NS = 116444736000000000
_snap_buf = None


def process_snapshot() -> list[tuple[int, int, str, float, int, int]]:
    """[(pid, ppid, name_lower, create_epoch, cpu_100ns, working_set_bytes)] for every process.

    One NtQuerySystemInformation(SystemProcessInformation) call instead of opening each
    process (what psutil does): ~1 ms for 300 processes, and no access-denied gaps."""
    import struct
    global _snap_buf
    size = ctypes.c_ulong(0)
    for _ in range(4):
        if _snap_buf is None or len(_snap_buf) < size.value:
            _snap_buf = ctypes.create_string_buffer(max(size.value + 65536, 512 * 1024))
        st = _ntdll.NtQuerySystemInformation(5, _snap_buf, len(_snap_buf), ctypes.byref(size))
        if st == 0:
            break
        if st & 0xFFFFFFFF != 0xC0000004:          # STATUS_INFO_LENGTH_MISMATCH -> grow and retry
            raise OSError(f"NtQuerySystemInformation failed: {st & 0xFFFFFFFF:#x}")
    else:
        raise OSError("process snapshot: buffer kept growing")
    raw = _snap_buf.raw
    base = ctypes.addressof(_snap_buf)
    out, off = [], 0
    while True:
        nxt = struct.unpack_from("<I", raw, off)[0]
        create, user, kernel = struct.unpack_from("<qqq", raw, off + 32)
        nlen, _nmax, nptr = struct.unpack_from("<HH4xQ", raw, off + 56)
        pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
        ws = struct.unpack_from("<Q", raw, off + 144)[0]
        name = raw[nptr - base: nptr - base + nlen].decode("utf-16-le", "ignore").lower() if nptr and nlen else ("idle" if pid == 0 else "")
        out.append((pid, ppid, name, (create - _EPOCH_100NS) / 1e7 if create else 0.0, user + kernel, ws))
        if not nxt:
            break
        off += nxt
    return out
