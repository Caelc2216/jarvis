"""Jarvis learns what's installed, so "open <anything>" works without editing code.

Sources: every Start-menu app (desktop programs, Store apps, Steam games, Chrome apps),
desktop shortcuts, and Steam libraries. Saved to apps.json and refreshed in the background.
Nicknames the user teaches ("the game" = Rocket League) are saved in aliases.json.
"""

import difflib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
INDEX = HERE / "apps.json"
ALIASES = HERE / "aliases.json"
MAX_AGE_S = 6 * 3600
_lock = threading.Lock()

JUNK = re.compile(r"\b(uninstall|uninstaller|readme|read me|release notes|documentation|manual|help|"
                  r"about|license|website|support|verifier|redistributable|changelog|feedback|repair)\b", re.I)


# --------------------------------------------------------------- scanning --
def _start_menu() -> list[dict]:
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         encoding="utf-8", creationflags=0x08000000, timeout=60)
    items = json.loads(out.stdout or "[]")
    items = [items] if isinstance(items, dict) else items
    apps = []
    for it in items:
        name, app_id = (it.get("Name") or "").strip(), (it.get("AppID") or "").strip()
        if not name or not app_id or JUNK.search(name):
            continue
        if app_id.startswith(("steam://", "http://", "https://")):
            target = app_id
        else:
            target = "shell:AppsFolder\\" + app_id
        exe = os.path.basename(app_id).lower() if app_id.lower().endswith(".exe") else ""
        apps.append({"name": name, "target": target, "exe": exe, "source": "start menu"})
    return apps


def _desktop() -> list[dict]:
    apps = []
    for d in (Path.home() / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
              Path.home() / "OneDrive" / "Desktop"):
        if d.is_dir():
            for f in d.iterdir():
                if f.suffix.lower() in (".lnk", ".url") and not JUNK.search(f.stem):
                    apps.append({"name": f.stem, "target": str(f), "exe": "", "source": "desktop"})
    return apps


def _steam() -> list[dict]:
    import paths

    if not paths.steam_root():
        return []
    roots = [Path(paths.steam_root())]
    vdf = roots[0] / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        roots += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))]
    apps, seen = [], set()
    for root in roots:
        for m in (root / "steamapps").glob("appmanifest_*.acf"):
            text = m.read_text(encoding="utf-8", errors="ignore")
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and appid.group(1) not in seen and not JUNK.search(name.group(1)) \
                    and "steamworks" not in name.group(1).lower() and "proton" not in name.group(1).lower():
                seen.add(appid.group(1))
                apps.append({"name": name.group(1), "target": f"steam://rungameid/{appid.group(1)}",
                             "exe": "", "source": "steam"})
    return apps


def _clean(name: str) -> str:
    """'Battlefield™ 6' -> 'Battlefield 6', 'Overwatch® 2' -> 'Overwatch 2'."""
    name = re.sub(r"[™®©]", "", name)
    return re.sub(r"\s+", " ", name).strip(" :")


def scan() -> list[dict]:
    found, seen = [], set()
    for source in (_start_menu, _desktop, _steam):
        try:
            for a in source():
                a["name"] = _clean(a["name"])
                key = _norm(a["name"])
                if key and key not in seen:
                    seen.add(key)
                    found.append(a)
        except Exception as e:
            print(f"  (app scan: {source.__name__} failed: {e})")
    with _lock:
        INDEX.write_text(json.dumps({"scanned": time.time(), "apps": found}, indent=1), encoding="utf-8")
    return found


def index(refresh_if_old=True) -> list[dict]:
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return scan()
    if refresh_if_old and time.time() - data.get("scanned", 0) > MAX_AGE_S:
        threading.Thread(target=scan, daemon=True).start()  # refresh quietly, use the old list for now
    return data.get("apps", [])


# --------------------------------------------------------------- matching --
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def rank(query: str, apps: list[dict]) -> list[tuple[float, dict]]:
    q, qw = _norm(query), _words(query)
    if not q:
        return []
    out = []
    for a in apps:
        n, nw = _norm(a["name"]), _words(a["name"])
        if q == n:
            s = 1.0
        else:
            s = difflib.SequenceMatcher(None, q, n).ratio()
            if all(any(w == x or (len(w) > 3 and x.startswith(w)) for x in nw) for w in qw):
                s = max(s, 0.85 - 0.02 * max(0, len(nw) - len(qw)))  # every word said is in the name
            elif n.startswith(q) and len(q) >= 4:
                s = max(s, 0.8)
        out.append((round(s, 3), a))
    return sorted(out, key=lambda x: -x[0])


def aliases() -> dict:
    try:
        return json.loads(ALIASES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Everyday names for apps whose real names people rarely say.
COMMON = {"vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "code": "Visual Studio Code",
          "chrome": "Google Chrome", "google": "Google Chrome", "browser": "Google Chrome",
          "edge": "Microsoft Edge", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint",
          "file explorer": "File Explorer", "explorer": "File Explorer", "files": "File Explorer",
          "calc": "Calculator", "terminal": "Terminal", "task manager": "Task Manager",
          "control panel": "Control Panel", "paint": "Paint", "snipping tool": "Snipping Tool",
          "cod": "Call of Duty", "rdr2": "Red Dead Redemption 2", "gta": "Grand Theft Auto"}


def find(query: str) -> tuple[dict | None, list[dict]]:
    """(best app or None, close alternatives) for what the user said."""
    q = query.lower().strip().removeprefix("the ").removeprefix("my ").strip()
    al = aliases()
    if q in al:
        query = al[q]
    elif q in COMMON:
        exact = [a for a in index() if a["name"].lower() == COMMON[q].lower()]
        if exact:
            return exact[0], []
        query = COMMON[q]
    for attempt in range(2):
        ranked = rank(query, index())
        good = [(s, a) for s, a in ranked if s >= 0.72]
        if good and (len(good) == 1 or good[0][0] >= 0.99 or good[0][0] - good[1][0] >= 0.1):
            return good[0][1], []
        if good:
            return None, [a for _, a in good[:4]]
        if attempt == 0:
            scan()  # maybe it was just installed
    # Only suggest near-misses that share a real word with what was said; otherwise just ask.
    said = {w for w in _words(query) if len(w) > 2}
    return None, [a for s, a in ranked[:3] if s >= 0.55 and said & set(_words(a["name"]))]


def launch(app: dict) -> None:
    os.startfile(app["target"])


# ------------------------------------------------------------------ tools --
def open_installed(name: str) -> str:
    app, options = find(name)
    if app:
        launch(app)
        return f"Opened {app['name']}."
    if options:
        return ("Not sure which one. Ask the user which they meant: " + "; ".join(o["name"] for o in options)
                + ". Once they answer, open it and save the nickname with learn_nickname.")
    return (f"I couldn't find an app called {name}. Ask the user what the app is called (as it appears in the "
            "Start menu), then try again and save the nickname with learn_nickname.")


def learn_nickname(nickname: str, app_name: str) -> str:
    app, options = find(app_name)
    if not app:
        return f"I couldn't find '{app_name}' to link it to." + (
            f" Did they mean: {'; '.join(o['name'] for o in options)}?" if options else "")
    al = aliases()
    al[nickname.lower().strip().removeprefix("the ").removeprefix("my ").strip()] = app["name"]
    ALIASES.write_text(json.dumps(al, indent=2), encoding="utf-8")
    return f"Got it: '{nickname}' now means {app['name']}."


def forget_nickname(nickname: str) -> str:
    al = aliases()
    key = nickname.lower().strip().removeprefix("the ").removeprefix("my ").strip()
    if al.pop(key, None) is None:
        return f"I didn't have a nickname '{nickname}'."
    ALIASES.write_text(json.dumps(al, indent=2), encoding="utf-8")
    return f"Forgot '{nickname}'."


def search_apps(query: str) -> str:
    ranked = [a["name"] for s, a in rank(query, index()) if s >= 0.5][:8]
    return "Matching apps: " + "; ".join(ranked) if ranked else f"No installed apps match '{query}'."


def rescan_apps() -> str:
    found = scan()
    games = sum(a["target"].startswith("steam://") for a in found)
    return f"Found {len(found)} apps, including {games} Steam games."
