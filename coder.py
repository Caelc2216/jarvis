"""Code mode: Jarvis hands your coding requests to Claude Code, working in one project folder.

    "Hey Jarvis, let's code on my algorithms project"   -> opens it in VS Code, code mode on
    "add input validation to the parser and run the tests" -> Claude Code does it
    "undo that"            -> puts the files back the way they were before the last request
    "open it in the terminal" -> the same Claude Code session, in a terminal you can type in
    "stop coding"          -> back to normal Jarvis

How it stays safe:
  - Claude Code only works inside the chosen project folder.
  - It can read, search and edit files there freely. Before it runs any command that isn't on a
    short safe list (tests, git status...), or touches a file outside the project, Jarvis asks you
    (out loud, or Allow / Don't allow buttons on the display). coder_hook.py does the asking.
  - Before every request Jarvis takes a private snapshot of the project (its own git store in
    coder_snapshots/, never touching the project's own git), so "undo that" always works.
  - It uses your Claude plan through the `claude` command, like the Claude brain (no API key).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent.resolve()
STATE = HERE / "coder_state.json"
SNAPSHOTS = HERE / "coder_snapshots"
NO_WINDOW = 0x08000000
MAX_TASK_S = int(os.getenv("CODER_MAX_MINUTES", "30")) * 60

# Claude Code may use these without asking (all stay inside the project; edits are auto-accepted there).
ALLOWED_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "TodoWrite",
                 "WebSearch", "WebFetch", "Task", "Agent", "ToolSearch"]

VOICE_NOTE = ("The user is talking to you by voice (or typing) through their assistant, Jarvis, and is "
              "watching their editor rather than this output. Do the work in this project folder. When you "
              "finish, end with a short plain-language summary they'll hear read aloud: two or three "
              "sentences, no markdown, no code blocks, no file paths unless essential. Say what you changed, "
              "whether tests passed, and anything they need to do next. If something needs their decision, "
              "ask one clear question at the end.")

_proc: subprocess.Popen | None = None
_cancelled = False
_lock = threading.Lock()


# --------------------------------------------------------- Claude Code or Codex --
def backend() -> str:
    """'claude' (Claude Code) or 'codex' (OpenAI Codex). CODER=claude|codex in .env decides; otherwise
    it follows the brain chosen in setup (ChatGPT/Codex brain -> Codex), if that tool is installed."""
    choice = os.getenv("CODER", "").strip().lower()
    if choice in ("claude", "codex"):
        return choice
    prefer = "codex" if os.getenv("BRAIN", "").strip().lower() in ("codex", "chatgpt", "openai") else "claude"
    other = "claude" if prefer == "codex" else "codex"
    return other if not shutil.which(prefer) and shutil.which(other) else prefer


def label() -> str:
    return "Codex" if backend() == "codex" else "Claude Code"


_sandbox_checked: tuple[bool, str] | None = None


def _codex_sandbox_settings() -> list[str]:
    """Jarvis ignores your personal Codex config (so its add-on tools can't get around the sandbox), but
    keeps the one setting that says which Windows sandbox Codex set up on this PC."""
    try:
        import tomllib

        cfg = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
        mode = (cfg.get("windows") or {}).get("sandbox")
    except Exception:
        mode = None
    return ["-c", f'windows.sandbox="{mode}"'] if isinstance(mode, str) and mode.isalnum() else []


def codex_sandbox_ok() -> tuple[bool, str]:
    """Codex is only ever run inside its sandbox. Check the sandbox actually works on this PC first."""
    global _sandbox_checked
    if _sandbox_checked and _sandbox_checked[0]:
        return _sandbox_checked
    exe = shutil.which("codex")
    try:
        r = subprocess.run([exe, "sandbox", *_codex_sandbox_settings(), "cmd", "/c", "echo", "jarvis-sandbox-ok"],
                           capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=90, creationflags=NO_WINDOW)
        ok = "jarvis-sandbox-ok" in r.stdout
        detail = (r.stderr or r.stdout).strip()
    except Exception as e:
        ok, detail = False, str(e)
    if ok:
        _sandbox_checked = (True, "")
    else:
        print(f"  (Codex sandbox check failed: {detail[-400:]})")
        why = ("Codex's safety sandbox isn't working on this PC, so I won't let it run code. "
               + ("Its install folder is so deep that Windows can't open its files. Turning on Windows long "
                  "paths, or reinstalling Codex somewhere shorter, fixes it. " if "cannot find the path" in detail
                  else "The details are in jarvis.log. ")
               + "Claude Code works without it, if you have it: set CODER=claude in the .env file.")
        _sandbox_checked = (False, why)
    return _sandbox_checked


# ------------------------------------------------------------------- state --
def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def active() -> str:
    """The project folder code mode is on, or "" if code mode is off."""
    project = _load().get("project", "")
    return project if project and Path(project).is_dir() else ""


def project_name(path: str | None = None) -> str:
    path = path or active()
    return re.sub(r"[-_]+", " ", Path(path).name) if path else ""


def _roots() -> list[str]:
    """Where your projects live: CODE_FOLDERS in .env, else the folders your VS Code routine searches."""
    env = [p.strip() for p in os.getenv("CODE_FOLDERS", "").split(";") if p.strip()]
    if env:
        return env
    try:
        import routines

        for routine in routines.load().get("routines", {}).values():
            for step in routine.get("steps", []):
                if step.get("type") == "vscode" and step.get("projects_folders"):
                    return [str(Path(p).expanduser()) for p in step["projects_folders"]]
    except Exception:
        pass
    home = Path.home() / "Documents"
    return [str(p) for p in (home / "Code", home / "Projects", home) if p.is_dir()][:2]


# ---------------------------------------------------------- start / stop --
START_RE = re.compile(
    r"^(?:hey jarvis[, ]*)?(?:(?:let'?s|let us|can we|can you|i want to|i'd like to|help me|time to|go ahead and|"
    r"please)\s+)*(?:start\s+)?(?:code|coding|program|programming)"
    r"(?:\s+mode)?(?:\s+(?:on|in|for|with)\s+(?:the\s+|my\s+)?(.+?))?(?:\s+project)?[.!?]*$", re.I)
CLAUDE_RE = re.compile(r"^(?:open|start|use)\s+(?:claude code|claude)\s+(?:on|in|for)\s+(?:the\s+|my\s+)?(.+?)"
                       r"(?:\s+project)?[.!?]*$", re.I)
STOP_RE = re.compile(r"^(?:ok(?:ay)?[, ]*)?(?:stop|exit|leave|end|quit|done|finish(?:ed)?)\s+(?:with\s+)?"
                     r"(?:the\s+)?(?:coding|code mode|code)\b|^(?:i'?m\s+)?done coding\b|^(?:that'?s all|we'?re done)"
                     r"(?:\s+for now)?[.!]*$", re.I)
UNDO_RE = re.compile(r"^(?:(?:okay|ok|please|(?:can|could|would) you|go ahead and|just)[,\s]+)*"
                     r"(?:undo|revert|roll back|rollback)\b(?:\s+(?:that|it|this|(?:the|that|your)\s+last\s+(?:change|"
                     r"changes|one|edit|request)|(?:that|those|the|your)\s+changes?|what you (?:just )?did))?"
                     r"(?:\s+please)?[.!?]*$", re.I)

# ------------------------------------------- who is this for: Jarvis or Claude Code? --
# In code mode most things go to Claude Code. These go to Jarvis instead:
#   - anything that starts with "Jarvis" ("okay Jarvis, play some music")
#   - switching / stopping projects, undo, the live view (handled by code mode itself)
#   - Jarvis's everyday jobs: music, timers, weather, time, lights, opening/closing apps...
# and anything starting with "Claude" always goes to Claude Code.
ADDRESS_JARVIS = re.compile(r"^\s*(?:(?:hey|okay|ok|hi|yo|so|um|uh)[,\s]+)?jarvis\b[\s,.:;!?-]*", re.I)
ADDRESS_CLAUDE = re.compile(r"^\s*(?:(?:hey|okay|ok)[,\s]+)?(?:(?:tell|ask)\s+)?(?:claude(?:\s+code)?|codex)\b"
                            r"(?:\s+to\b)?[\s,.:;!?-]*", re.I)
# Leaving and re-entering code mode.
BACK_TO_JARVIS = re.compile(r"^(?:(?:okay|ok|alright)[,\s]+)?(?:(?:go\s+)?back to jarvis|(?:jarvis[,\s]+)?(?:you )?take "
                            r"(?:it )?over(?: jarvis)?|exit (?:claude(?: code)?|codex)|leave (?:claude(?: code)?|codex)|"
                            r"(?:that'?s )?enough coding)[.!]*$", re.I)
BACK_TO_CODE = re.compile(r"^(?:(?:okay|ok|alright)[,\s]+)?(?:(?:go\s+)?back to|return to|resume|continue|keep)\s+"
                          r"(?:with\s+)?(?:the\s+|my\s+)?(?:claude(?: code)?|codex|coding|code mode|the code|programming)"
                          r"(?:\s+session)?[.!]*$", re.I)
JARVIS_TOPICS = re.compile(
    r"^(?:(?:can|could|would) you\s+|please\s+)?(?:play|pause|resume|unpause|skip|shuffle)\b"
    r"|^(?:(?:can|could|would) you\s+|please\s+)?(?:play|put on)\b.*\b(?:music|song|songs|album|playlist|track|by)\b"
    r"|\bnext (?:song|track)\b"
    r"|\b(?:what'?s|what is) (?:playing|this song)\b|\bvolume\b|\b(?:mute|unmute)\b"
    r"|\b(?:set|start|cancel)\s+(?:a\s+|the\s+|my\s+)?timer\b|\btimer for\b|\bremind me\b|\balarm\b"
    r"|\bweather\b|\bforecast\b|\bwhat(?:'s| is)? the time\b|\bwhat time is it\b|\bwhat(?:'s| is) (?:the )?date\b"
    r"|\b(?:turn|switch) (?:on|off) (?:the |my )?lights?\b|\blights? (?:on|off)\b|\bdim the lights\b"
    r"|\b(?:text|voice) mode\b|\bcheck for (?:an )?updates?\b|\bupdate yourself\b|\bhow are you performing\b"
    r"|\bwhat windows\b|\btake a screenshot\b", re.I)
APP_ACTION = re.compile(r"^(?:(?:can|could|would) you\s+|please\s+)?(open|launch|start|close|quit|minimi[sz]e|"
                        r"maximi[sz]e|focus|switch to)\s+(?:up\s+)?(?:the\s+|my\s+)?(.+?)(?:\s+(?:app|window|for me))?"
                        r"[.!?]*$", re.I)
PROJECT_WORD = re.compile(r"\b(?:folder|project|directory|repo|repository|codebase)\b", re.I)
PREPOSITION = re.compile(r"\b(?:in|on|to|for|with|into)\s+(?:my\s+|the\s+|a\s+|our\s+)?", re.I)


def _project_phrase(text: str) -> str | None:
    """'...terminal in the algorithms homework folder' -> 'algorithms homework' (the words between the
    last in/on/to/for before 'folder|project' and that word)."""
    m = PROJECT_WORD.search(text)
    if not m:
        return None
    before = text[:m.start()]
    preps = list(PREPOSITION.finditer(before))
    if not preps:
        return None
    name = before[preps[-1].end():].strip(" ,.")
    return name or None
SWITCH_VERB = re.compile(r"\b(?:switch|change|jump|go (?:over )?to|new session|fresh session|start|open|continue|"
                         r"work(?:ing)? on|code|coding|session)\b", re.I)


def addressed(text: str) -> tuple[str | None, str]:
    """('jarvis' | 'claude' | None, the text without the name)."""
    m = ADDRESS_JARVIS.match(text)
    if m and text[m.end():].strip():
        return "jarvis", text[m.end():].strip()
    m = ADDRESS_CLAUDE.match(text)
    if m and text[m.end():].strip():
        return "claude", text[m.end():].strip()
    return None, text


def for_jarvis(text: str) -> bool:
    """Is this one of Jarvis's everyday jobs rather than a coding request?"""
    if JARVIS_TOPICS.search(text):
        return True
    m = APP_ACTION.match(text.strip())
    if m:
        name = m.group(2).strip()
        if re.search(r"\.\w{1,5}$|\b(?:file|function|class|method|test|tests|module|component|page|readme|"
                     r"terminal|folder|directory|branch|pr|pull request|issue)\b", name, re.I):
            return False  # "open the readme", "close the file": that's coding
        try:
            import apps

            app, _ = apps.find(name)
            return app is not None
        except Exception:
            return False
    return False


def switch_target(text: str) -> tuple[str | None, bool]:
    """'switch gears to a new Claude code session in my Jarvis folder' -> (that folder, new session?)."""
    name = _project_phrase(text)
    if not name or not SWITCH_VERB.search(text):
        return None, False
    folder, _ = find_project(name)
    current = active()
    if folder and current and Path(folder) != Path(current) and Path(folder).is_relative_to(current):
        return None, False  # a folder inside this project: that's a coding request, not a switch
    return folder, bool(re.search(r"\b(?:new|fresh)\b", text, re.I))
# "let me type in it myself": the same Claude Code conversation, in a terminal you can type in.
TAKEOVER_RE = re.compile(r"\b(?:take over|let me (?:type|take over|drive|use it)|type (?:in|into) it|(?:continue|"
                         r"carry on|finish) (?:it )?(?:myself|on my own)|interactive)\b", re.I)
# "show me what it's doing": the live view of what Claude Code is doing.
SHOW_RE = re.compile(r"\b(?:see|show|display|watch|view|pull up|bring up|open|look at)\b.*"
                     r"\b(?:terminal|cli|console|command line|live view|claude code window|progress|"
                     r"what (?:it'?s|you'?re|claude(?: code)?(?:'s| is)) doing)\b", re.I)
TERMINAL_RE = SHOW_RE  # older name


def _heard(text: str) -> str:
    """Speech-to-text often hears "Claude" as "cloud" / "clawed"."""
    return re.sub(r"\b(?:cloud|clawed|clod|claud)(?=\s+code\b)", "Claude", text or "", flags=re.I)


def match_start(text: str) -> str | None:
    """'let's code on my algorithms project' -> 'algorithms'. '' means "code mode, but which project?"."""
    text = _heard(text).strip()
    m = START_RE.match(text) or CLAUDE_RE.match(text)
    if not m:
        return None
    return (m.group(1) or "").strip(" .!?")


def find_project(query: str) -> tuple[str | None, list[str]]:
    """(the folder, or None and up to 3 close matches to choose from)."""
    if query and Path(query).expanduser().is_dir():
        return str(Path(query).expanduser().resolve()), []
    import routines

    ranked = [(s, f) for s, f in routines.rank_projects(query, _roots()) if s >= 0.45]
    if ranked and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08):
        return str(ranked[0][1]), []
    return None, [str(f) for _, f in ranked[:3]]


def start(folder: str, fresh: bool = False) -> str:
    """Turn code mode on for this folder and open it in VS Code. fresh: start a new Claude Code
    conversation instead of continuing the last one there."""
    folder = str(Path(folder).resolve())
    state = _load()
    sessions = state.get("sessions", {})
    # Pick up where you left off in this project if it was recent (within 12 hours).
    prev = {} if fresh else sessions.get(folder, {})
    if time.time() - prev.get("t", 0) > 12 * 3600:
        prev = {}
    state.update(project=folder, session_id=prev.get("id"), snapshots=state.get("snapshots", {}))
    _save(state)
    _open_vscode(folder)
    _live(f"\n=== Code mode: {folder} ===")
    shown = ""
    if os.getenv("CODER_LIVE_WINDOW", "on").lower() not in ("off", "0", "false", "no"):
        show_live()
        shown = " The live view shows what Claude Code is doing."
    _activity(f"Code mode · {project_name(folder)}")
    again = " We'll pick up where we left off." if prev else ""
    shown = shown.replace("Claude Code", label())
    return (f"Code mode on for {project_name(folder)} with {label()}, open in VS Code.{again}{shown} Tell me "
            "what you'd like built or fixed. Say undo that to reverse a change, or back to Jarvis when you're done.")


def stop() -> str:
    state = _load()
    name = project_name(state.get("project", ""))
    if state.get("project"):
        state["last_project"] = state["project"]
    state["project"] = ""
    _save(state)
    _activity("Code mode off")
    return (f"Done coding on {name}. Back to normal; say back to {label().split()[0]} to pick it up again."
            if name else "Code mode was already off.")


def last_project() -> str:
    """The project code mode was last on (to go straight back to it)."""
    p = _load().get("last_project", "")
    return p if p and Path(p).is_dir() else ""


def resume() -> str:
    """Back into code mode on the last project, continuing the same conversation."""
    folder = last_project()
    if not folder:
        return ""
    state = _load()
    state.update(project=folder, session_id=state.get("sessions", {}).get(folder, {}).get("id"))
    _save(state)
    _activity(f"Code mode · {project_name(folder)}")
    return f"Back to {label()} on {project_name(folder)}."


def _open_vscode(folder: str) -> None:
    try:
        import paths
        import winlayout as wl

        wl.launch([paths.vscode() or "code", folder])
    except Exception as e:
        print(f"  (couldn't open VS Code: {e})")


LIVE = HERE / "coder_live.log"
LIVE_TITLE = "Claude Code - live (Jarvis)"


def _live(text: str) -> None:
    """Add to the live view (a terminal window that follows this file)."""
    try:
        if LIVE.exists() and LIVE.stat().st_size > 2_000_000:
            LIVE.write_text(LIVE.read_text(encoding="utf-8", errors="replace")[-200_000:], encoding="utf-8")
        with open(LIVE, "a", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n")
    except OSError:
        pass


def _bring_up(title: str) -> bool:
    """Bring an already-open window with this title to the front."""
    try:
        import winlayout as wl

        for hwnd in wl.windows(title_has=title):
            wl.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            wl.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def show_live() -> str:
    """Open (or bring up) the live view: a terminal that shows what Claude Code is doing as it happens."""
    if _bring_up(LIVE_TITLE):
        return "Here's the live view of what Claude Code is doing."
    LIVE.touch(exist_ok=True)
    follow = (f"Get-Content -LiteralPath '{LIVE}' -Wait -Tail 80 -Encoding UTF8")  # no ';' (wt would split it)
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-w", "new", "--title", LIVE_TITLE, "--suppressApplicationTitle",
                          "powershell", "-NoExit", "-NoProfile", "-Command", follow], creationflags=NO_WINDOW)
    else:
        subprocess.Popen(["powershell", "-NoExit", "-NoProfile", "-Command",
                          f"$host.UI.RawUI.WindowTitle = '{LIVE_TITLE}'; {follow}"], creationflags=0x00000010)
    return "Opened the live view of what Claude Code is doing."


def _terminal(folder: str, args: list, title: str) -> None:
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-w", "new", "--title", title, "-d", folder, *args], creationflags=NO_WINDOW)
    else:
        subprocess.Popen(["cmd", "/k", subprocess.list2cmdline(args)], cwd=folder, creationflags=0x00000010)


def open_terminal() -> str:
    """Take over: the same conversation in a terminal you can type in (interactive Claude Code / Codex)."""
    folder, state = active(), _load()
    if not folder:
        return "Code mode isn't on."
    name = label()
    if busy():
        return f"{name} is still working on your last request. Try again when it's finished."
    sess = state.get("sessions", {}).get(folder, {})
    sid = state.get("session_id") if sess.get("backend", "claude") == backend() else None
    exe = shutil.which(backend()) or backend()
    if backend() == "codex":
        args = [exe, "resume", sid] if sid else [exe]
    else:
        args = [exe] + (["--resume", sid] if sid else [])
    _terminal(folder, args, f"{name} - {project_name(folder)}")
    return (f"Opened {name} in a terminal" + (", with our conversation so far." if sid else ".")
            + " Type there yourself. If you ask me for more, finish there first so you don't both edit at once.")


def open_claude_terminal(project: str, tool: str = "") -> str:
    """Tool: a plain interactive Claude Code or Codex session in a project folder (no code mode)."""
    tool = (tool or "").strip().lower()
    which = "codex" if tool in ("codex", "chatgpt", "openai") else "claude" if tool.startswith("claude") else backend()
    name = "Codex" if which == "codex" else "Claude Code"
    exe = shutil.which(which)
    if not exe:
        return f"{name} isn't installed on this PC (I couldn't find the {which} command)."
    folder, options = find_project(project or "")
    if not folder:
        return ("Several projects match: " + "; ".join(options) + ". Ask which.") if options else \
            "I couldn't find that project. Ask the user for the folder's name."
    _terminal(folder, [exe], f"{name} - {project_name(folder)}")
    return f"Opened {name} in a terminal in {project_name(folder)}."


# ---------------------------------------------------------------- snapshots --
def _git_dir(folder: str) -> Path:
    return SNAPSHOTS / hashlib.sha1(folder.lower().encode()).hexdigest()[:12]


def _git(folder: str, *args, timeout: float = 120) -> subprocess.CompletedProcess:
    gd = _git_dir(folder)
    return subprocess.run(["git", f"--git-dir={gd}", f"--work-tree={folder}", "-c", "core.autocrlf=false",
                           "-c", "core.safecrlf=false", "-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost",
                           *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW, timeout=timeout)


def snapshot(folder: str, label: str) -> str | None:
    """Save the project's files (privately) and return the snapshot id, or None if that's not possible."""
    if not shutil.which("git"):
        return None
    gd = _git_dir(folder)
    try:
        if not (gd / "HEAD").exists():  # new, or a half-deleted leftover: (re)create the store
            gd.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", str(gd)], capture_output=True, creationflags=NO_WINDOW)
            _git(folder, "config", "core.bare", "false")
            (gd / "info").mkdir(exist_ok=True)
            (gd / "info" / "exclude").write_text(
                "node_modules/\n.venv/\nvenv/\nenv/\n__pycache__/\n*.pyc\ndist/\nbuild/\n.next/\ntarget/\nbin/\nobj/\n"
                ".gradle/\n.idea/\n*.log\n", encoding="utf-8")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-q", "--allow-empty", "--no-verify", "-m", label[:200])
        r = _git(folder, "rev-parse", "HEAD")
        return r.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  (couldn't snapshot the project: {e})")
        return None


def changed_files(folder: str, before: str, after: str) -> list[str]:
    r = _git(folder, "diff", "--name-only", before, after)
    return [f for f in r.stdout.splitlines() if f.strip()]


def undo() -> str:
    folder = active()
    if not folder:
        return "Code mode isn't on, so there's nothing to undo."
    state = _load()
    history = state.get("snapshots", {}).get(folder, [])
    if not history:
        return "There's nothing of mine to undo here."
    last = history.pop()
    now = snapshot(folder, "before undo")
    if not now:
        return "I couldn't undo that, sorry: Git isn't available."
    files = changed_files(folder, last["before"], now)
    _git(folder, "read-tree", "-u", "--reset", last["before"])  # files back as they were; new ones removed
    _git(folder, "commit", "-q", "--allow-empty", "--no-verify", "-m", f"undo: {last.get('task', '')[:150]}")
    _save(state)
    _activity(f"Undid: {last.get('task', '')[:40]}")
    if not files:
        return "That request didn't change any files, so there was nothing to undo."
    names = ", ".join(Path(f).name for f in files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
    return f"Undone. I put back {len(files)} file{'s' if len(files) != 1 else ''}: {names}."


# -------------------------------------------------------------------- run --
def _activity(text: str) -> None:
    try:
        import hud

        hud.activity(text)
    except Exception:
        pass


def _describe(name: str, inp: dict) -> str | None:
    """A short, speakable line for what Claude Code is doing."""
    f = Path(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "").name
    if name == "Read":
        return f"Reading {f}" if f else "Reading"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {f}"
    if name == "Write":
        return f"Writing {f}"
    if name == "Bash":
        return "Running " + (inp.get("description") or inp.get("command", ""))[:50]
    if name in ("Glob", "Grep", "LS"):
        return "Searching the project"
    if name in ("WebSearch", "WebFetch"):
        return "Looking it up online"
    if name == "TodoWrite":
        return "Planning the steps"
    if name in ("Task", "Agent"):
        return "Starting a helper"
    return None


def _live_line(name: str, inp: dict, folder: str) -> str:
    """One line for the live view."""
    def rel(p):
        try:
            return str(Path(p).resolve().relative_to(folder))
        except (ValueError, OSError):
            return str(p)

    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    if name in ("Bash", "PowerShell"):
        return f"  $ {inp.get('command', '')}" + (f"    # {inp['description']}" if inp.get("description") else "")
    if name in ("Edit", "MultiEdit"):
        return f"  ~ Editing {rel(path)}"
    if name == "Write":
        return f"  + Writing {rel(path)}"
    if name == "Read":
        return f"  . Reading {rel(path)}"
    if name in ("Glob", "Grep"):
        return f"  ? Searching: {inp.get('pattern', '')}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return "  Plan:\n" + "\n".join(f"    [{'x' if t.get('status') == 'completed' else ' '}] {t.get('content', '')}"
                                       for t in todos)
    if name in ("WebSearch", "WebFetch"):
        return f"  @ {name}: {inp.get('query') or inp.get('url', '')}"
    return f"  * {name}"


def _hook_settings() -> str:
    py = sys.executable
    if Path(py).stem.lower() == "pythonw":  # hooks talk over stdin/stdout: needs the console python
        alt = Path(py).with_name("python.exe")
        py = str(alt) if alt.exists() else py
    command = f'"{py}" "{HERE / "coder_hook.py"}"'
    return json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": command, "timeout": 330}]}]}})


def ask(task: str, on_progress=None) -> str:
    """Give the coding assistant one request in the current project. Blocks until it's done; returns
    its summary (to be spoken)."""
    folder = active()
    if not folder:
        return "Code mode isn't on. Say let's code on, and the project's name."
    which, name = backend(), label()
    exe = shutil.which(which)
    if not exe:
        return (f"I can't find {name} (the {which} command). Install it and sign in, then try again."
                + (" Or set CODER=codex in .env to use Codex instead." if which == "claude" else ""))
    if which == "codex":
        ok, why = codex_sandbox_ok()
        if not ok:
            return why
    state = _load()
    sess = state.get("sessions", {}).get(folder, {})
    sid = state.get("session_id") if sess.get("backend", "claude") == which else None
    before = snapshot(folder, f"before: {task}")
    t0 = time.time()
    _live(f"\n[{time.strftime('%H:%M:%S')}] You: {task}")

    if which == "claude":
        sid, result, error, turns = _run_claude(exe, folder, task, sid, on_progress)
    else:
        sid, result, error, turns, offline = _run_codex(exe, folder, task, sid, on_progress, network=False)
        if offline and not _cancelled:
            # The sandbox keeps Codex offline. It needed the internet (e.g. to install a package): ask.
            import hud

            allowed = False
            if hud.approval_handler:
                allowed, _ = hud.approval_handler(f"use the internet for this request ({offline[:200]})",
                                                  "use the internet to finish this request")
            if allowed:
                _live("  (internet allowed for this request)")
                retry = ("Internet access is now allowed for this request. Retry the steps that failed because "
                         "there was no network, then finish what I asked.")
                sid, result2, error, turns2, _ = _run_codex(exe, folder, retry, sid, on_progress, network=True)
                result, turns = result2 or result, turns + turns2
            else:
                result += " I didn't give it internet access, so anything that needed a download wasn't done."

    took = time.time() - t0
    _live(f"[{time.strftime('%H:%M:%S')}] " + ("Stopped." if _cancelled else
                                               f"Problem: {error[:200]}" if error and not result else
                                               f"Done in {took:.0f} s.") + "\n")
    after = snapshot(folder, f"after: {task}")
    state = _load()
    state["session_id"] = sid
    state.setdefault("sessions", {})[folder] = {"id": sid, "t": time.time(), "backend": which}
    files = changed_files(folder, before, after) if before and after else []
    if before and after and files:
        history = state.setdefault("snapshots", {}).setdefault(folder, [])
        history.append({"before": before, "after": after, "task": task[:200], "t": time.time()})
        del history[:-20]
    _save(state)
    try:
        import metrics

        metrics.record("code", backend=which, seconds=took, turns=turns, files=len(files), ok=not error)
    except Exception:
        pass

    if _cancelled:
        return "Stopped." + (" Say undo that to put the files back." if files else "")
    if error and not result:
        if re.search(r"usage limit|limit reached|rate limit|quota", error, re.I):
            return f"{name} says you've hit your plan's usage limit. It resets after a while; try again later."
        return f"{name} ran into a problem: {error[:200]}"
    return result


def _spawn(cmd: list, folder: str, env: dict, text: str) -> subprocess.Popen:
    global _proc, _cancelled
    with _lock:
        _cancelled = False
        _proc = subprocess.Popen(cmd, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                 creationflags=NO_WINDOW)
        _proc.stdin.write(text)
        _proc.stdin.close()
        return _proc


def _finish(proc: subprocess.Popen, timer: threading.Timer) -> str:
    global _proc
    timer.cancel()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    with _lock:
        _proc = None
    return (proc.stderr.read() or "").strip()


def _progress(line: str, on_progress) -> None:
    if line:
        _activity(line[:60])
        if on_progress:
            on_progress(line)


def _run_claude(exe: str, folder: str, task: str, sid: str | None, on_progress):
    cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--settings", _hook_settings(),
           "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS,
           "--append-system-prompt", VOICE_NOTE]
    if os.getenv("CODER_MODEL"):
        cmd += ["--model", os.environ["CODER_MODEL"]]
    if sid:
        cmd += ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        cmd += ["--session-id", sid]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # use the Claude plan, not API billing
    env["JARVIS_CODER_PROJECT"] = folder

    result, error, turns, bash_ids = "", "", 0, set()
    proc = _spawn(cmd, folder, env, task)
    timer = threading.Timer(MAX_TASK_S, cancel)
    timer.start()
    try:
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind = e.get("type")
            if kind == "system" and e.get("session_id"):
                sid = e["session_id"]
            elif kind == "assistant":
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        name, inp = c.get("name", ""), c.get("input") or {}
                        if name != "ToolSearch":  # Claude Code housekeeping, not worth showing
                            _live(_live_line(name, inp, folder))
                        if name in ("Bash", "PowerShell"):
                            bash_ids.add(c.get("id"))
                        _progress(_describe(name, inp), on_progress)
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        _live("  Claude: " + c["text"].strip().replace("\n", "\n          "))
            elif kind == "user":  # tool results: show command output and refusals
                for c in e.get("message", {}).get("content", []) or []:
                    if not isinstance(c, dict) or c.get("type") != "tool_result":
                        continue
                    body = c.get("content")
                    if isinstance(body, list):
                        body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                    body = str(body or "").strip()
                    if c.get("is_error"):
                        _live("    ! " + body[:300].replace("\n", "\n      "))
                    elif c.get("tool_use_id") in bash_ids and body:
                        _live(_output_block(body))
            elif kind == "result":
                sid = e.get("session_id") or sid
                turns = e.get("num_turns", 0)
                result = e.get("result") or ""
                if e.get("is_error"):
                    error = result or e.get("subtype", "error")
    finally:
        stderr = _finish(proc, timer)
    if not result and not error:
        error = stderr[-300:] or "Claude Code stopped without an answer."
    return sid, result, error, turns


# Signs that a command failed because the Codex sandbox keeps it offline.
OFFLINE = re.compile(r"getaddrinfo|ENOTFOUND|EAI_AGAIN|Could not resolve host|No such host is known|"
                     r"Temporary failure in name resolution|Name or service not known|Network is unreachable|"
                     r"Failed to establish a new connection|remote name could not be resolved|WinError 10013|"
                     r"forbidden by its access permissions|unable to access 'https?:|ECONNREFUSED|ETIMEDOUT|"
                     r"Could not fetch URL|connection (?:was )?(?:refused|reset)", re.I)


def _plain_command(cmd: str) -> str:
    """Codex wraps commands in powershell.exe -Command '...': show just the command."""
    m = re.match(r"""^"?[^"]*powershell(?:\.exe)?"?\s+(?:-NoProfile\s+)?-Command\s+(['"])(.*)\1\s*$""", cmd or "",
                 re.I | re.S)
    return (m.group(2) if m else cmd or "").strip()


def _run_codex(exe: str, folder: str, task: str, sid: str | None, on_progress, network: bool):
    """One Codex request, sandboxed to the project folder (and offline unless network=True).
    Returns (session id, summary, error, turns, what needed the internet or "")."""
    opts = ["--json", "--skip-git-repo-check", "--ignore-user-config",  # no personal MCP tools: nothing unsandboxed
            "--sandbox", "workspace-write", "-c", 'approval_policy="never"', *_codex_sandbox_settings(),
            # only the project folder is writable (not the temp folders Codex allows by default)
            "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c", "sandbox_workspace_write.exclude_slash_tmp=true", "-C", folder]
    if network:
        opts += ["-c", "sandbox_workspace_write.network_access=true"]
    model = os.getenv("CODER_MODEL") or os.getenv("CODEX_MODEL")
    if model:
        opts += ["-m", model]
    if sid:
        cmd, text = [exe, "exec", *opts, "resume", sid, "-"], task
    else:
        cmd, text = [exe, "exec", *opts, "-"], f"<instructions>\n{VOICE_NOTE}\n</instructions>\n\n{task}"
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}  # use the ChatGPT plan, not API billing

    result, error, turns, offline = "", "", 0, ""
    proc = _spawn(cmd, folder, env, text)
    timer = threading.Timer(MAX_TASK_S, cancel)
    timer.start()
    try:
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind, item = e.get("type"), e.get("item") or {}
            it = item.get("type")
            if kind == "thread.started":
                sid = e.get("thread_id") or sid
            elif kind == "turn.completed":
                turns += 1
            elif kind in ("turn.failed", "error"):
                error = str((e.get("error") or {}).get("message") or e.get("message") or "Codex failed")
            elif kind == "item.started" and it == "command_execution":
                command = _plain_command(item.get("command", ""))
                _live(f"  $ {command}")
                _progress("Running " + command[:50], on_progress)
            elif kind == "item.completed":
                if it == "command_execution":
                    out = (item.get("aggregated_output") or "").strip()
                    if out:
                        _live(_output_block(out))
                    if item.get("exit_code") not in (0, None):
                        _live(f"    ! exit code {item.get('exit_code')}")
                        if not network and OFFLINE.search(out):
                            offline = _plain_command(item.get("command", ""))[:120]
                elif it == "file_change":
                    for ch in item.get("changes") or []:
                        path = ch.get("path", "")
                        try:
                            path = str(Path(path).resolve().relative_to(folder))
                        except (ValueError, OSError):
                            pass
                        mark = {"add": "+ Writing", "delete": "- Deleting"}.get(ch.get("kind"), "~ Editing")
                        _live(f"  {mark} {path}" + ("" if item.get("status") != "failed" else "  (failed)"))
                        _progress(f"{mark[2:]} {Path(path).name}", on_progress)
                elif it == "agent_message" and (item.get("text") or "").strip():
                    result = item["text"].strip()
                    _live("  Codex: " + result.replace("\n", "\n         "))
                elif it == "todo_list":
                    _live("  Plan:\n" + "\n".join(f"    [{'x' if t.get('completed') else ' '}] {t.get('text', '')}"
                                                  for t in item.get("items") or []))
                    _progress("Planning the steps", on_progress)
                elif it == "web_search":
                    _live(f"  @ Web search: {item.get('query', '')}")
                    _progress("Looking it up online", on_progress)
                elif it == "mcp_tool_call":
                    _live(f"  * {item.get('server', '')}.{item.get('tool', '')}")
    finally:
        stderr = _finish(proc, timer)
    if not result and not error:
        error = stderr[-300:] or "Codex stopped without an answer."
    return sid, result, error, turns, offline


def _output_block(body: str) -> str:
    lines = body.splitlines()
    shown = lines[:15] + ([f"... ({len(lines) - 15} more lines)"] if len(lines) > 15 else [])
    return "\n".join("    | " + ln for ln in shown)


def busy() -> bool:
    return _proc is not None


def cancel() -> None:
    """Stop the request Claude Code is working on (the Stop button)."""
    global _cancelled
    with _lock:
        if _proc and _proc.poll() is None:
            _cancelled = True
            subprocess.run(["taskkill", "/PID", str(_proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)


# ------------------------------------------------- tools for Jarvis's brain --
def start_coding(project: str, new_session: bool = False) -> str:
    """Tool: turn code mode on (when the user asks in their own words)."""
    folder, options = find_project(project or "")
    if folder:
        return start(folder, fresh=new_session)
    if options:
        return "Several projects match: " + "; ".join(options) + ". Ask the user which, then call again with its path."
    return "I couldn't find a project by that name. Ask the user for the folder's name."


def stop_coding() -> str:
    return stop()
