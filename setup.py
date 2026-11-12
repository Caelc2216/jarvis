"""Jarvis setup wizard. Run by setup.bat (or: python setup.py). Safe to run again any time.

Asks a few simple questions, installs what's needed, writes .env, and starts Jarvis.
Nothing personal ever leaves this PC except what you choose (e.g. an ElevenLabs key you paste in).
"""

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ENV = HERE / ".env"
DEFAULT_VOICE = "onwK4e9ZLuTAKqWW03F9"  # ElevenLabs "Daniel": calm British voice
AUTO = "--defaults" in sys.argv  # for testing: accept every default without asking

# ------------------------------------------------------------------ helpers --
CYAN, DIM, BOLD, RESET = "\033[96m", "\033[2m", "\033[1m", "\033[0m"
os.system("")  # turn on colors in the Windows console


def say(text=""):
    print(text)


def title(text):
    print(f"\n{CYAN}{BOLD}── {text} {'─' * max(0, 54 - len(text))}{RESET}")


def ask(question, default=""):
    if AUTO:
        return default
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    answer = input(f"{question}{hint}: ").strip()
    return answer or default


def yes(question, default=True):
    if AUTO:
        return default
    d = "Y/n" if default else "y/N"
    answer = input(f"{question} {DIM}[{d}]{RESET}: ").strip().lower()
    return default if not answer else answer.startswith("y")


def choose(question, options, default=1):
    """options: list of (label, description). Returns the 1-based choice."""
    say(question)
    for i, (label, desc) in enumerate(options, 1):
        star = f" {CYAN}(recommended){RESET}" if i == default else ""
        say(f"  {BOLD}{i}{RESET}. {label}{star}")
        if desc:
            say(f"     {DIM}{desc}{RESET}")
    while True:
        answer = ask("Pick a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say("Please type one of the numbers.")


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, **kw)


def read_env() -> dict:
    values = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(values: dict) -> None:
    order = ["JARVIS_USER_NAME", "BRAIN", "OLLAMA_MODEL", "CLAUDE_CODE_MODEL", "ELEVENLABS_API_KEY",
             "ELEVENLABS_VOICE_ID", "WAKE_WORD", "HUD", "HUD_MONITOR", "HUD_AREA", "HUD_SIZE", "HUD_ON_TOP",
             "HUD_AUTOHIDE", "WELCOME_ACTIONS", "MIC_DEVICE", "REMOTE", "DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID"]
    keys = order + [k for k in values if k not in order]
    lines = ["# Jarvis settings, written by setup.py. Run setup.bat again to change them.",
             "# Keep this file private: it can hold your keys."]
    lines += [f"{k}={values[k]}" for k in keys if k in values]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clean_secret(text: str) -> str:
    """Drop what sneaks in when pasting: spaces, quotes, and invisible characters (e.g. a stray Ctrl+V)."""
    text = "".join(ch for ch in text if ch.isprintable()).strip().strip("'\"").strip()
    return text.split("=", 1)[1].strip() if "=" in text and text.split("=", 1)[0].isupper() else text


def secret_input(label: str, current: str, check, env_key: str) -> str:
    """Ask for a key/token, show what arrived, and test it. Returns a working value or ''."""
    for attempt in range(3):
        raw = getpass.getpass(f"\n  Paste the {label} (it stays hidden; right-click to paste) and press Enter"
                              + (" [Enter = keep the current one]" if current else "") + ": ")
        value = _clean_secret(raw) or current
        if not value:
            say("  Nothing arrived. Try right-clicking in this window to paste, then press Enter.")
            continue
        say(f"  Got {len(value)} characters, starting with '{value[:4]}…'. Checking it...")
        ok, msg = check(value)
        say(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            return value
    say(f"\n  Still not working. Another way: open the .env file in this folder with Notepad, add a line")
    say(f"     {env_key}=<paste here>")
    say("  save it, then come back here.")
    if not AUTO and yes("  Done that? Check it now?", True):
        value = _clean_secret(read_env().get(env_key, ""))
        if value:
            ok, msg = check(value)
            say(("  ✓ " if ok else "  ✗ ") + msg)
            if ok:
                return value
    return ""


def check_elevenlabs(key: str) -> tuple[bool, str]:
    import requests

    if key.isdigit() or (len(key) == 64 and not key.startswith("sk")):
        return False, ("That's the key's ID, not the key. The real key starts with 'sk_' and is only shown once, "
                       "in the popup right after you click Create Key. Make a new key and copy it from that popup.")
    try:
        r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{DEFAULT_VOICE}",
                          headers={"xi-api-key": key}, json={"text": "Hi", "model_id": "eleven_flash_v2_5"},
                          timeout=20)
    except Exception as e:
        return False, f"Couldn't reach ElevenLabs ({e.__class__.__name__}). Check the internet connection."
    if r.ok:
        return True, "The key works."
    try:
        detail = r.json().get("detail", {})
    except ValueError:
        detail = {}
    status = str(detail.get("status", "") if isinstance(detail, dict) else "")
    friendly = {
        "api_key_id_used_as_api_key": "That's the key's ID, not the key. Copy the one starting with 'sk_' from the "
                                      "popup shown right after Create Key.",
        "invalid_api_key": "ElevenLabs doesn't recognise that key. Copy it again, or make a new one.",
        "missing_permissions": "The key is missing permissions. Edit it and set 'Text to Speech' to Access.",
        "quota_exceeded": "This account has used up its free characters for the month.",
        "detected_unusual_activity": "ElevenLabs has paused free use on this account (often caused by a VPN).",
    }
    message = detail.get("message") if isinstance(detail, dict) else str(detail)
    return False, friendly.get(status, f"ElevenLabs said: {message or r.text[:200]}")


def check_discord_token(token: str) -> tuple[bool, str]:
    import requests

    import remote_discord

    if token.count(".") != 2 or not remote_discord.bot_id_from_token(token).isdigit():
        if token.isdigit():
            return False, "That's the Application ID. Go to Bot > Reset Token and copy the token instead."
        return False, ("That doesn't look like a bot token (it should be ~70 characters with two dots). "
                       "Go to Bot > Reset Token > Copy.")
    try:
        r = requests.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"},
                         timeout=20)
    except Exception as e:
        return False, f"Couldn't reach Discord ({e.__class__.__name__})."
    if r.ok:
        return True, f"The token works (bot name: {r.json().get('username')})."
    return False, "Discord rejected that token. It may have been reset since you copied it: reset it once more and copy the newest one."


def gpu_memory_gb() -> float:
    try:
        out = run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                  capture_output=True, timeout=10).stdout
        return max(float(x) for x in out.split()) / 1024
    except Exception:
        return 0.0


def winget(package_id: str) -> bool:
    if not shutil.which("winget"):
        return False
    r = run(["winget", "install", "-e", "--id", package_id, "--accept-source-agreements",
             "--accept-package-agreements", "--silent"])
    return r.returncode == 0


def ollama_exe() -> str:
    return shutil.which("ollama") or str(Path(os.environ["LOCALAPPDATA"]) / "Programs/Ollama/ollama.exe")


# ------------------------------------------------------------------ steps --
def step_name(env):
    title("1. About you")
    env["JARVIS_USER_NAME"] = ask("What should Jarvis call you?", env.get("JARVIS_USER_NAME", ""))


def step_brain(env):
    title("2. Jarvis's brain")
    gb = gpu_memory_gb()
    has_claude, has_codex = bool(shutil.which("claude")), bool(shutil.which("codex"))
    say("The brain is the AI that understands you and decides what to do.")
    options = [
        ("Free, runs on this PC (Ollama)",
         f"No account needed. Needs a decent graphics card" + (f" — yours has {gb:.0f} GB, good." if gb >= 6 else
                                                               " — yours may be slow at this.")),
        ("Claude (needs a Claude Pro or Max subscription)",
         "Smartest, can search the web." + (" Claude Code is installed." if has_claude else " Installs Claude Code.")),
        ("ChatGPT (needs a ChatGPT plan, even Go works)",
         "Slower to answer." + (" Codex is installed." if has_codex else " Installs Codex.")),
    ]
    default = 1 if gb >= 6 else (2 if has_claude else 1)
    pick = choose("Which brain should Jarvis use?", options, default)

    if pick == 1:
        env["BRAIN"] = "ollama"
        model = "qwen3:8b" if gb >= 8 else "qwen3:4b"
        env["OLLAMA_MODEL"] = model
        if not Path(ollama_exe()).exists():
            say("Installing Ollama (free, about 1 GB)...")
            if not winget("Ollama.Ollama"):
                say("Couldn't install it automatically. Get it from ollama.com, then run setup again.")
                return
        say(f"Downloading the {model} model (a few GB, this can take a while)...")
        run([ollama_exe(), "pull", model])
    elif pick == 2:
        env["BRAIN"] = "claude"
        env.setdefault("CLAUDE_CODE_MODEL", "sonnet")
        if not shutil.which("claude"):
            say("Installing Claude Code (Anthropic's official installer)...")
            run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "irm https://claude.ai/install.ps1 | iex"])
        say("\nA window will open so you can sign in to your Claude account. Come back here when it's done.")
        if not AUTO:
            input("Press Enter to open it...")
            run(["cmd", "/c", "start", "/wait", "claude"])
    else:
        env["BRAIN"] = "codex"
        if not shutil.which("codex"):
            if not shutil.which("npm"):
                say("Installing Node.js first...")
                winget("OpenJS.NodeJS.LTS")
            say("Installing Codex...")
            run(["cmd", "/c", "npm", "install", "-g", "@openai/codex@latest"])
        say("\nSign in to your ChatGPT account in the browser window that opens.")
        if not AUTO:
            run(["cmd", "/c", "codex", "login"])


def step_voice(env):
    title("3. Jarvis's voice")
    pick = choose("How should Jarvis sound?", [
        ("Built-in Windows voice", "Free, works right away, sounds a bit robotic."),
        ("Realistic voice from ElevenLabs", "Free account at elevenlabs.io. You'll paste in a key."),
    ], 2 if env.get("ELEVENLABS_API_KEY") else 1)
    if pick == 1:
        env.pop("ELEVENLABS_API_KEY", None)
        env.pop("ELEVENLABS_VOICE_ID", None)
        return
    say("\nOn elevenlabs.io: sign in > Developers > API Keys > Create Key.")
    say("Set 'Text to Speech' and 'Speech to Text' to Access, click Create, then copy the key")
    say("from the popup (it starts with sk_ and is only shown that once).")
    if AUTO:
        return
    key = secret_input("ElevenLabs key", env.get("ELEVENLABS_API_KEY", ""), check_elevenlabs, "ELEVENLABS_API_KEY")
    if key:
        env["ELEVENLABS_API_KEY"] = key
        env.setdefault("ELEVENLABS_VOICE_ID", DEFAULT_VOICE)
        say("Saved. Jarvis will use the 'Daniel' voice (calm, British).")
    else:
        say("Keeping the built-in Windows voice for now. Run setup.bat again to try another key.")


def step_display(env):
    title("4. On-screen display")
    sys.path.insert(0, str(HERE))
    import winlayout

    mons = winlayout.monitors()
    say("Jarvis shows a small glowing display in a corner of your screen while it's awake.")
    if len(mons) == 1:
        env["HUD_MONITOR"] = "main"
    else:
        names = ["left", "main", "right"] if len(mons) >= 3 else (
            ["main", "right"] if mons[0]["primary"] else ["left", "main"])
        pick = choose(f"You have {len(mons)} screens. Which one should it appear on?",
                      [(n.capitalize() + " screen", "") for n in names], len(names))
        env["HUD_MONITOR"] = names[pick - 1]
    env.setdefault("HUD_AREA", "bottom-right")
    env.setdefault("HUD_SIZE", "420x640")
    env.setdefault("HUD_ON_TOP", "on")
    env.setdefault("HUD_AUTOHIDE", "on")
    env.setdefault("HUD", "on")
    env.setdefault("WAKE_WORD", "on")
    env.setdefault("WELCOME_ACTIONS", "")


def step_mic(env):
    title("Microphone check")
    sys.path.insert(0, str(HERE))
    import mic_check

    for _ in range(4):  # Windows never asks Jarvis for mic permission, so check the switches ourselves
        blocked = mic_check.privacy_block()
        if not blocked:
            break
        say(f"Windows is blocking the microphone: {blocked}.")
        say("A Settings page is opening. Turn ON all three switches there, especially")
        say(f"{BOLD}'Let desktop apps access your microphone'{RESET} (near the bottom).")
        os.startfile(mic_check.SETTINGS_URL)
        if AUTO:
            return
        input("Press Enter when you've turned them on...")
    else:
        say("The microphone is still blocked. Jarvis will remind you until it's turned on.")
        return

    device = mic_check.device_from_env()
    for _ in range(3):
        if AUTO:
            return
        input(f"\nWhen you press Enter, {BOLD}say something out loud for 3 seconds{RESET}. Ready? ")
        peak, all_zero, error = mic_check.sample(3.0, device)
        bars = int(min(1.0, peak * 4) * 30)
        say(f"Level: [{'#' * bars}{'.' * (30 - bars)}]")
        if not error and peak >= 0.02:
            say("Heard you loud and clear.")
            return
        say("I couldn't hear much." if not error and not all_zero else
            "I got complete silence: the mic may be muted, or a different one is selected.")
        mics = mic_check.list_inputs()
        if not mics:
            say("No microphone was found. Plug one in and run setup.bat again.")
            return
        pick = choose("Which microphone do you talk into?", [(name, "") for _, name in mics], 1)
        device, name = mics[pick - 1]
        env["MIC_DEVICE"] = name[:31]
    say("Still not hearing much. Check the mic isn't muted (some headsets have a mute switch), then run setup again.")


def step_remote(env, standalone=False):
    title("Text Jarvis from your phone (Discord)" if standalone else "5b. Text Jarvis from your phone")
    say("Optional: message Jarvis from anywhere through a private Discord bot that only you can use.")
    if not yes("Set this up now?", standalone):
        return
    import webbrowser

    import remote_discord

    say(f"""
{BOLD}Make your bot (about 3 minutes):{RESET}
  1. A Discord page is opening. Sign in if asked, then click {BOLD}New Application{RESET}.
     Name it "Jarvis", tick the box, click {BOLD}Create{RESET}.
  2. On the left click {BOLD}Bot{RESET}. Change the {BOLD}Username{RESET} to Jarvis and click Save.
     Then {BOLD}Reset Token{RESET} > {BOLD}Yes, do it!{RESET} and {BOLD}Copy{RESET}.
     (This token is like a password for your bot. Don't share it.)""")
    if not AUTO:
        webbrowser.open("https://discord.com/developers/applications")
    token = "" if AUTO else secret_input("bot token", env.get("DISCORD_BOT_TOKEN", ""), check_discord_token,
                                         "DISCORD_BOT_TOKEN")
    if not token:
        say("No working token, skipping. Run 'Set up Discord.bat' any time to try again.")
        return
    env["DISCORD_BOT_TOKEN"] = token
    write_env(env)  # keep it even if pairing below gets interrupted
    say(f"""
  3. Now add the bot to a server you own so you can message it.
     A page is opening: pick your server, click {BOLD}Authorize{RESET}.
     (No server? In Discord click the + on the left > Create My Own > For me and my friends.)""")
    webbrowser.open(remote_discord.invite_url(token))
    if not AUTO:
        input("\n  Press Enter once the bot is in your server...")
    say(f"""
  4. In Discord, find the bot in your server's member list, click it, and choose {BOLD}Message{RESET}.""")
    paired = remote_discord.pair(token)
    if not paired:
        say("Didn't get the code in time. Run setup.bat again to try once more.")
        return
    owner_id, owner_name = paired
    env["DISCORD_BOT_TOKEN"] = token
    env["DISCORD_OWNER_ID"] = str(owner_id)
    env["REMOTE"] = "on"
    say(f"\n  Paired with {owner_name}. Only that Discord account can control Jarvis.")


def step_apps():
    title("5. Getting to know this PC")
    import apps
    import routines

    found = apps.scan()
    games = [a["name"] for a in found if a["target"].startswith("steam://")]
    say(f"Found {len(found)} apps" + (f" and {len(games)} Steam games" if games else "") + ".")
    routines.load()  # creates a starter routine if none exist yet
    say("You can open any of them by voice, e.g. \"Hey Jarvis, open " + (games[0] if games else "Chrome") + "\".")


def step_startup():
    title("6. Start with Windows")
    if yes("Start Jarvis automatically when you turn on the PC?", True):
        run([sys.executable, str(HERE / "startup.py"), "on"])


def finish():
    title("All set!")
    say(f"""How to use Jarvis:
  • Say {BOLD}"Hey Jarvis"{RESET} (or clap twice), wait for it to answer, then talk normally.
  • Try: "open Discord", "what's the weather in Denver", "set a 10 minute timer",
         "play some music", "close Discord", "remember that I like pizza".
  • Teach it: "make a routine called gaming that opens Discord and Steam" — then just say "gaming".
  • If it doesn't know an app, it asks you, and remembers your answer.

To change anything later, run {BOLD}setup.bat{RESET} again.""")
    if yes("\nStart Jarvis now?", True):
        run([sys.executable, str(HERE / "startup.py"), "start"])
        say("Jarvis is running. Say \"Hey Jarvis\"!")


def main():
    env = read_env()
    if "--mic" in sys.argv:  # just the microphone check: python setup.py --mic
        os.environ.update(env)
        run([sys.executable, str(HERE / "startup.py"), "stop"])  # free the mic while testing
        step_mic(env)
        write_env(env)
        run([sys.executable, str(HERE / "startup.py"), "start"])
        say("\nJarvis restarted. Say \"Hey Jarvis\" to try it.")
        return
    if "--discord" in sys.argv:  # just (re)do the Discord part: python setup.py --discord
        step_remote(env, standalone=True)
        write_env(env)
        if env.get("DISCORD_OWNER_ID"):
            say("\nRestarting Jarvis so it picks this up...")
            run([sys.executable, str(HERE / "startup.py"), "stop"])
            run([sys.executable, str(HERE / "startup.py"), "start"])
            say("Done. Text your bot \"help\" to see what it can do.")
        return
    say(f"{CYAN}{BOLD}\n   J A R V I S   setup{RESET}\n   A few quick questions and you're done.")
    # Save after every step, so closing the window part-way never loses earlier answers.
    for step in (step_name, step_brain, step_voice, step_display, step_mic):
        step(env)
        write_env(env)
    if not AUTO:
        step_remote(env)
        write_env(env)
    os.environ.update(env)
    step_apps()
    if not AUTO:
        step_startup()
        finish()
    else:
        say("\n(defaults mode: skipped start-with-Windows and starting Jarvis)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nSetup cancelled. Run setup.bat to start again.")
