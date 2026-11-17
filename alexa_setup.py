"""Connect an Amazon Echo to Jarvis: "Alexa, tell Jarvis that dinner is ready" -> Jarvis says it on the PC.

Run "Set up Alexa.bat". Needs the Discord remote set up first (Set up Discord.bat).
It asks for a Discord webhook link (made in a private channel of a server your Jarvis bot is in),
checks it, saves only the webhook's id and channel to .env, writes alexa_skill/lambda_function.py
(the code you paste into the Alexa developer console), and sends a test message to Jarvis.
"""

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

import setup
from setup import BOLD, CYAN, HERE, RESET, read_env, say, secret_input, title, write_env, yes

SKILL = HERE / "alexa_skill"
UA = {"User-Agent": "DiscordBot (jarvis-alexa-setup, 1.0)"}
WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/(\d+)/([\w-]+)$")


def _get(url: str, token: str = "") -> tuple[int, dict]:
    headers = dict(UA, **({"Authorization": f"Bot {token}"} if token else {}))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


PRIVATE_HELP = ("\n  The channel is private, and your Jarvis bot isn't allowed in it. In Discord: channel settings (gear)\n"
                "  > Permissions > Add members or roles > pick your Jarvis bot > allow View Channel,\n"
                "  Read Message History and Add Reactions > Save. Then run:  Set up Alexa.bat test")


def send_test() -> None:
    """'Set up Alexa.bat test': check the bot can read the channel, then post a test message."""
    env = read_env()
    import remote_discord

    token, channel = env.get("DISCORD_BOT_TOKEN", ""), env.get("ALEXA_CHANNEL_ID", "")
    out = SKILL / "lambda_function.py"
    if not (token and channel and out.exists()):
        say("  Alexa isn't set up yet: run Set up Alexa.bat without 'test' first.")
        return
    code, _ = _get(f"https://discord.com/api/v10/channels/{channel}/messages?limit=1", token)
    if code != 200:
        say(PRIVATE_HELP if code == 403 else f"  Discord said {code or 'no connection'}; try again in a minute.")
        return
    say("  ✓ Jarvis's bot can read the channel.")
    m = re.search(r'^WEBHOOK_URL = "([^"]+)"', out.read_text(encoding="utf-8"), re.M)
    bot_id = remote_discord.bot_id_from_token(token)
    subprocess.run([sys.executable, str(HERE / "startup.py"), "stop"], cwd=HERE, capture_output=True)
    subprocess.run([sys.executable, str(HERE / "startup.py"), "start"], cwd=HERE)
    import time

    time.sleep(12)
    body = json.dumps({"content": f"<@{bot_id}> This is a test from the Alexa setup.", "username": "Alexa",
                       "allowed_mentions": {"users": [bot_id]}}).encode()
    req = urllib.request.Request(m.group(1) + "?wait=true", data=body, method="POST",
                                 headers=dict(UA, **{"Content-Type": "application/json"}))
    urllib.request.urlopen(req, timeout=10).close()
    say("  Sent. You should hear: 'Message from Alexa: This is a test from the Alexa setup.'")


def main() -> None:
    if "test" in sys.argv[1:]:
        return send_test()
    title("Alexa -> Jarvis")
    env = read_env()
    token = env.get("DISCORD_BOT_TOKEN", "")
    if not token:
        say("  First set up the Discord remote: double-click 'Set up Discord.bat', then run this again.")
        return
    import remote_discord

    bot_id = remote_discord.bot_id_from_token(token)
    say("  1. In Discord, in a server your Jarvis bot is in (make a private one if you like),")
    say("     create a text channel, e.g. #alexa.")
    say("  2. Channel settings (gear) > Integrations > Webhooks > New Webhook > Copy Webhook URL.")
    info = {}

    def check(url: str) -> tuple[bool, str]:
        m = WEBHOOK_RE.match(url.split("?")[0].rstrip("/"))
        if not m:
            return False, "That isn't a webhook link. It starts with https://discord.com/api/webhooks/"
        code, data = _get(url)
        if code != 200:
            return False, f"Discord didn't accept that webhook ({code or 'no connection'}). Copy it again."
        code_g, _ = _get(f"https://discord.com/api/v10/guilds/{data.get('guild_id')}", token)
        if code_g != 200:
            return False, ("The webhook works, but your Jarvis bot isn't in that server. Invite it with:\n     "
                           + remote_discord.invite_url(token))
        info.update(id=data["id"], channel=data["channel_id"], url=url.split("?")[0].rstrip("/"))
        code_c, _ = _get(f"https://discord.com/api/v10/channels/{data['channel_id']}/messages?limit=1", token)
        if code_c != 200:
            say(PRIVATE_HELP)
            return True, "The webhook works, but Jarvis's bot can't see that channel yet (see above)."
        return True, f"Webhook '{data.get('name', 'webhook')}' works, and Jarvis's bot can read that channel."

    url = secret_input("Discord webhook link", "", check, "ALEXA_WEBHOOK_URL")
    if not url or not info:
        say("  Not set up. Run this again when you have the webhook link.")
        return

    env = read_env()
    env.update(ALEXA_WEBHOOK_ID=info["id"], ALEXA_CHANNEL_ID=info["channel"])
    env.pop("ALEXA_WEBHOOK_URL", None)  # never kept in .env: it only goes into the skill's code
    write_env(env)

    code = (SKILL / "lambda_function.template.py").read_text(encoding="utf-8")
    code = code.replace("__WEBHOOK_URL__", info["url"]).replace("__BOT_ID__", bot_id)
    out = SKILL / "lambda_function.py"
    out.write_text(code, encoding="utf-8")
    say(f"\n  {CYAN}✓ Saved. The skill's code is ready:{RESET} {out}")
    say("    (It contains the webhook link, so keep that file private. It's never uploaded anywhere by Jarvis.)")

    # Restart Jarvis so it starts listening for Alexa messages, then send a test.
    subprocess.run([sys.executable, str(HERE / "startup.py"), "stop"], cwd=HERE, capture_output=True)
    subprocess.run([sys.executable, str(HERE / "startup.py"), "start"], cwd=HERE)
    if yes("  Send a test message to Jarvis now? (Jarvis should say it out loud in a few seconds)", True):
        import time

        time.sleep(12)  # Jarvis connecting to Discord
        body = json.dumps({"content": f"<@{bot_id}> This is a test from the Alexa setup.", "username": "Alexa",
                           "allowed_mentions": {"users": [bot_id]}}).encode()
        req = urllib.request.Request(info["url"] + "?wait=true", data=body, method="POST",
                                     headers=dict(UA, **{"Content-Type": "application/json"}))
        try:
            urllib.request.urlopen(req, timeout=10).close()
            say("  Sent. You should hear: 'Message from Alexa: This is a test from the Alexa setup.'")
        except Exception as e:
            say(f"  Couldn't send the test ({e}).")

    say(f"\n{BOLD}  Last part: the Alexa skill (about 15 minutes){RESET}")
    say("   1. Go to https://developer.amazon.com/alexa/console/ask and sign in with the SAME Amazon")
    say("      account your Echo uses (create a developer account if asked; it's free).")
    say("   2. Create Skill > name: Jarvis > Other > Custom > Alexa-hosted (Python) > Start from Scratch.")
    say("   3. Build tab > Interaction Model > JSON Editor: replace everything with the contents of")
    say(f"      {SKILL / 'interaction_model.json'}   then Save and Build skill.")
    say(f"   4. Code tab: replace lambda_function.py with   {out}   then Save and Deploy.")
    say("   5. Test tab: set 'Skill testing is enabled in' to Development. Type: tell jarvis computer that dinner is ready")
    say("   Then on any Echo: 'Alexa, tell Jarvis Computer that dinner is ready.'")
    say("   (Alexa needs a two-word name; to use another, change invocationName in the JSON editor. Avoid")
    say("   home / house / everyone: 'tell home ...' starts Alexa's own announcement instead of the skill.)")


if __name__ == "__main__":
    main()
