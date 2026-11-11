"""Find, launch and position windows on your monitors (Windows only).

Positions in routines.json can be:
  {"monitor": "main", "area": "left"}     monitor: main | left | right | 1 | 2 | 3
                                           area: full | maximize | left | right | top | bottom |
                                                 top-left | top-right | bottom-left | bottom-right |
                                                 left-third | middle-third | right-third
  {"x": 0, "y": 0, "w": 1280, "h": 1392}  exact pixels (what "save layout" records)
  {"x": ..., "maximized": true}           saved from a maximized window
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

# Work in real pixels even with display scaling.
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:
    pass

SW_RESTORE, SW_MAXIMIZE = 9, 3


# --------------------------------------------------------------- monitors --
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitors() -> list[dict]:
    """Monitors sorted left to right, each with its work area (excluding the taskbar)."""
    found = []

    def cb(hmon, hdc, rect, data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcWork
        found.append({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                      "primary": bool(info.dwFlags & 1)})
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    return sorted(found, key=lambda m: m["x"])


def monitor(name) -> dict:
    mons = monitors()
    primary = next((m for m in mons if m["primary"]), mons[0])
    name = str(name).lower()
    if name in ("main", "primary", "center", "middle"):
        return primary
    if name == "left":
        left = [m for m in mons if m["x"] < primary["x"]]
        return left[-1] if left else primary
    if name == "right":
        right = [m for m in mons if m["x"] > primary["x"]]
        return right[0] if right else primary
    if name.isdigit() and 1 <= int(name) <= len(mons):
        return mons[int(name) - 1]  # numbered left to right
    return primary


AREAS = {
    "full": (0, 0, 1, 1), "left": (0, 0, .5, 1), "right": (.5, 0, .5, 1),
    "top": (0, 0, 1, .5), "bottom": (0, .5, 1, .5),
    "top-left": (0, 0, .5, .5), "top-right": (.5, 0, .5, .5),
    "bottom-left": (0, .5, .5, .5), "bottom-right": (.5, .5, .5, .5),
    "left-third": (0, 0, 1 / 3, 1), "middle-third": (1 / 3, 0, 1 / 3, 1), "right-third": (2 / 3, 0, 1 / 3, 1),
    "left-two-thirds": (0, 0, 2 / 3, 1), "right-two-thirds": (1 / 3, 0, 2 / 3, 1),
}


# ---------------------------------------------------------------- windows --
def _exe(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def _title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def windows(exe: str = "", title_has: str = "", title_not: tuple = ()) -> list[int]:
    """Visible top-level app windows, optionally filtered by program and title."""
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # skip owned popups
            return True
        title = _title(hwnd)
        if not title:
            return True
        if exe and _exe(hwnd) != exe.lower():
            return True
        if title_has and title_has.lower() not in title.lower():
            return True
        if any(bad.lower() in title.lower() for bad in title_not):
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if not user32.IsIconic(hwnd) and (r.right - r.left < 300 or r.bottom - r.top < 200):  # splash screens
            return True
        out.append(hwnd)
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    user32.EnumWindows(proto(cb), 0)
    return out


def wait_for_window(exe, before=(), title_has="", title_not=(), timeout=30.0, allow_existing=False):
    """Wait for a new window of `exe` (not in `before`). Falls back to an existing one if allowed."""
    end = time.time() + timeout
    while time.time() < end:
        new = [h for h in windows(exe, title_has, title_not) if h not in before]
        if new:
            time.sleep(0.8)  # let it finish appearing before moving it
            return new[0]
        time.sleep(0.5)
    if allow_existing:
        existing = windows(exe, title_has, title_not)
        return existing[0] if existing else None
    return None


def _frame_offsets(hwnd):
    """Windows 10/11 windows have invisible borders; return (left, top, right, bottom) padding."""
    r, f = wt.RECT(), wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(f), ctypes.sizeof(f)) != 0:  # EXTENDED_FRAME_BOUNDS
        return 0, 0, 0, 0
    return f.left - r.left, f.top - r.top, r.right - f.right, r.bottom - f.bottom


def place(hwnd, spec: dict) -> None:
    if not hwnd or not spec:
        return
    user32.ShowWindow(hwnd, SW_RESTORE)
    if "x" in spec:
        x, y, w, h = spec["x"], spec["y"], spec["w"], spec["h"]
    else:
        m = monitor(spec.get("monitor", "main"))
        area = spec.get("area", "full")
        fx, fy, fw, fh = AREAS.get("full" if area == "maximize" else area, AREAS["full"])
        x, y = m["x"] + round(m["w"] * fx), m["y"] + round(m["h"] * fy)
        w, h = round(m["w"] * fw), round(m["h"] * fh)
        if area == "maximize":
            spec = {**spec, "maximized": True}
    pl, pt, pr, pb = _frame_offsets(hwnd)
    user32.SetWindowPos(hwnd, 0, x - pl, y - pt, w + pl + pr, h + pt + pb, 0x0004 | 0x0040)
    if spec.get("maximized"):
        user32.ShowWindow(hwnd, SW_MAXIMIZE)


def capture(hwnd) -> dict:
    """Current position of a window, in the format place() accepts."""
    maximized = bool(user32.IsZoomed(hwnd))
    if maximized:
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)
    f = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(f), ctypes.sizeof(f)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(f))
    spec = {"x": f.left, "y": f.top, "w": f.right - f.left, "h": f.bottom - f.top}
    if maximized:
        spec["maximized"] = True
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    return spec


def foreground() -> int:
    return user32.GetForegroundWindow()


def describe(hwnd) -> str:
    return f"{_title(hwnd)} ({_exe(hwnd)})"


def launch(cmd) -> None:
    """Start a program without a console window popping up."""
    if isinstance(cmd, str) and (cmd.lower().endswith((".lnk", ".url")) or cmd.startswith(("http", "shell:"))
                                 or ":" in cmd[:12] and not os.path.exists(cmd)):
        os.startfile(cmd)
    else:
        subprocess.Popen(cmd, creationflags=0x08000000, close_fds=True)  # CREATE_NO_WINDOW
