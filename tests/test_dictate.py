"""dictate: hotkey -> mic -> text, and the paste that follows it.

Nothing here needs a microphone or a display. The speech fixture is a real 16 kHz WAV
(tests/fixtures/dictate-welcome.wav, spoken by macOS `say`), so the local engine path is
exercised on real audio; the paste path is exercised against a fake runner, so no keystroke
ever reaches the machine running the tests.

Licences of what these tests pull in: faster-whisper MIT, ctranslate2 MIT, tokenizers Apache-2.0,
av LGPL-3.0 (as a wheel-bundled shared library), soundfile BSD-3. All GPL-free.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from listen_app import dictate, paste
from listen_app.dictate import (
    CloudEngine,
    DictateError,
    EngineMissing,
    Recorder,
    Session,
    StubEngine,
    WhisperEngine,
    make_engine,
    model_dir,
    too_long,
    too_short,
    transcribe,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPEECH = FIXTURES / "dictate-welcome.wav"
SPOKEN = "Open the notes window please."
MODEL = model_dir()
_MODEL_MISSING = [part for part, present in (
    ("faster-whisper", importlib.util.find_spec("faster_whisper") is not None),
    (f"model at {MODEL}", (MODEL / "model.bin").exists()),
) if not present]
needs_model = pytest.mark.skipif(bool(_MODEL_MISSING), reason="missing: " + ", ".join(_MODEL_MISSING))


def speech() -> np.ndarray:
    audio, rate = sf.read(SPEECH, dtype="float32")
    assert rate == dictate.SAMPLE_RATE, f"fixture must be {dictate.SAMPLE_RATE} Hz"
    return audio


# ----- the fixture itself ---------------------------------------------------
def test_the_speech_fixture_is_real_16k_mono_audio():
    data, rate = sf.read(SPEECH, dtype="float32")
    assert rate == dictate.SAMPLE_RATE
    assert data.ndim == 1
    assert 1.0 < len(data) / rate < 4.0, "about two seconds of speech"
    assert float(np.abs(data).max()) > 0.05, "not silence"


# ----- the local engine, on real audio --------------------------------------
@needs_model
def test_local_engine_reads_the_spoken_fixture():
    """The whole point: a spoken sentence comes back as those words. Skipped only without a model."""
    engine = WhisperEngine(MODEL)
    assert engine.transcribe(speech()) == SPOKEN


@needs_model
def test_local_engine_never_writes_a_temp_file(monkeypatch, tmp_path):
    """faster-whisper's own file decoder is broken here (av 19 dropped metadata_errors), and a
    file would also leave a second copy of the audio on disk. Audio goes in as an array."""
    engine = WhisperEngine(MODEL)
    calls = []
    real = engine._model.transcribe

    def spy(audio, **kwargs):
        calls.append(type(audio).__name__)
        return real(audio, **kwargs)

    monkeypatch.setattr(engine._model, "transcribe", spy)
    engine.transcribe(speech())
    assert calls == ["ndarray"]


def test_engine_refuses_missing_model(tmp_path):
    with pytest.raises(EngineMissing) as excinfo:
        WhisperEngine(tmp_path / "nope")
    assert "model.bin" in str(excinfo.value)


def test_engine_refuses_a_wrong_sample_rate():
    engine = StubEngine("x")
    assert engine.transcribe(np.zeros(8000, dtype=np.float32), 8000) == "x"  # stub ignores rate
    real = WhisperEngine.__new__(WhisperEngine)  # no model load: only the rate gate is under test
    with pytest.raises(DictateError) as excinfo:
        real.transcribe(np.zeros(8000, dtype=np.float32), 8000)
    assert "16000" in str(excinfo.value)


# ----- the engine factory stays local by default ----------------------------
def test_factory_uses_the_local_model_when_it_exists(tmp_path, monkeypatch):
    """The routing decision: a model on disk wins over a configured cloud key.

    faster_whisper is replaced with a stub so the test proves the choice, not the model's bytes.
    """
    import faster_whisper

    class StubModel:
        def __init__(self, path, **_kwargs):
            self.path = str(path)

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubModel)
    (tmp_path / dictate.DEFAULT_MODEL_DIR).mkdir(parents=True)
    (tmp_path / dictate.DEFAULT_MODEL_DIR / "model.bin").write_bytes(b"stub")
    monkeypatch.setenv("LISTEN_APP_STT_URL", "https://example.invalid/v1/audio/transcriptions")
    monkeypatch.setenv("LISTEN_APP_STT_KEY", "not-a-real-key")
    engine = make_engine(tmp_path)
    assert isinstance(engine, WhisperEngine)
    assert engine._model.path.endswith(dictate.DEFAULT_MODEL_DIR)


def test_factory_refuses_cloud_unless_both_url_and_key_are_set(tmp_path, monkeypatch):
    for var in ("LISTEN_APP_STT_URL", "LISTEN_APP_STT_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LISTEN_APP_STT_URL", "https://example.invalid/v1/audio/transcriptions")
    with pytest.raises(EngineMissing) as excinfo:
        make_engine(tmp_path)  # url without a key must not silently go cloud
    assert "LISTEN_APP_STT_KEY" in str(excinfo.value)


def test_factory_goes_cloud_only_when_deliberately_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("LISTEN_APP_STT_URL", "https://example.invalid/v1/audio/transcriptions")
    monkeypatch.setenv("LISTEN_APP_STT_KEY", "not-a-real-key")
    engine = make_engine(tmp_path)
    assert isinstance(engine, CloudEngine)


def test_cloud_engine_reports_a_status_never_a_body(monkeypatch):
    """A provider can echo the key back in an error body, so only the status is surfaced."""
    engine = CloudEngine("https://example.invalid/v1/audio/transcriptions", "sk-secret-value")

    class Reply:
        status_code = 401
        text = "Incorrect API key provided: sk-secret-value"

        def json(self):
            return {"error": self.text}

    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply())
    with pytest.raises(DictateError) as excinfo:
        engine.transcribe(speech())
    assert "401" in str(excinfo.value)
    assert "sk-secret-value" not in str(excinfo.value)


# ----- guards ---------------------------------------------------------------
def test_a_click_is_not_a_dictation():
    assert too_short(np.zeros(100, dtype=np.float32)) is True
    assert transcribe(StubEngine("should not run"), np.zeros(100, dtype=np.float32)) == ""


def test_a_dictation_over_the_ceiling_is_refused_before_the_engine_runs():
    engine = StubEngine("should not run")
    with pytest.raises(DictateError) as excinfo:
        transcribe(engine, np.zeros(int(dictate.MAX_SECONDS * dictate.SAMPLE_RATE) + 1, dtype=np.float32))
    assert "300" in str(excinfo.value)
    assert engine.calls == []


def test_transcribe_trims_the_engine_output():
    assert transcribe(StubEngine("  hello there \n"), np.zeros(dictate.SAMPLE_RATE, dtype=np.float32)) == "hello there"


# ----- the session the window drives ---------------------------------------
class FakeRecorder:
    """A recorder with no microphone: it returns whatever it was primed with."""

    def __init__(self, audio: np.ndarray | None = None, fail: str = "") -> None:
        self.audio = audio if audio is not None else np.zeros(dictate.SAMPLE_RATE, dtype=np.float32)
        self.fail = fail
        self.recording = False

    def start(self) -> None:
        if self.fail:
            raise DictateError(self.fail)
        self.recording = True

    def stop(self) -> np.ndarray:
        self.recording = False
        return self.audio


def run_session(audio: np.ndarray, engine=None, statuses=None) -> tuple[list[str], str]:
    """Start, stop, and wait for the worker the Session spawns."""
    import threading

    texts: list[str] = []
    done = threading.Event()
    session = Session(
        engine_factory=lambda: engine or StubEngine(SPOKEN),
        on_text=texts.append,
        on_status=(statuses.append if statuses is not None else (lambda _s: None)),
        recorder_factory=lambda: FakeRecorder(audio),
    )
    session.start()
    assert session.recording is True
    session.stop(then=lambda _text: done.set())
    assert done.wait(5), "the session never finished transcribing"
    assert session.recording is False
    return texts, ""


def test_session_delivers_the_transcript_once():
    texts, _ = run_session(np.zeros(dictate.SAMPLE_RATE, dtype=np.float32))
    assert texts == [SPOKEN]


def test_session_says_so_when_nothing_was_heard():
    statuses: list[str] = []
    texts, _ = run_session(np.zeros(10, dtype=np.float32), statuses=statuses)
    assert texts == []
    assert any("Heard nothing" in status for status in statuses)


def test_session_reports_a_dead_microphone_without_raising():
    statuses: list[str] = []
    session = Session(engine_factory=lambda: StubEngine(SPOKEN), on_status=statuses.append,
                      recorder_factory=lambda: FakeRecorder(fail="cannot open the microphone: denied"))
    with pytest.raises(DictateError):
        session.start()
    assert session.recording is False


def test_session_reports_an_engine_failure_instead_of_vanishing():
    statuses: list[str] = []

    def broken():
        raise EngineMissing("no model at /nowhere")

    texts: list[str] = []
    import threading

    done = threading.Event()
    session = Session(engine_factory=broken, on_text=texts.append, on_status=statuses.append,
                      recorder_factory=lambda: FakeRecorder())
    session.start()
    session.stop(then=lambda _t: done.set())
    assert done.wait(5)
    assert texts == []
    assert any("no model at /nowhere" in status for status in statuses)


# ----- paste ---------------------------------------------------------------
class FakeRunner:
    """Stands in for subprocess.run: records argv, answers the clipboard read."""

    def __init__(self, clipboard: str = "previous clipboard") -> None:
        self.clipboard = clipboard
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None):
        self.calls.append(tuple(argv))
        self.inputs.append(input)
        if argv[0] in {"pbpaste", "Get-Clipboard"} or "Get-Clipboard" in argv:
            return type("Done", (), {"returncode": 0, "stdout": self.clipboard, "stderr": ""})()
        if argv[0] == "wl-paste":
            return type("Done", (), {"returncode": 0, "stdout": self.clipboard, "stderr": ""})()
        if argv[0] in {"xclip", "xsel"} and "-o" in argv or "--output" in argv:
            return type("Done", (), {"returncode": 0, "stdout": self.clipboard, "stderr": ""})()
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def test_macos_copies_then_sends_command_v_then_restores():
    runner = FakeRunner()
    paste.paste_text("open the notebook", platform="darwin", runner=runner, sleeper=lambda _s: None)
    tools = [argv[0] for argv in runner.calls]
    assert tools == ["pbpaste", "pbcopy", "osascript", "pbcopy"]
    assert "keystroke \"v\" using command down" in runner.calls[2][2]
    assert runner.inputs[1] == "open the notebook"
    assert runner.inputs[3] == "previous clipboard", "the old clipboard is put back"


def test_an_empty_dictation_pastes_nothing():
    """Heard nothing must not mean: replace whatever the user had copied."""
    runner = FakeRunner()
    paste.paste_text("", platform="darwin", runner=runner, sleeper=lambda _s: None)
    assert runner.calls == []


def test_the_text_never_becomes_a_shell_string():
    runner = FakeRunner()
    hostile = "hello'; rm -rf ~; #"
    paste.paste_text(hostile, platform="darwin", runner=runner, sleeper=lambda _s: None)
    assert all(isinstance(argv, tuple) for argv in runner.calls)
    assert runner.calls[1][0] == "pbcopy"
    assert runner.inputs[1] == hostile, "passed as input, never as an argument"


def test_windows_uses_clip_and_sendkeys():
    runner = FakeRunner()
    paste.paste_text("hi", platform="win32", runner=runner, sleeper=lambda _s: None)
    assert runner.calls[1][0] == "clip.exe"
    assert "SendKeys" in " ".join(runner.calls[2])


def test_linux_prefers_wayland_and_says_what_is_missing():
    runner = FakeRunner()
    which = lambda name: f"/usr/bin/{name}" if name in {"wl-copy", "wtype"} else None  # noqa: E731
    paste.paste_text("hi", platform="linux", runner=runner, sleeper=lambda _s: None, which=which)
    assert [argv[0] for argv in runner.calls] == ["wl-paste", "wl-copy", "wtype", "wl-copy"]

    with pytest.raises(paste.PasteError) as excinfo:
        paste.paste_text("hi", platform="linux", runner=runner, sleeper=lambda _s: None, which=lambda _n: None)
    assert "xdotool" in str(excinfo.value)


def test_a_missing_paste_tool_names_itself():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(paste.PasteError) as excinfo:
        paste.paste_text("hi", platform="darwin", runner=missing, sleeper=lambda _s: None)
    assert "pbpaste is not installed" in str(excinfo.value)


# ----- a defect found live (2026-10-21) -------------------------------------
def test_the_callers_message_is_the_last_word_in_the_status_line():
    """Measured defect: the session's own "Dictated." was emitted after on_text, so it arrived
    after the window's paste message and overwrote it. A failed paste then looked identical to a
    successful one. The session now stops talking once the transcript is handed over."""
    import threading

    statuses: list[str] = []
    texts: list[str] = []
    done = threading.Event()

    session = Session(
        engine_factory=lambda: StubEngine(SPOKEN),
        on_text=texts.append,
        on_status=statuses.append,
        recorder_factory=lambda: FakeRecorder(np.zeros(dictate.SAMPLE_RATE, dtype=np.float32)),
    )
    session.start()
    session.stop(then=lambda _text: (statuses.append("caller's own message"), done.set()))
    assert done.wait(5)

    assert texts == [SPOKEN]
    assert statuses[-1] == "caller's own message", statuses
    assert "Dictated." not in statuses, "the session must not speak over the caller"
