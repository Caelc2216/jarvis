"""Turn a reply into something worth saying out loud.

Brains sometimes answer like a web page (headings, bullet lists, emoji, links, a "Sources" list).
Reading that aloud sounds odd and, with ElevenLabs, every character costs voice credits. So:
  - the display gets the whole answer, tidied up;
  - the voice says only the first few sentences (SPEAK_MAX_CHARS) and points to the screen for the rest.
"""

import os
import re

SPEAK_MAX_CHARS = int(os.getenv("SPEAK_MAX_CHARS", "450"))

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")


def tidy(text: str) -> str:
    """The whole answer without markdown, emoji, links or a sources list (for the display and the voice)."""
    text = text or ""
    text = re.split(r"\n\s*\**\s*(?:sources?|references?|citations?)\s*:?\**\s*\n", text, flags=re.I)[0]
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]*\)", r"\1", text)   # [title](url) -> title
    text = re.sub(r"\(?https?://\S+\)?", "", text)                        # bare links
    text = _EMOJI.sub("", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s+", "", line)            # bullets and numbered lists
        line = re.sub(r"[*#`>|]|(?<!\w)_+|_+(?!\w)", "", line).strip()  # keep snake_case names
        if not line or re.fullmatch(r"[-=—–\s]+", line):
            continue
        if line[-1] not in ".!?:;,":
            line += "."                                                   # headings/bullets end a sentence
        lines.append(line)
    text = " ".join(lines)
    text = text.replace("→", " to ").replace("—", ", ").replace("–", "-")
    return re.sub(r"\s+", " ", text).replace(" ,", ",").replace(" .", ".").strip()


_QUOTED_PATH = re.compile(r"""["'`]((?:[A-Za-z]:|~|\.{1,2})?[\\/][^"'`\n]*)["'`]""")
# Folders in the middle of a path may contain spaces ("Jarvis MD\"); the last part may not.
_WIN_PATH = re.compile(r"""(?<![\w/])(?:[A-Za-z]:|~)[\\/](?:[^\\/\n"'`<>|]+[\\/])*[^\s\\/"'`<>|,;()]*""")
_REL_PATH = re.compile(r"""(?<![\w:/\\])(?:(?:[\w.-]+[\\/])+[\w-]+\.[A-Za-z0-9]{1,5}\b|(?:[\w.-]+[\\/]){2,}[\w.-]*)""")


def _name(path: str) -> str:
    """The part of a path worth hearing: 'C:\\Users\\me\\...\\Jarvis MD' -> 'Jarvis MD'."""
    parts = [p for p in re.split(r"[\\/]+", path.strip().rstrip(".")) if p and not p.endswith(":")]
    return parts[-1] if parts else path


def short_paths(text: str) -> str:
    """Say file and folder names, not whole paths (the display keeps the full ones)."""
    text = _QUOTED_PATH.sub(lambda m: _name(m.group(1)), text)
    text = _WIN_PATH.sub(lambda m: _name(m.group(0)), text)
    return _REL_PATH.sub(lambda m: _name(m.group(0)), text)


def spoken(text: str, display_on: bool = True, limit: int = SPEAK_MAX_CHARS) -> tuple[str, float]:
    """(what to say, the fraction of the tidied answer that gets said)."""
    full = short_paths(tidy(text))
    if len(full) <= limit:
        return full, 1.0
    cut = max(full.rfind(". ", 0, limit), full.rfind("! ", 0, limit), full.rfind("? ", 0, limit))
    if cut < limit // 3:  # no sentence end in a sensible place: cut at a word
        cut = full.rfind(" ", 0, limit)
    head = full[:cut + 1].strip()
    tail = " The rest is on screen." if display_on else " I'll stop there; ask if you'd like the rest."
    return head + tail, len(head) / len(full)
