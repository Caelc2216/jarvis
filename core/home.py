"""Where Jarvis keeps things, and the one-time move from the old everything-in-one-folder layout.

    Jarvis/            buttons (.bat), README, jarvis.py + startup.py (launchers), .env
      core/            the assistant itself
      features/        optional extras: code mode, Discord remote, Alexa
      installer/       setup, updater
      data/            what Jarvis learned or saved on this PC (never touched by updates)
      logs/            jarvis.log, update.log, metrics
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE, FEATURES, INSTALLER = ROOT / "core", ROOT / "features", ROOT / "installer"
DATA, LOGS = ROOT / "data", ROOT / "logs"
ENV = ROOT / ".env"
for _d in (DATA, LOGS):
    try:
        _d.mkdir(exist_ok=True)
    except OSError:
        pass
OLD_LAYOUT = DATA / "backups" / "old-layout"  # leftovers from before the folders, kept just in case

# Saved state that used to sit next to jarvis.py.
DATA_FILES = ["memory.json", "routines.json", "aliases.json", "apps.json", "devices.json", "timers.json",
              "routine_windows.json", "close_history.json", "update_state.json", "update_announced.txt",
              "ui_settings.json", ".hud_token", "coder_state.json", "voice_state.json", "alexa_seen.json",
              "tinytuya.json", "tuya-raw.json", "snapshot.json", "jarvis.ico"]
DATA_DIRS = {"coder_snapshots": "coder_snapshots", ".hud-profile": "hud-profile", "backups": "backups"}
LOG_FILES = ["jarvis.log", "update.log", "metrics.jsonl", "coder_live.log"]
# Program files of the old layout (now in core/, features/, installer/, dev/).
OLD_PROGRAM = ["brains.py", "tools.py", "routines.py", "hud.py", "hud.html", "speech.py", "apps.py", "music.py",
               "paths.py", "windows_control.py", "winlayout.py", "metrics.py", "mcp_server.py", "icon.py",
               "icon.png", "mic_check.py", "coder.py", "coder_hook.py", "remote_discord.py", "setup.py",
               "setup_lights.py", "alexa_setup.py", "updater.py", "make_package.py", "release.py"]


def _move(src: Path, dst: Path) -> None:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as e:  # in use (an open browser profile, a log being written): try again next start
        print(f"  (couldn't move {src.name} yet: {e})")


def _aside(src: Path) -> None:
    dst = OLD_LAYOUT / src.name
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    _move(src, dst)


def migrate() -> None:
    """Move an install from the old flat layout into data/ and logs/. Safe to run every start."""
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    for old_name, new_name in DATA_DIRS.items():
        old = ROOT / old_name
        if old.is_dir():
            _move(old, DATA / new_name) if not (DATA / new_name).exists() else _aside(old)
    for name in DATA_FILES:
        old = ROOT / name
        if old.is_file():
            _move(old, DATA / name) if not (DATA / name).exists() else _aside(old)
    for name in LOG_FILES:
        old = ROOT / name
        if not old.is_file():
            continue
        new = LOGS / name
        if not new.exists():
            _move(old, new)
            continue
        try:  # both exist (an old updater wrote one more line): keep everything in the new one
            with open(new, "ab") as out:
                out.write(old.read_bytes())
            old.unlink()
        except OSError:
            pass
    skill = ROOT / "alexa_skill"
    if skill.is_dir():
        private = skill / "lambda_function.py"  # has the webhook link in it
        if private.is_file() and not (FEATURES / "alexa_skill" / private.name).exists():
            _move(private, FEATURES / "alexa_skill" / private.name)
        _aside(skill)
    for name in OLD_PROGRAM:
        if (ROOT / name).is_file():
            _aside(ROOT / name)
    shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)  # compiled copies of the old files
