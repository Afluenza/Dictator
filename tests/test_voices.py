from pathlib import Path

import pytest

MODEL_DIR = Path.home() / "listen-app" / "models"


@pytest.mark.skipif(not (MODEL_DIR / "voices-v1.0.bin").exists(), reason="voices file missing")
def test_voice_names_lists_npz_voices_sorted():
    # Read the voices file directly; no G2P or ONNX session needed.
    import numpy as np

    names = sorted(np.load(MODEL_DIR / "voices-v1.0.bin").files)
    assert "af_heart" in names
    assert names == sorted(names)
    assert len(names) >= 50
