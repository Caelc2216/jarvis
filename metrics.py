"""How fast and how heavy Jarvis is. Everything stays on this PC, in metrics.jsonl.

    python metrics.py          a report for the last 24 hours (or double-click "Jarvis Stats.bat")
    python metrics.py 168      ...for the last week

Recorded:
  reply      every answer: hearing (speech-to-text), thinking (brain), speaking (voice) and the total
             wait from when you stop talking to when Jarvis starts answering
  resources  every minute: CPU and memory of Jarvis, of Ollama (if running), and of the whole PC
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
FILE = HERE / "metrics.jsonl"
MAX_BYTES = 5_000_000
_lock = threading.Lock()


def record(kind: str, **fields) -> None:
    line = json.dumps({"t": round(time.time(), 1), "kind": kind,
                       **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in fields.items()}})
    with _lock:
        try:
            if FILE.exists() and FILE.stat().st_size > MAX_BYTES:  # keep it small: drop the older half
                lines = FILE.read_text(encoding="utf-8").splitlines()
                FILE.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
            with open(FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ------------------------------------------------------------ resources --
def _sample() -> dict:
    import psutil

    me = psutil.Process()
    jarvis_cpu = me.cpu_percent(interval=None)  # since the last call (≈ a minute)
    ollama = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("ollama")]
    ollama_mb = sum(p.memory_info().rss for p in ollama if p.is_running()) / 1e6
    ollama_cpu = sum(p.cpu_percent(interval=None) for p in ollama if p.is_running())
    return {"jarvis_cpu": jarvis_cpu / psutil.cpu_count(), "jarvis_mb": me.memory_info().rss / 1e6,
            "ollama_cpu": ollama_cpu / psutil.cpu_count(), "ollama_mb": ollama_mb,
            "pc_cpu": psutil.cpu_percent(interval=None), "pc_mem": psutil.virtual_memory().percent}


def watch_resources(every_s: int = 60) -> None:
    """Background thread: sample CPU/memory once a minute (cheap: a few milliseconds each time)."""
    try:
        import psutil

        psutil.Process().cpu_percent(interval=None)  # first call primes the counters
        psutil.cpu_percent(interval=None)
    except Exception:
        return
    while True:
        time.sleep(every_s)
        try:
            record("resources", **_sample())
        except Exception:
            pass


# --------------------------------------------------------------- report --
def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def summary(hours: float = 24, spoken: bool = False) -> str:
    since = time.time() - hours * 3600
    replies, res = [], []
    try:
        for line in FILE.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("t", 0) >= since:
                (replies if e["kind"] == "reply" else res if e["kind"] == "resources" else []).append(e)
    except FileNotFoundError:
        pass
    span = f"the last {int(hours)} hours" if hours < 48 else f"the last {round(hours / 24)} days"
    if not replies and not res:
        return f"No stats yet for {span}. Talk to Jarvis a bit and check again."

    def med(key, rows):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return _pct(vals, 50), _pct(vals, 90), len(vals)

    lines = [f"Jarvis stats for {span}:"]
    voice = [r for r in replies if r.get("via") == "voice"]
    if voice:
        total, total90, n = med("response_s", voice)
        stt, _, _ = med("stt_s", voice)
        think, think90, _ = med("think_s", voice)
        tts, _, _ = med("tts_s", voice)
        lines.append(f"  Voice replies: {n}. Typical wait after you stop talking: {total:.1f} s "
                     f"(slowest 1 in 10: {total90:.1f} s)")
        lines.append(f"    understanding you {stt:.1f} s · thinking {think:.1f} s · starting to speak {tts:.1f} s")
    brains = {}
    for r in replies:
        brains.setdefault(r.get("brain", "?"), []).append(r.get("think_s", 0))
    if brains:
        lines.append("  Thinking time by brain: " + ", ".join(
            f"{b} {_pct(v, 50):.1f} s ({len(v)}x)" for b, v in sorted(brains.items())))
    texts = [r for r in replies if r.get("via") == "discord"]
    if texts:
        lines.append(f"  Discord replies: {len(texts)}, typical {_pct([r['think_s'] for r in texts], 50):.1f} s")
    if res:
        jc = [r["jarvis_cpu"] for r in res]
        jm = [r["jarvis_mb"] for r in res]
        lines.append(f"  Jarvis itself: about {_pct(jc, 50):.1f}% CPU (peaks {max(jc):.0f}%), "
                     f"{_pct(jm, 50):.0f} MB memory")
        om = [r["ollama_mb"] for r in res if r.get("ollama_mb", 0) > 50]
        if om:
            oc = [r["ollama_cpu"] for r in res if r.get("ollama_mb", 0) > 50]
            lines.append(f"  Ollama (local brain): up to {max(om) / 1000:.1f} GB memory, peaks {max(oc):.0f}% CPU")
        pc = [r["pc_cpu"] for r in res]
        lines.append(f"  Whole PC: CPU typically {_pct(pc, 50):.0f}% (peaks {max(pc):.0f}%), "
                     f"memory {_pct([r['pc_mem'] for r in res], 50):.0f}% used")
    lines += _voice_credits()
    lines += _advice(replies, res)
    text = "\n".join(lines)
    if spoken:  # shorter, no symbols, for reading aloud
        text = text.replace("·", ",").replace("  ", " ")
    return text


def _voice_credits() -> list[str]:
    """How much of this month's ElevenLabs voice allowance is left (shared by every PC using the key)."""
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        return []
    try:
        import requests

        r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key}, timeout=8)
        if not r.ok:
            return []
        s = r.json()
        used, limit = s.get("character_count", 0), s.get("character_limit", 0) or 1
        reset = time.strftime("%b %d", time.localtime(s.get("next_character_count_reset_unix", 0)))
    except Exception:
        return []
    out = [f"  Voice credits (ElevenLabs): {used:,} of {limit:,} used this month ({used * 100 // limit}%), "
           f"resets {reset}"]
    if used >= limit * 0.8:
        out.append("  Tip: voice credits are nearly gone; Jarvis switches to the Windows voice when they run out. "
                   "Shorter questions use fewer, or upgrade the ElevenLabs plan.")
    return out


def _advice(replies, res) -> list[str]:
    tips = []
    local = [r for r in replies if r.get("brain") == "ollama"]
    om = [r["ollama_mb"] for r in res if r.get("ollama_mb", 0) > 50]
    if local and (_pct([r["think_s"] for r in local], 50) > 6 or (om and max(om) > 6000)):
        tips.append("  Tip: the local brain (Ollama) is slow or heavy on this PC. Switching to Claude or ChatGPT "
                    "(run setup.bat) moves the thinking to the cloud, so this PC stays fast.")
    stt = [r.get("stt_s", 0) for r in replies if r.get("via") == "voice"]
    if stt and _pct(stt, 50) > 2.5:
        tips.append("  Tip: understanding speech is slow; giving your ElevenLabs key 'Speech to Text' access "
                    "is usually faster and more accurate than the free recognizer.")
    if res and _pct([r["jarvis_cpu"] for r in res], 50) > 15:
        tips.append("  Tip: Jarvis is using a lot of CPU while idle. Set PERFORMANCE=lite in .env.")
    return tips


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24
    print(summary(hours))
    if os.environ.get("JARVIS_PAUSE"):
        input("\nPress Enter to close")
