def test_gui_module_imports_extractors_it_uses():
    # The window imports the file extractors it uses. A broken import would fail here, not at launch.
    from listen_app import gui

    assert callable(gui.extract_file)
    assert callable(gui.validate_url)
    assert gui.ExtractError is not None
    assert gui.UnsupportedInput is not None


def test_a_failed_load_clears_the_box_so_a_summary_cannot_mislabel_old_text():
    """Wiring rule: the source URL is committed only with text that arrived.

    Before the fix, a failed link load left the previous text in the box AND set the pending
    source to the URL that failed, so Summarize would have labelled old text with the wrong link.
    Proven by driving the handler, not by matching source text: an assertion about one exact
    statement breaks on any refactor that keeps the behaviour, and proves nothing when it passes.
    """
    import queue

    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._events = queue.Queue()
    app._load_seq = 0
    app._loaded_source = "https://example.com/old"
    app._busy = False
    boxed: list[str] = []
    app._set_text = boxed.append
    statuses: list[str] = []
    app.status = type("S", (), {"configure": lambda _self, **kw: statuses.append(kw.get("text", ""))})()

    def fail(_path):
        raise OSError("no such file")

    app._extract_async(lambda: fail("/nowhere.txt"), "Loaded nowhere.txt", "Reading file...")
    while not app._events.empty():
        app._handle(*app._events.get())  # the same call _poll makes

    assert boxed == [""], "a failed load must clear the box, so old text cannot be summarised"
    assert app._loaded_source == "", "a failed load must forget the source it never got text from"
    assert statuses[-1] == "no such file", "the reason reaches the status line"


def test_a_transcript_reaches_the_box_and_is_pasted_at_the_cursor():
    """The dictation wiring, with no microphone and no keystroke leaving the machine.

    The window object is built without its constructor (no engine load, no bridge, no mainloop),
    the transcript arrives exactly as the event queue delivers it, and paste_text is captured.
    """
    import pytest

    from listen_app import gui, paste

    try:
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
    except Exception as exc:  # no display
        pytest.skip(f"no display: {exc}")

    class Button:
        """Stands in for the CTkButton: records what the window asked of it."""

        def __init__(self):
            self.seen = []

        def configure(self, **kwargs):
            self.seen.append(kwargs)

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._busy = False
    app._loaded_source = "https://example.com/old"  # a stale source from an earlier page
    app._paste = True
    app.dictate_button = Button()

    boxed: list[str] = []
    statuses: list[str] = []
    app._set_text = boxed.append
    app.status = type("S", (), {"configure": lambda _self, **kw: statuses.append(kw.get("text", ""))})()

    pasted: list[str] = []
    original = paste.paste_text
    paste.paste_text = lambda text, *args, **kwargs: pasted.append(text)
    try:
        app._on_dictated("Open the notes window please.")
    finally:
        paste.paste_text = original
        root.destroy()

    assert boxed == ["Open the notes window please."]
    assert pasted == ["Open the notes window please."]
    assert app._loaded_source == "", "speech is not a page, so no source is attributed to it"
    assert app.dictate_button.seen[-1]["text"] == "Dictate", "the button goes back to Dictate"
    assert "pasted at the cursor" in statuses[-1]


def test_a_failed_paste_says_the_text_is_still_in_the_box():
    """Paste can fail for a missing Accessibility grant. That is partial success, not silence."""
    import pytest

    from listen_app import gui, paste

    try:
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
    except Exception as exc:
        pytest.skip(f"no display: {exc}")

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._busy = False
    app._loaded_source = ""
    app._paste = True
    app.dictate_button = type("B", (), {"configure": lambda _self, **_kw: None})()
    boxed: list[str] = []
    statuses: list[str] = []
    app._set_text = boxed.append
    app.status = type("S", (), {"configure": lambda _self, **kw: statuses.append(kw.get("text", ""))})()

    def refuse(_text, *args, **kwargs):
        raise paste.PasteError("osascript failed: not allowed to send keystrokes")

    original = paste.paste_text
    paste.paste_text = refuse
    try:
        app._on_dictated("a dictated line")
    finally:
        paste.paste_text = original
        root.destroy()

    assert boxed == ["a dictated line"], "the text is not lost when the paste fails"
    assert "could not paste at the cursor" in statuses[-1]
    assert "not allowed to send keystrokes" in statuses[-1]


# ----- defects found live (2026-10-21) --------------------------------------
def test_the_window_button_follows_a_hotkey_that_started_the_mic():
    """Measured defect: pressing Alt+Shift+D started the mic (bridge said recording: true) but the
    button still read "Dictate", because only the button's own handler relabelled it.

    The bridge callback runs on a request thread, so it may not touch widgets: it queues a state
    event, and the Tk thread applies it.
    """
    import queue

    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._events = queue.Queue()
    app._dictation = None
    app._loaded_source = ""
    app._busy = False
    labels: list[str] = []
    app.dictate_button = type("B", (), {"configure": lambda _self, **kw: labels.append(kw["text"])})()
    statuses: list[str] = []
    app.status = type("S", (), {"configure": lambda _self, **kw: statuses.append(kw.get("text", ""))})()

    class Recording:
        def __init__(self):
            self.recording = False

        def start(self):
            self.recording = True

        def stop(self, then=None):
            self.recording = False

    one = Recording()  # the real _session() caches its recorder; the fake must too
    app._session = lambda: one

    assert app._dictate_toggle() is True
    assert app._dictate_toggle() is False

    kinds = []
    while not app._events.empty():
        kind, payload = app._events.get_nowait()
        kinds.append((kind, payload))
        app._handle(kind, payload)
    assert kinds == [("dictation_state", True), ("dictation_state", False)], kinds
    assert labels == ["Stop", "Dictate"], "the button follows the real microphone state"

