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
import os
import sys
import threading
import time
import wave
from pathlib import Path

# In the background there's no window, so send all output (including a crash while starting up,
# like a missing package) to jarvis.log before anything else can fail.
if "--background" in sys.argv:
    _log = open(Path(__file__).with_name("jarvis.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
    print(f"\n--- Jarvis started {time.strftime('%Y-%m-%d %H:%M')} (Python {sys.version.split()[0]}) ---")

    def _crash(kind, value, tb):
        import traceback

        print("JARVIS CRASHED:\n" + "".join(traceback.format_exception(kind, value, tb)))

    sys.excepthook = _crash

import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))  # works no matter where it's started from

import hud  # noqa: E402
import updater  # noqa: E402
import remote_discord  # noqa: E402
import mic_check  # noqa: E402
import metrics  # noqa: E402
import routines  # noqa: E402
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
def speak(text: str) -> None:
    print(f"Jarvis: {text}")
    if not text:
        return
    hud.said(text)
    with _speak_lock:  # timers can speak from another thread
        if os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID"):
            try:
                return _speak_elevenlabs(text)
            except Exception as e:
                print(f"  (ElevenLabs failed: {e}; using built-in voice)")
        import pyttsx3

        engine = pyttsx3.init()
        hud.speaking(len(text.split()) / 2.6)  # about 2.6 words a second
        engine.say(text)
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


def _speak_elevenlabs(text: str) -> None:
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
    hud.speaking(len(audio) / 22050)
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

            d = Path(__file__).with_name("models")
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
    """Block until "Hey Jarvis" (or a double clap, if enabled). Returns "hey jarvis" or "clap"."""
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
    hud.state("idle", wake_hint())
    hud.set_info(routine="")
    print("Listening for " + " or ".join(filter(None, ['"Hey Jarvis"' if model else "", "a double clap" if clap else ""]))
          + "... (Ctrl+C to quit)")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, blocksize=frame, dtype="int16") as stream:
        while True:
            data, _ = stream.read(frame)
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

    block = int(SAMPLE_RATE * BLOCK_MS / 1000)
    chunks, heard, quiet, loud_run = [], False, 0.0, 0
    noise = None
    start = time.time()
    print("  (listening...)")
    hud.state("listening", "")
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

    remote_discord.start(remote_ask, remote_status)

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
                speak("I'll be here if you need me.")
                break
            if not heard:
                continue
            if heard.lower().strip(" .!") in ("goodbye", "bye", "exit", "quit", "goodbye jarvis"):
                speak("Goodbye.")
                break
            try:
                if name := routines.match_save(heard):
                    routines.save_layout(name, speak)
                elif name := routines.match(heard):
                    routines.run(name, speak, listen)
                else:
                    t0 = time.time()
                    reply = brain.ask(heard)
                    TIMING["think_s"] = time.time() - t0
                    TIMING.pop("tts_s", None)
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
