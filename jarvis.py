"""Start Jarvis, or one of its helpers. The .bat buttons all go through here.

    python jarvis.py                 talk to Jarvis (see core/assistant.py for --text, --no-clap)
    python jarvis.py setup           setup questions   (setup --mic, setup --discord)
    python jarvis.py update          check for updates (update --apply --restart)
    python jarvis.py stats [hours]   speed and CPU/memory
    python jarvis.py alexa [test]    connect an Echo
    python jarvis.py lights          find smart lights
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The code lives in these folders; put them first so they win over any leftover old copies.
sys.path[:0] = [str(ROOT / d) for d in ("core", "features", "installer")]

HELPERS = {"setup": "installer/setup.py", "update": "installer/updater.py", "stats": "core/metrics.py",
           "alexa": "installer/alexa_setup.py", "lights": "installer/setup_lights.py"}


def main() -> None:
    import home

    try:
        home.migrate()
    except Exception as e:  # never stop Jarvis from starting over a file that couldn't move
        print(f"  (moving old files into data/ and logs/ didn't finish: {e})")
    helper = HELPERS.get(sys.argv[1]) if len(sys.argv) > 1 else None
    if helper:
        import runpy

        sys.argv = [str(ROOT / helper)] + sys.argv[2:]
        runpy.run_path(str(ROOT / helper), run_name="__main__")
        return
    import assistant

    try:
        assistant.main()
    except KeyboardInterrupt:
        print("\nShutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
