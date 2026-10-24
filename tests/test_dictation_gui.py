"""The Dictation window and the engine label the user sees. Needs a display, skipped without one.

The window is built on a real Tk root and driven through its own handlers — the same calls a
click makes — so what is asserted is what the user would get, not that some source line exists.
Every test runs against a temporary config dir with no keychain, so nothing can read or write the
user's real saved key.
"""

import pytest

pytest.importorskip("tkinter")


@pytest.fixture
def config(tmp_path, monkeypatch):
    from listen_app import dictate

    for var in ("LISTEN_APP_STT_URL", "LISTEN_APP_STT_KEY", "LISTEN_APP_MODEL_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LISTEN_APP_DICTATE_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dictate, "_keyring", lambda: None)
    return tmp_path / "cfg"


@pytest.fixture
def root():
    import customtkinter as ctk

    try:
        root = ctk.CTk()
        root.withdraw()
        return root
    except Exception as exc:  # no display
        pytest.skip(f"no display: {exc}")


def make_window(root):
    from listen_app.dictation_gui import DictationWindow

    return DictationWindow(root)


def test_the_window_opens_showing_the_saved_choice_and_no_key(config, root):
    from listen_app import dictate

    try:
        win = make_window(root)
        report = win.report()
        assert report["engine"] == "auto"
        assert report["engine_label"] == "Automatic"
        assert report["key_saved"] is False
        assert report["url"] == dictate.DEFAULT_CLOUD_URL
        assert report["key_field"] == "", "a saved key must never be put back into the field"
        win.destroy()
    finally:
        root.destroy()


def test_choosing_cloud_persists_and_is_picked_up_by_the_factory(config, root):
    from listen_app import dictate

    try:
        win = make_window(root)
        win._pick_engine("In the cloud")
        assert dictate.load_settings()["engine"] == "cloud"
        assert win.report()["engine_label"] == "In the cloud"
        win.destroy()
    finally:
        root.destroy()


def test_saving_a_key_stores_it_clears_the_field_and_reports_where(config, root):
    from listen_app import dictate

    try:
        win = make_window(root)
        win.key_entry.insert(0, "sk-typed-by-the-user")
        win.url_entry.delete(0, "end")
        win.url_entry.insert(0, "https://example.invalid/v1/audio/transcriptions")
        win._save_key()

        report = win.report()
        assert report["key_saved"] is True
        assert report["key_field"] == "", "the field is emptied once the key is saved"
        assert "Key saved" in report["status"]
        assert dictate.load_key() == "sk-typed-by-the-user"
        assert "sk-typed-by-the-user" not in report["status"], "the key is never echoed in the UI"
        win.destroy()
    finally:
        root.destroy()


def test_a_plain_http_address_away_from_this_machine_is_refused_in_the_window(config, root):
    from listen_app import dictate

    try:
        win = make_window(root)
        win.key_entry.insert(0, "sk-typed-by-the-user")
        win.url_entry.delete(0, "end")
        win.url_entry.insert(0, "http://example.com/v1/asr")
        win._save_key()

        assert "https" in win.report()["status"]
        assert dictate.has_key() is False, "a refused address must not store the key either"
        win.destroy()
    finally:
        root.destroy()


def test_an_empty_key_is_refused_with_a_plain_reason(config, root):
    try:
        win = make_window(root)
        win._save_key()
        assert "Paste the key" in win.report()["status"]
        win.destroy()
    finally:
        root.destroy()


def test_removing_the_key_says_so_and_forgets_it(config, root):
    from listen_app import dictate

    dictate.save_key("sk-to-be-removed")
    try:
        win = make_window(root)
        assert win.report()["key_saved"] is True
        win._clear_key()
        assert win.report()["key_saved"] is False
        assert "removed" in win.report()["status"]
        assert dictate.load_key() == ""
        win.destroy()
    finally:
        root.destroy()


def test_the_window_says_which_engine_will_actually_run(config, root):
    """The note is the point of the window: it must not claim cloud is ready without a key."""
    from listen_app import dictate

    try:
        win = make_window(root)
        win._pick_engine("In the cloud")
        assert "Audio is sent" in win.report()["note"]

        dictate.save_key("sk-present")
        win._refresh()
        assert "no key saved" not in win.report()["key_state"]

        dictate.clear_key()
        win._refresh()
        assert "no key saved" in win.report()["key_state"]
        assert "key" in win.report()["status"].lower()
        win.destroy()
    finally:
        root.destroy()


# ----- the label the user sees after a dictation ---------------------------
def test_the_status_line_names_the_engine_that_ran():
    """Local and cloud differ in privacy and in cost, so the transcript says which one ran."""
    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._busy = False
    app._loaded_source = ""
    app._paste = False
    app.dictate_button = type("B", (), {"configure": lambda _self, **_kw: None})()
    app._set_text = lambda _text: None
    statuses: list[str] = []
    app.status = type("S", (), {"configure": lambda _self, **kw: statuses.append(kw.get("text", ""))})()

    class Session:
        engine_name = "cloud"

    app._dictation = Session()
    app._on_dictated("a dictated line")
    assert statuses[-1] == "Dictated into the box (cloud)."


def test_no_engine_name_is_claimed_before_one_has_run():
    """A window with no dictation yet must not invent a label."""
    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    assert app._dictation_engine_name() == ""
    app._dictation = type("S", (), {"engine_name": "local"})()
    assert app._dictation_engine_name() == "local"
