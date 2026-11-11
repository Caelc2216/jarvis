"""Keeps Jarvis up to date from its GitHub releases. Your settings and everything Jarvis has learned
(.env, memory, routines, nicknames, app list, lights) are never touched — only program files change.

    python updater.py              check, and say whether an update is available
    python updater.py --apply      download and install the newest version (backs up first)
    python updater.py --apply --restart   ...and restart the background Jarvis (used by voice/Update Jarvis.bat)

If the new version won't start, the backup is put back automatically.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent.resolve()
VERSION_FILE = HERE / "version.json"
BACKUPS = HERE / "backups"
STATE = HERE / "update_state.json"
LOG = HERE / "update.log"

# Never overwritten or deleted by an update: everything personal or learned on this PC.
KEEP = {".env", "memory.json", "routines.json", "aliases.json", "apps.json", "devices.json", "timers.json",
        "routine_windows.json", "close_history.json", "update_state.json", "jarvis.log", "update.log",
        "icon.png", "jarvis.ico", "tinytuya.json", "snapshot.json", "tuya-raw.json"}
KEEP_DIRS = {"backups", ".hud-profile", "plugins", ".git", "__pycache__", "dist"}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def local() -> dict:
    return json.loads(VERSION_FILE.read_text(encoding="utf-8"))


def _v(s: str) -> tuple:
    return tuple(int(x) for x in s.lstrip("v").split(".")[:3] if x.isdigit())


def latest() -> dict | None:
    """The newest release on GitHub: {"version", "zip_url", "notes"}."""
    repo = local()["repo"]
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "jarvis-updater"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.load(r)
    asset = next((a for a in rel.get("assets", []) if a["name"] == "Jarvis.zip"), None)
    if not asset:
        return None
    return {"version": rel["tag_name"].lstrip("v"), "zip_url": asset["browser_download_url"],
            "notes": (rel.get("body") or "").strip()}


def check() -> str:
    """Spoken-friendly status. Also remembers the result for Jarvis to mention on wake."""
    try:
        new = latest()
    except Exception as e:
        return f"I couldn't check for updates right now ({e.__class__.__name__})."
    mine = local()["version"]
    available = bool(new and _v(new["version"]) > _v(mine))
    STATE.write_text(json.dumps({"checked": time.time(), "available": new["version"] if available else "",
                                 "notes": new["notes"] if available else ""}), encoding="utf-8")
    if not available:
        return f"I'm up to date (version {mine})."
    notes = f" What's new: {new['notes'][:300]}" if new["notes"] else ""
    return f"Version {new['version']} is available (I'm on {mine}).{notes}"


def update_available() -> str:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("available", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def _program_files(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and rel.parts[0] not in KEEP_DIRS and rel.name not in KEEP:
            out.append(rel)
    return out


def _python() -> str:
    exe = Path(sys.executable)
    if exe.stem.lower().startswith("pythonw"):
        alt = exe.with_name(exe.name.lower().replace("pythonw", "python"))
        if alt.exists():
            return str(alt)
    return str(exe)


def _self_test() -> bool:
    """Does the installed code at least load? (Catches a broken or half-copied update.)"""
    r = subprocess.run([_python(), "-c", "import jarvis, brains, tools, routines, hud, apps"], cwd=HERE,
                       capture_output=True, text=True, creationflags=0x08000000, timeout=120)
    if r.returncode != 0:
        log("Self-test failed:\n" + (r.stderr or r.stdout)[-1500:])
    return r.returncode == 0


def _restore(backup: Path) -> None:
    for rel in _program_files(backup):
        dst = HERE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / rel, dst)


def apply(restart: bool = False) -> str:
    new = latest()
    mine = local()["version"]
    if not new or _v(new["version"]) <= _v(mine):
        return f"Already up to date (version {mine})."
    log(f"Updating {mine} -> {new['version']}")

    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / "Jarvis.zip"
        urllib.request.urlretrieve(new["zip_url"], zpath)
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():  # refuse anything that would land outside the folder
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    raise RuntimeError(f"Unsafe path in update: {name}")
            z.extractall(tmp)
        src = Path(tmp) / "Jarvis"
        if not (src / "jarvis.py").exists():
            raise RuntimeError("The download doesn't look like Jarvis.")

        if restart:
            subprocess.run([_python(), str(HERE / "startup.py"), "stop"], cwd=HERE, creationflags=0x08000000)

        backup = BACKUPS / mine
        if backup.exists():
            shutil.rmtree(backup)
        for rel in _program_files(HERE):
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE / rel, backup / rel)

        for rel in _program_files(src):
            dst = HERE / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst)

    subprocess.run([_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                    str(HERE / "requirements.txt")], cwd=HERE, creationflags=0x08000000)

    if not _self_test():
        _restore(backup)
        log(f"Rolled back to {mine}.")
        result = f"The update to {new['version']} didn't work, so I put version {mine} back."
    else:
        STATE.write_text(json.dumps({"checked": time.time(), "available": "", "notes": ""}), encoding="utf-8")
        log(f"Updated to {new['version']}.")
        result = f"Updated to version {new['version']}."
    if restart:
        subprocess.run([_python(), str(HERE / "startup.py"), "start"], cwd=HERE, creationflags=0x08000000)
    return result


def start_update_in_background() -> str:
    """Used by the voice command: a separate process stops Jarvis, updates, and starts it again."""
    pyw = Path(_python()).with_name(Path(_python()).name.replace("python", "pythonw"))
    exe = str(pyw if pyw.exists() else _python())
    subprocess.Popen([exe, str(HERE / "updater.py"), "--apply", "--restart"], cwd=HERE,
                     creationflags=0x08000000 | 0x00000008)  # no window, detached
    return "Updating now. I'll be back in about a minute; say Hey Jarvis when you hear me."


if __name__ == "__main__":
    try:
        if "--apply" in sys.argv:
            print(apply(restart="--restart" in sys.argv))
        else:
            print(check())
    except Exception as e:
        log(f"Update failed: {e}")
        print(f"Update failed: {e}")
        if "--restart" in sys.argv:  # never leave Jarvis stopped because an update failed
            subprocess.run([_python(), str(HERE / "startup.py"), "start"], cwd=HERE, creationflags=0x08000000)
        sys.exit(1)
