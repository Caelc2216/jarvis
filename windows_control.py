"""Close, minimize, maximize, restore or focus windows by name.

Closing is the same as clicking the window's X: apps get to ask "save changes?",
nothing is force-killed. Jarvis's own HUD and Windows' desktop/taskbar are never touched.
"""

import json
import time
from pathlib import Path

import winlayout as wl

# Hard limit, enforced here so no brain can loop around it: at most this many windows
# closed in a rolling window of time (routine closes like "close school" are exempt).
MAX_CLOSES = 3
CLOSE_WINDOW_S = 120
HISTORY = Path(__file__).with_name("close_history.json")
PROTECTED_APPS = {"claude.exe", "explorer.exe"}  # never close the Claude app or Windows' shell windows


def _recent_closes() -> list[float]:
    try:
        stamps = json.loads(HISTORY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        stamps = []
    return [t for t in stamps if time.time() - t < CLOSE_WINDOW_S]


def _record_closes(n: int) -> None:
    HISTORY.write_text(json.dumps(_recent_closes() + [time.time()] * n), encoding="utf-8")

# Spoken names -> the program behind the window.
APP_NAMES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "browser": "chrome.exe",
    "vs code": "code.exe", "vscode": "code.exe", "visual studio code": "code.exe", "code": "code.exe",
    "discord": "discord.exe", "steam": "steamwebhelper.exe", "apple music": "applemusic.exe",
    "music": "applemusic.exe", "spotify": "spotify.exe", "file explorer": "explorer.exe",
    "explorer": "explorer.exe", "files": "explorer.exe", "notepad": "notepad.exe",
    "calculator": "calculatorapp.exe", "settings": "systemsettings.exe", "terminal": "windowsterminal.exe",
    "visual studio": "devenv.exe", "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
    "outlook": "olk.exe", "teams": "ms-teams.exe", "zoom": "zoom.exe", "obs": "obs64.exe",
}
FRIENDLY = {v: k.title() for k, v in reversed(list(APP_NAMES.items()))}
FRIENDLY.update({"code.exe": "VS Code", "steamwebhelper.exe": "Steam", "applemusic.exe": "Apple Music"})
NEVER = {"Jarvis", "Program Manager", "Windows Input Experience", "Microsoft Text Input Application"}


HIDDEN_APPS = {"nvidia overlay.exe", "powertoys.quickaccess.exe", "textinputhost.exe", "shellexperiencehost.exe",
               "searchhost.exe", "startmenuexperiencehost.exe", "lockapp.exe"}


def _real(hwnd) -> bool:
    """A window a person would call a window: not cloaked/invisible, not an overlay or tool window."""
    import ctypes

    cloaked = ctypes.c_int(0)
    wl.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
    exstyle = wl.user32.GetWindowLongW(hwnd, -20)
    return (not cloaked.value and not exstyle & 0x80  # WS_EX_TOOLWINDOW
            and wl._title(hwnd) not in NEVER and wl._exe(hwnd) not in HIDDEN_APPS)


def _all():
    return [h for h in wl.windows() if _real(h)]


def _app(hwnd) -> str:
    exe = wl._exe(hwnd)
    if exe == "applicationframehost.exe":  # Windows Store apps: the title is the app's name
        return wl._title(hwnd)
    return FRIENDLY.get(exe, exe.removesuffix(".exe").capitalize())


def list_windows() -> str:
    wins = _all()
    if not wins:
        return "No windows are open."
    return "Open windows: " + "; ".join(f"{_app(h)}: {wl._title(h)[:60]}" for h in wins)


def _find(target: str) -> list[int]:
    t = target.lower().strip().removesuffix(" window").removesuffix(" windows").removeprefix("the ")
    wins = _all()
    # A routine name ("school", "games") means every app that routine opens.
    try:
        import routines

        name = routines.match(t) if t not in APP_NAMES else None
        if name and t in [name, *routines.load()["routines"][name].get("keywords", [])]:
            # Only the exact windows the routine opened, never your other windows of the same app.
            return [h for h in routines.opened_windows(name) if h in wins]
    except Exception:
        pass
    if t in APP_NAMES:  # an app: all of its windows
        by_exe = [h for h in wins if wl._exe(h) == APP_NAMES[t]]
        if by_exe:
            return by_exe
    by_title = [h for h in wins if t in wl._title(h).lower()]  # also covers Store apps like Calculator
    if by_title:
        return by_title
    return [h for h in wins if t.replace(" ", "") in wl._exe(h).removesuffix(".exe")]


def window_action(target: str, action: str = "close") -> str:
    """action: close | minimize | maximize | restore | focus"""
    action = action.lower()
    if action not in ("close", "minimize", "maximize", "restore", "focus"):
        return "Action must be close, minimize, maximize, restore or focus."
    if target.lower().strip() in ("all", "everything", "all windows"):
        if action == "close":
            return "I won't close every window at once. Name the app or window to close."
        hits = _all()
    else:
        hits = _find(target)
    if not hits:
        return f"I can't find a window matching '{target}'. {list_windows()}"
    if action == "close":
        protected = [h for h in hits if wl._exe(h) in PROTECTED_APPS]
        hits = [h for h in hits if h not in protected]
        if not hits:
            return "I'm not allowed to close that window."
        is_routine = False
        try:
            import routines

            is_routine = bool(routines.match(target)) and bool(routines.opened_windows(routines.match(target)))
        except Exception:
            pass
        if not is_routine:
            used = len(_recent_closes())
            if used + len(hits) > MAX_CLOSES:
                return (f"Stopped: that would close {len(hits)} more window(s), and I only close up to "
                        f"{MAX_CLOSES} every {CLOSE_WINDOW_S // 60} minutes so nothing gets lost. "
                        "Tell the user which windows are still open and let them close the rest, "
                        "or ask them to name one window at a time.")
        _record_closes(len(hits))
    names = [f"{_app(h)} ({wl._title(h)[:40]})" for h in hits]
    for h in hits:
        if action == "close":
            wl.user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE: like clicking the X
        elif action == "minimize":
            wl.user32.ShowWindow(h, 6)
        elif action == "maximize":
            wl.user32.ShowWindow(h, 3)
        elif action == "restore":
            wl.user32.ShowWindow(h, 9)
        elif action == "focus":
            wl.user32.ShowWindow(h, 9)
            wl.user32.SetForegroundWindow(h)
    if action != "close":
        return f"Done: {action} {', '.join(names)}."
    time.sleep(1.5)
    still = [h for h in hits if wl.user32.IsWindow(h) and wl.user32.IsWindowVisible(h)]
    closed = len(hits) - len(still)
    msg = f"Closed {closed} window(s): {', '.join(names)}." if closed else ""
    if still:
        msg += (f" {len(still)} didn't close yet, probably asking to save changes: "
                + ", ".join(f"{_app(h)} ({wl._title(h)[:40]})" for h in still) + ".")
    return msg.strip()
