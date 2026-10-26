"""Desktop hang detection: whether explorer, DWM and the foreground window respond."""
import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

SMTO_ABORTIFHUNG = 0x0002
SMTO_BLOCK = 0x0001
WM_NULL = 0x0000
TIMEOUT_MS = 3000

user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def user_idle_s():
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if user32.GetLastInputInfo(ctypes.byref(li)):
        return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0
    return None


def foreground():
    """(hwnd, pid, hung)"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None, False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value, bool(user32.IsHungAppWindow(hwnd))


class Watchdog:
    """Two threads: shell ping (WM_NULL to the desktop window) and DwmFlush.
    An 'in progress' value also counts, so a hang is visible before it ends."""

    def __init__(self):
        self._lock = threading.Lock()
        self.ui_max = 0.0
        self.dwm_max = 0.0
        self._ui_start = None
        self._dwm_start = None
        self._stop = False
        threading.Thread(target=self._ui_loop, daemon=True, name="wd-ui").start()
        threading.Thread(target=self._dwm_loop, daemon=True, name="wd-dwm").start()

    def _ui_loop(self):
        res = ctypes.c_size_t()
        while not self._stop:
            hwnd = user32.GetShellWindow()
            t0 = time.perf_counter()
            self._ui_start = t0
            ok = True
            if hwnd:
                ok = bool(user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_BLOCK | SMTO_ABORTIFHUNG,
                                                     TIMEOUT_MS, ctypes.byref(res)))
            dt = (time.perf_counter() - t0) * 1000
            if not ok:  # timeout or window already considered hung (ABORTIFHUNG returns immediately)
                dt = max(dt, TIMEOUT_MS)
            self._ui_start = None
            with self._lock:
                self.ui_max = max(self.ui_max, dt)
            time.sleep(0.25)

    def _dwm_loop(self):
        while not self._stop:
            t0 = time.perf_counter()
            self._dwm_start = t0
            try:
                dwmapi.DwmFlush()
            except Exception:
                pass
            dt = (time.perf_counter() - t0) * 1000
            self._dwm_start = None
            with self._lock:
                self.dwm_max = max(self.dwm_max, dt)
            time.sleep(0.1)

    def take(self):
        """Returns (ui_ms, dwm_ms) – maximum since the last call, including the ongoing measurement."""
        now = time.perf_counter()
        with self._lock:
            ui, dwm = self.ui_max, self.dwm_max
            self.ui_max = self.dwm_max = 0.0
        if self._ui_start is not None:
            ui = max(ui, (now - self._ui_start) * 1000)
        if self._dwm_start is not None:
            dwm = max(dwm, (now - self._dwm_start) * 1000)
        return ui, dwm

    def stop(self):
        self._stop = True
