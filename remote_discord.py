"""Text Jarvis from anywhere through a Discord bot.

- Only the paired owner can use it (DISCORD_OWNER_ID). Everyone else is ignored, silently.
- Direct messages only, never in servers.
- Same brain and same locked-down tools as voice. Replies are texted back, not spoken.
- "screenshot" sends a picture of the screens; Jarvis also texts you when timers finish
  or an update is out.

Settings in .env (setup.bat fills these in):  DISCORD_BOT_TOKEN, DISCORD_OWNER_ID, REMOTE=on|off
"""

import asyncio
import base64
import io
import os
import secrets
import threading
import time

HELP = ("Text me like you'd talk to me: \"open Steam\", \"what's playing?\", \"pause\", \"set a timer for 20 "
        "minutes\", \"what windows are open?\", \"close Discord\".\n"
        "Extras: **screenshot** (see your screens), **status**, **help**.")

_client = None
_loop: asyncio.AbstractEventLoop | None = None
_owner_id = 0


def enabled() -> bool:
    return (os.getenv("REMOTE", "on").lower() not in ("off", "0", "false", "no")
            and bool(os.getenv("DISCORD_BOT_TOKEN", "").strip()) and bool(os.getenv("DISCORD_OWNER_ID", "").strip()))


def bot_id_from_token(token: str) -> str:
    """A bot token starts with the bot's id, base64-encoded (used to build the invite link)."""
    part = token.split(".")[0]
    try:
        return base64.b64decode(part + "=" * (-len(part) % 4)).decode()
    except Exception:
        return ""


def invite_url(token: str) -> str:
    return f"https://discord.com/oauth2/authorize?client_id={bot_id_from_token(token)}&scope=bot&permissions=0"


def _chunks(text: str, size: int = 1900):
    text = text or "(no reply)"
    while text:
        yield text[:size]
        text = text[size:]


def _screenshot():
    import discord
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    if img.width > 3000:  # keep it under Discord's upload limit and quick on a phone
        img = img.resize((3000, int(img.height * 3000 / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    buf.seek(0)
    return discord.File(buf, filename=f"screens-{time.strftime('%H%M')}.jpg")


def _make_client(on_owner_message, on_mention=None):
    import discord

    intents = discord.Intents.default()  # DMs carry their text without the privileged content intent

    class JarvisBot(discord.Client):
        async def on_ready(self):
            print(f"  (Discord remote ready as {self.user})")

        async def on_message(self, message):
            if message.author.bot:
                return
            if isinstance(message.channel, discord.DMChannel):
                await on_owner_message(self, message)
            elif self.user in message.mentions and on_mention:
                # Bots only see server messages that @mention them. Used for pairing,
                # and to point people to DMs (commands are never run from a server channel).
                await on_mention(self, message)

    return JarvisBot(intents=intents)


def start(ask, describe_status) -> None:
    """Run the bot in the background. ask(text) -> reply text, using its own conversation."""
    global _client, _loop, _owner_id
    if not enabled():
        return
    token = os.environ["DISCORD_BOT_TOKEN"].strip()
    _owner_id = int(os.environ["DISCORD_OWNER_ID"].strip())

    async def handle(client, message):
        if message.author.id != _owner_id:
            return  # not the owner: ignore completely
        text = message.content.strip()
        low = text.lower().strip(" !.?")
        import hud

        hud.activity(f"Discord · {text[:40]}")
        try:
            if low in ("help", "?", "commands"):
                await message.channel.send(HELP)
            elif low in ("screenshot", "ss", "screen", "show me my screen", "send a screenshot"):
                await message.channel.send(file=await asyncio.get_running_loop().run_in_executor(None, _screenshot))
            elif low in ("status", "ping"):
                await message.channel.send(describe_status())
            elif text:
                async with message.channel.typing():
                    reply = await asyncio.get_running_loop().run_in_executor(
                        None, ask, f"(Texted from the user's phone; reply by text, a few sentences is fine.) {text}")
                for part in _chunks(reply):
                    await message.channel.send(part)
        except Exception as e:
            await message.channel.send(f"Sorry, that didn't work: {e}")

    async def mentioned(client, message):
        if message.author.id == _owner_id:  # only answer the owner, and only to say where to talk
            await message.reply("For privacy I only take commands in a direct message. Click my name > Message.")

    def run():
        global _client, _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _client = _make_client(handle, mentioned)
        while True:  # reconnect forever (Wi-Fi drops, sleep/wake)
            try:
                _loop.run_until_complete(_client.start(token, reconnect=True))
            except Exception as e:
                print(f"  (Discord remote stopped: {e}; retrying in 60 s)")
                if "Improper token" in str(e) or "401" in str(e):
                    return  # a bad token won't fix itself
                time.sleep(60)
                _client = _make_client(handle, mentioned)

    threading.Thread(target=run, daemon=True).start()


def notify(text: str) -> None:
    """Send the owner a DM (timers, updates...). Does nothing if the remote isn't running."""
    if not (_client and _loop and _owner_id and _client.is_ready()):
        return

    async def send():
        user = _client.get_user(_owner_id) or await _client.fetch_user(_owner_id)
        await user.send(text)

    try:
        asyncio.run_coroutine_threadsafe(send(), _loop)
    except Exception:
        pass


# ------------------------------------------------------------------ pairing --
def pair(token: str, timeout_s: int = 600) -> tuple[int, str] | None:
    """Used by setup: show a code, wait for someone to DM it to the bot, return (their id, name)."""
    import discord

    code = f"{secrets.randbelow(900000) + 100000}"
    result = {}

    async def handle(client, message):
        said = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
        if said == code:
            result["id"], result["name"] = message.author.id, str(message.author)
            await message.author.send("Paired! From now on I only listen to you, here in this direct message. "
                                      "Try: **what time is it?**")
            if not isinstance(message.channel, discord.DMChannel):
                await message.reply("Paired! I've sent you a direct message; talk to me there.")
            await client.close()

    async def main():
        client = _make_client(handle, handle)  # accept the code in a DM or as an @mention in a server

        async def stop_later():
            await asyncio.sleep(timeout_s)
            await client.close()

        asyncio.get_running_loop().create_task(stop_later())
        try:
            await client.start(token)
        except discord.LoginFailure:
            result["error"] = "That bot token didn't work. Copy it again (Bot > Reset Token)."

    print(f"\n  Pairing code:  {code}\n")
    print("  In Discord, send that code to your bot: click the bot's name > Message, and send it.")
    print("  (Typing @ and the bot's name with the code in a server channel works too.)")
    print(f"  (Waiting up to {timeout_s // 60} minutes...)")
    asyncio.run(main())
    if "error" in result:
        print("  " + result["error"])
        return None
    return (result["id"], result["name"]) if "id" in result else None
