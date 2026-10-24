"""dictate: hotkey -> mic -> text, and the paste that follows it.

Nothing here needs a microphone or a display. The speech fixture is a real 16 kHz WAV
(tests/fixtures/dictate-welcome.wav, spoken by macOS `say`), so the local engine path is
exercised on real audio; the paste path is exercised against a fake runner, so no keystroke
ever reaches the machine running the tests.

Licences of what these tests pull in: faster-whisper MIT, ctranslate2 MIT, tokenizers Apache-2.0,
av LGPL-3.0 (as a wheel-bundled shared library), soundfile BSD-3. All GPL-free.
"""

import importlib.util
import os
import threading
import time
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


# ----- the engine factory: local by default, cloud only when chosen ---------
@pytest.fixture
def config(tmp_path, monkeypatch):
    """An empty config dir and no keychain, so no test can read the real saved key or setting.

    Without this, the suite on a machine with dictation configured would pick up the user's own
    cloud key and start posting audio to a live endpoint from a test.
    """
    for var in ("LISTEN_APP_STT_URL", "LISTEN_APP_STT_KEY", "LISTEN_APP_MODEL_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LISTEN_APP_DICTATE_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dictate, "_keyring", lambda: None)
    return tmp_path / "cfg"


def write_model(base: Path) -> None:
    """A model directory the factory treats as installed, without loading anything."""
    (base / dictate.DEFAULT_MODEL_DIR).mkdir(parents=True, exist_ok=True)
    (base / dictate.DEFAULT_MODEL_DIR / "model.bin").write_bytes(b"stub")


def stub_whisper(monkeypatch) -> None:
    """Replace the model class so a test proves the routing choice, not the model's bytes."""
    import faster_whisper

    class StubModel:
        def __init__(self, path, **_kwargs):
            self.path = str(path)

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubModel)


def test_factory_uses_the_local_model_when_it_exists(config, monkeypatch):
    """Auto keeps the original behaviour: a model on disk wins over a configured cloud key."""
    stub_whisper(monkeypatch)
    write_model(config.parent)
    monkeypatch.setenv("LISTEN_APP_STT_URL", "https://example.invalid/v1/audio/transcriptions")
    monkeypatch.setenv("LISTEN_APP_STT_KEY", "not-a-real-key")
    engine = make_engine(config.parent)
    assert isinstance(engine, WhisperEngine)
    assert engine._model.path.endswith(dictate.DEFAULT_MODEL_DIR)


def test_factory_refuses_cloud_unless_a_key_is_saved(config):
    """An address without a key must not quietly go cloud."""
    dictate.save_settings(cloud_url="https://example.invalid/v1/audio/transcriptions")
    with pytest.raises(EngineMissing) as excinfo:
        make_engine(config.parent)
    assert "key" in str(excinfo.value)


def test_factory_goes_cloud_only_when_deliberately_configured(config, monkeypatch):
    monkeypatch.setenv("LISTEN_APP_STT_URL", "https://example.invalid/v1/audio/transcriptions")
    monkeypatch.setenv("LISTEN_APP_STT_KEY", "not-a-real-key")
    engine = make_engine(config.parent)
    assert isinstance(engine, CloudEngine)


def test_the_saved_cloud_choice_beats_a_local_model(config, monkeypatch):
    """The gap the card named: cloud was unreachable whenever a model was on disk.

    A user who chooses cloud is sending audio on purpose; falling back to local would hide that
    choice, and falling back to local silently would be worse than the error.
    """
    stub_whisper(monkeypatch)
    write_model(config.parent)
    dictate.save_key("sk-test-key")
    dictate.save_settings(engine="cloud", cloud_url="https://example.invalid/v1/audio/transcriptions")
    engine = make_engine(config.parent)
    assert isinstance(engine, CloudEngine), "the saved choice decides, not the disk"
    assert engine.url == "https://example.invalid/v1/audio/transcriptions"


def test_the_saved_local_choice_beats_a_saved_key(config, monkeypatch):
    stub_whisper(monkeypatch)
    write_model(config.parent)
    dictate.save_key("sk-test-key")
    dictate.save_settings(engine="local")
    engine = make_engine(config.parent)
    assert isinstance(engine, WhisperEngine)


def test_choosing_local_without_a_model_says_so_instead_of_going_cloud(config):
    """No silent substitution in either direction: the error names the fix."""
    dictate.save_key("sk-test-key")
    dictate.save_settings(engine="local")
    with pytest.raises(EngineMissing) as excinfo:
        make_engine(config.parent)
    assert "No local transcription model" in str(excinfo.value)


def test_an_unknown_saved_engine_falls_back_to_auto(config):
    """A hand-edited or corrupted setting must not wedge dictation."""
    (config).mkdir(parents=True, exist_ok=True)
    (config / "dictate.json").write_text('{"engine": "telepathy"}', encoding="utf-8")
    assert dictate.load_settings()["engine"] == "auto"


def test_the_settings_file_never_contains_the_key(config):
    """The key belongs in the keychain (or the 0600 key file), never in the settings file."""
    dictate.save_key("sk-very-secret-value")
    dictate.save_settings(engine="cloud", cloud_url="https://example.invalid/v1/audio/transcriptions")
    settings = dictate.settings_path().read_text(encoding="utf-8")
    assert "audio/transcriptions" in settings
    assert "sk-very-secret-value" not in settings, settings


def test_a_saved_key_is_readable_and_removable(config):
    assert dictate.save_key("sk-round-trip") in {"keychain", "file"}
    assert dictate.load_key() == "sk-round-trip"
    assert dictate.has_key() is True
    dictate.clear_key()
    assert dictate.load_key() == ""


def test_the_key_file_is_owner_only(config):
    """With no keychain, the fallback file must not be readable by another account.

    The check differs by platform because the mechanism does. On POSIX the mode bits are the
    whole story; on Windows they are not (chmod only sets the read-only attribute), so the test
    reads the real DACL back with icacls instead of trusting a chmod that did nothing. Windows CI
    is what caught that: the file came back 0o666 and the mode assertion failed there.
    """
    import subprocess
    import sys

    assert dictate.save_key("sk-owner-only") == "file"
    target = dictate.key_file()

    if sys.platform == "win32":
        acl = subprocess.run(["icacls", str(target)], capture_output=True, text=True, timeout=30)
        assert acl.returncode == 0, acl.stderr
        output = acl.stdout
        # A grantee line reads "<path> ACCOUNT:(F)". Parsing on ":" is wrong (the path starts with
        # "C:"), so this asserts on the absence of the broad grantees instead, then that the
        # account itself is the one granted.
        for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users", "Users:("):
            assert broad not in output, f"{broad} can still read the key file: {output}"
        user = os.environ.get("USERNAME", "")
        assert user and f"\\{user}:(" in output, f"the key file is not granted to {user}: {output}"
        # icacls prints the grantee names; anything with a broad or a group SID is a leak.
        assert "(I)" not in output, f"an inherited entry is still present: {output}"
        return

    assert target.stat().st_mode & 0o777 == 0o600, oct(target.stat().st_mode & 0o777)


def test_icacls_is_asked_for_an_owner_only_acl(config, monkeypatch):
    """The Windows branch on any platform: inheritance removed, one grantee, the account named.

    Faking os.name would break pathlib on this machine, so the branch is a function of its own and
    driven directly. What this cannot prove on macOS is that Windows enforces the DACL; the Windows
    CI job proves that by reading the ACL back with icacls.
    """
    target = config / "stt-key"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sk-windows-key", encoding="utf-8")
    calls: list[list[str]] = []

    class Ok:
        returncode = 0
        stdout = "processed file"
        stderr = ""

    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **k: calls.append(list(argv)) or Ok())
    dictate._windows_owner_only(target, user="tester")

    assert calls and calls[0][0] == "icacls", calls
    assert "/inheritance:r" in calls[0], calls
    assert "/grant:r" in calls[0] and "tester:F" in calls[0], calls
    assert target.exists()


def test_a_key_that_cannot_be_restricted_is_removed_rather_than_left_shared(config, monkeypatch):
    """If the OS refuses to lock the file down, the key must not sit in a readable file.

    The failure is closed, not silent: the write is undone and the caller is told, rather than a
    readable key being left on disk with a cheerful "saved".
    """
    target = config / "stt-key"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sk-cannot-be-restricted", encoding="utf-8")

    class Failed:
        returncode = 5
        stdout = ""
        stderr = "Access is denied."

    monkeypatch.setattr(dictate.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(OSError) as excinfo:
        dictate._windows_owner_only(target, user="tester")
    assert "restrict" in str(excinfo.value)
    assert not target.exists(), "a key that cannot be locked down must not be left on disk"


def test_cloud_needs_https_except_on_this_machine(config):
    """Audio and a bearer key must not cross plain http to another host."""
    for bad in ("http://example.com/v1/asr", "ftp://example.com/v1/asr", "example.com/v1/asr"):
        with pytest.raises(DictateError):
            dictate.check_cloud_url(bad)
    assert dictate.check_cloud_url("http://127.0.0.1:8765/v1/asr") == "http://127.0.0.1:8765/v1/asr"
    assert dictate.check_cloud_url("http://localhost:8765/") == "http://localhost:8765/"
    assert dictate.check_cloud_url("https://api.openai.com/v1/audio/transcriptions")


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


# ----- the cloud request, against a real socket ----------------------------
class StubEndpoint:
    """A local HTTP server that behaves like an OpenAI-compatible transcription endpoint.

    It is a real socket, so the multipart body and the Authorization header are what
    requests actually sends, not what a mock was told to expect.
    """

    def __init__(self, status: int = 200, payload: bytes = b'{"text": "hello from the cloud"}',
                 delay: float = 0.0) -> None:
        import http.server
        import threading

        self.status = status
        self.payload = payload
        self.delay = delay
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the base class names it
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if outer.delay:
                    import time as _time

                    _time.sleep(outer.delay)
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.payload)))
                self.end_headers()
                self.wfile.write(outer.payload)

            def log_message(self, *_args):  # silence: a test run should not print request lines
                pass

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            # The stdlib default backlog is 5, so eight simultaneous connects get REFUSED and
            # surface as ConnectionError. That is the stub's limit, not the engine's: raise it so
            # the concurrency test measures the client, not the test server.
            request_queue_size = 64

            def handle_error(self, request, client_address):
                pass  # a client that timed out mid-reply is the point of one of these tests

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/audio/transcriptions"


def test_the_cloud_request_carries_a_bearer_key_and_a_wav_file(config):
    with StubEndpoint() as stub:
        engine = CloudEngine(stub.url, "sk-live-key")
        assert engine.transcribe(speech()) == "hello from the cloud"

    sent = stub.requests[0]
    assert sent["path"] == "/v1/audio/transcriptions"
    assert sent["headers"]["Authorization"] == "Bearer sk-live-key"
    assert "multipart/form-data" in sent["headers"]["Content-Type"]
    body = sent["body"]
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'filename="dictation.wav"' in body
    assert b"RIFF" in body, "the payload is a real WAV file, not raw samples"
    assert b'name="file"' in body


def test_a_non_200_is_a_dictate_error_carrying_no_key(config):
    with StubEndpoint(status=429, payload=b'{"error": "slow down sk-live-key"}') as stub:
        engine = CloudEngine(stub.url, "sk-live-key")
        with pytest.raises(DictateError) as excinfo:
            engine.transcribe(speech())
    assert "429" in str(excinfo.value)
    assert "sk-live-key" not in str(excinfo.value)


def test_a_dead_endpoint_is_a_dictate_error_not_a_hang(config):
    """Connection refused must be reported, and fast: no retry loop, no 60 s wait."""
    import socket
    import time

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing is listening on it now
    engine = CloudEngine(f"http://127.0.0.1:{port}/v1/audio/transcriptions", "sk-live-key", timeout=5)
    started = time.monotonic()
    with pytest.raises(DictateError) as excinfo:
        engine.transcribe(speech())
    assert time.monotonic() - started < 5
    assert "sk-live-key" not in str(excinfo.value)


def test_a_slow_endpoint_hits_its_timeout(config):
    with StubEndpoint(delay=2.0) as stub:
        engine = CloudEngine(stub.url, "sk-live-key", timeout=0.4)
        with pytest.raises(DictateError) as excinfo:
            engine.transcribe(speech())
    assert "Timeout" in str(excinfo.value) or "ReadTimeout" in str(excinfo.value)


def test_a_reply_that_is_not_json_is_reported_plainly(config):
    with StubEndpoint(payload=b"<html>gateway error</html>") as stub:
        engine = CloudEngine(stub.url, "sk-live-key")
        with pytest.raises(DictateError) as excinfo:
            engine.transcribe(speech())
    assert "not JSON" in str(excinfo.value)


def test_cloud_refuses_a_plain_http_address_that_is_not_local(config):
    with pytest.raises(DictateError):
        CloudEngine("http://example.com/v1/asr", "sk-live-key")


def test_concurrent_cloud_dictations_stay_separate(config):
    """Eight dictations at once: one WAV per request, all answered, no shared buffer."""
    with StubEndpoint() as stub:
        engine = CloudEngine(stub.url, "sk-live-key")
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def one():
            try:
                barrier.wait(timeout=10)
                results.append(engine.transcribe(speech()))
            except BaseException as exc:  # noqa: BLE001 - collected, then asserted
                errors.append(exc)

        threads = [threading.Thread(target=one) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

    assert errors == []
    assert results == ["hello from the cloud"] * 8
    assert len(stub.requests) == 8
    assert all(request["body"].count(b"RIFF") == 1 for request in stub.requests), "one WAV per request"


def test_a_300_second_dictation_is_accepted_and_beyond_it_is_refused(config):
    """The ceiling is the same for both engines: bounded before any bytes are encoded."""
    at_limit = np.zeros(int(dictate.MAX_SECONDS * dictate.SAMPLE_RATE), dtype=np.float32)
    assert too_long(at_limit) is False
    assert too_long(np.zeros(at_limit.size + 1, dtype=np.float32)) is True


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


def test_the_session_reports_which_engine_actually_ran():
    """The window and the popup both show this label, so it must come from the built engine.

    It is "" before any engine exists: a window that has not dictated yet must not claim one.
    """
    session = Session(engine_factory=lambda: CloudEngine("https://example.invalid/v1/asr", "sk-x"),
                      on_text=lambda _t: None, on_status=lambda _s: None)
    assert session.engine_name == "", "no engine has been built yet"

    done = threading.Event()
    session._finish(np.zeros(dictate.SAMPLE_RATE, dtype=np.float32), lambda _t: done.set())
    assert done.wait(5)
    assert session.engine_name == "cloud"


def test_a_changed_setting_is_picked_up_after_reset():
    """Choosing another engine must not keep dictating with the old one until the app restarts."""
    built: list[str] = []

    def factory():
        built.append(str(len(built)))
        return StubEngine(SPOKEN)

    session = Session(engine_factory=factory, on_text=lambda _t: None)
    session._get_engine()
    session._get_engine()
    assert len(built) == 1, "the engine is built once and reused"

    session.reset()
    session._get_engine()
    assert len(built) == 2, "reset drops the cached engine so the new setting takes effect"


def test_overlapping_finishes_load_the_engine_once():
    """Two dictations finishing at once must share one model load, not build two."""
    built: list[int] = []

    def slow_factory():
        time.sleep(0.2)
        built.append(1)
        return StubEngine(SPOKEN)

    session = Session(engine_factory=slow_factory, on_text=lambda _t: None)
    audio = np.zeros(dictate.SAMPLE_RATE, dtype=np.float32)
    threads = [threading.Thread(target=session._finish, args=(audio, None)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(built) == 1, f"the model was loaded {len(built)} times"
