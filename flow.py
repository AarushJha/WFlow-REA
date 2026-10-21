import os
import sys
import time
import subprocess
import tempfile
import threading
import numpy as np
import sounddevice as sd
import soundfile as sf
import requests
from pynput import keyboard

# Load API Key from .env if present
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# Global API Key configuration (can also be overridden via GROQ_API_KEY environment variable or .env)
DEFAULT_GROQ_API_KEY = "your_groq_api_key_here"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", DEFAULT_GROQ_API_KEY)

# ================= Configuration =================
SAMPLE_RATE = 16000
CHANNELS = 1
# =================================================

audio_buffer = []
is_recording = False
stream = None
lock = threading.Lock()

# Try loading native macOS Quartz for low-level CGEvent key posting
try:
    import Quartz
    HAS_QUARTZ = True
except ImportError:
    HAS_QUARTZ = False


def audio_callback(indata, frames, time_info, status):
    if is_recording:
        audio_buffer.append(indata.copy())


def start_recording():
    global is_recording, audio_buffer, stream
    with lock:
        if is_recording:
            return
        is_recording = True
        audio_buffer = []
        print("\n" + "=" * 50)
        print("🔴 [RECORDING...] Speak clearly now.")
        print("   Press F8 (or Fn+F8) or press [ENTER] to stop & paste.")
        print("=" * 50)

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=audio_callback
        )
        stream.start()
    except Exception as e:
        print(f"❌ Error accessing microphone: {e}")
        is_recording = False


def stop_and_transcribe():
    global is_recording, stream
    with lock:
        if not is_recording:
            return
        is_recording = False

    print("\n⏳ [STOPPED] Processing audio with Groq Whisper...")
    if stream:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    if not audio_buffer:
        print("⚠️ No audio data captured.")
        return

    audio_data = np.concatenate(audio_buffer, axis=0)
    duration_sec = len(audio_data) / SAMPLE_RATE

    if duration_sec < 0.3:
        print(f"⚠️ Audio duration too short ({duration_sec:.2f}s). Skipping.")
        return

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        temp_wav_path = f.name
        sf.write(temp_wav_path, audio_data, SAMPLE_RATE)

    try:
        t0 = time.time()
        text = transcribe_with_groq(temp_wav_path)
        latency = time.time() - t0
    finally:
        if os.path.exists(temp_wav_path):
            os.remove(temp_wav_path)

    if text:
        print(f"⚡ [Groq Whisper] Transcribed in {latency:.2f}s:")
        print(f"   👉 \"{text}\"")
        insert_text_into_active_app(text)
    else:
        print("⚠️ No speech recognized in audio.")


def transcribe_with_groq(wav_file_path):
    """Sends audio to Groq's high-speed Whisper Large v3 endpoint."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(wav_file_path, "rb") as f:
            files = {
                "file": ("audio.wav", f, "audio/wav"),
                "model": (None, "whisper-large-v3"),
                "temperature": (None, "0.0")
            }
            res = requests.post(url, headers=headers, files=files, timeout=12)

        if res.status_code == 200:
            return res.json().get("text", "").strip()
        else:
            print(f"❌ Groq API error {res.status_code}: {res.text}")
            return None
    except Exception as e:
        print(f"❌ Network/Request error: {e}")
        return None


def insert_text_into_active_app(text):
    """
    Wispr Flow paste strategy:
    1. Backup existing clipboard content.
    2. Write transcription to clipboard.
    3. Simulate Cmd + V into focused app.
    4. Restore original clipboard.
    """
    # 1. Backup clipboard
    old_clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout

    # 2. Write new text to clipboard
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
    p.communicate(text)

    # 3. Paste via native Quartz CGEvent (fallback to AppleScript)
    pasted = False
    if HAS_QUARTZ:
        try:
            kVK_ANSI_V = 9  # Virtual key code for 'v'
            event_down = Quartz.CGEventCreateKeyboardEvent(None, kVK_ANSI_V, True)
            Quartz.CGEventSetFlags(event_down, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)

            event_up = Quartz.CGEventCreateKeyboardEvent(None, kVK_ANSI_V, False)
            Quartz.CGEventSetFlags(event_up, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)
            pasted = True
        except Exception as e:
            print(f"⚠️ Quartz paste failed ({e}), trying AppleScript...")

    if not pasted:
        script = 'tell application "System Events" to keystroke "v" using {command down}'
        subprocess.run(["osascript", "-e", script])

    # 4. Restore original clipboard
    time.sleep(0.18)
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
    p.communicate(old_clip)
    print("📋 [Clipboard] Pasted into active app & original clipboard restored.\n")


def toggle_recording():
    if not is_recording:
        start_recording()
    else:
        stop_and_transcribe()


def on_press(key):
    # Support F8 (or Fn+F8)
    if key == keyboard.Key.f8:
        toggle_recording()


def terminal_input_listener():
    """Fallback: allows pressing ENTER in the terminal to toggle recording."""
    while True:
        try:
            input()
            toggle_recording()
        except (EOFError, KeyboardInterrupt):
            break


def main():
    print("=" * 65)
    print("      🚀 WFlow — High-Speed Voice Dictation Prototype")
    print("=" * 65)
    print("• Speech Model : Groq Whisper-Large-v3 (Cloud <300ms)")
    print(f"• API Key       : {GROQ_API_KEY[:8]}...{GROQ_API_KEY[-4:]} (Active)")
    print("• Hotkey        : Press F8 (or Fn+F8 on MacBook keyboard)")
    print("• Terminal Mode : You can ALSO press [ENTER] in this window to record!")
    print("=" * 65)
    print("System active. Focus any app and press F8 (or press ENTER here)...\n")

    # Start terminal ENTER listener in a background thread
    t = threading.Thread(target=terminal_input_listener, daemon=True)
    t.start()

    # Start global hotkey listener
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting WFlow.")
        sys.exit(0)
