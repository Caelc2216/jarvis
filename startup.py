"""Start Jarvis automatically when you sign in to Windows (hidden, waiting for "Hey Jarvis").

    python startup.py on      add Jarvis to your Startup folder
    python startup.py off     remove it
    python startup.py status  check
    python startup.py stop    stop the Jarvis running in the background right now
    python startup.py start   start it in the background right now

It runs with pythonw (no console window) and writes its output to jarvis.log.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
SHORTCUT = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\Jarvis.lnk"


def _pythonw() -> str:
    exe = Path(sys.executable)
    for candidate in (exe.with_name("pythonw.exe"), shutil.which("pythonw")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise SystemExit("Couldn't find pythonw.exe")


def on() -> None:
    target, script = _pythonw(), HERE / "jarvis.py"
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath=$env:TGT; $s.Arguments=$env:ARGS; $s.WorkingDirectory=$env:WD;"
        "$s.Description='Jarvis voice assistant'; if ($env:ICO) { $s.IconLocation=$env:ICO }; $s.Save()"
    )
    import icon

    ico = icon.ensure()
    env = {**os.environ, "LNK": str(SHORTCUT), "TGT": target, "ARGS": f'"{script}" --background', "WD": str(HERE),
           "ICO": str(ico) if ico else ""}
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env=env)
    print(f"Jarvis will start when you sign in.\n  shortcut: {SHORTCUT}")


def off() -> None:
    if SHORTCUT.exists():
        SHORTCUT.unlink()
        print("Removed. Jarvis won't start with Windows any more.")
    else:
        print("Jarvis wasn't set to start with Windows.")


def _running() -> list[str]:
    # Only this folder's Jarvis (there could be another copy somewhere else on the PC).
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'pythonw*' -and "
          "$_.CommandLine -like ('*' + $env:JARVIS_SCRIPT + '*--background*') } | ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         env={**os.environ, "JARVIS_SCRIPT": str(HERE / "jarvis.py")})
    return out.stdout.split()


def stop() -> None:
    pids = _running()
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    print(f"Stopped {len(pids)} background Jarvis." if pids else "Jarvis wasn't running in the background.")


def start() -> None:
    if _running():
        print("Jarvis is already running in the background.")
        return
    log = HERE / "jarvis.log"
    size_before = log.stat().st_size if log.exists() else 0
    proc = subprocess.Popen([_pythonw(), str(HERE / "jarvis.py"), "--background"], cwd=HERE)
    for _ in range(20):  # give it ~10 s to get going, and catch it if it dies right away
        time.sleep(0.5)
        if proc.poll() is not None:
            break
    if proc.poll() is None:
        print('Started Jarvis in the background. Say "Hey Jarvis" to wake it.')
        return
    print("Jarvis stopped right after starting. Here's what it said:\n")
    if log.exists():
        with open(log, encoding="utf-8", errors="replace") as f:
            f.seek(size_before)
            print("".join(f.read().splitlines(keepends=True)[-25:]) or "(nothing logged)")
    else:
        print("(no jarvis.log was written)")
    print("\nDouble-click 'Troubleshoot Jarvis.bat' to see more, and send a screenshot of it.")


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["status"])[0]
    actions = {"on": on, "off": off, "stop": stop, "start": start}
    if cmd in actions:
        actions[cmd]()
    else:
        print("ON - starts with Windows" if SHORTCUT.exists() else "OFF - not starting with Windows")
        print("Running in the background now." if _running() else "Not running in the background right now.")
