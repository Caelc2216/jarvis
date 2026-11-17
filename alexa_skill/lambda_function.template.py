"""Jarvis Alexa skill: "Alexa, ask Jarvis Computer to say dinner is ready" -> Jarvis reads it out on the PC.

This is the skill's code. "Set up Alexa.bat" turns this template into lambda_function.py with your
two values filled in; copy THAT file into the skill's Code tab in the Alexa developer console.

How it works: Alexa sends the words to this code (hosted free by Amazon), which posts them to a
private Discord channel through a webhook. Jarvis's Discord bot sees the post and speaks it.
Nothing here can control the PC: Jarvis only ever reads these messages out.
No extra packages needed (plain Python standard library).
"""

import json
import urllib.request

WEBHOOK_URL = "__WEBHOOK_URL__"   # the Discord webhook (keep private: anyone with it can post)
BOT_ID = "__BOT_ID__"             # Jarvis's Discord bot (mentioning it is how the bot sees the message)


def _say(text, end=True, reprompt=None):
    response = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if reprompt:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": response}


def _send(message):
    body = json.dumps({"content": f"<@{BOT_ID}> {message}", "username": "Alexa",
                       "allowed_mentions": {"users": [BOT_ID]}}).encode()
    req = urllib.request.Request(WEBHOOK_URL + "?wait=true", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (jarvis-alexa-skill, 1.0)"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return 200 <= r.status < 300


def lambda_handler(event, context):
    request = event.get("request", {})
    kind = request.get("type")
    if kind == "LaunchRequest":
        return _say("What should I tell Jarvis? Start with say, for example: say dinner is ready.", end=False,
                    reprompt="Say, for example: say dinner is ready.")
    if kind != "IntentRequest":
        return _say("")  # session ended etc.

    intent = request.get("intent", {})
    name = intent.get("name")
    if name == "MessageIntent":
        message = ((intent.get("slots") or {}).get("message") or {}).get("value", "").strip()
        if not message:
            return _say("What should I tell Jarvis?", end=False, reprompt="Say, for example: say dinner is ready.")
        message = message[0].upper() + message[1:]
        try:
            ok = _send(message)
        except Exception:
            ok = False
        return _say("Okay, I've told Jarvis." if ok else "Sorry, I couldn't reach Jarvis just now.")
    if name == "AMAZON.HelpIntent":
        return _say("You can say: ask Jarvis Computer to say dinner is ready, and Jarvis will read it out on the computer.",
                    end=False, reprompt="What should I tell Jarvis?")
    if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NavigateHomeIntent"):
        return _say("Okay.")
    return _say("Sorry, I didn't catch that. Try: ask Jarvis Computer to say dinner is ready.")
