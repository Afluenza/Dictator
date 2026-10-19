"""Tests for the voice-memo helpers that do not need a microphone or a model."""

import numpy as np

from listen_app.memos import resample, save_wav


def test_resample_changes_length_by_rate_ratio():
    samples = np.zeros(24000, dtype=np.float32)
    out = resample(samples, 24000, 16000)
    assert abs(out.size - 16000) <= 1


def test_resample_same_rate_is_identity():
    samples = np.linspace(-1, 1, 100, dtype=np.float32)
    assert np.array_equal(resample(samples, 16000, 16000), samples)


def test_save_wav_round_trips_length(tmp_path):
    import soundfile as sf

    path = save_wav(np.zeros(16000, dtype=np.float32), tmp_path / "memo.wav")
    data, rate = sf.read(str(path))
    assert rate == 16000 and len(data) == 16000
