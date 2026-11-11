"""Build dist/Jarvis.zip: a clean copy to give someone else. Run: python make_package.py

Only the files listed below are copied, so your .env (keys), memory, routines, app list,
light keys and logs can never end up in the zip. Afterwards it checks the zip for anything
from your .env and for your user name, and refuses to finish if it finds one.
"""

import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DIST = HERE / "dist"
OUT = DIST / "Jarvis"

FILES = [
    "jarvis.py", "brains.py", "tools.py", "apps.py", "routines.py", "music.py", "hud.py", "hud.html",
    "winlayout.py", "windows_control.py", "mcp_server.py", "startup.py", "paths.py", "setup.py",
    "setup_lights.py", "icon.py", "icon.png", "requirements.txt", ".env.example", ".gitignore", "README.txt",
    "setup.bat", "Start Jarvis.bat", "Stop Jarvis.bat", "Update Jarvis.bat", "updater.py", "version.json",
    "remote_discord.py", "Set up Discord.bat",
    "models/hey_jarvis_v0.1.onnx", "models/melspectrogram.onnx", "models/embedding_model.onnx",
]


def secrets() -> list[str]:
    """Values from your .env that must never be shared (keys, ids), plus your Windows user name."""
    found = []
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.strip().endswith("VOICE_ID"):
                    continue  # public built-in ElevenLabs voices, not secret
                if value.strip() and any(w in key for w in ("KEY", "SECRET", "TOKEN", "ID", "PASSWORD")):
                    found.append(value.strip())
    user = os.environ.get("USERNAME", "")
    if len(user) >= 3:
        found.append(user)
    return found


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    for f in FILES:
        src = HERE / f
        if not src.exists():
            sys.exit(f"Missing {f}")
        dst = OUT / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Safety check before zipping.
    problems = []
    needles = secrets()
    public_repo = json.loads((HERE / "version.json").read_text(encoding="utf-8"))["repo"]  # meant to be public
    for p in OUT.rglob("*"):
        if p.is_file() and p.suffix not in (".onnx", ".png"):
            text = p.read_text(encoding="utf-8", errors="ignore").replace(public_repo, "")
            for n in needles:
                if n.lower() in text.lower():
                    problems.append(f"{p.relative_to(OUT)} contains a private value ({n[:3]}…)")
            if re.search(r"sk-[A-Za-z0-9_-]{20,}|sk_[a-f0-9]{30,}", text):
                problems.append(f"{p.relative_to(OUT)} looks like it contains an API key")
    if problems:
        sys.exit("NOT packaged, private data found:\n  " + "\n  ".join(problems))

    zip_path = DIST / "Jarvis.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob("*")):
            if p.is_file():
                z.write(p, Path("Jarvis") / p.relative_to(OUT))
    size = zip_path.stat().st_size / 1e6
    print(f"Packaged {len(FILES)} files -> {zip_path} ({size:.1f} MB). Checked: no keys or personal info inside.")


if __name__ == "__main__":
    main()
