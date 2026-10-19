"""Text-to-speech engines behind one interface.

KokoroEngine: open-weight Kokoro-82M (Apache-2.0). The ONNX graph runs directly
on onnxruntime. English phonemes come from misaki (Apache-2.0) with no espeak
fallback. No GPL module is imported, and nothing is downloaded at runtime.
SayEngine:    macOS `say` fallback, Darwin only, no model download.

Both engines return float32 mono samples at `sample_rate`, and both expose
`iter_chunks`, which synthesizes one sentence at a time so playback can start
before the whole document is rendered.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

import numpy as np

MIN_SPEED = 0.5
MAX_SPEED = 2.0
KOKORO_RATE = 24000
MAX_TOKENS = 510  # Kokoro's context limit, excluding the two boundary pad tokens.
SAY_BASE_WPM = 175  # macOS `say` default rate; speed 1.0 maps to this.
SENTENCE_GAP_S = 0.2
INTRA_OP_THREADS = 4  # ONNX Runtime CPU threads for the Kokoro graph
G2P_MODEL = "en_core_web_sm"  # spaCy model misaki loads; must already be installed locally.

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
MAX_PIECE_CHARS = 300  # measured ~1.1 tokens per char; 300 chars stays under MAX_TOKENS with margin


def clamp_speed(speed: float) -> float:
    if not isinstance(speed, (int, float)) or speed != speed:
        raise ValueError(f"speed must be a number, got {speed!r}")
    return min(MAX_SPEED, max(MIN_SPEED, float(speed)))


def _wrap_piece(piece: str, limit: int = MAX_PIECE_CHARS) -> list[str]:
    """Break one over-long piece at word boundaries. Kokoro silently drops tokens past MAX_TOKENS."""
    pieces: list[str] = []
    current = ""
    for word in piece.split():
        while len(word) > limit:  # a single word longer than the limit is cut hard
            if current:
                pieces.append(current)
                current = ""
            pieces.append(word[:limit])
            word = word[limit:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > limit:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def split_sentences(text: str) -> list[str]:
    """Split on sentence punctuation and newlines, then wrap any piece longer than MAX_PIECE_CHARS.
    Empty pieces are dropped."""
    parts = [part.strip() for part in _SENTENCE_RE.split(text) if part and part.strip()]
    return [wrapped for part in parts for wrapped in _wrap_piece(part)]


class TTSEngine(Protocol):
    sample_rate: int

    def iter_chunks(self, text: str, voice: str, speed: float = 1.0) -> Iterator[np.ndarray]: ...

    def speak(self, text: str, voice: str, speed: float = 1.0) -> np.ndarray: ...


def _join(chunks: Iterator[np.ndarray], sample_rate: int) -> np.ndarray:
    gap = np.zeros(int(SENTENCE_GAP_S * sample_rate), dtype=np.float32)
    parts: list[np.ndarray] = []
    for chunk in chunks:
        if parts:
            parts.append(gap)
        parts.append(chunk)
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)


def _load_g2p():
    """Build the misaki English G2P without letting it download anything.

    misaki calls spacy.cli.download when the model is missing. That is a network
    fetch, so the model must be present before we get here.
    """
    import spacy
    from misaki import en

    if not spacy.util.is_package(G2P_MODEL):
        raise RuntimeError(
            f"spaCy model {G2P_MODEL} is not installed; install it locally before using Kokoro"
        )
    return en.G2P(trf=False, british=False, fallback=None)


def _halve(text: str) -> list[str]:
    """Split text in two at a word boundary, or in the middle of one word that cannot be split."""
    words = text.split()
    if len(words) > 1:
        mid = len(words) // 2
        return [" ".join(words[:mid]), " ".join(words[mid:])]
    if len(text) < 2:
        raise ValueError("text too long for Kokoro's token limit and cannot be split further")
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


@dataclass
class KokoroEngine:
    model_path: str
    voices_path: str
    vocab_path: str
    default_voice: str = "af_heart"
    sample_rate: int = KOKORO_RATE

    def __post_init__(self) -> None:
        import onnxruntime as rt

        self._vocab: dict[str, int] = json.loads(Path(self.vocab_path).read_text(encoding="utf-8"))
        self._g2p = _load_g2p()
        # Measured on the M4 Mac (readthrough/bench_engines.log): full graph optimisation with 4
        # threads renders fastest. 10 threads oversubscribes the cores and is about 2x slower.
        options = rt.SessionOptions()
        options.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = min(INTRA_OP_THREADS, os.cpu_count() or INTRA_OP_THREADS)
        self._session = rt.InferenceSession(self.model_path, sess_options=options, providers=["CPUExecutionProvider"])
        self._voices = np.load(self.voices_path)

    def _render(self, text: str, voice: str, speed: float) -> np.ndarray:
        name = voice or self.default_voice
        if name not in self._voices:
            raise ValueError(f"unknown Kokoro voice {name!r}")
        return self._render_named(text, name, speed)

    def _render_named(self, text: str, name: str, speed: float) -> np.ndarray:
        phonemes, _ = self._g2p(text)
        # shortcut: unknown phoneme characters are dropped silently, no per-char report; upgrade if words vanish
        ids = [self._vocab[c] for c in phonemes if c in self._vocab]
        if len(ids) > MAX_TOKENS:
            # Digits and symbols can give many phonemes per character, so a 300-char piece can exceed the limit.
            # shortcut: halves are rendered separately and joined with no gap; seam not checked by ear
            halves = _halve(text)
            return np.concatenate([self._render_named(part, name, speed) for part in halves])
        if not ids:
            return np.zeros(0, dtype=np.float32)
        style = np.asarray(self._voices[name][len(ids) - 1], dtype=np.float32)
        tokens = np.array([[0, *ids, 0]], dtype=np.int64)
        audio = self._session.run(
            None,
            {
                "tokens": tokens,
                "style": style,
                "speed": np.array([clamp_speed(speed)], dtype=np.float32),
            },
        )[0]
        return np.asarray(audio, dtype=np.float32).ravel()

    def iter_chunks(self, text: str, voice: str, speed: float = 1.0) -> Iterator[np.ndarray]:
        # shortcut: one graph call per sentence, no cross-sentence prosody; upgrade if seams are audible
        for sentence in split_sentences(text):
            yield self._render(sentence, voice, speed)

    def speak(self, text: str, voice: str, speed: float = 1.0) -> np.ndarray:
        return _join(self.iter_chunks(text, voice, speed), self.sample_rate)

    def voice_names(self) -> list[str]:
        return sorted(self._voices.files)


@dataclass
class SayEngine:
    """macOS `say`. Speed maps to words per minute; voice is a `say` voice name."""

    default_voice: str = "Samantha"
    sample_rate: int = 24000
    say_bin: str = "/usr/bin/say"

    def __post_init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("SayEngine needs macOS `say`; use KokoroEngine on this platform")

    def _render(self, text: str, voice: str, speed: float) -> np.ndarray:
        import soundfile as sf

        wpm = round(SAY_BASE_WPM * clamp_speed(speed))
        with tempfile.TemporaryDirectory(prefix="listen-say-") as tmp:
            src = Path(tmp) / "chunk.txt"  # -f keeps text starting with "-" from being read as a flag
            src.write_text(text, encoding="utf-8")
            out = Path(tmp) / "chunk.wav"
            subprocess.run(
                [
                    self.say_bin,
                    "-v", voice or self.default_voice,
                    "-r", str(wpm),
                    "--data-format=LEI16@24000",
                    "-o", str(out),
                    "-f", str(src),
                ],
                check=True,
                timeout=120,
            )
            samples, sr = sf.read(str(out), dtype="float32", always_2d=False)
        if samples.ndim > 1:
            samples = samples.mean(axis=1)
        if sr != self.sample_rate:
            raise RuntimeError(f"say returned sample rate {sr}, expected {self.sample_rate}")
        return samples.astype(np.float32)

    def iter_chunks(self, text: str, voice: str, speed: float = 1.0) -> Iterator[np.ndarray]:
        for sentence in split_sentences(text):
            yield self._render(sentence, voice, speed)

    def speak(self, text: str, voice: str, speed: float = 1.0) -> np.ndarray:
        return _join(self.iter_chunks(text, voice, speed), self.sample_rate)


def default_model_dir() -> Path:
    return Path(os.environ.get("LISTEN_APP_MODEL_DIR", Path.home() / "listen-app" / "models"))


MODEL_FILE = "kokoro-v1.0.int8.onnx"  # default: 92 MB, same output as fp32 by ear (Commander, round 5)
MODEL_FILE_FP32 = "kokoro-v1.0.onnx"  # fallback: 326 MB, kept in models/ for comparison


def make_kokoro(model_dir: Path | None = None, model_file: str = MODEL_FILE) -> KokoroEngine:
    base = model_dir or default_model_dir()
    return KokoroEngine(
        str(base / model_file),
        str(base / "voices-v1.0.bin"),
        str(base / "kokoro-vocab.json"),
    )


if __name__ == "__main__":
    import argparse

    import soundfile as sf

    parser = argparse.ArgumentParser(description="Render text to a WAV file.")
    parser.add_argument("text")
    parser.add_argument("out")
    parser.add_argument("--engine", choices=["kokoro", "say"], default="kokoro")
    parser.add_argument("--voice", default="")
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args()

    engine = make_kokoro() if args.engine == "kokoro" else SayEngine()
    audio = engine.speak(args.text, args.voice, args.speed)
    sf.write(args.out, audio, engine.sample_rate)
    print(f"engine={args.engine} sr={engine.sample_rate} samples={len(audio)} "
          f"duration={len(audio) / engine.sample_rate:.2f}s speed={clamp_speed(args.speed)}")
