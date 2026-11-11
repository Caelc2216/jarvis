"""Things Jarvis can actually do.

Each tool does one narrow job. None of them can run arbitrary commands,
delete files, or reach anything not listed here. To add a tool, write a
function and add an entry to TOOLS at the bottom.
"""

import ctypes
import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from music import list_playlists, music_control, now_playing, play_music, play_playlist
from windows_control import list_windows, window_action
from apps import forget_nickname, rescan_apps, search_apps
from routines import add_to_routine, create_routine, delete_routine, list_routines, remove_from_routine

HERE = Path(__file__).parent
MEMORY_FILE = HERE / "memory.json"
LIGHTS_FILE = HERE / "devices.json"  # made by `python -m tinytuya wizard`
TIMERS_FILE = HERE / "timers.json"

# Apps Jarvis may open by name. Add your own: name -> URI, URL or program.
APPS = {
    "apple music": "shell:AppsFolder\\AppleInc.AppleMusicWin_nzyj5cx40ttqa!App",
    "spotify": "spotify:",
    "chrome": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "calendar": "https://calendar.google.com",
    "claude": "https://claude.ai",
    "chatgpt": "https://chatgpt.com",
    "discord": "discord:",
    "calculator": "calc.exe",
    "notepad": "notepad.exe",
    "file explorer": "explorer.exe",
    "settings": "ms-settings:",
}


# ------------------------------------------------------------- basics --
def get_time() -> str:
    return dt.datetime.now().strftime("%A, %B %d %Y, %I:%M %p")


def get_weather(location: str) -> str:
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1}, timeout=10,
    ).json()
    if not geo.get("results"):
        return f"Couldn't find a place called {location}."
    place = geo["results"][0]
    wx = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "forecast_days": 2, "timezone": "auto",
        }, timeout=10,
    ).json()
    return json.dumps({"place": f"{place['name']}, {place.get('admin1', '')}",
                       "current": wx.get("current"), "daily": wx.get("daily")})


# ------------------------------------------------------------ computer --
def open_app(name: str) -> str:
    """Open anything: a nickname, a built-in shortcut (gmail, youtube...), or any installed app/game."""
    import apps

    key = name.lower().strip().removeprefix("the ").removeprefix("my ").strip()
    key = apps.aliases().get(key, key)
    target = APPS.get(key.lower())
    if target:
        os.startfile(target)
        return f"Opened {key}."
    return apps.open_installed(key)


def learn_nickname(nickname: str, app_name: str) -> str:
    import apps

    if app_name.lower().strip() in APPS:  # e.g. "my email" -> gmail
        al = apps.aliases()
        al[nickname.lower().strip().removeprefix("the ").removeprefix("my ").strip()] = app_name.lower().strip()
        apps.ALIASES.write_text(json.dumps(al, indent=2), encoding="utf-8")
        return f"Got it: '{nickname}' now means {app_name}."
    return apps.learn_nickname(nickname, app_name)


def open_website(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return "I can only open web addresses."
    os.startfile(url)
    return f"Opened {url}."


def search_youtube(query: str) -> str:
    os.startfile("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
    return f"Searching YouTube for {query}."


def spotify_search(query: str) -> str:
    os.startfile("spotify:search:" + urllib.parse.quote(query))
    return f"Opened Spotify search for {query}. The user may need to press play."


_KEYS = {
    "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1,
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
}


def media_control(action: str, steps: int = 1) -> str:
    key = _KEYS.get(action)
    if key is None:
        return f"Unknown action. Use one of: {', '.join(_KEYS)}."
    steps = max(1, min(int(steps), 50)) if action.startswith("volume") else 1
    for _ in range(steps):  # each volume step is about 2%
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)
    return f"Done: {action}" + (f" x{steps}" if steps > 1 else "")


def set_timer(minutes: float, label: str = "timer") -> str:
    # Timers go in a file that jarvis.py watches, so they still fire when a tool
    # runs inside a short-lived Claude Code or Codex process.
    due = time.time() + max(1, float(minutes) * 60)
    with _timer_lock:
        timers = _load_timers()
        timers.append({"due": due, "label": label})
        TIMERS_FILE.write_text(json.dumps(timers), encoding="utf-8")
    return f"Timer set for {minutes:g} minutes ({label})."


_timer_lock = threading.Lock()


def _load_timers() -> list[dict]:
    try:
        return json.loads(TIMERS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def pop_due_timers() -> list[str]:
    """Called by jarvis.py: return labels of timers that are done and remove them."""
    with _timer_lock:
        timers = _load_timers()
        now = time.time()
        due = [t["label"] for t in timers if t["due"] <= now]
        if due:
            TIMERS_FILE.write_text(json.dumps([t for t in timers if t["due"] > now]), encoding="utf-8")
        return due


# -------------------------------------------------------------- memory --
def load_memory() -> list[str]:
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def remember(fact: str) -> str:
    facts = load_memory()
    if fact not in facts:
        facts.append(fact)
        MEMORY_FILE.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    return "Saved."


def forget(fact_contains: str) -> str:
    facts = load_memory()
    kept = [f for f in facts if fact_contains.lower() not in f.lower()]
    MEMORY_FILE.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    return f"Forgot {len(facts) - len(kept)} item(s)."


# -------------------------------------------------------------- lights --
COLORS = {
    "red": (255, 0, 0), "orange": (255, 120, 0), "yellow": (255, 210, 0),
    "green": (0, 255, 0), "blue": (0, 60, 255), "purple": (140, 0, 255),
    "pink": (255, 60, 150), "cyan": (0, 255, 255), "white": (255, 255, 255),
}


def _bulbs(name: str):
    import tinytuya

    if not LIGHTS_FILE.exists():
        raise RuntimeError("Lights aren't set up yet. Run: python -m tinytuya wizard")
    devices = json.loads(LIGHTS_FILE.read_text(encoding="utf-8"))
    name = name.lower().strip()
    picked = [d for d in devices if name in ("all", "everything", "lights")
              or name in d.get("name", "").lower()]
    if not picked:
        raise RuntimeError("No light matches that name. Lights: "
                           + ", ".join(d.get("name", "?") for d in devices))
    for d in picked:
        if d.get("ip") and d.get("version"):
            bulb = tinytuya.BulbDevice(d["id"], address=d["ip"], local_key=d["key"],
                                       version=float(d["version"]))
        else:  # not seen on Wi-Fi during setup: find it (and its version) now
            bulb = tinytuya.BulbDevice(d["id"], address="Auto", local_key=d["key"])
        bulb.set_socketTimeout(3)
        yield d.get("name", d["id"]), bulb


def control_lights(name: str, action: str, value: str = "") -> str:
    results = []
    for label, bulb in _bulbs(name):
        if action == "on":
            r = bulb.turn_on()
        elif action == "off":
            r = bulb.turn_off()
        elif action == "brightness":
            r = bulb.set_brightness_percentage(max(1, min(int(float(value)), 100)))
        elif action == "color":
            rgb = COLORS.get(value.lower())
            if rgb is None:
                return f"I know these colors: {', '.join(COLORS)}."
            r = bulb.set_white_percentage(100, 50) if value.lower() == "white" else bulb.set_colour(*rgb)
        else:
            return "Action must be on, off, brightness or color."
        ok = not (isinstance(r, dict) and r.get("Error"))
        results.append(f"{label}: {'ok' if ok else r.get('Error')}")
    return "; ".join(results)


def list_lights() -> str:
    if not LIGHTS_FILE.exists():
        return "Lights aren't set up yet."
    return ", ".join(d.get("name", "?") for d in json.loads(LIGHTS_FILE.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- updates --
def check_for_updates() -> str:
    import updater

    return updater.check()


def update_jarvis() -> str:
    import updater

    if not updater.update_available():
        status = updater.check()
        if not updater.update_available():
            return status
    return updater.start_update_in_background()


def jarvis_version() -> str:
    import updater

    return f"Jarvis version {updater.local()['version']}."


# --------------------------------------------------------------- registry --
def _s(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS = [
    (get_time, "Get the current local date and time.", _s({}, [])),
    (get_weather, "Current weather and today/tomorrow forecast for a place (Fahrenheit).",
     _s({"location": {"type": "string", "description": "City name, e.g. Austin"}}, ["location"])),
    (open_app, "Open any installed app or game (Start menu, Steam, Store apps, desktop shortcuts), a nickname "
     "the user taught you, or one of these sites: " + ", ".join(APPS) + ". If it says it's unsure or can't "
     "find it, ask the user, then save their answer with learn_nickname so you know next time.",
     _s({"name": {"type": "string"}}, ["name"])),
    (learn_nickname, "Remember that a word or phrase the user uses means a specific app, e.g. "
     "'the game' = 'Rocket League' or 'my email' = 'gmail'. Use the app's real name.",
     _s({"nickname": {"type": "string"}, "app_name": {"type": "string"}}, ["nickname", "app_name"])),
    (forget_nickname, "Forget a nickname the user taught you.",
     _s({"nickname": {"type": "string"}}, ["nickname"])),
    (search_apps, "Search the user's installed apps and games by name (to answer 'do I have X?' or "
     "to find the exact name).", _s({"query": {"type": "string"}}, ["query"])),
    (rescan_apps, "Re-scan the PC for installed apps and games (after the user installs something).",
     _s({}, [])),
    # Keeping Jarvis itself up to date
    (check_for_updates, "Check whether a newer version of Jarvis is available.", _s({}, [])),
    (update_jarvis, "Download and install the newest Jarvis, then restart. Only when the user asks you to "
     "update. Their settings and everything you've learned are kept.", _s({}, [])),
    (jarvis_version, "Say which version of Jarvis this is.", _s({}, [])),
    # Routines the user builds by talking
    (create_routine, "Create (or replace) a routine: a name the user says to open several apps at once, "
     "e.g. name 'homework', apps ['Google Docs', 'Spotify']. Confirm the app list with the user first.",
     _s({"name": {"type": "string"}, "apps_to_open": {"type": "array", "items": {"type": "string"}},
         "keywords": {"type": "array", "items": {"type": "string"},
                      "description": "other words that should also start it"}}, ["name", "apps_to_open"])),
    (add_to_routine, "Add one app to an existing routine.",
     _s({"name": {"type": "string"}, "app_name": {"type": "string"}}, ["name", "app_name"])),
    (remove_from_routine, "Remove one app from an existing routine.",
     _s({"name": {"type": "string"}, "app_name": {"type": "string"}}, ["name", "app_name"])),
    (delete_routine, "Delete a routine. Only when the user clearly asks to delete it.",
     _s({"name": {"type": "string"}}, ["name"])),
    (list_routines, "List the user's routines and what each one opens.", _s({}, [])),
    (open_website, "Open a website in the browser.",
     _s({"url": {"type": "string"}}, ["url"])),
    (search_youtube, "Search YouTube in the browser.",
     _s({"query": {"type": "string"}}, ["query"])),
    (spotify_search, "Open Spotify search. Only use this when the user specifically says Spotify; "
     "otherwise use play_music (Apple Music).",
     _s({"query": {"type": "string"}}, ["query"])),
    (media_control, "Control media playback and system volume. Each volume step is about 2 percent.",
     _s({"action": {"type": "string", "enum": list(_KEYS)},
         "steps": {"type": "integer", "description": "Volume steps, default 1"}}, ["action"])),
    (set_timer, "Set a spoken timer or reminder.",
     _s({"minutes": {"type": "number"}, "label": {"type": "string"}}, ["minutes"])),
    (remember, "Save a lasting fact about the user (name, preferences, routines).",
     _s({"fact": {"type": "string"}}, ["fact"])),
    (forget, "Delete saved facts containing this text.",
     _s({"fact_contains": {"type": "string"}}, ["fact_contains"])),
    (control_lights, "Control the user's smart lights. name is a light name, part of one, or 'all'. "
     "For brightness, value is 1-100. For color, value is a color name: " + ", ".join(COLORS),
     _s({"name": {"type": "string"}, "action": {"type": "string", "enum": ["on", "off", "brightness", "color"]},
         "value": {"type": "string"}}, ["name", "action"])),
    (list_lights, "List the names of the user's smart lights.", _s({}, [])),
    # Windows on the user's screens
    (list_windows, "List the windows that are open right now (app and title).", _s({}, [])),
    (window_action, "Close, minimize, maximize, restore or focus windows. target is an app name "
     "('chrome', 'vs code', 'discord', 'steam', 'apple music'), words from a window's title, or a routine "
     "name ('school', 'games') to act on everything that routine opened. Closing is like clicking X, so "
     "apps can still ask to save. If unsure which window the user means, call list_windows first.",
     _s({"target": {"type": "string"},
         "action": {"type": "string", "enum": ["close", "minimize", "maximize", "restore", "focus"]}},
        ["target", "action"])),
    # Apple Music (the user's main music app)
    (play_music, "Play music on Apple Music. kind='song' for a specific song, 'album' for an album, "
     "'artist' to just play an artist. Put the artist in the query when you know it.",
     _s({"query": {"type": "string", "description": "e.g. 'Here Comes the Sun Beatles'"},
         "kind": {"type": "string", "enum": ["song", "album", "artist"]}}, ["query", "kind"])),
    (play_playlist, "Play one of the user's own Apple Music playlists by name.",
     _s({"name": {"type": "string"}}, ["name"])),
    (list_playlists, "List the user's Apple Music playlists.", _s({}, [])),
    (now_playing, "Say what song is playing right now (title, artist, album).", _s({}, [])),
    (music_control, "Play, pause, skip to next or go to previous track in the music app.",
     _s({"action": {"type": "string", "enum": ["play", "pause", "next", "previous"]}}, ["action"])),
]
FUNCTIONS = {f.__name__: f for f, _, _ in TOOLS}


def log_activity(name: str, args: dict) -> None:
    """Show a short line in the HUD's activity log."""
    try:
        import hud

        detail = next((str(v) for v in args.values() if isinstance(v, str) and v), "")
        hud.activity(name.replace("_", " ").capitalize() + (f" · {detail[:40]}" if detail else ""))
    except Exception:
        pass


def run_tool(name: str, args: dict) -> str:
    fn = FUNCTIONS.get(name)
    if fn is None:
        return f"Unknown tool {name}."
    print(f"  [tool] {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})", file=sys.stderr)
    log_activity(name, args)
    try:
        return str(fn(**args))
    except Exception as e:
        return f"Error: {e}"
