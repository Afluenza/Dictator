"""Speech-to-text: microphone -> text, on this machine.

The shape is deliberately the same as listen_app.tts: one small interface, a local default,
nothing phoned home. Built fresh from the OpenWhispr idea (MIT) rather than copied from it —
hotkey, capture, transcribe, paste — but here the transcription runs in-process on a model
already on disk, so there is no temp file, no daemon and no download at runtime.

Engines
- WhisperEngine: faster-whisper (MIT) on a local CTranslate2 model directory. Default.
- CloudEngine:  opt-in only. Speaks the OpenAI-compatible /audio/transcriptions shape, and is
                constructed only when the user has deliberately chosen it and saved a key, so
                the default path never sends audio anywhere.
- StubEngine:   fixed text, for tests and for a window with no model installed.

Choosing an engine is a saved setting (auto | local | cloud) in ~/.listen-app/dictate.json.
"auto" keeps the original behaviour: the local model when one is on disk, else cloud when it is
configured. The API key is never written into that file and never handed back to the window — it
goes to the OS keychain through `keyring` when that is installed, otherwise to
~/.listen-app/stt-key with owner-only permissions. LISTEN_APP_STT_URL and LISTEN_APP_STT_KEY still
work as environment overrides.

Audio is transcribed from a numpy array, never from a file path: the installed faster-whisper
(1.2.1) calls av.open(metadata_errors=...) and av 19 removed that keyword, so its file decoder
raises TypeError. Decoding here with soundfile is both working and one less moving part.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

import numpy as np

SAMPLE_RATE = 16_000  # what every Whisper build expects; asking the mic for another rate costs a resample
CHANNELS = 1
DEFAULT_MODEL_DIR = "whisper-small.en"
MIN_SECONDS = 0.25  # a click, not speech
MAX_SECONDS = 300.0  # a five-minute dictation; longer is a recording job, not dictation
CLOUD_TIMEOUT_S = 60
CLOUD_MODEL = "whisper-1"
DEFAULT_CLOUD_URL = "https://api.openai.com/v1/audio/transcriptions"
ENGINE_CHOICES = ("auto", "local", "cloud")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
KEYRING_SERVICE = "listen-app"
KEYRING_USER = "stt"


class DictateError(Exception):
    """Something the user should be told, in words that say what to do next."""


class EngineMissing(DictateError):
    """No transcription engine is installed or configured on this machine."""


def model_dir(base: Path | None = None) -> Path:
    """Where the local Whisper model lives. Mirrors listen_app.tts.default_model_dir."""
    root = base or Path(os.environ.get("LISTEN_APP_MODEL_DIR", Path.home() / "listen-app" / "models"))
    return root / DEFAULT_MODEL_DIR


# ----- settings and the API key ---------------------------------------------
def config_dir() -> Path:
    """Where the dictation setting and any fallback key file live. Overridable for tests."""
    return Path(os.environ.get("LISTEN_APP_DICTATE_DIR", Path.home() / ".listen-app"))


def settings_path() -> Path:
    return config_dir() / "dictate.json"


def key_file() -> Path:
    return config_dir() / "stt-key"


def load_settings(path: Path | None = None) -> dict:
    """The saved choice, with anything unreadable falling back to the safe default (auto + OpenAI)."""
    target = Path(path) if path else settings_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    engine = data.get("engine")
    url = data.get("cloud_url")
    return {
        "engine": engine if engine in ENGINE_CHOICES else "auto",
        "cloud_url": url if isinstance(url, str) and url.strip() else DEFAULT_CLOUD_URL,
    }


def save_settings(engine: str | None = None, cloud_url: str | None = None,
                  path: Path | None = None) -> dict:
    """Write the choice. Only the engine and address are stored here — never the key."""
    current = load_settings(path)
    if engine is not None:
        if engine not in ENGINE_CHOICES:
            raise ValueError(f"engine must be one of {', '.join(ENGINE_CHOICES)}")
        current["engine"] = engine
    if cloud_url is not None:
        current["cloud_url"] = check_cloud_url(cloud_url)
    target = Path(path) if path else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(temp, target)
    return current


def check_cloud_url(url: str) -> str:
    """https only, except plain http to this machine's own listener. Returns the URL unchanged."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise DictateError("Cloud transcription needs an address.")
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host in LOCAL_HOSTS:
        return cleaned
    if parts.scheme != "https":
        raise DictateError("Cloud transcription needs an https address (http only for localhost).")
    if not host:
        raise DictateError("Cloud transcription needs a host in the address.")
    return cleaned


def _keyring():
    """keyring when it is installed, else None. It is optional: the app has no hard dependency."""
    try:
        import keyring  # noqa: F401  (presence is the whole test)
    except Exception:  # ImportError, or a backend that explodes on import
        return None
    return keyring


def load_key() -> str:
    """The API key, from the environment, the OS keychain, or the owner-only file. Never logged."""
    from_env = os.environ.get("LISTEN_APP_STT_KEY", "")
    if from_env:
        return from_env
    vault = _keyring()
    if vault is not None:
        try:
            saved = vault.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:  # a locked or headless keychain must not break dictation setup
            saved = None
        if saved:
            return saved
    try:
        return key_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_key(key: str) -> str:
    """Store the key. Returns where it went: "keychain" or "file". The key is never echoed back."""
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("the key is empty")
    vault = _keyring()
    if vault is not None:
        try:
            vault.set_password(KEYRING_SERVICE, KEYRING_USER, cleaned)
            return "keychain"
        except Exception:  # no usable backend: fall through to the file
            pass
    target = key_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(cleaned, encoding="utf-8")
    target.chmod(0o600)
    return "file"


def clear_key() -> None:
    """Forget the stored key, in both places. A missing one is not an error."""
    vault = _keyring()
    if vault is not None:
        try:
            vault.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass
    try:
        key_file().unlink()
    except OSError:
        pass


def has_key() -> bool:
    return bool(load_key())


def describe(settings: dict | None = None, base: Path | None = None) -> str:
    """One plain line naming what dictation will use. It never contains the key."""
    choice = (settings or load_settings())["engine"]
    local_ready = (model_dir(base) / "model.bin").exists()
    keyed = has_key()
    if choice == "local":
        return "local model" if local_ready else "local model (not installed)"
    if choice == "cloud":
        return "cloud" if keyed else "cloud (no key saved)"
    return "local model" if local_ready else ("cloud" if keyed else "not set up")


# ----- engines --------------------------------------------------------------
class STTEngine(Protocol):
    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str: ...


class WhisperEngine:
    """faster-whisper on a local model. Loading costs about 0.4 s, so it is done once and reused."""

    name = "local"

    def __init__(self, path: Path | None = None, compute_type: str = "int8") -> None:
        self.path = Path(path or model_dir())
        if not (self.path / "model.bin").exists():
            raise EngineMissing(
                f"No local transcription model at {self.path}. Dictation needs a faster-whisper "
                "model directory (model.bin, config.json, tokenizer.json, vocabulary.txt)."
            )
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # reported, not raised into a thread
            raise EngineMissing("faster-whisper is not installed (MIT; pip install faster-whisper)") from exc
        self._model = WhisperModel(str(self.path), device="cpu", compute_type=compute_type)

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        if sample_rate != SAMPLE_RATE:
            raise DictateError(f"engine expects {SAMPLE_RATE} Hz audio, got {sample_rate}")
        mono = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        if mono.size == 0:
            return ""
        segments, _info = self._model.transcribe(mono, beam_size=1, language="en", vad_filter=True)
        # Join on spaces: Whisper's own segments each carry a leading space.
        return " ".join(segment.text.strip() for segment in segments).strip()


class CloudEngine:
    """Opt-in BYOK. Never constructed unless the user chose it and saved a key."""

    name = "cloud"

    def __init__(self, url: str, key: str, model: str = CLOUD_MODEL, timeout: float = CLOUD_TIMEOUT_S) -> None:
        if not url or not key:
            raise EngineMissing("Cloud transcription needs an address and an API key.")
        self.url = check_cloud_url(url)
        self.key = key
        self.model = model
        self.timeout = timeout

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        import io

        import soundfile as sf

        buffer = io.BytesIO()
        sf.write(buffer, np.asarray(audio, dtype=np.float32), sample_rate, format="WAV", subtype="PCM_16")
        buffer.seek(0)
        try:
            import requests
        except ImportError as exc:
            raise EngineMissing("cloud transcription needs requests") from exc
        try:
            reply = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.key}"},
                files={"file": ("dictation.wav", buffer, "audio/wav")},
                data={"model": self.model},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DictateError(f"Cloud transcription failed: {type(exc).__name__}") from exc
        if reply.status_code != 200:
            # The body can echo the key on some providers; report the status only.
            raise DictateError(f"Cloud transcription refused it (HTTP {reply.status_code}).")
        try:
            return str(reply.json().get("text", "")).strip()
        except ValueError as exc:
            raise DictateError("Cloud transcription returned something that is not JSON.") from exc


class StubEngine:
    """Fixed text. Tests use it, and a window with no model says so instead of finding out late."""

    name = "stub"

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[tuple[int, int]] = []

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        self.calls.append((len(audio), sample_rate))
        return self.text


def make_engine(base: Path | None = None, settings: dict | None = None) -> STTEngine:
    """Build the engine the user chose. A local model is still the default when the choice is auto.

    "cloud" is honoured even when a local model is installed: the choice is the user's, not a
    guess from what happens to be on disk. Choosing an engine that is not set up raises
    EngineMissing with the fix, rather than silently falling back to the other one.
    """
    saved = settings if settings is not None else load_settings()
    chosen = saved["engine"]
    local = model_dir(base)
    local_ready = (local / "model.bin").exists()
    url = os.environ.get("LISTEN_APP_STT_URL", "") or saved["cloud_url"]
    key = load_key()

    if chosen == "local" or (chosen == "auto" and local_ready):
        if not local_ready:
            raise EngineMissing(
                f"No local transcription model at {local}. Put a faster-whisper model there, "
                "or switch dictation to cloud."
            )
        return WhisperEngine(local)
    if chosen == "cloud" or (chosen == "auto" and key):
        if not key:
            raise EngineMissing("Cloud transcription is selected but no API key is saved.")
        return CloudEngine(url, key)
    raise EngineMissing(
        f"Dictation is unavailable: no model at {local} and no cloud key saved. "
        "Put a faster-whisper model there, or choose cloud and save a key."
    )


def engine_name(engine: object) -> str:
    """What ran, in one word, for the window and the extension. Unknown engines say so."""
    return str(getattr(engine, "name", "") or "unknown")


# ----- microphone -----------------------------------------------------------
class Recorder:
    """Records the default input device into memory until stop().

    sounddevice is imported here rather than at module import, so the rest of the app (and its
    tests) keep working on a machine with no PortAudio.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.error = ""
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            self.error = f"no audio input: {exc}"
            raise DictateError(self.error) from exc
        self._frames = []
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate, channels=self.channels, dtype="float32",
                callback=self._on_audio,
            )
            stream.start()
        except Exception as exc:  # no device, permission denied, device busy
            self.error = f"cannot open the microphone: {exc}"
            raise DictateError(self.error) from exc
        self._stream = stream

    def _on_audio(self, indata, _frames, _time, status) -> None:
        # A dropped block is not fatal; keeping the rest is more use than losing the sentence.
        if getattr(status, "input_overflow", False):
            self.error = "input overflowed; part of the audio was dropped"
        with self._lock:
            self._frames.append(indata.copy())

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self._lock:
            frames, self._frames = self._frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)


def too_short(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bool:
    return len(audio) < int(MIN_SECONDS * sample_rate)


def too_long(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bool:
    return len(audio) > int(MAX_SECONDS * sample_rate)


def transcribe(engine: STTEngine, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    """One dictation in, one line out. Empty audio is an empty string, not an error."""
    if too_short(audio, sample_rate):
        return ""
    if too_long(audio, sample_rate):
        raise DictateError(f"that dictation is longer than {int(MAX_SECONDS)} seconds; record less at a time")
    return engine.transcribe(audio, sample_rate).strip()


# ----- the window's recorder, kept out of the Tk thread ---------------------
class Session:
    """Drives a Recorder + engine pair for the window and the browser extension.

    `on_text` and `on_status` are called from a worker thread, so the window must queue them
    rather than touch Tk from here.
    """

    def __init__(self, engine_factory: Callable[[], STTEngine] = make_engine,
                 on_text: Callable[[str], None] | None = None,
                 on_status: Callable[[str], None] | None = None,
                 recorder_factory: Callable[[], Recorder] = Recorder) -> None:
        self._engine_factory = engine_factory
        self._recorder_factory = recorder_factory
        self._on_text = on_text or (lambda _text: None)
        self._on_status = on_status or (lambda _status: None)
        self._engine: STTEngine | None = None
        self._recorder: Recorder | None = None
        self._lock = threading.Lock()
        self._engine_lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._recorder is not None and self._recorder.recording

    @property
    def engine_name(self) -> str:
        """The engine already built, or "" until one is. The window shows this after a dictation."""
        return engine_name(self._engine) if self._engine is not None else ""

    def reset(self) -> None:
        """Forget the built engine, so the next dictation picks up a changed setting."""
        with self._engine_lock:
            self._engine = None

    def _get_engine(self) -> STTEngine:
        # Two dictations can finish at once; without the lock both would load the model (memory, 0.4 s).
        with self._engine_lock:
            if self._engine is None:
                self._on_status("Loading the transcription engine...")
                self._engine = self._engine_factory()
            return self._engine

    def start(self) -> None:
        with self._lock:
            if self._recorder is not None and self._recorder.recording:
                return
            recorder = self._recorder_factory()
            recorder.start()
            self._recorder = recorder
        self._on_status("Listening. Press Stop when you are done.")

    def stop(self, then: Callable[[str], None] | None = None) -> None:
        """Stop the mic on this thread, then transcribe on a worker so the window stays live."""
        with self._lock:
            recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        audio = recorder.stop()
        threading.Thread(target=self._finish, args=(audio, then), daemon=True).start()

    def _finish(self, audio: np.ndarray, then: Callable[[str], None] | None) -> None:
        text = ""
        try:
            engine = self._get_engine()
            self._on_status("Transcribing...")
            text = transcribe(engine, audio)
            if not text:
                self._on_status("Heard nothing. Press Dictate and try again.")
            else:
                self._on_text(text)
        except DictateError as exc:
            self._on_status(str(exc))
        except Exception as exc:  # reported in the window, never raised in a thread
            self._on_status(f"Dictation failed: {type(exc).__name__}: {exc}")
        # The caller's `then` runs last and owns the final status line: it is the one that knows
        # whether the text reached the cursor. Measured before this: the session's own "Dictated."
        # arrived after the window's paste message and overwrote a failed paste with a cheerful one.
        if then is not None:
            try:
                then(text)
            except Exception as exc:  # the caller's problem must not vanish silently
                self._on_status(f"Dictation failed: {type(exc).__name__}: {exc}")
