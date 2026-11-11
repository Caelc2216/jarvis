"""Apple Music control for Jarvis (Windows app).

- Finds songs/albums/artists with Apple's free iTunes Search API (no key).
- Opens them in the Apple Music app with a music:// link.
- Starts playback by pressing the app's own Play button / double-clicking the
  track through Windows UI Automation (the cursor is put back afterwards).
- Reads "now playing" and controls playback through Windows' media session API.
"""

import asyncio
import ctypes
import os
import time

import requests

APP_ID = "shell:AppsFolder\\AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"
WINDOW_NAME = "Apple Music"


# --------------------------------------------------------- media session --
async def _session():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

    mgr = await M.request_async()
    sessions = list(mgr.get_sessions())
    apple = [s for s in sessions if "applemusic" in s.source_app_user_model_id.lower()]
    return (apple or sessions or [None])[0]


async def _now_playing():
    s = await _session()
    if s is None:
        return None
    p = await s.try_get_media_properties_async()
    status = {4: "playing", 5: "paused", 3: "stopped"}.get(int(s.get_playback_info().playback_status), "idle")
    app = "Apple Music" if "applemusic" in s.source_app_user_model_id.lower() else s.source_app_user_model_id
    return {"title": p.title, "artist": p.artist, "album": p.album_title, "status": status, "app": app}


def _run(coro):
    return asyncio.run(coro)


def now_playing() -> str:
    info = _run(_now_playing())
    if not info or not info["title"]:
        return "Nothing is playing right now."
    return (f"{info['status'].capitalize()}: {info['title']} by {info['artist']}"
            + (f", from {info['album']}" if info["album"] else "") + f" ({info['app']}).")


async def _control(action: str):
    s = await _session()
    if s is None:
        return False
    fn = {"play": s.try_play_async, "pause": s.try_pause_async,
          "next": s.try_skip_next_async, "previous": s.try_skip_previous_async,
          "play_pause": s.try_toggle_play_pause_async}[action]
    return await fn()


def _playing_title() -> str:
    info = _run(_now_playing())
    return info["title"] if info and info["status"] == "playing" else ""


# ------------------------------------------------------------ the app UI --
def _with_ui(fn):
    """UI Automation needs COM set up on whatever thread calls it."""
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        return fn(auto)


def _window(auto, wait=8.0):
    w = auto.WindowControl(searchDepth=1, Name=WINDOW_NAME)
    if not w.Exists(wait, 0.5):
        return None
    popup = w.ButtonControl(Name="Start Listening")  # one-time "What's New" dialog
    if popup.Exists(0.3):
        popup.GetInvokePattern().Invoke()
        time.sleep(0.5)
    return w


def _double_click_keep_cursor(control):
    import ctypes.wintypes as wt

    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    try:
        control.DoubleClick(simulateMove=False, waitTime=0.1)
    finally:
        ctypes.windll.user32.SetCursorPos(pt.x, pt.y)


def _press_page_play(auto, w) -> bool:
    btn = w.ButtonControl(AutomationId="PlayButtonElement", searchDepth=30)
    if not btn.Exists(6, 0.5):
        return False
    btn.GetInvokePattern().Invoke()
    return True


def _open(url: str):
    os.startfile("music://" + url.split("://", 1)[1])


def _wait_playing(timeout=8.0, expect: str = "", changed_from: str = "") -> str:
    """Wait until a song is playing that matches `expect`, or differs from `changed_from`."""
    end = time.time() + timeout
    while time.time() < end:
        title = _playing_title()
        if title and (not expect or expect.lower()[:20] in title.lower()) \
                and (not changed_from or title != changed_from or expect):
            return title
        time.sleep(0.7)
    return ""


# ---------------------------------------------------------------- search --
def _search(term: str, entity: str):
    r = requests.get("https://itunes.apple.com/search", timeout=10, params={
        "term": term, "media": "music", "entity": entity, "limit": 1, "country": "us"})
    results = r.json().get("results", [])
    return results[0] if results else None


# ----------------------------------------------------------------- tools --
def play_music(query: str, kind: str = "song") -> str:
    """kind: song, album or artist."""
    kind = kind if kind in ("song", "album", "artist") else "song"
    if kind == "artist":
        # Play the artist's most popular song; the album carries on from there.
        hit = _search(query, "song")
        if not hit:
            return f"Couldn't find {query} on Apple Music."
        kind = "song"
    else:
        hit = _search(query, "song" if kind == "song" else "album")
        if not hit:
            return f"Couldn't find {query} on Apple Music."

    if kind == "song":
        url, track = hit["trackViewUrl"], hit["trackName"]
        label = f"{track} by {hit['artistName']}"
    else:
        url, track = hit["collectionViewUrl"], None
        label = f"the album {hit['collectionName']} by {hit['artistName']}"

    before = _playing_title()
    try:
        _open(url)
    except OSError:
        return "Apple Music isn't installed on this PC. Use spotify_search or search_youtube instead."
    time.sleep(1.5)

    def start(auto):
        w = _window(auto)
        if w is None:
            return "Apple Music didn't open."
        if track:
            key = track.lower()[:30]
            item = w.ListItemControl(searchDepth=30, Compare=lambda c, d: c.Name.lower().startswith("track ")
                                     and key in c.Name.lower())
            if item.Exists(8, 0.5):
                item.SetFocus()
                _double_click_keep_cursor(item)
                return None
            return "found the song but couldn't press play"
        return None if _press_page_play(auto, w) else "opened the album but couldn't press play"

    problem = _with_ui(start)
    playing = _wait_playing(expect=track or "", changed_from=before)
    if playing:
        return f"Now playing {label} on Apple Music." if track else \
            f"Now playing {label} on Apple Music, starting with {playing}."
    return f"Opened {label} in Apple Music, but {problem or 'it did not start'}; the user may need to press play."


def _playlist_items(auto, w):
    header = w.ListItemControl(AutomationId="Sidebar_Header_Playlists", searchDepth=30)
    if not header.Exists(3, 0.5):
        return []
    found = []

    def walk(c, depth):  # playlists sit a level or two below the header
        for ch in c.GetChildren():
            if ch.ControlTypeName == "ListItemControl" and "ePlaylist" in (ch.AutomationId or ""):
                found.append(ch)
            elif depth < 3:
                walk(ch, depth + 1)

    walk(header, 0)
    return found


def _plain(text: str) -> str:
    """'🙏 bless ' -> 'bless' so spoken names match playlist titles."""
    return " ".join("".join(ch for ch in text.lower() if ch.isalnum() or ch == " ").split())


def list_playlists() -> str:
    try:
        os.startfile(APP_ID)
    except OSError:
        return "Apple Music isn't installed on this PC."

    def get(auto):
        w = _window(auto)
        return [c.Name for c in _playlist_items(auto, w)] if w else []

    names = [_plain(n) for n in _with_ui(get)]
    return "Playlists: " + ", ".join(names) if names else "I couldn't read the playlists in Apple Music."


def play_playlist(name: str) -> str:
    try:
        os.startfile(APP_ID)
    except OSError:
        return "Apple Music isn't installed on this PC."

    def start(auto):
        w = _window(auto)
        if w is None:
            return "Apple Music didn't open."
        items = _playlist_items(auto, w)
        want = _plain(name).removesuffix(" playlist")
        match = next((c for c in items if _plain(c.Name) == want), None) or \
            next((c for c in items if _plain(c.Name) and (want in _plain(c.Name) or _plain(c.Name) in want)), None)
        if match is None:
            return f"No playlist called {name}. I can see: {', '.join(_plain(c.Name) for c in items)}."
        sel = match.GetSelectionItemPattern() if match.GetPattern(auto.PatternId.SelectionItemPattern) else None
        if sel:
            sel.Select()
        else:
            _double_click_keep_cursor(match)
        time.sleep(1.5)
        if not _press_page_play(auto, w):
            return f"Opened {match.Name} but couldn't press play."
        return f"OK:{_plain(match.Name)}"

    before = _playing_title()
    result = _with_ui(start)
    if not result.startswith("OK:"):
        return result
    title = _wait_playing(changed_from=before)
    return f"Playing your {result[3:]} playlist, starting with {title}." if title else \
        f"Opened your {result[3:]} playlist, but it didn't start; the user may need to press play."


def music_control(action: str) -> str:
    """play, pause, next, previous, play_pause for whatever app is playing."""
    ok = _run(_control(action))
    return f"Done: {action}." if ok else "Nothing is playing to control right now."
