"""Voice memos: record from the default microphone, then transcribe on this machine.

Recording uses sounddevice (MIT) and writes 16-bit WAV with soundfile (BSD-3).
Transcription uses faster-whisper (MIT) with a model that is already on disk. Nothing
is sent to a server, and the model is never downloaded silently at runtime.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

RECORD_RATE = 16000  # Whisper expects 16 kHz mono
CHANNELS = 1


class Recorder:
    """Captures mic audio into memory until stop(). Start and stop from any thread."""

    def __init__(self, samplerate: int = RECORD_RATE) -> None:
        self.samplerate = samplerate
        self._blocks: queue.Queue = queue.Queue()
        self._stream = None
        self._lock = threading.Lock()

    def _callback(self, indata, _frames, _time, status) -> None:
        self._blocks.put(indata.copy())

    def start(self) -> None:
        import sounddevice as sd

        with self._lock:
            if self._stream is not None:
                raise RuntimeError("already recording")
            self._stream = sd.InputStream(
                samplerate=self.samplerate, channels=CHANNELS, dtype="float32", callback=self._callback
            )
            self._stream.start()

    def stop(self) -> np.ndarray:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("not recording")
            self._stream.stop()
            self._stream.close()
            self._stream = None
        blocks = []
        while not self._blocks.empty():
            blocks.append(self._blocks.get())
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks, axis=0).reshape(-1)


def save_wav(samples: np.ndarray, path: Path, samplerate: int = RECORD_RATE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, samplerate, subtype="PCM_16")
    return path


def resample(samples: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    """Linear-interpolation resample to rate_out. Whisper only needs a close match."""
    if rate_in == rate_out or samples.size == 0:
        return samples.astype(np.float32)
    n_out = int(round(samples.size * rate_out / rate_in))
    positions = np.linspace(0, samples.size - 1, n_out)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


class Transcriber:
    """Wraps a local faster-whisper model. Loads once, transcribes on demand."""

    def __init__(self, model_dir: Path, compute_type: str = "int8") -> None:
        from faster_whisper import WhisperModel

        if not Path(model_dir).is_dir():
            raise RuntimeError(f"whisper model folder missing: {model_dir}")
        self._model = WhisperModel(str(model_dir), device="cpu", compute_type=compute_type, local_files_only=True)

    def transcribe_file(self, wav: Path) -> str:
        import soundfile as sf

        # Decode with soundfile and pass the samples in. faster-whisper's own decoder
        # calls av.open with an argument that the installed PyAV does not accept.
        samples, rate = sf.read(str(wav), dtype="float32", always_2d=False)
        if samples.ndim > 1:
            samples = samples.mean(axis=1)
        if rate != RECORD_RATE:
            samples = resample(samples, rate, RECORD_RATE)
        # beam 5 measured more accurate than beam 1 on clean and noisy test speech
        # (readthrough/accuracy), at about the same speed.
        segments, _info = self._model.transcribe(samples, beam_size=5, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()


# ----- memo library: each recording keeps its transcript next to it ----------
# A memo is <name>.wav plus, once transcribed, <name>.txt. The sidecar lets the Memos window
# show and reuse the transcript later without running Whisper again.

def transcript_path(wav: Path) -> Path:
    return Path(wav).with_suffix(".txt")


def save_transcript(wav: Path, text: str) -> Path:
    path = transcript_path(wav)
    path.write_text(text, encoding="utf-8")
    return path


def list_memos(folder: Path) -> list[dict]:
    """Every saved memo in `folder`, newest first. Each item: wav, created, text (or "" if not transcribed)."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    memos = []
    for wav in folder.glob("memo-*.wav"):
        sidecar = transcript_path(wav)
        memos.append({
            "wav": wav,
            "created": wav.stat().st_mtime,
            "text": sidecar.read_text(encoding="utf-8") if sidecar.exists() else "",
        })
    return sorted(memos, key=lambda m: m["created"], reverse=True)
