"""Jarvis's on-screen display (the HUD).

A tiny local web server (127.0.0.1 only) serves hud.html and streams Jarvis's
state to it; the page opens as a frameless Chrome app window placed by winlayout.

    hud.start()                    # in jarvis.py
    hud.state("listening")         # idle | listening | thinking | speaking
    hud.said("Opening Canvas.")    # what Jarvis is saying
    hud.heard("school")            # what the user said
    hud.activity("Opened Canvas")  # works from any process (posts to the server)

Settings in .env: HUD=on|off, HUD_MONITOR=left|main|right, HUD_AREA=bottom-right|full|...,
HUD_SIZE=420x640, HUD_ON_TOP=on|off.
"""

import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = int(os.getenv("HUD_PORT", "8765"))
import paths  # noqa: E402

BROWSER = paths.browser()                      # Chrome, or Edge if Chrome isn't installed
BROWSER_EXE = os.path.basename(BROWSER).lower()

_state = {"mode": "idle", "caption": "Say “Hey Jarvis”.", "you": "", "jarvis": "",
          "brain": "", "routine": "", "activity": [], "playing": None, "level": 0.0}
_lock = threading.Lock()
_clients: list[queue.Queue] = []
_server = None


# ------------------------------------------------------------ publishing --
def _push(event: str, data) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    for q in list(_clients):
        try:
            q.put_nowait(msg)
        except queue.Full:
            pass


def _update(**changes) -> None:
    with _lock:
        _state.update(changes)
        snapshot = dict(_state)
    _push("state", snapshot)


def state(mode: str, caption: str | None = None) -> None:
    if _server is None:
        return
    _update(mode=mode, **({"caption": caption} if caption is not None else {}))
    hide() if mode == "idle" else show()


def said(text: str) -> None:
    if _server is not None:
        _update(mode="speaking", caption=text, jarvis=text)
        show()


def heard(text: str) -> None:
    if _server is not None:
        _update(mode="thinking", caption="", you=text)
        show()


def level(value: float) -> None:
    """Mic loudness 0..1 while listening (drives the orb)."""
    if _server is not None:
        _push("level", round(min(1.0, value), 3))


def set_info(**info) -> None:
    if _server is not None:
        _update(**info)


def activity(text: str) -> None:
    """Add a line to the activity log. From another process this posts to the running HUD."""
    if _server is not None:
        with _lock:
            _state["activity"] = ([{"t": time.strftime("%H:%M"), "text": text}] + _state["activity"])[:8]
        _update()
        return
    try:  # e.g. a tool running inside Claude Code's MCP server process
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/activity", data=text.encode(), method="POST")
        urllib.request.urlopen(req, timeout=0.5).close()
    except Exception:
        pass


# ---------------------------------------------------------------- server --
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep jarvis.log quiet
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "hud.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/icon.png", "/favicon.ico"):
            import icon

            icon.ensure()
            if icon.PNG.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(icon.PNG.read_bytes())
            else:
                self.send_error(404)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=200)
            _clients.append(q)
            with _lock:
                q.put(f"event: state\ndata: {json.dumps(_state)}\n\n".encode())
            try:
                while True:
                    try:
                        self.wfile.write(q.get(timeout=15))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                _clients.remove(q)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/activity" and self.client_address[0] == "127.0.0.1":
            n = min(int(self.headers.get("Content-Length", 0)), 500)
            activity(self.rfile.read(n).decode("utf-8", "replace"))
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)


def _watch_music():
    """Keep the Now Playing card current."""
    import music

    last = None
    while True:
        try:
            info = music._run(music._now_playing())
            info = info if info and info.get("title") else None
            if info != last:
                last = info
                set_info(playing=info)
        except Exception:
            pass
        time.sleep(3)


# ---------------------------------------------------------------- window --
# The HUD shows only while Jarvis is awake; while it waits for "Hey Jarvis" it's hidden.
AUTOHIDE = os.getenv("HUD_AUTOHIDE", "on").lower() not in ("off", "0", "false", "no")
HIDE_DELAY_S = 1.5
_win_lock = threading.Lock()
_hwnd = None
_want_visible = False


def _find_hud() -> int | None:
    """The HUD window, even while hidden (so a restart reuses it instead of opening another)."""
    import ctypes
    import ctypes.wintypes as wt

    import winlayout as wl

    found = []

    def cb(hwnd, _):
        if wl._title(hwnd) == "Jarvis" and wl._exe(hwnd) == BROWSER_EXE and not wl.user32.GetWindow(hwnd, 4):
            found.append(hwnd)
        return True

    wl.user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def _ensure_window() -> int | None:
    """Return the HUD window, opening and placing it if it isn't there (or was closed)."""
    global _hwnd
    import winlayout as wl

    if _hwnd and wl.user32.IsWindow(_hwnd):
        return _hwnd
    _hwnd = _find_hud()
    if _hwnd:
        return _hwnd
    w, h = (int(x) for x in os.getenv("HUD_SIZE", "420x640").lower().split("x"))
    profile = HERE / ".hud-profile"  # its own Chrome profile, so it never mixes with your tabs
    if not BROWSER:
        print("  (HUD needs Chrome or Edge; neither was found)")
        return None
    before = set(wl.windows(BROWSER_EXE))
    subprocess.Popen([BROWSER, f"--app=http://127.0.0.1:{PORT}/", f"--user-data-dir={profile}",
                      f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check",
                      "--disable-features=Translate"], creationflags=0x08000000)
    _hwnd = wl.wait_for_window(BROWSER_EXE, before=before, title_has="Jarvis", timeout=20)
    if _hwnd:
        _place(_hwnd)
    return _hwnd


def _apply_visibility() -> None:
    import winlayout as wl

    with _win_lock:
        if _want_visible:
            hwnd = _ensure_window()
            if hwnd:
                wl.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE: appear without stealing focus
                if os.getenv("HUD_ON_TOP", "on").lower() in ("on", "1", "true", "yes"):
                    wl.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        else:
            hwnd = _hwnd if _hwnd and wl.user32.IsWindow(_hwnd) else _find_hud()
            if hwnd:
                wl.user32.ShowWindow(hwnd, 0)  # SW_HIDE: gone from screen and taskbar


def show() -> None:
    global _want_visible
    if _server is None:
        return
    if not _want_visible or not AUTOHIDE:
        _want_visible = True
        threading.Thread(target=_apply_visibility, daemon=True).start()


def hide() -> None:
    """Hide shortly after going idle, unless Jarvis wakes up again in the meantime."""
    if _server is None or not AUTOHIDE:
        return

    def later():
        global _want_visible
        time.sleep(HIDE_DELAY_S)
        if _state.get("mode") == "idle":
            _want_visible = False
            _apply_visibility()

    threading.Thread(target=later, daemon=True).start()


def mode() -> str:
    return _state.get("mode", "idle")


def _place(hwnd) -> None:
    import winlayout as wl

    w, h = (int(x) for x in os.getenv("HUD_SIZE", "420x640").lower().split("x"))
    m = wl.monitor(os.getenv("HUD_MONITOR", "right"))
    area = os.getenv("HUD_AREA", "bottom-right")
    if area == "full":
        wl.place(hwnd, {"monitor": os.getenv("HUD_MONITOR", "right"), "area": "maximize"})
    else:
        margin = 16
        x = m["x"] + (m["w"] - w - margin if "right" in area else margin)
        y = m["y"] + (m["h"] - h - margin if "bottom" in area else margin)
        wl.place(hwnd, {"x": x, "y": y, "w": w, "h": h})


def _open_window():
    """At startup: make sure the window exists and is in place, then show or hide it."""
    global _want_visible
    with _win_lock:
        hwnd = _ensure_window()
        if hwnd:
            _place(hwnd)
    _want_visible = not AUTOHIDE or _state.get("mode") != "idle"
    _apply_visibility()


def start(brain_name: str = "") -> None:
    global _server
    if os.getenv("HUD", "on").lower() in ("off", "0", "false", "no"):
        return
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    except OSError as e:
        print(f"  (HUD disabled: port {PORT} busy: {e})")
        return
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    threading.Thread(target=_watch_music, daemon=True).start()
    _update(brain=brain_name)
    threading.Thread(target=_open_window, daemon=True).start()
