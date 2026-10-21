import os
import sys
import time
import json
import queue
import tempfile
import threading
import subprocess
import numpy as np
import sounddevice as sd
import soundfile as sf
import requests
import customtkinter as ctk

# Fix macOS 15+ Sequoia Carbon assertion crash in pynput
try:
    import contextlib
    import pynput._util.darwin as _pynput_darwin
    from pynput import keyboard
    import pynput.keyboard._darwin as _pynput_kd

    with _pynput_darwin.keycode_context() as _ctx:
        _CACHED_KEYCODE_CTX = _ctx

    @contextlib.contextmanager
    def _safe_keycode_context():
        yield _CACHED_KEYCODE_CTX

    _pynput_darwin.keycode_context = _safe_keycode_context
    _pynput_kd.keycode_context = _safe_keycode_context
except Exception:
    from pynput import keyboard

# ================= Configuration & Defaults =================
DEFAULT_GROQ_API_KEY = "your_groq_api_key_here"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
ENV_PATH = os.path.join(APP_DIR, ".env")

SAMPLE_RATE = 16000
CHANNELS = 1

try:
    import Quartz
    HAS_QUARTZ = True
except ImportError:
    HAS_QUARTZ = False


def load_env_key():
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "GROQ_API_KEY":
                            return v.strip().strip('"').strip("'")
        except Exception:
            pass
    return os.environ.get("GROQ_API_KEY", DEFAULT_GROQ_API_KEY)


def save_env_key(key):
    os.environ["GROQ_API_KEY"] = key
    lines = []
    found = False
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("GROQ_API_KEY="):
                        lines.append(f"GROQ_API_KEY={key}\n")
                        found = True
                    else:
                        lines.append(line)
        except Exception:
            pass
    if not found:
        lines.append(f"GROQ_API_KEY={key}\n")
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        print(f"Error saving to .env: {e}")


def load_settings():
    default_settings = {
        "groq_api_key": load_env_key(),
        "model": "whisper-large-v3-turbo",
        "auto_paste": True,
        "sound_feedback": True,
        "input_device": None,
        "always_on_top": False
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_settings.update(saved)
        except Exception:
            pass
    return default_settings


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"Error saving settings: {e}")


# ================= Voice Engine =================
class VoiceEngine:
    def __init__(self, event_queue):
        self.event_queue = event_queue
        self.settings = load_settings()
        self.is_recording = False
        self.audio_buffer = []
        self.stream = None
        self.lock = threading.Lock()
        self.start_time = 0.0
        self.current_volume = 0.0

    def play_sound(self, sound_name="Tink"):
        if not self.settings.get("sound_feedback", True):
            return
        path = f"/System/Library/Sounds/{sound_name}.aiff"
        if os.path.exists(path):
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def audio_callback(self, indata, frames, time_info, status):
        # NOTE: Runs on real-time CoreAudio thread. DO NOT call any GUI/Tcl methods here!
        if self.is_recording:
            self.audio_buffer.append(indata.copy())
            try:
                rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
                norm_vol = min(1.0, float(rms) / 3200.0)
                self.current_volume = norm_vol
            except Exception:
                pass
        else:
            self.current_volume = 0.0

    def start_recording(self):
        with self.lock:
            if self.is_recording:
                return
            self.is_recording = True
            self.audio_buffer = []
            self.start_time = time.time()
            self.current_volume = 0.0

        self.play_sound("Pop")
        self.event_queue.put(("state", "recording", None))

        device_id = self.settings.get("input_device")
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=device_id,
                callback=self.audio_callback
            )
            self.stream.start()
        except Exception as e:
            print(f"❌ Microphone error: {e}")
            with self.lock:
                self.is_recording = False
            self.event_queue.put(("state", "error", f"Mic error: {e}"))

    def stop_and_transcribe(self):
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            self.current_volume = 0.0

        self.play_sound("Tink")
        self.event_queue.put(("state", "transcribing", None))

        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass

        if not self.audio_buffer:
            self.event_queue.put(("state", "idle", None))
            return

        threading.Thread(target=self._process_audio_async, daemon=True).start()

    def _process_audio_async(self):
        audio_data = np.concatenate(self.audio_buffer, axis=0)
        duration_sec = len(audio_data) / SAMPLE_RATE

        if duration_sec < 0.35:
            self.event_queue.put(("state", "idle", None))
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            temp_wav_path = f.name
            sf.write(temp_wav_path, audio_data, SAMPLE_RATE)

        text = None
        latency = 0.0
        try:
            t0 = time.time()
            text = self._transcribe_groq(temp_wav_path)
            latency = time.time() - t0
        finally:
            if os.path.exists(temp_wav_path):
                os.remove(temp_wav_path)

        if text:
            did_paste = False
            if self.settings.get("auto_paste", True):
                did_paste = self.insert_text_into_active_app(text)
            self.event_queue.put(("transcription", text, latency, duration_sec, did_paste))
        else:
            self.event_queue.put(("state", "idle", None))

    def _transcribe_groq(self, wav_file_path):
        api_key = self.settings.get("groq_api_key") or load_env_key()
        model = self.settings.get("model", "whisper-large-v3-turbo")
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            with open(wav_file_path, "rb") as f:
                files = {
                    "file": ("audio.wav", f, "audio/wav"),
                    "model": (None, model),
                    "temperature": (None, "0.0")
                }
                res = requests.post(url, headers=headers, files=files, timeout=12)

            if res.status_code == 200:
                return res.json().get("text", "").strip()
            else:
                print(f"❌ Groq API error {res.status_code}: {res.text}")
                return None
        except Exception as e:
            print(f"❌ Network error: {e}")
            return None

    def insert_text_into_active_app(self, text):
        try:
            old_clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
            p.communicate(text)

            pasted = False
            if HAS_QUARTZ:
                try:
                    kVK_ANSI_V = 9
                    event_down = Quartz.CGEventCreateKeyboardEvent(None, kVK_ANSI_V, True)
                    Quartz.CGEventSetFlags(event_down, Quartz.kCGEventFlagMaskCommand)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)

                    event_up = Quartz.CGEventCreateKeyboardEvent(None, kVK_ANSI_V, False)
                    Quartz.CGEventSetFlags(event_up, Quartz.kCGEventFlagMaskCommand)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)
                    pasted = True
                except Exception:
                    pass

            if not pasted:
                script = 'tell application "System Events" to keystroke "v" using {command down}'
                subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            time.sleep(0.20)
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
            p.communicate(old_clip)
            return True
        except Exception as e:
            print(f"Paste error: {e}")
            return False

    def toggle_recording(self):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_and_transcribe()


# ================= CustomTkinter GUI =================
class WFlowGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("WFlow — Voice Dictation")
        self.geometry("540x620")
        self.minsize(480, 520)

        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color="#121316")

        self.is_mini_mode = False
        self.normal_geom = "540x620"
        self.history = self.load_history()

        # Thread-safe event queue for GUI updates
        self.event_queue = queue.Queue()

        # Voice Engine
        self.engine = VoiceEngine(event_queue=self.event_queue)

        # Build UI Components
        self.setup_ui()

        # Start UI event polling loop (handles cross-thread messages safely)
        self.process_queue_events()

        # Start timer & VU meter loop
        self.update_timer()

        # Global Hotkey Listener (F8)
        self.hotkey_listener = None
        try:
            self.hotkey_listener = keyboard.Listener(on_press=self.on_global_key)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
        except Exception as e:
            print(f"Global hotkey listener warning: {e}")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_history(self):
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save_history(self):
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history[:50], f, indent=2)
        except Exception:
            pass

    def setup_ui(self):
        # 1. Top Navigation & Header
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", padx=20, pady=(16, 8))

        self.title_label = ctk.CTkLabel(
            self.header_frame,
            text="🎙️ WFlow",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#f8fafc"
        )
        self.title_label.pack(side="left")

        self.status_badge = ctk.CTkLabel(
            self.header_frame,
            text="● Ready",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#10b981",
            fg_color="#064e3b",
            corner_radius=12,
            padx=12,
            pady=4
        )
        self.status_badge.pack(side="left", padx=12)

        # Mini Mode Toggle Button
        self.btn_mini = ctk.CTkButton(
            self.header_frame,
            text="📌 Mini HUD",
            width=90,
            height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#27272a",
            hover_color="#3f3f46",
            command=self.toggle_mini_mode
        )
        self.btn_mini.pack(side="right")

        # 2. Hero Recording Section Card
        self.hero_card = ctk.CTkFrame(self, fg_color="#1c1d22", corner_radius=16)
        self.hero_card.pack(fill="x", padx=20, pady=10)

        self.record_btn = ctk.CTkButton(
            self.hero_card,
            text="🎙️ Start Dictation (F8)",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=54,
            corner_radius=27,
            fg_color="#6366f1",
            hover_color="#4f46e5",
            command=self.engine.toggle_recording
        )
        self.record_btn.pack(fill="x", padx=20, pady=(18, 10))

        # Audio VU Level Visualizer Bar
        self.meter_frame = ctk.CTkFrame(self.hero_card, fg_color="transparent")
        self.meter_frame.pack(fill="x", padx=22, pady=(0, 14))

        self.meter_bar = ctk.CTkProgressBar(
            self.meter_frame,
            height=6,
            corner_radius=3,
            progress_color="#6366f1",
            fg_color="#27272a"
        )
        self.meter_bar.set(0.0)
        self.meter_bar.pack(fill="x", side="left", expand=True)

        self.duration_label = ctk.CTkLabel(
            self.meter_frame,
            text="Press F8 in any app",
            font=ctk.CTkFont(size=11),
            text_color="#71717a"
        )
        self.duration_label.pack(side="right", padx=(10, 0))

        # 3. Main Tabs: [Dictation, History, Settings]
        self.tabview = ctk.CTkTabview(self, fg_color="#18181b", segmented_button_selected_color="#6366f1")
        self.tabview.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self.tab_dictate = self.tabview.add("Dictation")
        self.tab_history = self.tabview.add("History")
        self.tab_settings = self.tabview.add("Settings")

        self.setup_dictate_tab()
        self.setup_history_tab()
        self.setup_settings_tab()

    def setup_dictate_tab(self):
        lbl = ctk.CTkLabel(
            self.tab_dictate,
            text="Latest Dictation Result:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#a1a1aa"
        )
        lbl.pack(anchor="w", padx=10, pady=(10, 4))

        self.txt_latest = ctk.CTkTextbox(
            self.tab_dictate,
            font=ctk.CTkFont(size=14),
            fg_color="#121316",
            text_color="#f1f5f9",
            wrap="word",
            height=120,
            corner_radius=12
        )
        self.txt_latest.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.txt_latest.insert("1.0", "Speak your thoughts... When done, speech will appear here and paste into your active window.")

        self.meta_frame = ctk.CTkFrame(self.tab_dictate, fg_color="transparent")
        self.meta_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.lbl_meta = ctk.CTkLabel(
            self.meta_frame,
            text="⚡ Groq Whisper-Large-v3-Turbo",
            font=ctk.CTkFont(size=11),
            text_color="#71717a"
        )
        self.lbl_meta.pack(side="left")

        self.btn_copy = ctk.CTkButton(
            self.meta_frame,
            text="📋 Copy",
            width=75,
            height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#27272a",
            hover_color="#3f3f46",
            command=self.copy_latest_to_clip
        )
        self.btn_copy.pack(side="right")

    def setup_history_tab(self):
        top_bar = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        top_bar.pack(fill="x", padx=10, pady=(8, 4))

        lbl = ctk.CTkLabel(top_bar, text="Recent Transcriptions", font=ctk.CTkFont(size=12, weight="bold"), text_color="#a1a1aa")
        lbl.pack(side="left")

        btn_clear = ctk.CTkButton(
            top_bar,
            text="Clear",
            width=60,
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="#3f3f46",
            hover_color="#ef4444",
            command=self.clear_history
        )
        btn_clear.pack(side="right")

        self.history_scroll = ctk.CTkScrollableFrame(self.tab_history, fg_color="#121316", corner_radius=10)
        self.history_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.refresh_history_ui()

    def refresh_history_ui(self):
        for widget in self.history_scroll.winfo_children():
            widget.destroy()

        if not self.history:
            empty = ctk.CTkLabel(
                self.history_scroll,
                text="No dictations yet.\nPress F8 anywhere to start speaking!",
                font=ctk.CTkFont(size=12),
                text_color="#52525b"
            )
            empty.pack(pady=40)
            return

        for item in reversed(self.history):
            card = ctk.CTkFrame(self.history_scroll, fg_color="#1e1e24", corner_radius=8)
            card.pack(fill="x", padx=6, pady=4)

            time_str = item.get("time", "")
            lat_str = f"{item.get('latency', 0):.2f}s"
            head = ctk.CTkLabel(
                card,
                text=f"{time_str} • {lat_str}",
                font=ctk.CTkFont(size=10),
                text_color="#71717a"
            )
            head.pack(anchor="w", padx=10, pady=(6, 2))

            body = ctk.CTkLabel(
                card,
                text=item.get("text", ""),
                font=ctk.CTkFont(size=12),
                text_color="#e4e4e7",
                wraplength=400,
                justify="left"
            )
            body.pack(anchor="w", padx=10, pady=(0, 6))

    def clear_history(self):
        self.history = []
        self.save_history()
        self.refresh_history_ui()

    def setup_settings_tab(self):
        settings_frame = ctk.CTkScrollableFrame(self.tab_settings, fg_color="transparent")
        settings_frame.pack(fill="both", expand=True, padx=10, pady=10)

        lbl_api = ctk.CTkLabel(settings_frame, text="Groq API Key:", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e4e4e7")
        lbl_api.pack(anchor="w", pady=(0, 2))

        self.entry_api = ctk.CTkEntry(
            settings_frame,
            placeholder_text="gsk_...",
            font=ctk.CTkFont(size=12),
            show="•",
            height=34
        )
        self.entry_api.insert(0, self.engine.settings.get("groq_api_key", ""))
        self.entry_api.pack(fill="x", pady=(0, 6))

        btn_save_key = ctk.CTkButton(
            settings_frame,
            text="Save API Key",
            height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#27272a",
            hover_color="#3f3f46",
            command=self.save_api_key
        )
        btn_save_key.pack(anchor="w", pady=(0, 14))

        lbl_model = ctk.CTkLabel(settings_frame, text="Speech Model:", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e4e4e7")
        lbl_model.pack(anchor="w", pady=(0, 2))

        self.model_combo = ctk.CTkComboBox(
            settings_frame,
            values=["whisper-large-v3-turbo", "whisper-large-v3"],
            height=32,
            command=self.on_model_change
        )
        self.model_combo.set(self.engine.settings.get("model", "whisper-large-v3-turbo"))
        self.model_combo.pack(fill="x", pady=(0, 14))

        lbl_device = ctk.CTkLabel(settings_frame, text="Microphone Input Device:", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e4e4e7")
        lbl_device.pack(anchor="w", pady=(0, 2))

        devices = self.get_input_devices()
        device_names = [d["name"] for d in devices] if devices else ["Default Microphone"]
        self.device_combo = ctk.CTkComboBox(
            settings_frame,
            values=device_names,
            height=32,
            command=self.on_device_change
        )
        curr_dev_id = self.engine.settings.get("input_device")
        matching = [d["name"] for d in devices if d["id"] == curr_dev_id] if curr_dev_id is not None else []
        self.device_combo.set(matching[0] if matching else (device_names[0] if device_names else "Default"))
        self.device_combo.pack(fill="x", pady=(0, 14))

        self.switch_paste = ctk.CTkSwitch(
            settings_frame,
            text="Auto-paste into focused app (Cmd + V)",
            font=ctk.CTkFont(size=12),
            command=self.on_toggle_paste
        )
        if self.engine.settings.get("auto_paste", True):
            self.switch_paste.select()
        self.switch_paste.pack(anchor="w", pady=6)

        self.switch_sound = ctk.CTkSwitch(
            settings_frame,
            text="Play audio chimes on start / stop",
            font=ctk.CTkFont(size=12),
            command=self.on_toggle_sound
        )
        if self.engine.settings.get("sound_feedback", True):
            self.switch_sound.select()
        self.switch_sound.pack(anchor="w", pady=6)

        self.switch_ontop = ctk.CTkSwitch(
            settings_frame,
            text="Keep WFlow window always on top",
            font=ctk.CTkFont(size=12),
            command=self.on_toggle_ontop
        )
        if self.engine.settings.get("always_on_top", False):
            self.switch_ontop.select()
            self.attributes("-topmost", True)
        self.switch_ontop.pack(anchor="w", pady=6)

        lbl_hotkey_info = ctk.CTkLabel(
            settings_frame,
            text="⚡ Global Hotkey: Press F8 (or Fn + F8 on Mac keyboards) anywhere in macOS to dictate.",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            wraplength=420,
            justify="left"
        )
        lbl_hotkey_info.pack(anchor="w", pady=(14, 4))

    def get_input_devices(self):
        results = []
        try:
            devs = sd.query_devices()
            for idx, d in enumerate(devs):
                if d.get("max_input_channels", 0) > 0:
                    results.append({"id": idx, "name": f"{idx}: {d.get('name')}"})
        except Exception:
            pass
        return results

    def save_api_key(self):
        new_key = self.entry_api.get().strip()
        if new_key:
            self.engine.settings["groq_api_key"] = new_key
            save_settings(self.engine.settings)
            save_env_key(new_key)
            self.status_badge.configure(text="● API Key Saved", text_color="#10b981", fg_color="#064e3b")
            self.after(2500, lambda: self.status_badge.configure(text="● Ready", text_color="#10b981", fg_color="#064e3b"))

    def on_model_change(self, choice):
        self.engine.settings["model"] = choice
        save_settings(self.engine.settings)
        self.lbl_meta.configure(text=f"⚡ Groq {choice}")

    def on_device_change(self, choice):
        try:
            dev_id = int(choice.split(":")[0])
            self.engine.settings["input_device"] = dev_id
            save_settings(self.engine.settings)
        except Exception:
            pass

    def on_toggle_paste(self):
        self.engine.settings["auto_paste"] = bool(self.switch_paste.get())
        save_settings(self.engine.settings)

    def on_toggle_sound(self):
        self.engine.settings["sound_feedback"] = bool(self.switch_sound.get())
        save_settings(self.engine.settings)

    def on_toggle_ontop(self):
        val = bool(self.switch_ontop.get())
        self.engine.settings["always_on_top"] = val
        self.attributes("-topmost", val)
        save_settings(self.engine.settings)

    def toggle_mini_mode(self):
        if not self.is_mini_mode:
            self.normal_geom = self.geometry()
            self.is_mini_mode = True
            self.tabview.pack_forget()
            self.meter_frame.pack_forget()
            self.geometry("380x130")
            self.attributes("-topmost", True)
            self.btn_mini.configure(text="↗ Expand", fg_color="#6366f1")
            self.record_btn.configure(height=42)
        else:
            self.is_mini_mode = False
            self.geometry(self.normal_geom)
            self.attributes("-topmost", self.engine.settings.get("always_on_top", False))
            self.btn_mini.configure(text="📌 Mini HUD", fg_color="#27272a")
            self.record_btn.configure(height=54)
            self.meter_frame.pack(fill="x", padx=22, pady=(0, 14))
            self.tabview.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def copy_latest_to_clip(self):
        text = self.txt_latest.get("1.0", "end-1c").strip()
        if text:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
            p.communicate(text)
            self.btn_copy.configure(text="✅ Copied!", fg_color="#10b981")
            self.after(1500, lambda: self.btn_copy.configure(text="📋 Copy", fg_color="#27272a"))

    # ================= Thread-Safe Event Queue Processing =================
    def process_queue_events(self):
        while not self.event_queue.empty():
            try:
                event = self.event_queue.get_nowait()
                event_type = event[0]
                if event_type == "state":
                    _, state, error_msg = event
                    self.apply_state_change(state, error_msg)
                elif event_type == "transcription":
                    _, text, latency, duration_sec, did_paste = event
                    self.apply_transcription_done(text, latency, duration_sec, did_paste)
            except Exception:
                pass
        self.after(40, self.process_queue_events)

    def apply_state_change(self, state, error_msg):
        if state == "recording":
            self.status_badge.configure(text="🔴 Recording...", text_color="#ef4444", fg_color="#450a0a")
            self.record_btn.configure(
                text="⏹️ Stop & Paste (F8)",
                fg_color="#ef4444",
                hover_color="#dc2626"
            )
            self.meter_bar.configure(progress_color="#ef4444")
            self.duration_label.configure(text="Recording: 00:00", text_color="#ef4444")
        elif state == "transcribing":
            self.status_badge.configure(text="⏳ Transcribing...", text_color="#f59e0b", fg_color="#451a03")
            self.record_btn.configure(
                text="⏳ Transcribing audio...",
                fg_color="#d97706",
                hover_color="#b45309"
            )
            self.meter_bar.set(0.0)
            self.meter_bar.configure(progress_color="#f59e0b")
            self.duration_label.configure(text="Groq Whisper Cloud...", text_color="#f59e0b")
        elif state == "idle":
            self.status_badge.configure(text="● Ready", text_color="#10b981", fg_color="#064e3b")
            self.record_btn.configure(
                text="🎙️ Start Dictation (F8)",
                fg_color="#6366f1",
                hover_color="#4f46e5"
            )
            self.meter_bar.set(0.0)
            self.meter_bar.configure(progress_color="#6366f1")
            self.duration_label.configure(text="Press F8 in any app", text_color="#71717a")
        elif state == "error":
            self.status_badge.configure(text="⚠️ Error", text_color="#f87171", fg_color="#450a0a")
            self.record_btn.configure(
                text="🎙️ Start Dictation (F8)",
                fg_color="#6366f1",
                hover_color="#4f46e5"
            )
            if error_msg:
                self.duration_label.configure(text=error_msg[:30], text_color="#f87171")

    def apply_transcription_done(self, text, latency, duration_sec, did_paste):
        self.apply_state_change("idle", None)
        self.txt_latest.delete("1.0", "end")
        self.txt_latest.insert("1.0", text)

        paste_msg = "• Auto-pasted ✅" if did_paste else "• Copied to clipboard"
        self.lbl_meta.configure(text=f"⚡ {latency:.2f}s ({duration_sec:.1f}s audio) {paste_msg}")

        entry = {
            "time": time.strftime("%H:%M:%S"),
            "latency": latency,
            "duration": duration_sec,
            "text": text,
            "pasted": did_paste
        }
        self.history.append(entry)
        self.save_history()
        self.refresh_history_ui()

    def update_timer(self):
        # Update VU meter and duration smoothly on the main UI thread
        if self.engine.is_recording:
            vol = self.engine.current_volume
            self.meter_bar.set(vol)
            elapsed = int(time.time() - self.engine.start_time)
            mins = elapsed // 60
            secs = elapsed % 60
            self.duration_label.configure(text=f"Recording: {mins:02d}:{secs:02d}")
        else:
            self.meter_bar.set(0.0)
        self.after(50, self.update_timer)

    def on_global_key(self, key):
        if key == keyboard.Key.f8:
            self.engine.toggle_recording()

    def on_close(self):
        try:
            if self.engine.stream:
                self.engine.stream.stop()
                self.engine.stream.close()
        except Exception:
            pass
        self.destroy()
        sys.exit(0)


def main():
    app = WFlowGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
