import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import soundfile as sf

from listen_app.tts import (
    KOKORO_RATE,
    MAX_SPEED,
    MIN_SPEED,
    MAX_PIECE_CHARS,
    KokoroEngine,
    SayEngine,
    clamp_speed,
    split_sentences,
)

MODEL_DIR = Path.home() / "listen-app" / "models"
MODEL = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES = MODEL_DIR / "voices-v1.0.bin"
VOCAB = MODEL_DIR / "kokoro-vocab.json"
SAMPLE_TEXT = "Hello Commander. This is a short test of the open voice engine."


def _missing_kokoro_prereqs() -> list[str]:
    missing = [name for name, path in (("model", MODEL), ("voices", VOICES), ("vocab", VOCAB)) if not path.exists()]
    if importlib.util.find_spec("num2words") is None:
        missing.append("num2words (misaki imports it at load; LGPL-2.1, pinned in pyproject.toml)")
    if importlib.util.find_spec("en_core_web_sm") is None:
        missing.append("spaCy model en_core_web_sm")
    return missing


KOKORO_MISSING = _missing_kokoro_prereqs()
needs_kokoro = pytest.mark.skipif(
    bool(KOKORO_MISSING), reason="Kokoro prerequisites missing: " + "; ".join(KOKORO_MISSING)
)


def _kokoro() -> KokoroEngine:
    return KokoroEngine(str(MODEL), str(VOICES), str(VOCAB))


def test_clamp_speed_limits_range():
    assert clamp_speed(0.1) == MIN_SPEED
    assert clamp_speed(9) == MAX_SPEED
    assert clamp_speed(1.5) == 1.5


def test_clamp_speed_rejects_nan_and_strings():
    with pytest.raises(ValueError):
        clamp_speed(float("nan"))
    with pytest.raises(ValueError):
        clamp_speed("fast")


def test_split_sentences_drops_empty_pieces():
    assert split_sentences("One. Two!\nThree?  ") == ["One.", "Two!", "Three?"]
    assert split_sentences("   \n  ") == []


def test_split_sentences_wraps_long_piece_at_words_without_losing_text():
    # A long sentence with no punctuation must not reach Kokoro whole: its tokens past 510 are dropped.
    long_piece = " ".join(["word"] * 600)
    pieces = split_sentences(long_piece)
    assert len(pieces) > 1
    assert all(len(p) <= MAX_PIECE_CHARS for p in pieces)
    assert " ".join(pieces).split() == long_piece.split()


def test_split_sentences_cuts_a_single_overlong_word():
    pieces = split_sentences("x" * (MAX_PIECE_CHARS * 2 + 5))
    assert all(len(p) <= MAX_PIECE_CHARS for p in pieces)
    assert "".join(pieces) == "x" * (MAX_PIECE_CHARS * 2 + 5)


@needs_kokoro
def test_kokoro_writes_24khz_wav_with_duration(tmp_path):
    engine = _kokoro()
    audio = engine.speak(SAMPLE_TEXT, voice="af_heart", speed=1.0)
    out = tmp_path / "kokoro.wav"
    sf.write(str(out), audio, engine.sample_rate)

    info = sf.info(str(out))
    assert info.samplerate == KOKORO_RATE == 24000
    assert info.duration > 0
    assert info.frames == len(audio)


@needs_kokoro
def test_kokoro_faster_speed_gives_shorter_audio():
    engine = _kokoro()
    slow = engine.speak(SAMPLE_TEXT, voice="af_heart", speed=1.0)
    fast = engine.speak(SAMPLE_TEXT, voice="af_heart", speed=1.5)
    assert len(fast) < len(slow)


@needs_kokoro
def test_kokoro_loads_no_gpl_modules():
    # Run in a fresh interpreter so modules imported by other tests cannot hide a GPL import.
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from listen_app.tts import KokoroEngine\n"
        "m = Path(sys.argv[1])\n"
        "KokoroEngine(str(m / 'kokoro-v1.0.onnx'), str(m / 'voices-v1.0.bin'), str(m / 'kokoro-vocab.json'))\n"
        "bad = [n for n in ('phonemizer', 'espeakng_loader', 'kokoro_onnx') if n in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(MODEL_DIR)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"GPL modules loaded: {result.stdout.strip()}"


def test_say_engine_refuses_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="macOS"):
        SayEngine()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS say only")
def test_say_engine_writes_24khz_wav(tmp_path):
    engine = SayEngine()
    audio = engine.speak(SAMPLE_TEXT, voice="Samantha", speed=1.0)
    out = tmp_path / "say.wav"
    sf.write(str(out), audio, engine.sample_rate)

    info = sf.info(str(out))
    assert info.samplerate == 24000
    assert info.duration > 0


def test_empty_text_gives_empty_audio_without_loading_model():
    engine = SayEngine.__new__(SayEngine)  # no subprocess needed for empty input
    engine.sample_rate = 24000
    engine.default_voice = "Samantha"
    engine.say_bin = "/usr/bin/say"
    assert engine.speak("   ", voice="Samantha").shape == (0,)


def test_halve_splits_at_a_word_boundary_then_mid_word():
    from listen_app.tts import _halve

    assert _halve("one two three four") == ["one two", "three four"]
    assert _halve("abcdef") == ["abc", "def"]
    with pytest.raises(ValueError):
        _halve("x")
