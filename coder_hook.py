"""Claude Code calls this before it runs a command or edits a file in code mode (see coder.py).

Safe things go straight through: editing files inside the project, and a short list of read-only or
test commands. Anything else (installing packages, deleting things, scripts, files outside the project)
is put to the user by the running Jarvis: out loud, and as Allow / Don't allow buttons on the display.
If Jarvis can't be reached, the answer is no.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
PORT = int(os.getenv("HUD_PORT", "8765"))

# Commands that only look at things or run the project's own tests/builds. Anything chained,
# redirected or piped is never "safe" (it could hide something else).
SAFE = re.compile(
    r"^(git (status|diff|log|show|branch|rev-parse|ls-files|blame)\b"
    r"|ls\b|dir\b|pwd$|cd\b|cat\b|type\b|head\b|tail\b|wc\b|echo\b|where\b|which\b|tree\b"
    r"|python3? (-m (pytest|unittest|mypy|ruff|flake8|py_compile|black --check)\b|--version$)"
    r"|py (-m (pytest|unittest|mypy|ruff|flake8|py_compile)\b|--version$)"
    r"|pytest\b|ruff check\b|flake8\b|mypy\b"
    r"|npm (test|run (test|lint|build|typecheck|check))\b|npx (tsc|eslint|vitest|jest)\b|yarn (test|lint|build)\b"
    r"|pnpm (test|lint|build)\b|node --version$|tsc\b"
    r"|cargo (test|check|build|clippy|fmt --check)\b|go (test|build|vet)\b|dotnet (build|test)\b"
    r"|pip (list|show|freeze)\b|java -version$|javac\b|gradle(w)? (test|build)\b|mvn (test|compile)\b)",
    re.I)
DANGER = re.compile(r"[;&|<>`]|\$\(", re.I)


def decide(data: dict) -> tuple[str, str] | None:
    """('allow'|'ask', reason) or None to leave it to Claude Code's normal rules."""
    tool, inp = data.get("tool_name", ""), data.get("tool_input") or {}
    project = Path(os.getenv("JARVIS_CODER_PROJECT") or data.get("cwd") or ".").resolve()
    if tool in ("Bash", "PowerShell"):
        cmd = (inp.get("command") or "").strip()
        if cmd and SAFE.match(cmd) and not DANGER.search(cmd):
            return "allow", "safe command"
        what = inp.get("description") or ""
        return "ask", f"run the command {cmd[:160]}" + (f" ({what})" if what else "")
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        try:
            inside = Path(target).resolve().is_relative_to(project)
        except (OSError, ValueError):
            inside = False
        if inside:
            return None  # inside the project: edits are allowed
        return "ask", f"change a file outside the project: {target}"
    return None


def ask_jarvis(question: str) -> tuple[bool, str]:
    try:
        token = (HERE / ".hud_token").read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/coder/approve", method="POST",
                                     data=json.dumps({"text": question}).encode(),
                                     headers={"Content-Type": "application/json", "X-Jarvis-Token": token})
        with urllib.request.urlopen(req, timeout=320) as r:
            answer = json.load(r)
        return bool(answer.get("allow")), answer.get("note", "")
    except Exception as e:
        return False, f"couldn't reach Jarvis to ask ({e.__class__.__name__})"


def main() -> None:
    data = json.load(sys.stdin)
    verdict = decide(data)
    if not verdict:
        return
    kind, reason = verdict
    if kind == "ask":
        allowed, note = ask_jarvis(reason)
        kind = "allow" if allowed else "deny"
        reason = "The user allowed it." if allowed else ("The user said no" + (f": {note}" if note else "") +
                                                         ". Don't retry it; find another way or explain what's needed.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                             "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
