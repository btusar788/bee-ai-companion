# PREREQUISITE: Install required system audio components in your terminal via: pip install SpeechRecognition pyaudio
"""
Bee AI Companion - Cross-Platform Smartphone Dashboard & Ingestion Loop
=======================================================================
A native-feeling mobile application dashboard built with Flet, powered by a
thread-safe background poller that captures live physical microphone transcripts
and parses actionable tasks through the intelligent bee-skill engine.

Architectural highlights:
- Flet Dark-themed mobile application canvas with touch-responsive layout.
- Real-time scrolling transcript live-feed Card widget updated via local microphone stream.
- Interactive '⚡ Smart Actions' vertical column rendering dynamic ft.Checkbox items.
- Strict non-blocking concurrency: zero resource locks between Flet UI and live audio capture.
"""

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

import speech_recognition as sr
import flet as ft

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bee_ai_companion")

# ---------------------------------------------------------------------------
# Cross-Version Flet Compatibility Bridge (Flet 0.x and 1.x)
# ---------------------------------------------------------------------------
def _launch_flet_app(target: Optional[Callable[[ft.Page], None]] = None, **kwargs: Any) -> Any:
    """Universal execution launcher supporting both ft.app(target=main) and Flet 1.x ft.run(main)."""
    entry = target or kwargs.get("main")
    filtered_kwargs = {k: v for k, v in kwargs.items() if k != "target"}
    if hasattr(ft, "run") and callable(ft.run):
        return ft.run(entry, **filtered_kwargs)
    elif hasattr(ft.app, "run") and callable(ft.app.run):
        return ft.app.run(entry, **filtered_kwargs)
    elif hasattr(ft, "app") and callable(ft.app):
        return ft.app(target=entry, **filtered_kwargs)
    raise RuntimeError("No compatible Flet application runner found.")

ft.app = _launch_flet_app

if not hasattr(ft, "ElevatedButton"):
    class _ElevatedButton(ft.Button):
        """Cross-version bridge for ft.ElevatedButton supporting text and legacy parameters."""
        def __init__(self, *args: Any, text: Optional[str] = None, content: Any = None, **kwargs: Any) -> None:
            if text is not None and content is None:
                content = text
            super().__init__(*args, content=content, **kwargs)

    ft.ElevatedButton = _ElevatedButton

if not hasattr(ft.Page, "launch_url"):
    def _page_launch_url(self: ft.Page, url: str, *args: Any, **kwargs: Any) -> None:
        """Cross-version bridge for page.launch_url."""
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as err:
            logger.debug("Failed to launch URL via webbrowser: %s", err)

    ft.Page.launch_url = _page_launch_url


# ---------------------------------------------------------------------------
# 1. Local Memory Storage Layer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TranscriptRecord:
    """Represents an immutable single conversation transcript item."""

    id: int
    text: str
    source: str  # "microphone_stream" or "cli"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: Optional[Dict[str, Any]] = None


class TranscriptStore:
    """Thread-safe sequential in-memory storage for conversation transcripts.

    Provides atomic read and write operations using a reentrant lock, ensuring
    safe access across multiple threads without risk of race conditions or deadlocks.
    """

    def __init__(self, max_capacity: Optional[int] = 50) -> None:
        """Initialize the transcript store.

        Args:
            max_capacity: Limit on the number of stored transcripts to bound memory.
        """
        self._lock = threading.RLock()
        self._records: List[TranscriptRecord] = []
        self._counter: int = 0
        self._max_capacity = max_capacity

    def append(
        self,
        text: str,
        source: str = "cli",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TranscriptRecord:
        """Atomically append a new transcript to the store."""
        with self._lock:
            self._counter += 1
            record = TranscriptRecord(
                id=self._counter,
                text=text,
                source=source,
                metadata=metadata or {},
            )
            self._records.append(record)

            if self._max_capacity and len(self._records) > self._max_capacity:
                self._records.pop(0)

            return record

    def get_latest(self) -> Optional[TranscriptRecord]:
        """Return the most recently added transcript record, or None if empty."""
        with self._lock:
            return self._records[-1] if self._records else None

    def get_all(self) -> List[TranscriptRecord]:
        """Return a snapshot copy of all transcript records in sequential order."""
        with self._lock:
            return list(self._records)

    def count(self) -> int:
        """Return total count of stored records."""
        with self._lock:
            return len(self._records)

    def clear(self) -> None:
        """Clear all stored transcripts."""
        with self._lock:
            self._records.clear()


# ---------------------------------------------------------------------------
# 2. Intelligent Parsing Engine & Deep-Linking Protocol Layer
# ---------------------------------------------------------------------------
active_tasks: List[Dict[str, Any]] = []
_tasks_lock = threading.Lock()

ACTION_KEYWORDS: tuple[str, ...] = (
    "what to do",
    "what should i do",
    "what do i do",
    "help me",
    "help",
    "sos",
    "emergency",
    "accident",
    "whatsapp",
    "whats app",
    "what's up",
    "whats up",
    "remind me to",
    "i need to",
    "todo",
    "schedule",
    "buy",
    "call",
    "route to",
    "directions to",
    "navigate to",
    "share",
    "send",
    "broadcast",
)

TRANSPORT_COMMANDS: tuple[str, ...] = (
    "share", "send", "broadcast", "post", "forward", 
    "whatsapp", "whats app", "what's up", "whats up"
)
TARGET_PLATFORMS: tuple[str, ...] = ("whatsapp",)
DOCUMENT_EXTENSIONS: tuple[str, ...] = (
    "medical profile",
    "medical records",
    "medical record",
    "incident report",
    "voice log",
    "log",
    "file",
    "report",
    "records",
    "record",
    "transcript",
)

DATE_REGEX_PATTERNS: tuple[str, ...] = (
    # Textual month + day (e.g., "October 21st", "Oct 21", "January 5th, 2026")
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{2,4})?\b",
    # Day + textual month (e.g., "21st of October", "21 October")
    r"\b\d{1,2}(?:st|nd|rd|th)?(?:\s+of)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(?:\s*,?\s*\d{2,4})?\b",
    # Numeric combinations (e.g., "21-10", "21/10", "2026-10-21", "10/21/2026")
    r"\b\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?\b",
    # Relative day strings (e.g., "yesterday", "today", "tomorrow")
    r"\b(?:yesterday|today|tomorrow)\b",
)


def encode_url_payload(text: str) -> str:
    """Dedicated utility helper method that URL-encodes a text string cleanly
    so space characters and special symbols do not crash the pipeline.
    """
    if not text or not isinstance(text, str):
        return ""
    return urllib.parse.quote(text, safe="")


class WhatsAppShareManager:
    """Thread-safe state container and dispatcher for dynamic WhatsApp deep-links."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.extracted_date: str = "Recent"
        self.extracted_file_name: str = "log"
        self.payload_message: str = (
            f"Emergency Update - Secure File Transcript [{self.extracted_date}]: {self.extracted_file_name}"
        )
        self.encoded_payload: str = f"/send?text={encode_url_payload(self.payload_message)}"
        self._callbacks: List[Callable[[str], None]] = []

    def set_payload(self, extracted_date: str, extracted_file_name: str) -> tuple[str, str]:
        """Atomically updates payload details, generates encoded payload, and notifies listeners."""
        with self._lock:
            self.extracted_date = extracted_date
            self.extracted_file_name = extracted_file_name
            self.payload_message = (
                f"Emergency Update - Secure File Transcript [{extracted_date}]: {extracted_file_name}"
            )
            self.encoded_payload = f"/send?text={encode_url_payload(self.payload_message)}"
            callbacks = list(self._callbacks)
            encoded = self.encoded_payload
            payload = self.payload_message

        # Fire callbacks outside the lock to avoid thread deadlocks
        for cb in callbacks:
            try:
                cb(encoded)
            except Exception as err:
                logger.debug("Failed executing WhatsApp trigger callback: %s", err)

        return payload, encoded

    def get_encoded_payload(self) -> str:
        with self._lock:
            return self.encoded_payload

    def get_payload_message(self) -> str:
        with self._lock:
            return self.payload_message

    def register_trigger_callback(self, cb: Callable[[str], None]) -> None:
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def unregister_trigger_callback(self, cb: Callable[[str], None]) -> None:
        with self._lock:
            if cb in self._callbacks:
                self._callbacks.remove(cb)


whatsapp_share_manager = WhatsAppShareManager()


def extract_whatsapp_share_intent(text: str) -> Optional[tuple[str, str, str, str]]:
    """Scans transcript stream for dual matching intent: transport command + WhatsApp target entity.

    Pulls out mentioned dates (e.g., 'October 21st', '21-10', 'yesterday') alongside
    words matching document extensions (e.g., 'log', 'file', 'report', 'medical profile').
    Generates dynamic payload:
        payload_message = f"Emergency Update - Secure File Transcript [{extracted_date}]: {extracted_file_name}"
    Returns (extracted_date, extracted_file_name, payload_message, encoded_payload) or None.
    """
    if not text or not isinstance(text, str):
        return None

    try:
        platform_pattern = r"\b(whatsapp|whats\s*app|what's\s*up|whats\s*up)\b"
        transport_pattern = r"\b(" + "|".join(re.escape(cmd) for cmd in TRANSPORT_COMMANDS) + r")\b"

        has_platform = bool(re.search(platform_pattern, text, flags=re.IGNORECASE))
        has_transport = bool(re.search(transport_pattern, text, flags=re.IGNORECASE))

        if not (has_platform or has_transport):
            return None

        # 1. Cleanly pull out any dates mentioned in the sentence block
        extracted_date: str = ""
        for dp in DATE_REGEX_PATTERNS:
            date_match = re.search(dp, text, flags=re.IGNORECASE)
            if date_match:
                extracted_date = date_match.group(0).strip()
                break

        if not extracted_date:
            extracted_date = datetime.now(timezone.utc).strftime("%B %d")

        # 2. Extract words matching document extensions
        doc_pattern = r"\b(" + "|".join(re.escape(doc) for doc in DOCUMENT_EXTENSIONS) + r")\b"
        doc_match = re.search(doc_pattern, text, flags=re.IGNORECASE)
        if doc_match:
            extracted_file_name = doc_match.group(0).strip().lower()
        else:
            extracted_file_name = "log"

        # 3. Dynamic cross-platform text payload variable
        payload_message = f"Emergency Update - Secure File Transcript [{extracted_date}]: {extracted_file_name}"
        encoded_payload = f"/send?text={encode_url_payload(payload_message)}"

        return extracted_date, extracted_file_name, payload_message, encoded_payload

    except Exception as parse_err:
        logger.debug("Error during WhatsApp share intent extraction: %s", parse_err)
        return None


def process_wearable_text(transcript_text: str) -> List[Dict[str, Any]]:
    """Intelligent parsing engine tracking action keywords and dual-matching verbal intents.

    Scans raw transcript chunks for task-oriented action keywords as well as explicit
    file extraction matching phrases (transport action command + WhatsApp target entity),
    generating dynamic WhatsApp deep links and tracking active tasks thread-safely.
    """
    if not transcript_text or not isinstance(transcript_text, str):
        return []

    newly_added_tasks: List[Dict[str, Any]] = []

    for line in transcript_text.splitlines():
        trimmed_line = line.strip()
        if not trimmed_line:
            continue

        # 1. Dual matching intent tracking for hands-free WhatsApp media/file sharing
        share_intent = extract_whatsapp_share_intent(trimmed_line)
        if share_intent:
            extracted_date, extracted_file_name, payload_message, encoded_payload = share_intent
            logger.info(
                "Captured WhatsApp voice-share intent: date='%s', file='%s' -> payload='%s'",
                extracted_date,
                extracted_file_name,
                payload_message,
            )
            whatsapp_share_manager.set_payload(extracted_date, extracted_file_name)

            task_title = f"Share {extracted_file_name} to WhatsApp"
            with _tasks_lock:
                already_exists = any(
                    existing["task"].lower() == task_title.lower() and not existing.get("completed", False)
                    for existing in active_tasks
                )
                if not already_exists:
                    task_obj = {
                        "id": len(active_tasks) + 1,
                        "task": task_title,
                        "keyword": "whatsapp",
                        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                        "completed": False,
                    }
                    active_tasks.append(task_obj)
                    newly_added_tasks.append(task_obj)
            continue

        # 2. Extract standard task-oriented keywords into active_tasks
        for keyword in ACTION_KEYWORDS:
            pattern = rf"\b{re.escape(keyword)}\b"
            match = re.search(pattern, trimmed_line, flags=re.IGNORECASE)

            if not match:
                continue

            phrase_start = match.end()
            raw_phrase = trimmed_line[phrase_start:].strip()
            cleaned_phrase = raw_phrase.lstrip(":- \t\"'")
            first_clause = re.split(r"[\.!?]\s+", cleaned_phrase)[0].strip()
            final_phrase = first_clause.rstrip(".!?,;\"'")

            if (
                not final_phrase
                or (keyword in ("what to do", "what should i do", "what do i do") and final_phrase.lower() in ("in emergency", "in an emergency", "in accident", "in case of emergency"))
            ):
                keyword_fallbacks = {
                    "what to do": "Emergency guidance: Follow safety protocols",
                    "what should i do": "Emergency guidance: Follow safety protocols",
                    "what do i do": "Emergency guidance: Follow safety protocols",
                    "help me": "Emergency assistance request",
                    "help": "Immediate help alert",
                    "sos": "SOS emergency beacon triggered",
                    "emergency": "Critical emergency mode activated",
                    "accident": "Accident incident report logged",
                    "whatsapp": "Share voice transcript to WhatsApp",
                    "whats app": "Share voice transcript to WhatsApp",
                    "what's up": "Share voice transcript to WhatsApp",
                    "whats up": "Share voice transcript to WhatsApp",
                }
                final_phrase = keyword_fallbacks.get(keyword, f"{keyword.capitalize()} request")

            capitalized_task = final_phrase.capitalize()

            # Atomic non-blocking check and insertion under microsecond lock
            with _tasks_lock:
                already_exists = any(
                    existing["task"].lower() == capitalized_task.lower() and not existing.get("completed", False)
                    for existing in active_tasks
                )
                if already_exists:
                    logger.debug("Active task already pending: '%s'", capitalized_task)
                    break

                task_obj = {
                    "id": len(active_tasks) + 1,
                    "task": capitalized_task,
                    "keyword": keyword,
                    "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "completed": False,
                }
                active_tasks.append(task_obj)
                newly_added_tasks.append(task_obj)

            break

    return newly_added_tasks


# ---------------------------------------------------------------------------
# 3. Hardware Microphone & Wearable Collector Capture Pipeline
# ---------------------------------------------------------------------------
class HardwareMicrophone(sr.AudioSource):
    """Robust multi-channel hardware microphone source with automatic stereo downmixing.
    
    Bypasses Windows DirectSound alternating-channel zero drops by reading native
    2-channel streams and downmixing cleanly to 16-bit mono PCM via audioop.tomono.
    """

    def __init__(
        self,
        device_index: Optional[int] = None,
        sample_rate: Optional[int] = None,
        chunk_size: int = 1024,
    ) -> None:
        import pyaudio
        p = pyaudio.PyAudio()
        try:
            if device_index is None:
                device_info = p.get_default_input_device_info()
                device_index = device_info.get("index")
            else:
                device_info = p.get_device_info_by_index(device_index)

            self.device_name = device_info.get("name", f"Device #{device_index}")
            self.max_channels = min(2, max(1, device_info.get("maxInputChannels", 1)))
            native_rate = int(device_info.get("defaultSampleRate", 44100))
            self.SAMPLE_RATE = sample_rate or native_rate
        finally:
            p.terminate()

        self.device_index = device_index
        self.channels = self.max_channels
        self.format = pyaudio.paInt16
        self.SAMPLE_WIDTH = 2
        self.CHUNK = chunk_size
        self.audio = None
        self.stream = None

    def __enter__(self) -> "HardwareMicrophone":
        import pyaudio
        assert self.stream is None, "Microphone stream already open"
        self.audio = pyaudio.PyAudio()
        try:
            py_stream = self.audio.open(
                input_device_index=self.device_index,
                channels=self.channels,
                format=self.format,
                rate=self.SAMPLE_RATE,
                frames_per_buffer=self.CHUNK,
                input=True,
            )
            # Flush driver startup transient
            for _ in range(6):
                py_stream.read(self.CHUNK, exception_on_overflow=False)
            self.stream = self.MicrophoneStream(py_stream, self.channels)
        except Exception:
            self.audio.terminate()
            raise
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self.stream:
                self.stream.close()
        finally:
            self.stream = None
            if self.audio:
                self.audio.terminate()
                self.audio = None

    class MicrophoneStream:
        def __init__(self, pyaudio_stream: Any, channels: int) -> None:
            self.pyaudio_stream = pyaudio_stream
            self.channels = channels

        def read(self, size: int) -> bytes:
            import audioop
            data = self.pyaudio_stream.read(size, exception_on_overflow=False)
            if self.channels == 2:
                # Downmix interleaved stereo frames to 16-bit mono PCM cleanly
                return audioop.tomono(data, 2, 0.5, 0.5)
            return data

        def close(self) -> None:
            try:
                if not self.pyaudio_stream.is_stopped():
                    self.pyaudio_stream.stop_stream()
            finally:
                self.pyaudio_stream.close()


class WearableCollector:
    """Interacts directly with computer hardware microphone layer cleanly."""

    @staticmethod
    def get_available_devices() -> List[Dict[str, Any]]:
        """Returns all functional recording devices available on the system."""
        import pyaudio
        p = pyaudio.PyAudio()
        devices = []
        try:
            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        host_api = p.get_host_api_info_by_index(info["hostApi"]).get("name", "")
                        # DirectSound and WASAPI provide functional audio streams on Windows
                        if "DirectSound" in host_api or "WASAPI" in host_api:
                            devices.append({
                                "index": i,
                                "name": info["name"],
                                "host_api": host_api,
                                "display": f"{info['name']} ({host_api})",
                            })
                except Exception:
                    continue
        finally:
            p.terminate()
        return devices

    @staticmethod
    def find_active_microphone_device() -> Optional[Dict[str, Any]]:
        """Automatically scans system input audio interfaces to find a device that delivers live audio."""
        import pyaudio, audioop
        p = pyaudio.PyAudio()
        candidates = []
        try:
            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        name = info.get("name", "")
                        host_api = p.get_host_api_info_by_index(info["hostApi"]).get("name", "")
                        rate = int(info.get("defaultSampleRate", 44100))
                        channels = min(2, max(1, info.get("maxInputChannels", 1)))
                        stream = p.open(
                            format=pyaudio.paInt16,
                            channels=channels,
                            rate=rate,
                            input=True,
                            input_device_index=i,
                            frames_per_buffer=1024,
                        )
                        energies = []
                        for _ in range(3):
                            data = stream.read(1024, exception_on_overflow=False)
                            energies.append(audioop.rms(data, 2))
                        stream.stop_stream()
                        stream.close()
                        avg_rms = sum(energies) / len(energies)
                        candidates.append({
                            "index": i,
                            "name": name,
                            "host_api": host_api,
                            "rms": avg_rms,
                            "rate": rate,
                        })
                except Exception:
                    continue
        finally:
            p.terminate()

        def score(c: Dict[str, Any]) -> float:
            s = 0.0
            name_lower = c["name"].lower()
            # Windows DirectSound provides the highest compatibility
            if "directsound" in c["host_api"].lower():
                s += 2000.0
            elif "wasapi" in c["host_api"].lower():
                s += 500.0
            # Prioritize connected Bluetooth headsets
            if any(term in name_lower for term in ("headset", "buds", "oneplus", "airpod", "bluetooth", "wireless")):
                s += 1500.0
            elif "primary" in name_lower:
                s += 1000.0
            elif "microphone" in name_lower or "array" in name_lower:
                s += 800.0
            if c["rms"] > 0:
                s += 500.0
            return s

        if candidates:
            candidates.sort(key=score, reverse=True)
            return candidates[0]

        return None

    def __init__(self, device_index: Optional[int] = None, *args: Any, **kwargs: Any) -> None:
        import speech_recognition as sr
        self.recognizer = sr.Recognizer()
        # Stable conversational threshold parameters without erratic dynamic wandering
        self.recognizer.dynamic_energy_threshold = False
        self.recognizer.pause_threshold = 1.1
        self.recognizer.phrase_threshold = 0.4
        self.recognizer.non_speaking_duration = 0.5
        self.recognizer.operation_timeout = 8.0

        # Auto-detect the best active physical microphone if none specified
        if device_index is None:
            best_mic = self.find_active_microphone_device()
            if best_mic:
                self.device_index = best_mic["index"]
                self.device_name = f"{best_mic['name']} ({best_mic['host_api']})"
                logger.info(
                    "🎙️ Auto-detected active hardware microphone: [Device %d] %s (Signal RMS: %.1f)", 
                    self.device_index, self.device_name, best_mic["rms"]
                )
            else:
                self.device_index = None
                self.device_name = "Default System Microphone"
        else:
            self.device_index = device_index
            self.device_name = f"Device #{device_index}"

        self.microphone: Optional[HardwareMicrophone] = None
        self._source = None
        self._calibrated = False
        self._lock = threading.Lock()

    def _ensure_source(self) -> Any:
        if self._source is None:
            self.microphone = HardwareMicrophone(device_index=self.device_index)
            self._source = self.microphone.__enter__()
            logger.info("🎙️ Hardware microphone input stream initialized on %s.", self.device_name)
        return self._source

    def calibrate(self) -> None:
        with self._lock:
            if not self._calibrated:
                source = self._ensure_source()
                logger.info("🎙️ Calibrating room mic for ambient noise profile on %s...", self.device_name)
                self.recognizer.adjust_for_ambient_noise(source, duration=1.2)
                raw_ambient = float(self.recognizer.energy_threshold)
                # Keep threshold safely 35% above ambient noise floor, bounded between 300.0 and 25000.0
                self.recognizer.energy_threshold = min(25000.0, max(300.0, raw_ambient * 1.35))
                self._calibrated = True
                logger.info(
                    "🟢 Calibration complete on %s (ambient: %.1f -> threshold: %.1f). Voice framework active!", 
                    self.device_name, raw_ambient, self.recognizer.energy_threshold
                )

    def recalibrate(self) -> None:
        """Forces an explicit re-calibration cycle."""
        with self._lock:
            self._calibrated = False
            self.calibrate()

    def switch_device(self, new_device_index: Optional[int], new_device_name: Optional[str] = None) -> None:
        """Dynamically switches active microphone to a different hardware interface."""
        with self._lock:
            self.close()
            self.device_index = new_device_index
            self.device_name = new_device_name or (f"Device #{new_device_index}" if new_device_index is not None else "Default System Microphone")
            self._calibrated = False
            logger.info("🎙️ Switched active microphone interface to: %s", self.device_name)

    def fetch_latest_transcript(self) -> tuple[str, str, Dict[str, Any]]:
        """Listens directly to the microphone with continuous stream buffering."""
        import speech_recognition as sr
        with self._lock:
            try:
                source = self._ensure_source()
                if not self._calibrated:
                    self.calibrate()

                # Listen for speech without tearing down the audio device every cycle
                audio = self.recognizer.listen(source, timeout=3.0, phrase_time_limit=10.0)

                # Filter sub-second noise puffs / clicks
                raw_bytes = audio.get_raw_data()
                duration = len(raw_bytes) / (audio.sample_rate * audio.sample_width)
                if duration < 0.65:
                    return "", "microphone_stream", {}

                logger.info("⚡ Audio input detected on %s (%.2fs)! Transcribing...", self.device_name, duration)
                text_output = self.recognizer.recognize_google(audio)
                cleaned = text_output.strip()
                logger.info("🗣️ Speech recognized: '%s'", cleaned)
                return cleaned, "microphone_stream", {}
            except sr.WaitTimeoutError:
                # Normal silence / no speech in this 3-second slice
                return "", "microphone_stream", {}
            except sr.UnknownValueError:
                logger.info("🔉 Voice activity detected on %s, but phrase was unclear.", self.device_name)
                return "", "microphone_stream", {}
            except sr.RequestError as req_err:
                logger.warning("🌐 Google Speech API network error: %s", req_err)
                return "", "microphone_stream", {"error": str(req_err)}
            except Exception as err:
                logger.warning("⚠️ Microphone stream issue on %s: %s", self.device_name, err)
                self.close()
                return "", "microphone_stream", {"error": str(err)}

    def close(self) -> None:
        with self._lock:
            if self._source is not None and self.microphone is not None:
                try:
                    self.microphone.__exit__(None, None, None)
                except Exception:
                    pass
                self._source = None




# ---------------------------------------------------------------------------
# 4. Background Poller Worker Loop
# ---------------------------------------------------------------------------
class TranscriptPollerWorker:
    """Background worker that continuously captures audio transcripts via the collector.

    Runs in a dedicated thread and communicates with the Flet state loop via
    an asynchronous UI synchronization callback without resource locks.
    """

    POLL_INTERVAL_SECONDS: float = 0.5

    def __init__(
        self,
        store: TranscriptStore,
        collector: Optional[WearableCollector] = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        on_update_callback: Optional[Callable[[], None]] = None,
        on_transcript_callback: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.store = store
        self.collector = collector or WearableCollector()
        self.poll_interval = poll_interval
        self.on_update_callback = on_update_callback
        self.on_transcript_callback = on_transcript_callback

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _worker_loop(self) -> None:
        logger.info(
            "Background audio transcript poller started (interval: %.1fs).",
            self.poll_interval,
        )

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()

            try:
                # 1. Fetch transcript from physical microphone without holding any shared memory locks
                latest_transcript, source, metadata = self.collector.fetch_latest_transcript()

                # Process only when speech was actually captured (silence or timeout returns "")
                if latest_transcript and latest_transcript.strip():
                    latest_transcript = latest_transcript.strip()
                    logger.info("Captured voice transcript from microphone: '%s'", latest_transcript)

                    # 2. Append to local memory store under microsecond lock
                    record = self.store.append(text=latest_transcript, source=source, metadata=metadata)

                    # 3. Parse text for smart actions & intent extractions
                    new_tasks = process_wearable_text(latest_transcript)
                    if new_tasks:
                        logger.info("Extracted %d new action task(s)", len(new_tasks))

                    # 4. Fire transcript trigger callback for alert panels and deep-links
                    if self.on_transcript_callback is not None:
                        try:
                            self.on_transcript_callback(latest_transcript, source, metadata)
                        except Exception as cb_err:
                            logger.debug("Voice transcript callback failed: %s", cb_err)

                    # 5. Notify Flet state loop safely
                    if self.on_update_callback is not None:
                        try:
                            self.on_update_callback()
                        except Exception as cb_err:
                            logger.debug("UI callback notification failed (app closing): %s", cb_err)

            except Exception as loop_err:
                logger.error("Error in worker iteration: %s", loop_err, exc_info=True)

            elapsed = time.monotonic() - cycle_start
            wait_time = max(0.0, self.poll_interval - elapsed)
            if self._stop_event.wait(timeout=wait_time):
                break

        logger.info("Background audio transcript poller stopped cleanly.")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="TranscriptPollerThread",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        if self._thread is None or not self._thread.is_alive():
            if hasattr(self.collector, "close") and callable(self.collector.close):
                self.collector.close()
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if hasattr(self.collector, "close") and callable(self.collector.close):
            self.collector.close()



# ---------------------------------------------------------------------------
# 5. Flet Mobile UI Layer & Dashboard Orchestration
# ---------------------------------------------------------------------------
def main(page: ft.Page) -> None:
    """Flet application canvas target rendering a smartphone dashboard."""

    # 1. Canvas & Window Configuration
    page.title = "🐝 Bee AI Dashboard"
    page.theme_mode = ft.ThemeMode.DARK
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.padding = 0
    page.bgcolor = "#0D0F14"  # Deep dark slate mobile canvas

    # Set native smartphone viewport proportions on desktop window
    if hasattr(page, "window") and page.window is not None:
        page.window.width = 414
        page.window.height = 840
        page.window.min_width = 380
        page.window.min_height = 720
        page.window.resizable = True

    # 2. Shared Data Store & Ingestion Worker
    store = TranscriptStore(max_capacity=30)
    collector = WearableCollector()

    # Track rendered action tasks to prevent duplicate widget recreation
    rendered_task_keys: Set[int] = set()

    # 3. Native-style AppBar
    page.appbar = ft.AppBar(
        title=ft.Text("🐝 Bee AI Dashboard", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_400),
        center_title=True,
        bgcolor="#161922",
        elevation=3,
        actions=[
            ft.Container(
                content=ft.Row(
                    [
                        ft.Container(width=8, height=8, border_radius=4, bgcolor=ft.Colors.GREEN_ACCENT_400),
                        ft.Text("SYNC", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_ACCENT_400),
                    ],
                    spacing=5,
                ),
                padding=ft.Padding(left=0, top=0, right=16, bottom=0),
            )
        ],
    )

    # 4. Status Metric Pill Header
    metrics_row = ft.Container(
        content=ft.Row(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.MIC_ROUNDED, color=ft.Colors.GREEN_400, size=16),
                        ft.Text("Live Microphone Stream", size=11, color=ft.Colors.GREY_300, weight=ft.FontWeight.W_500),
                    ],
                    spacing=6,
                ),
                ft.Container(
                    content=ft.Text("Hardware Audio Loop", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_400),
                    padding=ft.Padding(left=8, top=2, right=8, bottom=2),
                    border_radius=8,
                    bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.AMBER_400),
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        ),
        padding=ft.Padding(left=16, top=10, right=16, bottom=10),
        bgcolor="#131620",
        border_radius=12,
        margin=ft.Margin(left=0, top=0, right=0, bottom=10),
    )

    # 5. Live Feed Card Widget (renders dynamic scrolling transcripts)
    transcript_card_text = ft.Text(
        value="Listening directly to physical microphone...\nTranscripts will populate automatically as you speak.",
        size=13,
        color=ft.Colors.GREY_300,
        selectable=True,
    )

    transcript_feed_column = ft.Column(
        controls=[
            ft.Container(
                content=transcript_card_text,
                padding=12,
                border_radius=10,
                bgcolor="#12151E",
            )
        ],
        scroll=ft.ScrollMode.AUTO,
        auto_scroll=True,
        height=190,
        spacing=8,
    )

    # Quick simulator dispatcher allowing instant testing of verbal commands
    def simulate_voice_phrase(phrase: str) -> None:
        cleaned = phrase.strip()
        if not cleaned:
            return
        logger.info("🧪 Voice Simulator input: '%s'", cleaned)
        store.append(text=cleaned, source="voice_simulator")
        process_wearable_text(cleaned)
        handle_voice_action_triggers(cleaned, "voice_simulator", {})
        sync_ui_state()

    def make_test_chip(label: str, phrase: str, icon_name: str, color: str = ft.Colors.AMBER_400) -> ft.Control:
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(icon_name, size=11, color=color),
                    ft.Text(label, size=9, weight=ft.FontWeight.W_500, color=ft.Colors.WHITE),
                ],
                spacing=3,
                tight=True,
            ),
            bgcolor="#151824",
            border_radius=6,
            padding=ft.Padding(left=6, top=3, right=6, bottom=3),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.18, color)),
            on_click=lambda _: simulate_voice_phrase(phrase),
            tooltip=f"Test voice command: '{phrase}'",
        )

    sim_chips_row = ft.Row(
        [
            make_test_chip("🚨 help me", "help me", ft.Icons.WARNING_ROUNDED, ft.Colors.RED_ACCENT_400),
            make_test_chip("❓ what to do", "what to do in emergency", ft.Icons.HELP_OUTLINE_ROUNDED, ft.Colors.ORANGE_300),
            make_test_chip("💬 whatsapp", "share emergency report to whatsapp", ft.Icons.CHAT_ROUNDED, ft.Colors.GREEN_ACCENT_400),
            make_test_chip("🚑 emergency", "emergency accident help", ft.Icons.LOCAL_HOSPITAL_ROUNDED, ft.Colors.RED_400),
            make_test_chip("📞 call 112", "call 112", ft.Icons.PHONE_ROUNDED, ft.Colors.CYAN_300),
            make_test_chip("📍 route hospital", "route to nearest hospital", ft.Icons.DIRECTIONS_ROUNDED, ft.Colors.BLUE_300),
            make_test_chip("📝 remind me", "remind me to check medicine", ft.Icons.ALARM_ROUNDED, ft.Colors.PURPLE_300),
            make_test_chip("🛒 buy", "buy first aid supplies", ft.Icons.SHOPPING_BAG_ROUNDED, ft.Colors.TEAL_300),
        ],
        wrap=True,
        spacing=4,
    )

    sim_text_field = ft.TextField(
        hint_text="Type or test phrase (e.g. 'help me', 'whatsapp')...",
        text_size=11,
        height=34,
        expand=True,
        border=ft.OutlineInputBorder() if hasattr(ft, "OutlineInputBorder") else ft.InputBorder.OUTLINE,
        bgcolor="#12151E",
        content_padding=ft.Padding(left=8, top=2, right=8, bottom=2),
        on_submit=lambda e: (
            simulate_voice_phrase(e.control.value),
            setattr(e.control, "value", ""),
            page.update(),
        ),
    )

    sim_send_btn = ft.IconButton(
        icon=ft.Icons.SEND_ROUNDED,
        icon_size=16,
        icon_color=ft.Colors.AMBER_400,
        tooltip="Submit simulated voice phrase",
        on_click=lambda _: (
            simulate_voice_phrase(sim_text_field.value),
            setattr(sim_text_field, "value", ""),
            page.update(),
        ),
    )

    sim_input_row = ft.Row([sim_text_field, sim_send_btn], spacing=4)

    # Hardware microphone status & recalibration controls
    device_label = collector.device_name
    if len(device_label) > 28:
        device_label = device_label[:26] + "..."
    mic_status_text = ft.Text(
        f"Active: {device_label}",
        size=10,
        color=ft.Colors.GREY_400,
    )

    def handle_recalibrate(_: Any) -> None:
        try:
            collector.recalibrate()
            threshold_val = int(collector.recognizer.energy_threshold)
            mic_status_text.value = f"Calibrated (floor: {threshold_val})"
            page.update()
        except Exception as exc:
            logger.warning("Recalibration failed: %s", exc)

    recalibrate_btn = ft.IconButton(
        icon=ft.Icons.TUNE_ROUNDED,
        icon_size=16,
        icon_color=ft.Colors.AMBER_400,
        tooltip="Recalibrate microphone ambient noise profile",
        on_click=handle_recalibrate,
    )

    transcript_card = ft.Card(
        content=ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.MIC_ROUNDED, color=ft.Colors.AMBER_400, size=18),
                                    ft.Column(
                                        [
                                            ft.Text("Live Voice Stream", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                                            mic_status_text,
                                        ],
                                        spacing=1,
                                    ),
                                ],
                                spacing=8,
                            ),
                            ft.Row(
                                [
                                    recalibrate_btn,
                                    ft.Container(
                                        content=ft.Row(
                                            [
                                                ft.Container(width=6, height=6, border_radius=3, bgcolor=ft.Colors.GREEN_ACCENT_400),
                                                ft.Text("LIVE", size=9, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_ACCENT_400),
                                            ],
                                            spacing=4,
                                        ),
                                        padding=ft.Padding(left=6, top=2, right=6, bottom=2),
                                        border_radius=6,
                                        bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.GREEN_ACCENT_400),
                                    ),
                                ],
                                spacing=4,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Divider(color=ft.Colors.with_opacity(0.08, ft.Colors.WHITE), height=10),
                    transcript_feed_column,
                    ft.Divider(color=ft.Colors.with_opacity(0.08, ft.Colors.WHITE), height=6),
                    ft.Text("Quick Voice Keyword Testing:", size=10, color=ft.Colors.GREY_400),
                    sim_chips_row,
                    sim_input_row,
                ],
                spacing=4,
            ),
            padding=14,
            border_radius=16,
            bgcolor="#1A1E29",
        ),
        elevation=2,
        margin=ft.Margin(left=0, top=0, right=0, bottom=14),
    )

    # 6. "⚡ Smart Actions" Section (Dynamic Interactive Checkboxes)
    actions_badge_text = ft.Text("0", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
    actions_badge = ft.Container(
        content=actions_badge_text,
        bgcolor=ft.Colors.AMBER_400,
        border_radius=10,
        padding=ft.Padding(left=7, top=1, right=7, bottom=1),
    )

    empty_actions_placeholder = ft.Container(
        content=ft.Column(
            [
                ft.Icon(ft.Icons.TASK_ALT_ROUNDED, size=32, color=ft.Colors.GREY_600),
                ft.Text(
                    "No pending actions detected.\nSpeak 'Remind me to...', 'Call...', 'Route to...', or 'Emergency' to capture tasks.",
                    size=12,
                    color=ft.Colors.GREY_500,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=6,
        ),
        padding=ft.Padding(left=0, top=30, right=0, bottom=30),
        alignment=ft.Alignment(0, 0),
    )

    actions_column = ft.Column(
        controls=[empty_actions_placeholder],
        scroll=ft.ScrollMode.AUTO,
        spacing=8,
        height=220,
    )

    smart_actions_card = ft.Card(
        content=ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.BOLT_ROUNDED, color=ft.Colors.AMBER_400, size=20),
                                    ft.Text("⚡ Smart Actions", size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                                ],
                                spacing=8,
                            ),
                            actions_badge,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Text(
                        "Interactive items detected from live microphone audio:",
                        size=11,
                        color=ft.Colors.GREY_400,
                    ),
                    ft.Divider(color=ft.Colors.with_opacity(0.08, ft.Colors.WHITE), height=14),
                    actions_column,
                ],
                spacing=6,
            ),
            padding=14,
            border_radius=16,
            bgcolor="#1A1E29",
        ),
        elevation=2,
    )

    # 7. Emergency Intervention & Safety System Panel
    emergency_guidance_box = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.HELP_OUTLINE_ROUNDED, size=16, color=ft.Colors.AMBER_400),
                        ft.Text("📋 What To Do In An Emergency:", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_300),
                    ],
                    spacing=6,
                ),
                ft.Text("1. Stay calm and assess immediate surroundings.", size=11, color=ft.Colors.WHITE),
                ft.Text("2. If in danger, tap 'Call 112' below for rapid dispatch.", size=11, color=ft.Colors.WHITE),
                ft.Text("3. Tap 'Route Navigation' for nearest hospital directions.", size=11, color=ft.Colors.WHITE),
                ft.Text("4. Tap 'Voice-Share' to send your status via WhatsApp.", size=11, color=ft.Colors.WHITE),
            ],
            spacing=3,
        ),
        bgcolor="#1C1417",
        padding=10,
        border_radius=8,
        border=ft.Border.all(1, ft.Colors.with_opacity(0.15, ft.Colors.RED_ACCENT_400)),
    )

    emergency_panel = ft.Container(
        visible=False,
        width=360,
        padding=14,
        border_radius=16,
        bgcolor="#2A1B1F",
        border=ft.Border.all(2, ft.Colors.RED_ACCENT_400),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.RED_ACCENT_400, size=24),
                        ft.Text("⚠️ EMERGENCY MODE ACTIVATED", size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.RED_400),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=8,
                ),
                emergency_guidance_box,
                ft.ElevatedButton(
                    text="🚨 Instant Call Emergency Services (112)",
                    icon=ft.Icons.PHONE_IN_TALK_ROUNDED,
                    color="white",
                    bgcolor=ft.Colors.RED_ACCENT_700,
                    width=340,
                    on_click=lambda _: page.launch_url("tel:112"),
                ),
                ft.ElevatedButton(
                    text="📍 Open Nearest Hospital Navigation",
                    icon=ft.Icons.DIRECTIONS_RUN_ROUNDED,
                    color="black",
                    bgcolor="amber_400",
                    width=340,
                    on_click=lambda _: page.launch_url("https://www.google.com/maps/search/hospital"),
                ),
                ft.ElevatedButton(
                    text="📂 Voice-Share Records via WhatsApp",
                    icon=ft.Icons.PHONELINK_RING_ROUNDED,
                    color="white",
                    bgcolor="#075E54",
                    width=340,
                    on_click=lambda _: (
                        encoded_payload := whatsapp_share_manager.get_encoded_payload(),
                        page.launch_url(f"https://api.whatsapp.com{encoded_payload}"),
                    ),
                ),
                ft.TextButton(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED, size=15, color=ft.Colors.GREY_400),
                            ft.Text("Dismiss Safety Panel", size=12, color=ft.Colors.GREY_400),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=6,
                    ),
                    on_click=lambda _: (setattr(emergency_panel, "visible", False), page.update()),
                ),
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    # 7b. Hands-Free Voice-Triggered WhatsApp Deep-Link Hook
    def handle_voice_whatsapp_trigger(encoded_payload: str) -> None:
        """Linked directly to trigger when the voice stream identifies the phrase context."""
        try:
            emergency_panel.visible = True
            now = time.time()
            if now - getattr(page, "last_whatsapp_launch_time", 0.0) > 3.0:
                page.last_whatsapp_launch_time = now
                logger.info("Executing voice-triggered WhatsApp dispatch: https://api.whatsapp.com%s", encoded_payload)
                page.launch_url(f"https://api.whatsapp.com{encoded_payload}")
            page.update()
        except Exception as trigger_err:
            logger.debug("Failed executing voice-triggered WhatsApp deep-link: %s", trigger_err)

    whatsapp_share_manager.register_trigger_callback(handle_voice_whatsapp_trigger)

    # 8. Mobile Viewport Layout Container
    mobile_canvas = ft.Container(
        width=390,
        content=ft.Column(
            [
                metrics_row,
                emergency_panel,
                transcript_card,
                smart_actions_card,
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            scroll=ft.ScrollMode.AUTO,
        ),
        padding=12,
    )

    page.add(mobile_canvas)

    # 8. Interactive Checkbox Generator
        # 8. Interactive Checkbox Generator (Polished Layout Architecture)
    def build_task_checkbox(task_item: Dict[str, Any]) -> ft.Control:
        """Dynamically generates a beautifully spaced, non-overlapping task row item."""
        def handle_checkbox_toggle(e: ft.ControlEvent) -> None:
            task_item["completed"] = bool(e.control.value)
            if e.control.value:
                e.control.label_style = ft.TextStyle(
                    decoration=ft.TextDecoration.LINE_THROUGH,
                    color=ft.Colors.GREY_500,
                )
            else:
                e.control.label_style = ft.TextStyle(
                    decoration=ft.TextDecoration.NONE,
                    color=ft.Colors.WHITE,
                )
            page.update()

        checkbox = ft.Checkbox(
            label=task_item["task"],
            value=task_item.get("completed", False),
            active_color=ft.Colors.AMBER_400,
            check_color=ft.Colors.BLACK,
            label_style=ft.TextStyle(
                color=ft.Colors.GREY_500 if task_item.get("completed", False) else ft.Colors.WHITE,
                decoration=ft.TextDecoration.LINE_THROUGH if task_item.get("completed", False) else ft.TextDecoration.NONE,
                size=13,
            ),
            on_change=handle_checkbox_toggle,
        )

        keyword_tag = ft.Container(
            content=ft.Text(f"#{task_item.get('keyword', 'task')}", size=9, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_300),
            padding=ft.Padding(left=6, top=2, right=6, bottom=2),
            border_radius=6,
            bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.AMBER_400),
        )

        time_tag = ft.Text(task_item.get("timestamp", ""), size=9, color=ft.Colors.GREY_500)

        # Unified vertical layout containing the clean metadata underneath to prevent text crushing
        metadata_row = ft.Row([keyword_tag, time_tag], spacing=8, alignment=ft.MainAxisAlignment.START)

        return ft.Container(
            content=ft.Column(
                [
                    ft.Row([checkbox], expand=True),
                    ft.Container(content=metadata_row, padding=ft.Padding(left=34, top=0, right=0, bottom=0))
                ],
                spacing=4,
            ),
            padding=ft.Padding(left=10, top=8, right=10, bottom=8),
            border_radius=10,
            bgcolor="#12151E",
            border=ft.Border.all(1, ft.Colors.with_opacity(0.06, ft.Colors.WHITE)),
        )


    last_rendered_record_id: Optional[int] = None

    # 9. UI Synchronization Handler (Safe Flet State Loop Update)
    def sync_ui_state() -> None:
        """Safely updates Flet UI components with fresh backend state without resource locks."""
        nonlocal last_rendered_record_id
        # A. Update Transcripts inside the Card Widget
        latest_record = store.get_latest()
        if latest_record is not None and latest_record.id != last_rendered_record_id:
            last_rendered_record_id = latest_record.id
            latest_transcript = latest_record.text
            display_text = latest_transcript
            transcript_card_text.value = display_text

            # Append structured bubble in the scrolling column
            time_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            bubble = ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(
                                    f"ID #{latest_record.id} • {latest_record.source.upper()}",
                                    size=10,
                                    color=ft.Colors.AMBER_300,
                                    weight=ft.FontWeight.W_500,
                                ),
                                ft.Text(time_str, size=10, color=ft.Colors.GREY_500),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        ft.Text(latest_record.text, size=12, color=ft.Colors.WHITE),
                    ],
                    spacing=2,
                ),
                padding=8,
                border_radius=8,
                bgcolor="#141722",
                border=ft.Border.all(1, ft.Colors.with_opacity(0.04, ft.Colors.WHITE)),
            )

            # Limit scroll history to last 15 bubbles
            if len(transcript_feed_column.controls) > 15:
                transcript_feed_column.controls.pop(0)
            transcript_feed_column.controls.append(bubble)

        # B. Concurrently Update Interactive Checkboxes in Smart Actions Column
        with _tasks_lock:
            current_tasks = list(active_tasks)

        actions_badge_text.value = str(len(current_tasks))

        # Check if new tasks need to be rendered
        for task in current_tasks:
            task_id = task.get("id", hash(task["task"]))
            if task_id not in rendered_task_keys:
                rendered_task_keys.add(task_id)

                # Remove placeholder if first task arrives
                if empty_actions_placeholder in actions_column.controls:
                    actions_column.controls.remove(empty_actions_placeholder)

                task_widget = build_task_checkbox(task)
                actions_column.controls.append(task_widget)

        # C. Flush UI State updates
        try:
            page.update()
        except Exception:
            pass

    # Initialize the trigger state tracker flags on the Flet page instance
    page.last_processed_trigger_phrase = ""
    page.last_trigger_timestamp = 0.0
    page.last_whatsapp_launch_time = 0.0

    # 10. Voice Trigger Action Handler for Alert Panels and Deep-Links
    def handle_voice_action_triggers(transcript: str, source: str, metadata: Dict[str, Any]) -> None:
        """Evaluates voice sentences and guarantees native shortcuts trigger cleanly."""
        text_lower = transcript.lower().strip()
        if not text_lower: 
            return

        now = time.time()
        last_phrase = getattr(page, "last_processed_trigger_phrase", "")
        last_time = getattr(page, "last_trigger_timestamp", 0.0)

        # 3-second debounce to block runaway browser tab floods while still allowing later re-triggers
        if text_lower == last_phrase and (now - last_time < 3.0):
            return 
        
        page.last_processed_trigger_phrase = text_lower
        page.last_trigger_timestamp = now

        # A. Emergency Safety Console Reveal & Guidance Protocol
        emergency_triggers = (
            "emergency", "accident", "help me", "help", "sos", 
            "what to do", "what should i do", "what do i do", "guide me", "save me"
        )
        if any(crisis in text_lower for crisis in emergency_triggers):
            emergency_panel.visible = True
            logger.info("🚨 Critical Crisis / Emergency Intercepted: Opening Safety Center.")

        # B. Call Dialer Redirection (Trigger once)
        if "call" in text_lower or "dial" in text_lower:
            emergency_panel.visible = True
            phone_match = re.search(r"\b(?:call|dial)\s+(\d{3,})\b", text_lower)
            tel_url = f"tel:{phone_match.group(1)}" if phone_match else "tel:112"
            logger.info("📞 Redirecting system launcher to dialing port: %s", tel_url)
            page.launch_url(tel_url)

        # C. Navigation Map Route Integration (Trigger once)
        if "route to" in text_lower or "directions to" in text_lower or "navigate to" in text_lower or "hospital" in text_lower:
            emergency_panel.visible = True
            route_match = re.search(r"(?:route|directions|navigate)\s+to\s+([^.!?]+)", text_lower)
            dest = route_match.group(1).strip() if route_match else "hospital"
            nav_url = f"https://www.google.com/maps/search/{encode_url_payload(dest)}"
            logger.info("📍 Directing device maps layout to turn-by-turn route: %s", nav_url)
            page.launch_url(nav_url)

        # D. WhatsApp Text Deep-Link Forwarding (Trigger once)
        whatsapp_triggers = ("whatsapp", "whats app", "what's up", "whats up")
        if any(wa in text_lower for wa in whatsapp_triggers) or any(cmd in text_lower for cmd in TRANSPORT_COMMANDS):
            emergency_panel.visible = True
            now = time.time()
            if now - getattr(page, "last_whatsapp_launch_time", 0.0) > 3.0:
                page.last_whatsapp_launch_time = now
                encoded = whatsapp_share_manager.get_encoded_payload()
                whatsapp_url = f"https://api.whatsapp.com{encoded}"
                logger.info("💬 Deep-linking hands-free payload straight to WhatsApp: %s", whatsapp_url)
                page.launch_url(whatsapp_url)

        try: 
            page.update()
        except Exception: 
            pass

    # 11. Background Audio Poller Worker Lifecycle
    worker = TranscriptPollerWorker(
        store=store,
        collector=collector,
        on_update_callback=sync_ui_state,
        on_transcript_callback=handle_voice_action_triggers,
    )
    worker.start()

    # Force complete audio engine shutdown when the application window vanishes
    page.on_disconnect = lambda _: worker.stop()


if __name__ == "__main__":
    ft.app(target=main)

