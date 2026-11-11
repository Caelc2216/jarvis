"""Keyword routines like "school" and "games": open apps and put each window in its place.

Edit routines.json to change what a routine opens and where. Or arrange the windows
yourself and say "save this layout as school" — Jarvis asks you to click each window
and remembers exactly where it is.
"""

import difflib
import json
import os
import re
import time
from pathlib import Path

import winlayout as wl

HERE = Path(__file__).parent
CONFIG = HERE / "routines.json"

import paths

APP_TYPES = {
    "chrome": {"exe": "chrome.exe", "cmd": [paths.chrome() or "chrome.exe", "--new-window"], "new_window": True},
    # A desktop/taskbar shortcut. Set "exe" to the program whose window it opens.
    "shortcut": {"exe": "", "cmd": [], "new_window": True},
    "vscode": {"exe": "code.exe", "cmd": [paths.vscode() or "code", "-n"], "new_window": True},
    "discord": {"exe": "discord.exe", "cmd": [paths.discord_updater() or "discord:", "--processStart", "Discord.exe"],
                "title_not": ("Updater",), "timeout": 45},
    # Steam's window belongs to its helper process, not steam.exe itself.
    "steam": {"exe": "steamwebhelper.exe", "cmd": [paths.steam_exe() or "steam://open/main"],
              "title_has": "Steam", "title_not": ("Friends",), "timeout": 45},
}


def default_config() -> dict:
    """A starter routine built from what this PC actually has. People add their own by voice."""
    steps = []
    if paths.discord_updater():
        steps.append({"label": "Discord", "type": "discord", "position": {"monitor": "main", "area": "left"}})
    if paths.steam_exe():
        steps.append({"label": "Steam", "type": "steam", "position": {"monitor": "main", "area": "right"}})
    routines = {}
    if steps:
        routines["games"] = {
            "keywords": ["games", "game", "gaming"],
            "say": "Game time. Opening " + " and ".join(s["label"] for s in steps) + ".",
            "steps": steps,
            "done": "Everything's up. Have fun.",
        }
    return {"ask_on_wake": "What are we doing today?", "routines": routines}


def load() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(default_config(), indent=2), encoding="utf-8")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def match(text: str) -> str | None:
    """Routine name if the user said a routine keyword ("school", "games", "school mode")."""
    words = _words(text)
    if not words or set(words) & {"what", "when", "where", "why", "how", "who", "which", "weather"}:
        return None  # a question that mentions school isn't a request to start school mode
    if set(words) & {"close", "quit", "exit", "kill", "shut", "stop", "minimize", "hide", "end", "done"}:
        return None  # "close school" / "I'm done with games" goes to the brain, which can close windows
    starters = {"start", "starting", "do", "doing", "lets", "let", "work", "working", "time", "mode",
                "begin", "open", "set", "up", "ready", "get"}
    if len(words) > 6 and not (len(words) <= 12 and set(words) & starters):
        return None
    for name, r in load()["routines"].items():
        for kw in [name, *r.get("keywords", [])]:
            if kw.lower() in words:
                return name
    return None


SAVE_RE = re.compile(r"\bsave\s+(?:this|the|my)?\s*(?:window\s+)?layout\s+(?:as|for)?\s*(?:the\s+)?([a-z0-9 ]+?)(?:\s+mode)?[.!]*$", re.I)


def match_save(text: str) -> str | None:
    m = SAVE_RE.search(text.strip())
    if not m:
        return None
    return match(m.group(1)) or m.group(1).strip().lower()


# ------------------------------------------------------------------ run --
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Folders never worth opening as a project.
SKIP_DIRS = {"node_modules", "bin", "obj", "__pycache__", "venv", ".venv", "dist", "build", "target",
             "packages", "properties", "wwwroot", "migrations", "assets", "public", "src", "lib", "models"}
# Words people say around a folder name that aren't part of it.
FILLER = {"in", "my", "the", "a", "an", "of", "for", "on", "to", "and", "is", "it", "its", "this", "that",
          "should", "be", "folder", "directory", "section", "documents", "document", "open",
          "please", "called", "named", "one", "under", "inside", "from", "with", "lets", "let", "s",
          "work", "working", "i", "want", "we", "are", "on", "go", "can", "you", "just", "um", "uh", "like"}
MAX_DEPTH = 5
# Generic folder names: searched through, but never offered as the project itself.
GENERIC = {"data", "app", "apps", "api", "datasets", "images", "img", "static", "components", "pages",
           "controllers", "services", "entities", "dtos", "utilities", "utils", "enums", "layout",
           "scripts", "styles", "config", "docs", "tests", "test", "code", "notes", "reading", "manage",
           "authorization", "server", "client", "frontend", "backend", "resources", "web"}


def _tokens(text: str) -> list[str]:
    """'Fall-2026/algorithmsHomework' -> ['fall', '2026', 'algorithms', 'homework']."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)          # camelCase
    text = re.sub(r"([a-zA-Z])(\d)|(\d)([a-zA-Z])", lambda m: " ".join(filter(None, m.groups())), text)
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t]


def project_folders(roots: list[str]) -> list[Path]:
    """Every plausible project folder under the roots (a few levels deep, junk skipped)."""
    found = []

    def walk(p: Path, depth: int):
        try:
            children = [c for c in p.iterdir() if c.is_dir()]
        except OSError:
            return
        for c in children:
            if c.name.startswith((".", "_")) or c.name.lower() in SKIP_DIRS:
                continue
            if c.name.lower() not in GENERIC:
                found.append(c)
            if depth < MAX_DEPTH:
                walk(c, depth + 1)

    for root in roots:
        if Path(root).is_dir():
            walk(Path(root), 1)
    return found


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a.isdigit() or b.isdigit():
        return 0.0  # numbers must match exactly (2025 vs 2026)
    return difflib.SequenceMatcher(None, a, b).ratio()


def rank_projects(spoken: str, roots: list[str]) -> list[tuple[float, Path]]:
    """Best-matching folders for what the user said, best first."""
    def stem(ts):
        return [t.rstrip("s") if len(t) > 4 else t for t in ts]  # homeworks -> homework

    every = stem(_tokens(spoken))
    words = [w for w in every if w not in FILLER]          # what has to be found in the path
    if not words:
        return []
    # "back end" should also match "backend": add joined neighbours.
    said = set(every) | {a + b for a, b in zip(every, every[1:])}
    ranked = []
    for folder in project_folders(roots):
        rel = [r for r in roots if str(folder).startswith(str(Path(r)))]
        rel_path = folder.relative_to(rel[0]) if rel else folder
        path_tokens = stem(_tokens(str(rel_path)))
        path_set = set(path_tokens) | {a + b for a, b in zip(path_tokens, path_tokens[1:])}
        leaf_tokens = stem(_tokens(folder.name)) or ["?"]

        def hit(w, i):  # was this spoken word found in the path (alone or joined to a neighbour)?
            pairs = [w] + ([every[i - 1] + w] if i else []) + ([w + every[i + 1]] if i + 1 < len(every) else [])
            return max((_sim(p, t) for p in pairs for t in path_set), default=0) >= 0.8

        idx = {w: i for i, w in enumerate(every)}
        coverage = sum(hit(w, idx[w]) for w in words) / len(words)
        leaf = sum(max((_sim(t, s) for s in said), default=0) >= 0.8 for t in leaf_tokens) / len(leaf_tokens)
        if leaf == 0 or coverage < 0.5:
            continue
        depth = len(rel_path.parts)
        score = 0.6 * coverage + 0.4 * leaf - 0.01 * depth
        ranked.append((round(score, 3), folder))
    return sorted(ranked, key=lambda x: -x[0])


def pick_project(spoken: str, roots) -> str | None:
    """Single best folder, or None if nothing matches well."""
    roots = [roots] if isinstance(roots, str) else roots
    ranked = rank_projects(spoken, roots)
    return str(ranked[0][1]) if ranked and ranked[0][0] >= 0.45 else None


def _describe(folder: Path, roots: list[str]) -> str:
    """Speakable name: 'algorithms homework in Fall 2026, Algorithms'."""
    for r in roots:
        if str(folder).startswith(str(Path(r))):
            parts = folder.relative_to(r).parts
            parent = ", ".join(" ".join(_tokens(p)) for p in parts[:-1][-2:])
            name = " ".join(_tokens(folder.name))
            return f"{name}, in {parent}" if parent else name
    return folder.name


def ask_for_project(step: dict, say, listen) -> str | None:
    roots = step.get("projects_folders") or [step.get("projects_folder", str(HERE.parent))]
    aliases = {k.lower(): v for k, v in step.get("aliases", {}).items()}
    say(step.get("ask", "Which project?"))
    for attempt in range(3):
        heard = (listen() or "").strip()
        plain = " ".join(_words(heard))
        if plain in ("skip", "none", "nothing", "no", "never mind", "nevermind", "no project"):
            say("No project then.")
            return None
        if plain in aliases:
            return aliases[plain]
        ranked = rank_projects(heard, roots)
        good = [(s, f) for s, f in ranked if s >= 0.45]
        if good and (len(good) == 1 or good[0][0] - good[1][0] >= 0.08):
            return str(good[0][1])
        if good:  # a few close matches: let the user choose
            options = [f for _, f in good[:3]]
            names = [_describe(f, roots) for f in options]
            say("I found a few. " + " ".join(f"{i}: {n}." for i, n in enumerate(names, 1))
                + " Which number?")
            pick = " ".join(_words(listen() or ""))
            for i, words in enumerate((("1", "one", "first"), ("2", "two", "second", "to", "too"),
                                       ("3", "three", "third")), 0):
                if i < len(options) and any(w in pick.split() for w in words):
                    return str(options[i])
            sub = pick_project(pick, [str(f.parent) for f in options]) if pick else None
            if sub and Path(sub) in options:
                return sub
        say("Sorry, I couldn't find that one. Try the folder's name, or say skip."
            if attempt < 2 else "I still can't find it, so I'll skip VS Code.")
    return None


def _open_step(step: dict, say, listen) -> int | None:
    kind = APP_TYPES.get(step["type"], {})
    exe = step.get("exe", kind.get("exe", ""))
    cmd = list(step.get("cmd", kind.get("cmd", [])))
    title_has = step.get("title_has", kind.get("title_has", ""))

    if step["type"] == "chrome":
        if step.get("url"):  # no url = a plain new Chrome window
            cmd.append(step["url"])
    elif step["type"] == "shortcut":  # any .lnk, e.g. a Chrome app like Canvas
        cmd = [step["path"]]
    elif step["type"] == "app":  # anything Jarvis found installed (Start menu, Steam, Store)
        cmd = [step["target"]]
        if not exe:  # don't know its window yet: just start it ("save this layout" learns the window)
            wl.launch(step["target"])
            import hud

            hud.activity(f"Opened {step.get('label', 'app')}")
            return -1
    elif step["type"] == "vscode":
        folder = None
        if step.get("folder"):
            folder = step["folder"]
        elif step.get("ask"):
            folder = ask_for_project(step, say, listen)
            if not folder:
                return None
            say(f"Opening {' '.join(_tokens(Path(folder).name))}.")
        if folder:
            cmd.append(folder)
            title_has = Path(folder).name

    before = set(wl.windows(exe))
    wl.launch(cmd if len(cmd) > 1 else cmd[0])
    hwnd = wl.wait_for_window(exe, before=() if not kind.get("new_window") and not step.get("new_window") else before,
                              title_has=title_has, title_not=tuple(kind.get("title_not", ())),
                              timeout=step.get("timeout", kind.get("timeout", 25)),
                              allow_existing=True)
    if hwnd:
        wl.place(hwnd, step.get("position"))
        import hud

        where = (step.get("position") or {}).get("monitor", "")
        hud.activity(f"Opened {step.get('label', step['type'])}" + (f" · {where} monitor" if where else ""))
    return hwnd


def run(name: str, say, listen) -> None:
    import hud

    routine = load()["routines"][name]
    hud.set_info(routine=name)
    hud.activity(f"Routine started · {name}")
    if routine.get("say"):
        say(routine["say"])
    missing, opened = [], []
    for step in routine["steps"]:
        try:
            hwnd = _open_step(step, say, listen)
            if hwnd and hwnd > 0:
                opened.append(hwnd)
            elif hwnd == -1:
                pass  # launched; its window isn't known yet
            elif not step.get("ask"):
                missing.append(step.get("label", step["type"]))
        except Exception as e:
            print(f"  (routine step {step.get('label')} failed: {e})")
            missing.append(step.get("label", step["type"]))
    remember_windows(name, opened)
    if missing:
        say(f"Mostly done, but I couldn't find the window for {', '.join(missing)}.")
    elif routine.get("done"):
        say(routine["done"])


# ------------------------------------------------- editing routines by voice --
def _step_for(app_name: str) -> tuple[dict | None, str]:
    """Turn a spoken app name into a routine step, using what Jarvis knows is installed."""
    import apps
    import tools

    key = app_name.lower().strip()
    key = apps.aliases().get(key, key)
    if key in tools.APPS:
        target = tools.APPS[key]
        if target.startswith("http"):
            return {"label": key.title(), "type": "chrome", "url": target, "new_window": True}, key.title()
        return {"label": key.title(), "type": "app", "target": target, "exe": ""}, key.title()
    app, options = apps.find(key)
    if not app:
        hint = f" Did you mean: {'; '.join(o['name'] for o in options)}?" if options else ""
        return None, f"I couldn't find {app_name}.{hint}"
    return {"label": app["name"], "type": "app", "target": app["target"], "exe": app.get("exe", "")}, app["name"]


def create_routine(name: str, apps_to_open: list, keywords: list | None = None) -> str:
    cfg = load()
    name = " ".join(_words(name))
    if not name:
        return "The routine needs a name."
    steps, labels, problems = [], [], []
    for a in apps_to_open:
        step, label = _step_for(str(a))
        if step:
            steps.append(step)
            labels.append(label)
        else:
            problems.append(label)
    if problems:
        return "Not saved. " + " ".join(problems) + " Ask the user, then try again."
    if not steps:
        return "Not saved: there were no apps to open."
    cfg["routines"][name] = {
        "keywords": sorted({name, *[" ".join(_words(k)) for k in (keywords or []) if _words(k)]}),
        "say": f"{name.title()} mode. Opening {', '.join(labels)}.",
        "steps": steps,
        "done": "All set.",
    }
    save(cfg)
    return (f"Saved the {name} routine: {', '.join(labels)}. The user can now just say '{name}'. "
            "To set where each window goes, they can arrange them and say 'save this layout as " + name + "'.")


def add_to_routine(name: str, app_name: str) -> str:
    cfg = load()
    r = cfg["routines"].get(match(name) or name.lower())
    if not r:
        return f"There's no routine called {name}. Routines: {', '.join(cfg['routines'])}."
    step, label = _step_for(app_name)
    if not step:
        return label
    r["steps"].append(step)
    save(cfg)
    return f"Added {label} to the {name} routine."


def remove_from_routine(name: str, app_name: str) -> str:
    cfg = load()
    key = match(name) or name.lower()
    r = cfg["routines"].get(key)
    if not r:
        return f"There's no routine called {name}."
    want = _norm(app_name)
    keep = [s for s in r["steps"] if want not in _norm(s.get("label", "")) and want not in _norm(s.get("type", ""))]
    if len(keep) == len(r["steps"]):
        return f"The {key} routine doesn't open {app_name}. It opens: {', '.join(s.get('label', s['type']) for s in r['steps'])}."
    r["steps"] = keep
    save(cfg)
    return f"Removed {app_name} from the {key} routine."


def delete_routine(name: str) -> str:
    cfg = load()
    key = match(name) or name.lower()
    if key not in cfg["routines"]:
        return f"There's no routine called {name}."
    del cfg["routines"][key]
    save(cfg)
    return f"Deleted the {key} routine."


def list_routines() -> str:
    cfg = load()
    return "Routines: " + "; ".join(
        f"{n} (opens {', '.join(s.get('label', s['type']) for s in r['steps'])}; say: {', '.join(r.get('keywords', [n]))})"
        for n, r in cfg["routines"].items())


OPENED_FILE = HERE / "routine_windows.json"


def remember_windows(name: str, hwnds: list[int]) -> None:
    """Which windows a routine opened, so "close school" closes exactly those."""
    try:
        data = json.loads(OPENED_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[name] = hwnds
    OPENED_FILE.write_text(json.dumps(data), encoding="utf-8")


def opened_windows(name: str) -> list[int]:
    try:
        hwnds = json.loads(OPENED_FILE.read_text(encoding="utf-8")).get(name, [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [h for h in hwnds if wl.user32.IsWindow(h)]


def save_layout(name: str, say, timeout_each: float = 25.0) -> None:
    cfg = load()
    if name not in cfg["routines"]:
        say(f"I don't have a routine called {name}. I know {', '.join(cfg['routines'])}.")
        return
    steps = cfg["routines"][name]["steps"]
    say(f"Saving the {name} layout. Arrange your windows first, then click each one when I ask.")
    taken = set()
    for step in steps:
        label = step.get("label", step["type"])
        exe = step.get("exe", APP_TYPES.get(step["type"], {}).get("exe", ""))
        say(f"Click on the {label} window.")
        start_fg = wl.foreground()
        end = time.time() + timeout_each
        hwnd = None
        while time.time() < end:
            fg = wl.foreground()
            if fg and fg != start_fg and fg not in taken and (not exe or wl._exe(fg) == exe):
                hwnd = fg
                break
            time.sleep(0.3)
        if not hwnd:
            say(f"I didn't see the {label} window get clicked, so I'll keep its old spot.")
            continue
        taken.add(hwnd)
        step["position"] = wl.capture(hwnd)
        if not step.get("exe") and step["type"] in ("app", "shortcut"):
            step["exe"] = wl._exe(hwnd)  # now Jarvis knows this app's window for next time
        print(f"  saved {label}: {step['position']}  [{wl.describe(hwnd)}]")
    save(cfg)
    say(f"Got it. The {name} layout is saved.")
