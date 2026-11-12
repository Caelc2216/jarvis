"""Is the microphone actually working? Windows never asks permission for desktop programs like Jarvis:
if "Let desktop apps access your microphone" is off, the mic just returns silence and nothing errors.
This finds that (and a muted / wrong mic) and says exactly what to fix.
"""

import os
import winreg

import numpy as np

SETTINGS_URL = "ms-settings:privacy-microphone"
SOUND_URL = "ms-settings:sound"
_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _consent(root, key) -> str:
    try:
        with winreg.OpenKey(root, key) as k:
            return str(winreg.QueryValueEx(k, "Value")[0])
    except OSError:
        return ""


def privacy_block() -> str:
    """Which Windows privacy switch is off, in words; '' if none."""
    if _consent(winreg.HKEY_LOCAL_MACHINE, _STORE) == "Deny":
        return "Microphone access is turned off for this whole PC"
    if _consent(winreg.HKEY_CURRENT_USER, _STORE) == "Deny":
        return "'Let apps access your microphone' is turned off"
    if _consent(winreg.HKEY_CURRENT_USER, _STORE + r"\NonPackaged") == "Deny":
        return "'Let desktop apps access your microphone' is turned off"
    return ""


SKIP = ("sound mapper", "primary sound capture", "stereo mix", "what u hear", "wave out", "loopback")


def list_inputs() -> list[tuple[int, str]]:
    """Real microphones (one entry per name), as (device index, name)."""
    import sounddevice as sd

    seen, out = set(), []
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].strip()
        if d["max_input_channels"] < 1 or any(s in name.lower() for s in SKIP):
            continue
        if sd.query_hostapis(d["hostapi"])["name"] != "MME":  # same mics repeat under each driver type
            continue
        key = name.lower()[:31]
        if key not in seen:
            seen.add(key)
            out.append((i, name))
    return out


def device_from_env() -> int | None:
    """The mic chosen in setup (MIC_DEVICE=part of its name), or None for Windows' default."""
    want = os.getenv("MIC_DEVICE", "").strip().lower()
    if not want:
        return None
    for i, name in list_inputs():
        if want in name.lower():
            return i
    return None


def sample(seconds: float = 2.0, device=None) -> tuple[float, bool, str]:
    """Record briefly: (peak level 0..1, all exactly zero?, error message)."""
    import sounddevice as sd

    try:
        data = sd.rec(int(16000 * seconds), samplerate=16000, channels=1, dtype="int16", device=device)
        sd.wait()
    except Exception as e:
        return 0.0, False, str(e)
    pcm = data[:, 0].astype(np.int32)
    return float(np.abs(pcm).max()) / 32768, not pcm.any(), ""


def diagnose(device=None, seconds: float = 2.0) -> tuple[bool, str]:
    """(working?, what to tell the user). 'Working' only needs the mic to deliver real sound."""
    blocked = privacy_block()
    if blocked:
        return False, (f"I can't hear you: {blocked} in Windows. Open Settings, Privacy and security, "
                       "Microphone, and turn on all three switches, especially 'Let desktop apps access "
                       "your microphone'.")
    peak, all_zero, error = sample(seconds, device)
    if error:
        return False, f"I couldn't open a microphone ({error}). Check one is plugged in and chosen in Sound settings."
    if all_zero:
        return False, ("My microphone is giving me complete silence. It may be muted, or the wrong mic is "
                       "selected. Check Settings, System, Sound, Input.")
    return True, f"Microphone OK (level {peak:.2f})."
