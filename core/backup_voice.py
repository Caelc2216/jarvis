"""Jarvis's backup voice: Kokoro, a free voice that runs on this PC (no internet, no account).

Used when there's no ElevenLabs key, when this month's ElevenLabs credits are used up, or when
ElevenLabs can't be reached. The Windows voice is only the last resort, if Kokoro isn't ready.

Its files (~115 MB) live in data/kokoro and download by themselves the first time they're needed.
Settings in .env:  BACKUP_VOICE=kokoro|windows   KOKORO_VOICE=bm_george   KOKORO_SPEED=1.0
"""

import os
import re
import threading
import time
from pathlib import Path

import home

FOLDER = home.DATA / "kokoro"
URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {"kokoro-v1.0.int8.onnx": 90_000_000, "voices-v1.0.bin": 27_000_000}  # name: smallest valid size
MODEL, VOICES = (FOLDER / name for name in FILES)
RATE = 24000

_kokoro = None
_load_lock = threading.Lock()
_downloading = threading.Event()


def wanted() -> bool:
    return os.getenv("BACKUP_VOICE", "kokoro").strip().lower() != "windows"


def downloaded() -> bool:
    return all((FOLDER / n).exists() and (FOLDER / n).stat().st_size >= size for n, size in FILES.items())


def installed() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def ready() -> bool:
    return wanted() and installed() and downloaded()


def download(progress=None) -> bool:
    """Fetch the voice files (resumes nothing: a half file is simply fetched again). True if all there."""
    import requests

    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, size in FILES.items():
        dest = FOLDER / name
        if dest.exists() and dest.stat().st_size >= size:
            continue
        part = dest.with_suffix(dest.suffix + ".part")
        with requests.get(URL + name, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            total, got = int(r.headers.get("content-length", 0)), 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(name, got / total)
        if part.stat().st_size < size:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{name} came down incomplete")
        part.replace(dest)
    return downloaded()


def download_in_background() -> None:
    """Start getting the voice ready without holding anything up (once per run)."""
    if _downloading.is_set() or not wanted() or (installed() and downloaded()):
        return
    _downloading.set()

    def run():
        try:
            if not installed():
                import subprocess
                import sys

                py = Path(sys.executable)
                py = py.with_name(py.name.lower().replace("pythonw", "python"))
                subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "kokoro-onnx"],
                               creationflags=0x08000000, timeout=900)
            print("  (getting the backup voice ready: downloading about 115 MB once)")
            if download():
                print("  (backup voice ready)")
        except Exception as e:
            print(f"  (couldn't get the backup voice ready: {e}; using the Windows voice for now)")
        finally:
            _downloading.clear()

    threading.Thread(target=run, daemon=True).start()


def _engine():
    global _kokoro
    with _load_lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            t = time.time()
            _kokoro = Kokoro(str(MODEL), str(VOICES))
            print(f"  (backup voice loaded in {time.time() - t:.1f} s)")
        return _kokoro


def _voice(k) -> str:
    v = os.getenv("KOKORO_VOICE", "bm_george").strip() or "bm_george"
    return v if v in k.get_voices() else "bm_george"


def _chunks(text: str, first_max: int = 120) -> list[str]:
    """Sentences, with the first piece short so the voice starts quickly."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;])\s+", text) if p.strip()]
    out = []
    for p in parts:
        if out and len(out[-1]) + len(p) < (first_max if len(out) == 1 else 260):
            out[-1] += " " + p
        else:
            out.append(p)
    return out or [text]


def speak(text: str, on_start=None) -> None:
    """Say it out loud. Makes the next sentence while the current one plays.
    on_start(estimated_seconds) is called when the sound starts (for the display's scrolling)."""
    import numpy as np
    import sounddevice as sd

    k = _engine()
    voice, speed = _voice(k), float(os.getenv("KOKORO_SPEED", "1.0") or 1.0)
    pieces = _chunks(text)
    made: list = [None] * len(pieces)
    done = [threading.Event() for _ in pieces]

    def make():
        for i, piece in enumerate(pieces):
            try:
                samples, _ = k.create(piece, voice=voice, speed=speed, lang="en-gb")
                made[i] = np.asarray(samples, dtype=np.float32)
            except Exception as e:
                print(f"  (backup voice couldn't say part of that: {e})")
            done[i].set()

    threading.Thread(target=make, daemon=True).start()
    for i in range(len(pieces)):
        done[i].wait()
        audio = made[i]
        if audio is None or not len(audio):
            continue
        if i == 0 and on_start:
            first_s = len(audio) / RATE
            on_start(first_s * len(text) / max(len(pieces[0]), 1))  # whole reply, from the first piece's pace
        try:
            sd.play(audio, RATE)
            sd.wait()
        except sd.PortAudioError as e:  # speakers changed (sleep/wake, Bluetooth): reconnect and replay
            print(f"  (speaker problem: {e}; reconnecting audio)")
            sd._terminate()
            sd._initialize()
            sd.play(audio, RATE)
            sd.wait()
