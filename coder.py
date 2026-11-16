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
UNDO_RE = re.compile(r"^(?:please\s+)?(?:undo|revert|roll back|rollback)\b(?:\s+(?:that|it|this|the last "
                     r"(?:change|changes|one|edit)|your changes|what you (?:just )?did))?[.!]*$", re.I)
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


def start(folder: str) -> str:
    """Turn code mode on for this folder and open it in VS Code."""
    folder = str(Path(folder).resolve())
    state = _load()
    sessions = state.get("sessions", {})
    # Pick up where you left off in this project if it was recent (within 12 hours).
    prev = sessions.get(folder, {})
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
    return (f"Code mode on for {project_name(folder)}, open in VS Code.{again}{shown} Tell me what you'd like "
            "built or fixed. Say undo that to reverse a change, or stop coding when you're done.")


def stop() -> str:
    state = _load()
    name = project_name(state.get("project", ""))
    state["project"] = ""
    _save(state)
    _activity("Code mode off")
    return f"Done coding on {name}. Back to normal." if name else "Code mode was already off."


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


def open_terminal() -> str:
    """Take over: the same Claude Code conversation in a terminal you can type in."""
    folder, sid = active(), _load().get("session_id")
    if not folder:
        return "Code mode isn't on."
    if busy():
        return "Claude Code is still working on your last request. Try again when it's finished."
    claude = shutil.which("claude") or "claude"
    args = [claude] + (["--resume", sid] if sid else [])
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-w", "new", "--title", f"Claude Code - {project_name(folder)}", "-d", folder, *args],
                         creationflags=NO_WINDOW)
    else:
        subprocess.Popen(["cmd", "/k", subprocess.list2cmdline(args)], cwd=folder, creationflags=0x00000010)
    return ("Opened Claude Code in a terminal" + (", with our conversation so far." if sid else ".")
            + " Type there yourself. If you ask me for more, finish there first so you don't both edit at once.")


def open_claude_terminal(project: str) -> str:
    """Tool: a plain interactive Claude Code session in a project folder (no code mode)."""
    folder, options = find_project(project or "")
    if not folder:
        return ("Several projects match: " + "; ".join(options) + ". Ask which.") if options else \
            "I couldn't find that project. Ask the user for the folder's name."
    claude = shutil.which("claude") or "claude"
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-w", "new", "-d", folder, claude], creationflags=NO_WINDOW)
    else:
        subprocess.Popen(["cmd", "/k", claude], cwd=folder, creationflags=0x00000010)
    return f"Opened Claude Code in a terminal in {project_name(folder)}."


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
        if not gd.exists():
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
    """Give Claude Code one request in the current project. Blocks until it's done; returns its summary."""
    global _proc, _cancelled
    folder = active()
    if not folder:
        return "Code mode isn't on. Say let's code on, and the project's name."
    claude = shutil.which("claude")
    if not claude:
        return "I can't find Claude Code (the claude command). Install it and sign in, then try again."
    state = _load()
    sid = state.get("session_id")
    before = snapshot(folder, f"before: {task}")

    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose", "--settings", _hook_settings(),
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

    t0, result, error, turns = time.time(), "", "", 0
    _live(f"\n[{time.strftime('%H:%M:%S')}] You: {task}")
    bash_ids = set()
    with _lock:
        _cancelled = False
        _proc = subprocess.Popen(cmd, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                 creationflags=NO_WINDOW)
    proc = _proc
    timer = threading.Timer(MAX_TASK_S, cancel)
    timer.start()
    try:
        proc.stdin.write(task)
        proc.stdin.close()
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
                        line_ = _describe(name, inp)
                        if line_:
                            _activity(line_[:60])
                            if on_progress:
                                on_progress(line_)
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
                        lines = body.splitlines()
                        shown = lines[:15] + ([f"... ({len(lines) - 15} more lines)"] if len(lines) > 15 else [])
                        _live("\n".join("    | " + ln for ln in shown))
            elif kind == "result":
                sid = e.get("session_id") or sid
                turns = e.get("num_turns", 0)
                result = e.get("result") or ""
                if e.get("is_error"):
                    error = result or e.get("subtype", "error")
        proc.wait(timeout=30)
        if not result and not error:
            error = (proc.stderr.read() or "").strip()[-300:] or "Claude Code stopped without an answer."
    finally:
        timer.cancel()
        with _lock:
            _proc = None

    took = time.time() - t0
    _live(f"[{time.strftime('%H:%M:%S')}] " + ("Stopped." if _cancelled else
                                               f"Problem: {error[:200]}" if error and not result else
                                               f"Done in {took:.0f} s.") + "\n")
    after = snapshot(folder, f"after: {task}")
    state = _load()
    state["session_id"] = sid
    state.setdefault("sessions", {})[folder] = {"id": sid, "t": time.time()}
    files = changed_files(folder, before, after) if before and after else []
    if before and after and files:
        history = state.setdefault("snapshots", {}).setdefault(folder, [])
        history.append({"before": before, "after": after, "task": task[:200], "t": time.time()})
        del history[:-20]
    _save(state)
    try:
        import metrics

        metrics.record("code", seconds=time.time() - t0, turns=turns, files=len(files), ok=not error)
    except Exception:
        pass

    if _cancelled:
        return "Stopped." + (" Say undo that to put the files back." if files else "")
    if error and not result:
        if "usage limit" in error.lower() or "limit reached" in error.lower():
            return "Claude Code says you've hit your plan's usage limit. It resets after a while; try again later."
        return f"Claude Code ran into a problem: {error[:200]}"
    return result


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
def start_coding(project: str) -> str:
    """Tool: turn code mode on (when the user asks in their own words)."""
    folder, options = find_project(project or "")
    if folder:
        return start(folder)
    if options:
        return "Several projects match: " + "; ".join(options) + ". Ask the user which, then call again with its path."
    return "I couldn't find a project by that name. Ask the user for the folder's name."


def stop_coding() -> str:
    return stop()
