"""Jarvis: "Hey Jarvis" -> spoken welcome -> talk to Claude, ChatGPT or a local model.

Run:   python jarvis.py           (say "Hey Jarvis", then talk)
       python jarvis.py --text    (skip mic, type to Jarvis)
       python jarvis.py --no-clap (skip waiting for the wake word, start talking right away)
Stop:  Ctrl+C, or say/type "goodbye".

What Jarvis can do lives in tools.py. The AI can only use those tools:
it cannot run commands or touch files beyond its own memory.json.
"""

import argparse
import io
import json
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

import home

# In the background there's no window, so send all output (including a crash while starting up,
# like a missing package) to jarvis.log before anything else can fail.
if "--background" in sys.argv:
    _log = open(home.LOGS / "jarvis.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
    print(f"\n--- Jarvis started {time.strftime('%Y-%m-%d %H:%M')} (Python {sys.version.split()[0]}) ---")

    def _crash(kind, value, tb):
        import traceback

        print("JARVIS CRASHED:\n" + "".join(traceback.format_exception(kind, value, tb)))

    sys.excepthook = _crash

import numpy as np
from dotenv import load_dotenv

load_dotenv(home.ENV)

import hud  # noqa: E402
import updater  # noqa: E402
import remote_discord  # noqa: E402
import mic_check  # noqa: E402
import metrics  # noqa: E402
import routines  # noqa: E402
import coder  # noqa: E402
import speech  # noqa: E402
import tools  # noqa: E402  (needs .env loaded first)
from brains import Router  # noqa: E402

# ---------------------------------------------------------------- settings --
# Opened automatically when you clap (names from APPS in tools.py).
WELCOME_ACTIONS = [a.strip() for a in os.getenv("WELCOME_ACTIONS", "apple music").split(",") if a.strip()]
_name = os.getenv("JARVIS_USER_NAME", "").strip()
WELCOME_LINE = os.getenv("WELCOME_LINE", f"Welcome back{', ' + _name if _name else ''}. All systems are online.")

SAMPLE_RATE = 16000
BLOCK_MS = 20
SPIKE_RATIO = 6.0      # a clap must be this many times louder than the room
MIN_RMS = 0.04         # ...and at least this loud overall
CLAP_GAP = (0.12, 0.8) # seconds allowed between the two claps
CLAP_DECAY = 0.35      # a clap drops below this fraction of its peak...
CLAP_MAX_BLOCKS = 4    # ...within this many 20 ms blocks (speech stays loud longer)
SILENCE_RMS = 0.015    # the most it will ever take to count as talking (normally it adapts lower)
SPEECH_MIN_RMS = 0.003 # the least: anything quieter is never treated as speech
SILENCE_S = 1.5        # stop recording after this much real silence (noise-gated mics go silent between words)
MAX_LISTEN_S = 15

_speak_lock = threading.Lock()


# ------------------------------------------------------------------ speech --
# When the ElevenLabs credits run out, Jarvis says so ONCE per month (remembered in voice_state.json,
# so restarts and updates don't repeat it) and skips ElevenLabs until the credits reset.
VOICE_STATE = home.DATA / "voice_state.json"


def _voice_state() -> dict:
    try:
        return json.loads(VOICE_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _credits_reset_at() -> float:
    """When this month's ElevenLabs credits come back (0 if ElevenLabs won't say)."""
    try:
        import requests

        r = requests.get("https://api.elevenlabs.io/v1/user/subscription", timeout=8,
                         headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]})
        return float(r.json().get("next_character_count_reset_unix", 0)) if r.ok else 0.0
    except Exception:
        return 0.0


def _elevenlabs_paused() -> bool:
    """Out of credits and not reset yet? (Checks again hourly in case the plan was topped up.)"""
    st = _voice_state()
    return time.time() < st.get("retry_at", 0)


def _credits_ran_out() -> str:
    """Remember it, and return the notice to give the user, or "" if they've already been told."""
    st, now = _voice_state(), time.time()
    reset = st.get("reset_at", 0) if st.get("reset_at", 0) > now else _credits_reset_at()
    already = st.get("told_at") and (now < st.get("reset_at", 0) if st.get("reset_at") else
                                     now - st["told_at"] < 30 * 86400)
    st.update(reset_at=reset, retry_at=min(reset, now + 3600) if reset > now else now + 3600)
    if not already:
        st["told_at"] = now
    try:
        VOICE_STATE.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass
    if already:
        return ""
    when = f" on {time.strftime('%B %d', time.localtime(reset)).replace(' 0', ' ')}" if reset > now else ""
    return f"I've used up this month's ElevenLabs voice credits, so I'll use the Windows voice until they reset{when}."


def speak(text: str) -> None:
    print(f"Jarvis: {text}")
    if not text:
        return
    # The display shows the whole answer; the voice says the first few sentences (saves voice credits).
    say, portion = speech.spoken(text, display_on=hud.active())
    hud.said(speech.tidy(text) or text)
    if not say:
        return
    with _speak_lock:  # timers can speak from another thread
        if os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID") and not _elevenlabs_paused():
            try:
                _speak_elevenlabs(say, portion)
                if VOICE_STATE.exists():  # it works again (reset or topped up): forget the old notice
                    VOICE_STATE.unlink(missing_ok=True)
                return
            except Exception as e:
                print(f"  (ElevenLabs failed: {e}; using built-in voice)")
                if "quota" in str(e).lower() or "credits" in str(e).lower():
                    note = _credits_ran_out()
                    if note:
                        say = f"{note} {say}"
                        remote_discord.notify(note + " Upgrading the ElevenLabs plan brings the voice back sooner.")
        import pyttsx3

        engine = pyttsx3.init()
        hud.speaking(len(say.split()) / 2.6, portion)  # about 2.6 words a second
        engine.say(say)
        engine.runAndWait()


def _raise_with_reason(r) -> None:
    """Like raise_for_status, but include ElevenLabs' own explanation so jarvis.log says what's wrong."""
    if r.ok:
        return
    try:
        detail = r.json().get("detail", r.text)
        detail = detail.get("message", detail) if isinstance(detail, dict) else detail
    except ValueError:
        detail = r.text
    raise RuntimeError(f"{r.status_code}: {str(detail)[:300]}")


def _speak_elevenlabs(text: str, portion: float = 1.0) -> None:
    import requests
    import sounddevice as sd

    voice = os.environ["ELEVENLABS_VOICE_ID"]
    t0 = time.time()
    # A hiccup (Wi-Fi blip, ElevenLabs busy for a moment) shouldn't drop to the Windows voice: try again.
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                params={"output_format": "pcm_22050"},
                headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                json={
                    "text": text,
                    "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
                    "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.2},
                },
                timeout=(5, 20),  # connect, then read
            )
            if r.status_code == 429 or r.status_code >= 500:  # busy / their side: worth retrying
                _raise_with_reason(r)
            break
        except (requests.ConnectionError, requests.Timeout, RuntimeError) as e:
            if attempt == 2:
                raise
            print(f"  (ElevenLabs hiccup: {e}; trying again)")
            time.sleep(0.7 * (attempt + 1))
    _raise_with_reason(r)  # anything else (bad key, out of credits) won't fix itself by retrying
    TIMING["tts_s"] = time.time() - t0  # how long until the voice can start
    audio = np.frombuffer(r.content, dtype=np.int16)
    hud.speaking(len(audio) / 22050, portion)
    try:
        sd.play(audio, 22050)
        sd.wait()
    except sd.PortAudioError as e:
        # The speakers changed under us (sleep/wake, Bluetooth, headphones): reconnect and play it again.
        print(f"  (speaker problem: {e}; reconnecting audio)")
        sd._terminate()
        sd._initialize()
        sd.play(audio, 22050)
        sd.wait()


TIMING: dict = {}  # timings of the current reply, for metrics


def ensure_microphone() -> None:
    """Make sure Jarvis can actually hear. Windows never prompts for desktop apps, so a blocked mic
    looks like silence. If it's blocked: say so, open the right Settings page, and wait until it's fixed."""
    import sounddevice as sd

    chosen = mic_check.device_from_env()
    if chosen is not None:
        sd.default.device = (chosen, sd.default.device[1])
        print(f"  (using microphone: {sd.query_devices(chosen)['name']})")
    blocked = mic_check.privacy_block()
    if not blocked:
        ok, msg = mic_check.diagnose(chosen)
        print(f"  ({msg})")  # silence alone could just be a quiet room / noise gate, so only log it
        return
    _, msg = mic_check.diagnose(chosen)
    print(f"  (microphone blocked: {blocked})")
    remote_discord.notify("Heads up: " + msg)
    speak(msg + " I've opened that page for you.")
    os.startfile(mic_check.SETTINGS_URL)
    while mic_check.privacy_block():
        hud.state("speaking", "Turn on microphone access in Settings › Privacy & security › Microphone.")
        time.sleep(10)
    speak("Thanks, I can hear you now. Say Hey Jarvis whenever you need me.")


def watch_updates() -> None:
    """Check GitHub for a newer Jarvis shortly after starting, then once a day."""
    time.sleep(60)
    told = ""
    while True:
        try:
            status = updater.check()
            new = updater.update_available()
            if new:
                print(f"  (update: {status})")
                hud.activity(f"Update available · v{new}")
                if new != told:
                    told = new
                    remote_discord.notify(f"Jarvis {new} is out. Text me \"update yourself\" to install it.")
        except Exception as e:
            print(f"  (update check failed: {e})")
        time.sleep(24 * 3600)


def announce_update() -> None:
    """Back from an update: say whether it worked (out loud, and by Discord text)."""
    try:
        news = updater.news_to_announce()
    except Exception as e:
        print(f"  (couldn't read the update result: {e})")
        return
    if not news:
        return
    hud.activity(news.split(".")[0])

    def text_it():  # the Discord bot needs a few seconds to connect after a restart
        for _ in range(30):
            if remote_discord._client and remote_discord._client.is_ready():
                remote_discord.notify(news)
                return
            time.sleep(2)

    threading.Thread(target=text_it, daemon=True).start()
    try:
        speak(news)
    except Exception as e:
        print(f"  (couldn't say the update result: {e})")
    hud.state("idle")


def _chime() -> None:
    """A soft two-note chime so an announcement doesn't start mid-word."""
    try:
        import sounddevice as sd

        rate = 22050
        notes = []
        for freq in (880, 1320):
            t = np.linspace(0, 0.18, int(rate * 0.18), endpoint=False)
            notes.append(0.25 * np.sin(2 * np.pi * freq * t) * np.exp(-t * 9))
        sd.play(np.concatenate(notes).astype(np.float32), rate)
        sd.wait()
    except Exception:
        pass


def announce_from_alexa(text: str) -> None:
    """Someone said "Alexa, tell Jarvis ...": read it out (announcements only; never run as a command)."""
    was_asleep = hud.mode() == "idle"
    hud.activity(f"Alexa · {text[:40]}")
    with _speak_lock:
        _chime()
    speak(f"Message from Alexa: {text}")
    if was_asleep:
        hud.state("idle", wake_hint() if hud.input_mode() == "voice" else None)


def watch_timers() -> None:
    """Speak up when a timer set by any brain is done."""
    while True:
        for label in tools.pop_due_timers():
            was_asleep = hud.mode() == "idle"
            remote_discord.notify(f"⏰ Your {label} is done.")
            speak(f"Excuse me, your {label} is done.")  # pops the HUD up while it talks
            if was_asleep:  # then tuck it away again
                hud.state("idle", wake_hint())
        time.sleep(1)


# ------------------------------------------------------------------ hearing --
WAKE_WORD = os.getenv("WAKE_WORD", "on").lower() not in ("off", "0", "false", "no")
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
WAKE_FRAMES = int(os.getenv("WAKE_FRAMES", "4"))  # 80 ms frames in a row above the threshold
WAKE_CLAP = os.getenv("WAKE_CLAP", "off").lower() in ("on", "1", "true", "yes")  # double clap to wake
# PERFORMANCE=lite: for laptops / slower PCs. Checks things less often and skips the on-screen display.
LITE = os.getenv("PERFORMANCE", "").lower() == "lite"
if LITE:
    os.environ["HUD"] = os.getenv("LITE_HUD", "off")  # the display costs a browser window; LITE_HUD=on keeps it
    os.environ.setdefault("MUSIC_WATCH_S", "15")
_wake_model = None


def _load_wake_model():
    """The open-source openWakeWord 'hey jarvis' model, running locally on the CPU."""
    global _wake_model
    if _wake_model is None and WAKE_WORD:
        try:
            from openwakeword.model import Model

            d = home.ROOT / "models"
            _wake_model = Model(wakeword_models=[str(d / "hey_jarvis_v0.1.onnx")], inference_framework="onnx",
                                melspec_model_path=str(d / "melspectrogram.onnx"),
                                embedding_model_path=str(d / "embedding_model.onnx"))
        except Exception as e:
            print(f"  (wake word unavailable, clap only: {e})")
    return _wake_model


def wake_hint() -> str:
    """What to say/do to wake Jarvis, for the HUD and messages."""
    model, clap = _load_wake_model(), _clap_enabled()
    if model and clap:
        return "Say “Hey Jarvis” or clap twice."
    return "Say “Hey Jarvis”." if model else "Clap twice to wake me."


def _clap_enabled() -> bool:
    # Off unless WAKE_CLAP=on, but always on if the wake word can't load (so Jarvis can still be woken).
    return WAKE_CLAP or _load_wake_model() is None


def wait_for_wake() -> str:
    """Block until "Hey Jarvis" (or a double clap, if enabled). Returns "hey jarvis" or "clap".
    In text mode the microphone is off: this waits (mic closed) until voice mode is back."""
    while True:
        if hud.input_mode() == "text":
            print("  (text mode: microphone off)")
            while hud.input_mode() == "text":
                time.sleep(0.3)
            print("  (voice mode)")
        heard = _listen_for_wake()
        if heard:
            return heard


def _listen_for_wake() -> str | None:
    """None means text mode was switched on while listening."""
    import sounddevice as sd

    model = _load_wake_model()
    clap = _clap_enabled()
    gain = round(mic_gain(), 1)
    frame = 1280                                   # 80 ms, what the wake word model expects
    sub = int(SAMPLE_RATE * BLOCK_MS / 1000)       # 20 ms pieces for clap detection
    ambient, last_clap, clock, streak = 0.01, -10.0, 0.0, 0
    spike = None  # (time, peak, blocks since) of a loud moment that might be a clap
    if model:
        model.reset()
    hud.state("idle", wake_hint() if hud.input_mode() == "voice" else None)
    hud.set_info(routine="")
    print("Listening for " + " or ".join(filter(None, ['"Hey Jarvis"' if model else "", "a double clap" if clap else ""]))
          + "... (Ctrl+C to quit)")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, blocksize=frame, dtype="int16") as stream:
        while True:
            data, _ = stream.read(frame)
            if hud.input_mode() == "text":
                return None  # closes the microphone
            pcm = data[:, 0]
            if model:
                heard_pcm = pcm if gain == 1.0 else \
                    np.clip(pcm.astype(np.int32) * gain, -32768, 32767).astype(np.int16)  # quiet mic: turn it up
                score = model.predict(heard_pcm)["hey_jarvis_v0.1"]
                streak = streak + 1 if score >= WAKE_THRESHOLD else 0
                if streak >= WAKE_FRAMES:
                    print("  (heard: Hey Jarvis)")
                    return "hey jarvis"
            if not clap:
                continue
            for i in range(0, frame, sub):             # clap check, using audio time not wall time
                clock += sub / SAMPLE_RATE
                rms = float(np.sqrt(np.mean((pcm[i:i + sub] / 32768.0) ** 2)))
                if spike:
                    t0, peak, n = spike
                    if rms < peak * CLAP_DECAY:        # died away fast: a clap, not a syllable
                        spike = None
                        gap = t0 - last_clap
                        if CLAP_GAP[0] <= gap <= CLAP_GAP[1]:
                            print("  (heard: double clap)")
                            return "clap"
                        last_clap = t0
                    elif n + 1 >= CLAP_MAX_BLOCKS:     # stayed loud: speech or music, ignore it
                        spike, last_clap = None, -10.0
                    else:
                        spike = (t0, max(peak, rms), n + 1)
                elif rms > MIN_RMS and rms > ambient * SPIKE_RATIO:
                    spike = (clock, rms, 0)
                else:
                    ambient = 0.95 * ambient + 0.05 * rms  # slowly track room noise


wait_for_double_clap = wait_for_wake  # old name


_speech_peak = 0.35  # how loud the user's speech usually arrives (learned), used to boost quiet mics


def _speech_threshold(noise_floor: float) -> float:
    """Loudness that counts as talking: just above this room's background noise, never above the old fixed line."""
    return min(SILENCE_RMS, max(SPEECH_MIN_RMS, noise_floor * 4))


def mic_gain() -> float:
    """How much to turn up the mic for the wake word (MIC_GAIN in .env overrides; else learned)."""
    forced = os.getenv("MIC_GAIN", "").strip()
    if forced:
        return max(0.1, float(forced))
    return float(np.clip(0.35 / max(_speech_peak, 1e-3), 1.0, 6.0))


def record_until_silence() -> bytes | None:
    """Record one utterance and return it as WAV bytes (None if nothing said).

    Works with quiet mics and mics with a noise gate: the speech threshold follows the room's
    background noise, it waits for a real pause before stopping, and quiet audio is turned up.
    """
    global _speech_peak
    import sounddevice as sd

    if hud.input_mode() == "text":  # switched to text mode mid-conversation
        return None
    block = int(SAMPLE_RATE * BLOCK_MS / 1000)
    chunks, heard, quiet, loud_run = [], False, 0.0, 0
    noise = None
    start = time.time()
    print("  (listening...)")
    hud.state("listening")  # keep the last reply on screen, so a long one can still be read and scrolled
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, blocksize=block, dtype="int16") as stream:
        while time.time() - start < MAX_LISTEN_S:
            data, _ = stream.read(block)
            chunks.append(data.copy())
            rms = float(np.sqrt(np.mean((data / 32768.0) ** 2)))
            if noise is None or (not heard and rms < (noise * 3 + 1e-4)):  # learn the room while nobody talks
                noise = rms if noise is None else 0.9 * noise + 0.1 * rms
            threshold = _speech_threshold(noise)
            if len(chunks) % 5 == 0:  # ~10 times a second, make the orb follow your voice
                hud.level(rms / max(threshold * 6, 1e-3))
            if rms > threshold:
                loud_run += 1
                if loud_run >= 2:  # two blocks in a row: talking, not a click
                    heard, quiet = True, 0.0
            else:
                loud_run = 0
                if heard:
                    quiet += BLOCK_MS / 1000
                    if quiet >= SILENCE_S:
                        break
                elif time.time() - start > 8:
                    return None  # nobody spoke
    if not heard:
        return None
    audio = np.concatenate(chunks)[:, 0].astype(np.float32) / 32768.0
    peak = float(np.abs(audio).max()) or 1e-6
    _speech_peak = 0.7 * _speech_peak + 0.3 * peak  # remember how loud this user's mic is
    gain = float(np.clip(0.6 / peak, 1.0, 12.0))     # turn quiet recordings up for better recognition
    print(f"  (speech level {peak:.3f}, room {noise:.4f}, boosted x{gain:.1f})")
    pcm = np.clip(audio * gain * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


_el_stt_off = False


def transcribe(wav: bytes) -> str:
    # ElevenLabs or OpenAI speech-to-text if you have a key, else Google's free recognizer.
    global _el_stt_off
    if os.getenv("ELEVENLABS_API_KEY") and not _el_stt_off:
        import requests

        try:
            r = requests.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                data={"model_id": "scribe_v1"},
                files={"file": ("speech.wav", wav, "audio/wav")},
                timeout=30,
            )
            _raise_with_reason(r)
            return r.json().get("text", "").strip()
        except Exception as e:
            print(f"  (ElevenLabs speech-to-text failed: {e})")
            if str(e).startswith(("401", "403")):  # the key can't do this; stop paying the delay every time
                _el_stt_off = True
                print("  (not using ElevenLabs speech-to-text until restart; give the key 'Speech to Text' access to use it)")
    if os.getenv("OPENAI_API_KEY"):
        from openai import OpenAI

        f = io.BytesIO(wav)
        f.name = "speech.wav"
        return OpenAI().audio.transcriptions.create(model="whisper-1", file=f).text.strip()
    import speech_recognition as sr

    with sr.AudioFile(io.BytesIO(wav)) as source:
        audio = sr.Recognizer().record(source)
    try:
        return sr.Recognizer().recognize_google(audio)
    except sr.UnknownValueError:
        return ""


# --------------------------------------------------------------------- main --
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", action="store_true", help="type instead of using the mic")
    parser.add_argument("--no-clap", action="store_true", help="skip the clap trigger")
    parser.add_argument("--background", action="store_true",
                        help="run hidden (used when starting with Windows); logs go to jarvis.log")
    args = parser.parse_args()


    brain = Router()
    hud.start({"ollama": "Ollama · local", "claude": "Claude · Pro plan", "codex": "ChatGPT · Codex",
               "chatgpt": "ChatGPT · Codex", "claude-api": "Claude · API", "openai": "ChatGPT · API"}
              .get(brain.default, brain.default))
    print(f"Brain: {brain.default}   (say 'ask Claude ...' or 'ask ChatGPT ...' to switch for one question)")
    threading.Thread(target=watch_timers, daemon=True).start()
    threading.Thread(target=watch_updates, daemon=True).start()
    threading.Thread(target=metrics.watch_resources, args=(300 if LITE else 60,), daemon=True).start()

    # Texting Jarvis over Discord: its own conversation, so it never mixes with what's said out loud.
    remote_brain, remote_lock = Router(), threading.Lock()

    def remote_ask(text: str) -> str:
        with remote_lock:
            t0 = time.time()
            reply = remote_brain.ask(text)
            metrics.record("reply", via="discord", brain=getattr(remote_brain, "last", "?"),
                           think_s=time.time() - t0, chars=len(reply or ""))
            return reply

    def remote_status() -> str:
        playing = hud._state.get("playing")
        return (f"Jarvis {updater.local()['version']} is running on your PC. Brain: {brain.default}. "
                f"Right now: {hud.mode()}."
                + (f" Playing: {playing['title']} by {playing['artist']}." if playing else ""))

    remote_discord.start(remote_ask, remote_status, announce_from_alexa)
    threading.Thread(target=serve_typed, args=(brain,), daemon=True).start()  # text mode on the display
    hud.approval_handler = ask_permission  # code mode: Claude Code asks before risky commands
    hud.stop_handler = coder.cancel
    _sync_coding_chip()

    def listen() -> str | None:
        """One thing the user says (None means they stayed quiet)."""
        if args.text:
            hud.state("listening", "Type in the terminal…")
            typed = input("You: ").strip()
            if typed:
                hud.heard(typed)
            return typed
        wav = record_until_silence()
        if wav is None:
            return None
        t0 = time.time()
        heard = transcribe(wav)
        TIMING.clear()
        TIMING["stt_s"] = time.time() - t0
        print(f"You: {heard}")
        if heard:
            hud.heard(heard)
        return heard

    global _voice_listen
    _voice_listen = listen
    if not args.text:
        try:
            ensure_microphone()
        except Exception as e:
            print(f"  (microphone check failed: {e})")

    announce_update()
    conversation = _conversation_factory(args, brain, listen)
    first_wake = True
    while True:
        try:
            first_wake = conversation(first_wake)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # never let one bad moment (e.g. the mic vanishing during sleep) end Jarvis
            import sounddevice as sd

            if isinstance(e, sd.PortAudioError) or "PortAudio" in type(e).__name__:
                recover_audio(e)
            else:
                print(f"  (unexpected error, carrying on: {e!r})")
                time.sleep(3)
        if (args.text or args.no_clap) and not args.background:
            return


def recover_audio(error) -> None:
    """The mic or speakers disappeared (sleep/wake, USB unplugged, driver reset).
    Wait for an input device to come back, then reconnect to the audio system."""
    import sounddevice as sd

    print(f"  (audio device problem: {error}; waiting for the microphone to come back)")
    hud.state("idle", "Waiting for the microphone to come back…")
    waited = 0
    while True:
        time.sleep(3)
        waited += 3
        try:
            sd._terminate()           # PortAudio only notices new/changed devices after a restart
            sd._initialize()
            chosen = mic_check.device_from_env()
            if chosen is not None:
                sd.default.device = (chosen, sd.default.device[1])
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, blocksize=1280, dtype="int16") as s:
                s.read(1280)          # can we actually record?
            print(f"  (microphone is back after {waited} s: {sd.query_devices(kind='input')['name']})")
            hud.state("idle", wake_hint())
            return
        except Exception as e:
            if waited % 60 == 0:
                print(f"  (still waiting for a microphone after {waited} s: {e})")


def record_reply(brain, heard: str, reply: str) -> None:
    """Log how long this answer took, and show it in the HUD's activity list."""
    stt, think, tts = TIMING.get("stt_s", 0.0), TIMING.get("think_s", 0.0), TIMING.get("tts_s", 0.0)
    total = stt + think + tts
    metrics.record("reply", via="voice", brain=getattr(brain, "last", "?"), stt_s=stt, think_s=think,
                   tts_s=tts, response_s=total, words=len(heard.split()), chars=len(reply or ""))
    print(f"  (reply took {total:.1f} s: hearing {stt:.1f} · thinking {think:.1f} · voice {tts:.1f})")
    hud.activity(f"Reply in {total:.1f} s (thinking {think:.1f})")


BRAIN_LOCK = threading.Lock()  # voice and typed messages take turns with the same conversation
_asked = {"until": 0.0}  # Jarvis's brain just asked the user a question


def _brain_asked(reply: str) -> None:
    """Remember if the reply ended with a question, so the answer goes back to the brain
    ("Which folder?" -> "algorithms homework" must not start the school routine)."""
    _asked["until"] = time.time() + 120 if (reply or "").rstrip().rstrip(")").endswith("?") else 0.0


def _answering() -> bool:
    """Is this message (probably) the answer to the question Jarvis just asked? Checked once."""
    answering, _asked["until"] = time.time() < _asked["until"], 0.0
    return answering


# ----------------------------------------------------------------- code mode --
_voice_listen = None  # main()'s listen(), for asking permission out loud
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|allow|allowed|go ahead|do it|okay|ok|fine|approve|please do)\b", re.I)
NO = re.compile(r"^\s*(no|nope|nah)\b|\b(don'?t|do not|deny|denied|stop|cancel|never|not allowed|block)\b", re.I)


def _yes_no(text: str) -> bool | None:
    if not text:
        return None
    if NO.search(text):
        return False
    return True if YES.search(text) else None


def ask_permission(question: str, say: str = "") -> tuple[bool, str]:
    """Claude Code wants to do something risky: ask the user (buttons on the display, and out loud
    in voice mode). Anything other than yes/no is passed back to Claude Code as the user's
    instructions (e.g. "make it a file instead"). No answer within 5 minutes means no."""
    answered, ev = {}, threading.Event()

    def done(allowed: bool, note: str = "") -> None:
        if "allow" not in answered:
            answered.update(allow=allowed, note=note)
        ev.set()

    aid = hud.show_approval(question, done)
    remote_discord.notify(f"{coder.label()} wants to {question}. Answer on the Jarvis display.")
    try:
        if hud.input_mode() == "voice" and _voice_listen:
            speak(f"{coder.label()} wants to {say or question}. Should I allow it?")
            for _ in range(2):
                if ev.is_set():
                    break
                said = (_voice_listen() or "").strip()
                verdict = _yes_no(said)
                if verdict is not None:
                    done(verdict)
                elif len(said.split()) >= 3:  # a change of plan: tell Claude Code instead of running it
                    done(False, f"The user said: {said}")
                    speak(f"Okay, I'll tell {coder.label()}.")
                elif said:
                    speak("Sorry, yes or no?")
        else:  # text mode: a typed yes / no works as well as the buttons
            other = []
            deadline = time.time() + 300
            while not ev.is_set() and time.time() < deadline:
                typed = hud.next_typed(timeout=0.5)
                if typed is None:
                    continue
                verdict = _yes_no(typed)
                if verdict is not None:
                    done(verdict)
                elif len(typed.split()) >= 3:
                    done(False, f"The user said: {typed}")
                else:
                    other.append(typed)
            for t in other:  # not an answer: keep it for after
                hud._typed.put(t)
        ev.wait(timeout=300)
    finally:
        hud.clear_approval(aid)
    allowed = answered.get("allow", False)
    hud.activity(("Allowed: " if allowed else "Not allowed: ") + (say or question)[:40])
    hud.state("thinking", "Back to work…" if allowed else "Finding another way…")
    return allowed, answered.get("note", "") if "allow" in answered else "no answer in time"


def _sync_coding_chip() -> None:
    hud.set_info(coding=coder.project_name() if coder.active() else "", coder_label=coder.label())


def handle_code(text: str, say, listen) -> bool:
    """Code mode requests. Returns True if it handled the message."""
    text = coder._heard(text).strip()  # "cloud code" -> "Claude code"
    who, plain = coder.addressed(text)  # "Jarvis, ..." / "Claude, ..." / "Codex, ..."
    m = re.match(r"^\W*(?:(?:hey|ok|okay)[,\s]+)?(?:(?:tell|ask)\s+)?(codex|claude)\b", text, re.I)
    named = (m.group(1).lower() if m else None) if who == "claude" else None  # which one they addressed
    query = coder.match_start(plain) if who != "claude" else None
    if query is not None:
        folder, options = coder.find_project(query) if query else (None, [])
        if not folder and query and not options:
            return False  # e.g. "help me code in Python": not a project, let the brain answer
        if not folder:
            try:
                folder = routines.ask_for_project({"projects_folders": coder._roots(),
                                                   "ask": "Which project should we code on?"}, say, listen)
            except routines.RoutineCancelled:
                say("Okay, never mind.")
                return True
            if not folder:
                return True
        say(coder.start(folder, fresh=bool(re.search(r"\b(?:new|fresh)\b", plain, re.I)),
                        tool=coder.tool_mentioned(plain)))
        _sync_coding_chip()
        return True
    if not coder.active():
        # "back to Claude" / "continue coding" / "Codex, fix the tests": straight back into the last project.
        back = coder.BACK_TO_CODE.match(text) or re.fullmatch(r"(?:claude(?: code)?|codex)[.!?]*", text, re.I)
        if (back or who == "claude") and coder.last_project():
            wanted = named or ("codex" if back and re.search(r"\bcodex\b", text, re.I) else
                               "claude" if back and re.search(r"\bclaude\b", text, re.I) else None)
            if wanted and wanted != coder.backend():
                hud.activity(coder.set_tool(wanted))
            note = coder.resume()
            _sync_coding_chip()
            if who == "claude" and not back:
                hud.activity(note)
                _code_request(plain, say)
            else:
                say(note + " What's next?")
            return True
        return False
    _sync_coding_chip()
    if coder.BACK_TO_JARVIS.match(text) or (who == "jarvis" and re.match(r"(?:you )?take (?:it )?over\b", plain, re.I)):
        say(coder.stop())
        _sync_coding_chip()
        return True
    if (sw := coder.TOOL_SWITCH.match(plain)) and who != "claude":  # "switch to Codex" / "use Claude instead"
        say(coder.set_tool(sw.group(1)))
        _sync_coding_chip()
        return True
    if who == "claude":
        if named and named != coder.backend():  # "Codex, ..." while Claude Code is in use: switch, then do it
            hud.activity(coder.set_tool(named))
            _sync_coding_chip()
        _code_request(plain, say)
        return True
    current = coder.active()
    target, fresh = coder.switch_target(plain)
    if coder.STOP_RE.search(plain):
        say(coder.stop())
        _sync_coding_chip()
    elif coder.UNDO_RE.match(plain):
        say(coder.undo())
    elif target and (Path(target) != Path(current) or fresh):
        say(coder.start(target, fresh=fresh))  # switch projects (or start over in this one)
        _sync_coding_chip()
    elif coder.TAKEOVER_RE.search(plain):
        say(coder.open_terminal())
    elif (coder.SHOW_RE.search(plain) or (target and re.search(r"\b(?:live|terminal|cli)\b", plain, re.I))) \
            and len(plain.split()) <= 20:
        say(coder.show_live())
    elif target:
        say(f"We're already working on {coder.project_name()}. What would you like done?")
    elif who == "jarvis" or coder.for_jarvis(plain):
        print("  (code mode: this one's for Jarvis)")
        hud.activity(f"For Jarvis, not {coder.label()}")
        return False  # the normal Jarvis handles it; code mode stays on
    else:
        _code_request(plain, say)
    return True


def _code_request(text: str, say) -> None:
    hud.activity(f"To {coder.label()}")
    hud.set_info(coding_busy=True, coder_label=coder.label())
    hud.state("thinking", f"Working on it in {coder.label()}…")
    t0 = time.time()
    try:
        reply = coder.ask(text, on_progress=lambda line: hud.state("thinking", line + "…"))
    finally:
        hud.set_info(coding_busy=False)
    print(f"  (code request took {time.time() - t0:.0f} s)")
    say(reply or "Done.")


def serve_typed(brain) -> None:
    """Text mode: answer messages typed on the display. Replies show on screen, and are also
    spoken if "Read replies aloud" is on."""

    def say(text: str) -> None:
        if hud.read_aloud():
            speak(text)
            hud.state("idle")
        else:
            print(f"Jarvis: {text}")
            hud.replied(speech.tidy(text) or text)

    def next_message() -> str:  # for routines that ask a follow-up (like which project to open)
        hud.state("idle")
        return hud.next_typed(timeout=120) or ""

    while True:
        text = hud.next_typed()
        if not text:
            continue
        print(f"You (typed): {text}")
        hud.heard(text)
        try:
            if handle_code(text, say, next_message):
                pass
            elif name := routines.match_save(text):
                routines.save_layout(name, say)
            elif name := routines.match(text, strict=_answering()):
                routines.run(name, say, next_message)
            else:
                t0 = time.time()
                with BRAIN_LOCK:
                    reply = brain.ask(text)
                think = time.time() - t0
                metrics.record("reply", via="text", brain=getattr(brain, "last", "?"), think_s=think,
                               chars=len(reply or ""))
                _brain_asked(reply)
                say(reply or "Sorry, I didn't get anything back there.")
                hud.activity(f"Reply in {think:.1f} s")
        except Exception as e:
            print(f"  (error: {e})")
            say("Sorry, something went wrong there. The details are in the log.")
        hud.set_info(routine="")


def _conversation_factory(args, brain, listen):
    """Builds the wake -> talk cycle used by main(); split out so errors can be recovered from."""
    state = {"told_about_update": ""}

    def conversation(first_wake: bool) -> bool:
        if not (args.text or args.no_clap):
            wait_for_wake()
        if first_wake:
            for a in WELCOME_ACTIONS:
                tools.open_app(a)
            ask = routines.load().get("ask_on_wake", "")
            new = updater.update_available()
            if new and new != state["told_about_update"]:  # mention a new version once, then leave it be
                state["told_about_update"] = new
                ask = f"By the way, version {new} is out; say 'update yourself' whenever you like. {ask}"
            speak(f"{WELCOME_LINE} {ask}".strip())
            first_wake = False
        else:
            speak("Yes?")

        # One conversation: lasts until you go quiet or say goodbye.
        while True:
            heard = listen()
            if heard is None:
                if hud.input_mode() == "text":
                    break  # switched to typing: the text-mode handler takes it from here
                speak("I'll be here if you need me.")
                break
            if not heard:
                continue
            if heard.lower().strip(" .!") in ("goodbye", "bye", "exit", "quit", "goodbye jarvis"):
                speak("Goodbye.")
                break
            try:
                if handle_code(heard, speak, listen):
                    continue
                if name := routines.match_save(heard):
                    routines.save_layout(name, speak)
                elif name := routines.match(heard, strict=_answering()):
                    routines.run(name, speak, listen)
                else:
                    t0 = time.time()
                    with BRAIN_LOCK:  # typed messages share this conversation
                        reply = brain.ask(heard)
                    TIMING["think_s"] = time.time() - t0
                    TIMING.pop("tts_s", None)
                    _brain_asked(reply)
                    speak(reply)
                    record_reply(brain, heard, reply)
            except Exception as e:
                print(f"  (error: {e})")
                speak("Sorry, something went wrong there. The details are in the log.")
        return False  # after the first conversation, later wakes just say "Yes?"

    return conversation


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutting down.")
        sys.exit(0)
