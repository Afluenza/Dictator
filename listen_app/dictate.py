"""Speech-to-text: microphone -> text, on this machine.

The shape is deliberately the same as listen_app.tts: one small interface, a local default,
nothing phoned home. Built fresh from the OpenWhispr idea (MIT) rather than copied from it —
hotkey, capture, transcribe, paste — but here the transcription runs in-process on a model
already on disk, so there is no temp file, no daemon and no download at runtime.

Engines
- WhisperEngine: faster-whisper (MIT) on a local CTranslate2 model directory. Default.
- CloudEngine:  opt-in only. Speaks the OpenAI-compatible /audio/transcriptions shape, and is
                constructed only when both LISTEN_APP_STT_URL and LISTEN_APP_STT_KEY are set,
                so the default path never sends audio anywhere.
- StubEngine:   fixed text, for tests and for a window with no model installed.

Audio is transcribed from a numpy array, never from a file path: the installed faster-whisper
(1.2.1) calls av.open(metadata_errors=...) and av 19 removed that keyword, so its file decoder
raises TypeError. Decoding here with soundfile is both working and one less moving part.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

SAMPLE_RATE = 16_000  # what every Whisper build expects; asking the mic for another rate costs a resample
CHANNELS = 1
DEFAULT_MODEL_DIR = "whisper-small.en"
MIN_SECONDS = 0.25  # a click, not speech
MAX_SECONDS = 300.0  # a five-minute dictation; longer is a recording job, not dictation
CLOUD_TIMEOUT_S = 60
CLOUD_MODEL = "whisper-1"


class DictateError(Exception):
    """Something the user should be told, in words that say what to do next."""


class EngineMissing(DictateError):
    """No transcription engine is installed or configured on this machine."""


def model_dir(base: Path | None = None) -> Path:
    """Where the local Whisper model lives. Mirrors listen_app.tts.default_model_dir."""
    root = base or Path(os.environ.get("LISTEN_APP_MODEL_DIR", Path.home() / "listen-app" / "models"))
    return root / DEFAULT_MODEL_DIR


# ----- engines --------------------------------------------------------------
class STTEngine(Protocol):
    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str: ...


class WhisperEngine:
    """faster-whisper on a local model. Loading costs about 0.4 s, so it is done once and reused."""

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
    """Opt-in BYOK. Never constructed unless the user set both the URL and the key."""

    def __init__(self, url: str, key: str, model: str = CLOUD_MODEL, timeout: float = CLOUD_TIMEOUT_S) -> None:
        if not url or not key:
            raise EngineMissing("Cloud transcription needs both LISTEN_APP_STT_URL and LISTEN_APP_STT_KEY.")
        self.url = url
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

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[tuple[int, int]] = []

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        self.calls.append((len(audio), sample_rate))
        return self.text


def make_engine(base: Path | None = None) -> STTEngine:
    """Local first. Cloud only when the user deliberately configured it."""
    local = model_dir(base)
    if (local / "model.bin").exists():
        return WhisperEngine(local)
    url = os.environ.get("LISTEN_APP_STT_URL", "")
    key = os.environ.get("LISTEN_APP_STT_KEY", "")
    if url and key:
        return CloudEngine(url, key)
    raise EngineMissing(
        f"Dictation is unavailable: no model at {local} and no cloud key set. "
        "Put a faster-whisper model there, or set LISTEN_APP_STT_URL and LISTEN_APP_STT_KEY."
    )


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

    @property
    def recording(self) -> bool:
        return self._recorder is not None and self._recorder.recording

    def _get_engine(self) -> STTEngine:
        if self._engine is None:
            self._on_status("Loading the transcription model...")
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
