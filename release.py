"""Publish a new Jarvis version that every installed copy can update to.

    python release.py 1.1.0 "Added Spotify control and fixed the timer bug"

It: sets the version, builds the clean zip (with its privacy check), commits, tags,
pushes to GitHub, and creates the GitHub release with Jarvis.zip attached.
Installed copies notice within a day (or right away if someone says "update yourself").
"""

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()


def run(*cmd, check=True):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, cwd=HERE, check=check, text=True)


def main():
    if len(sys.argv) < 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        sys.exit('Usage: python release.py 1.2.3 "what changed"')
    version, notes = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    vf = HERE / "version.json"
    data = json.loads(vf.read_text(encoding="utf-8"))
    old = data["version"]
    if tuple(map(int, version.split("."))) <= tuple(map(int, old.split("."))):
        sys.exit(f"The new version must be higher than {old}.")
    data["version"] = version
    vf.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    run(sys.executable, "make_package.py")            # stops here if anything private is found
    run("git", "add", "-A")
    run("git", "commit", "-m", f"Release v{version}" + (f"\n\n{notes}" if notes else ""), check=False)
    run("git", "tag", f"v{version}")
    run("git", "push")
    run("git", "push", "origin", f"v{version}")
    run("gh", "release", "create", f"v{version}", str(HERE / "dist" / "Jarvis.zip"),
        "--title", f"Jarvis {version}", "--notes", notes or f"Jarvis {version}")
    print(f"\nPublished Jarvis {version}. Installed copies will offer the update within a day.")


if __name__ == "__main__":
    main()
