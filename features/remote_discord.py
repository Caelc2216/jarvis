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

import home

HELP = ("Text me like you'd talk to me: \"open Steam\", \"what's playing?\", \"pause\", \"set a timer for 20 "
        "minutes\", \"what windows are open?\", \"close Discord\".\n"
        "Extras: **screenshot** (see your screens), **status**, **stats** (speed and CPU/memory), **help**.")

_client = None
_loop: asyncio.AbstractEventLoop | None = None
_owner_id = 0

# Alexa -> Jarvis: the private Alexa skill posts "<@bot> message" through one Discord webhook
# (Set up Alexa.bat). Only messages from THAT webhook count, and they're only ever read out loud,
# never run as commands (anyone near the Echo can talk to it).
ALEXA_STATE = str(home.DATA / "alexa_seen.json")
ALEXA_CATCH_UP_MIN = 15  # after a restart/sleep, read messages up to this old; skip older ones


def _alexa_ids() -> tuple[str, str]:
    return os.getenv("ALEXA_WEBHOOK_ID", "").strip(), os.getenv("ALEXA_CHANNEL_ID", "").strip()


def _alexa_text(client, message) -> str:
    text = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "")
    return " ".join(text.split())[:300]


def _alexa_seen(mid: int | None = None) -> int:
    """The newest Alexa message already read out (so a restart doesn't repeat it)."""
    import json

    if mid is not None:
        try:
            with open(ALEXA_STATE, "w", encoding="utf-8") as f:
                json.dump({"last": mid}, f)
        except OSError:
            pass
        return mid
    try:
        with open(ALEXA_STATE, encoding="utf-8") as f:
            return int(json.load(f).get("last", 0))
    except (OSError, ValueError):
        return 0


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


def _make_client(on_owner_message, on_mention=None, on_alexa=None):
    import discord

    intents = discord.Intents.default()  # DMs carry their text without the privileged content intent

    class JarvisBot(discord.Client):
        async def on_ready(self):
            print(f"  (Discord remote ready as {self.user})")
            if on_alexa:
                await self._alexa_catch_up()

        async def _alexa_catch_up(self):
            """Read Alexa messages that arrived while Jarvis was off or the PC slept (recent ones only)."""
            hook, channel_id = _alexa_ids()
            if not (hook and channel_id):
                return
            try:
                import datetime

                channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                since = discord.utils.utcnow() - datetime.timedelta(minutes=ALEXA_CATCH_UP_MIN)
                last = _alexa_seen()
                async for m in channel.history(after=since, oldest_first=True, limit=20):
                    if str(m.webhook_id or "") == hook and m.id > last:
                        await on_alexa(self, m)
            except Exception as e:
                if "Missing Access" in str(e) or "50001" in str(e):
                    print("  (Alexa: the bot can't see the Alexa channel. It's private and the bot isn't allowed in: "
                          "channel settings > Permissions > add the bot with View Channel + Read Message History)")
                else:
                    print(f"  (couldn't check for missed Alexa messages: {e})")

        async def on_message(self, message):
            hook, _ = _alexa_ids()
            if on_alexa and hook and message.webhook_id and str(message.webhook_id) == hook:
                if message.id > _alexa_seen():
                    await on_alexa(self, message)
                return
            if message.author.bot:
                return
            if isinstance(message.channel, discord.DMChannel):
                await on_owner_message(self, message)
            elif self.user in message.mentions and on_mention:
                # Bots only see server messages that @mention them. Used for pairing,
                # and to point people to DMs (commands are never run from a server channel).
                await on_mention(self, message)

    return JarvisBot(intents=intents)


def start(ask, describe_status, announce=None) -> None:
    """Run the bot in the background. ask(text) -> reply text, using its own conversation.
    announce(text): read a message from Alexa out loud (if the Alexa link is set up)."""
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
            elif low in ("stats", "performance", "speed"):
                import metrics

                await message.channel.send("```\n" + metrics.summary(24)[:1900] + "\n```")
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

    async def alexa(client, message):
        _alexa_seen(message.id)
        text = _alexa_text(client, message)
        if not text:
            return
        print(f"  (message from Alexa: {text})")
        if announce:
            asyncio.get_running_loop().run_in_executor(None, announce, text)
        try:
            await message.add_reaction("✅")  # shows in Discord that Jarvis got it
        except Exception:
            pass

    def run():
        global _client, _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _client = _make_client(handle, mentioned, alexa if announce else None)
        while True:  # reconnect forever (Wi-Fi drops, sleep/wake)
            try:
                _loop.run_until_complete(_client.start(token, reconnect=True))
            except Exception as e:
                print(f"  (Discord remote stopped: {e}; retrying in 60 s)")
                if "Improper token" in str(e) or "401" in str(e):
                    return  # a bad token won't fix itself
                time.sleep(60)
                _client = _make_client(handle, mentioned, alexa if announce else None)

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
