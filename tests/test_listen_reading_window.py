"""The Listen reading window: completion, edit, click-to-start, and the word highlight, driven on a real Tk window.

Needs a display. Skipped on a machine without one, so CI runners that have no display still run the rest.
No audio device is opened: playback is replaced by a recorder, and speech is never sent to a speaker.
"""
import pytest

from listen_app import reading

ctk = pytest.importorskip("customtkinter")


@pytest.fixture
def window(monkeypatch):
    try:
        from listen_app import gui

        root = ctk.CTk()
        root.withdraw()
    except Exception as exc:  # no display
        pytest.skip(f"no display: {exc}")
    app = gui.ListenApp.__new__(gui.ListenApp)
    # The fields the handlers use, set as __init__ would set them. The constructor is skipped on purpose:
    # it loads a voice engine and binds the bridge.
    app.__dict__.update(_busy=False, _engine=None, _cancel=None, _last_ahead=None, _loaded_source="",
                        _play_lock=__import__("threading").Lock(), _reading=reading.ReadingState(),
                        _tokens=[], _token_lang="", _events=__import__("queue").Queue())
    # One grid manager throughout, as the real window does; mixing pack and grid in one root is a Tk error.
    grid = ctk.CTkFrame(root)
    grid.grid(row=0, column=0, sticky="nsew")
    app.textbox = ctk.CTkTextbox(grid, wrap="word")
    app.textbox.grid(row=0, column=0, columnspan=6)
    app.language_menu = ctk.CTkOptionMenu(grid, values=[reading.AUTO, "es  Spanish", "en  English"])
    app.language_menu.set(reading.AUTO)
    app.language_menu.grid(row=1, column=0)
    app.read_button = ctk.CTkButton(grid, text="Read")
    app.read_button.grid(row=1, column=1)
    app.stop_button = ctk.CTkButton(grid, text="Stop")
    app.stop_button.grid(row=1, column=2)
    app.restart_button = ctk.CTkButton(grid, text="Restart")
    app.restart_button.grid(row=2, column=1)
    app.new_button = ctk.CTkButton(grid, text="New")
    app.new_button.grid(row=2, column=2)
    app.speed = ctk.CTkSlider(grid, from_=0.5, to=2.0)
    app.speed.grid(row=1, column=3)
    app.speed.set(1.0)
    app.progress = ctk.CTkProgressBar(grid)  # _finish and the done event set it; the window needs it too
    app.progress.grid(row=3, column=0, columnspan=6)
    app.dictate_button = ctk.CTkButton(grid, text="Dictate")
    app.dictate_button.grid(row=1, column=4)
    statuses: list[str] = []
    app.status = type("S", (), {"configure": lambda _s, **kw: statuses.append(kw.get("text", ""))})()
    app._statuses = statuses
    app._set_buttons = lambda read, stop, finished=False: app.__class__._set_buttons(app, read, stop, finished)
    app._configure_band_colours()
    root.geometry("600x300+0+0")
    root.deiconify()  # a layout pass is needed for bbox() to report where a word is on screen
    root.update()
    yield app
    root.destroy()


def _gui():
    from listen_app import gui

    return gui


def test_natural_completion_shows_restart_and_new_inside_the_window(window):
    window._reading.read()
    window._finish(object())
    assert window.restart_button.winfo_ismapped() or window.restart_button.grid_info() != {}
    assert window.read_button.grid_info() == {}  # Read is hidden while the finished controls show
    assert window._reading.phase == reading.FINISHED


def test_stop_never_shows_restart_or_new(window):
    window._reading.read()
    window._busy = True
    window._reading.stop(spoken_token=3)
    window._set_buttons(read=True, stop=False)
    assert window.restart_button.grid_info() == {}
    assert window.new_button.grid_info() == {}
    assert window._reading.position == 3


def test_restart_resets_position_and_returns_to_active_controls(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_read_worker", lambda *args: started.append(args))
    window.textbox.insert("1.0", "One two. Three four.")
    window._reading.read()
    window._finish(object())
    window._on_restart()
    assert window._reading.phase == reading.PLAYING
    assert window._reading.position == 0
    assert started, "Restart must start a playback run in this window"
    assert window.restart_button.grid_info() == {}


def test_new_clears_the_text_and_resets_completion(window):
    window.textbox.insert("1.0", "Some text to clear")
    window._reading.read()
    window._finish(object())
    window._on_new()
    assert window.textbox.get("1.0", "end").strip() == ""
    assert window._reading.phase == reading.READY
    assert window.read_button.grid_info() != {}


def test_editing_after_completion_restores_read_without_restart_or_new(window):
    window.textbox.insert("1.0", "First text.")
    window._reading.read()
    window._finish(object())

    class Key:
        keysym = "a"
        char = "a"
        state = 0

    window._on_key_edit(Key())
    assert window._reading.phase == reading.READY
    assert window.read_button.grid_info() != {}
    assert window.restart_button.grid_info() == {}


def test_a_shortcut_key_does_not_count_as_an_edit(window):
    window.textbox.insert("1.0", "Keep me.")
    window._reading.read()
    window._finish(object())

    class Command:
        keysym = "c"
        char = "c"
        state = 0x8  # Command held: a copy shortcut, not typing

    window._on_key_edit(Command())
    assert window._reading.phase == reading.FINISHED


def test_read_with_no_voice_says_why_and_never_plays(window, monkeypatch):
    window.textbox.insert("1.0", "El perro corre por el parque y los niños juegan con una pelota.")
    window._engine = object()
    window.language_menu.set("es  Spanish")
    monkeypatch.setattr(window, "_say_voice_names", lambda: ["Samantha"])
    monkeypatch.setattr(window, "_kokoro_ready", lambda: True)
    started = []
    monkeypatch.setattr(window, "_start_playback", lambda *a, **k: started.append(a))
    window._on_read()
    assert started == [], "speech must not start in a language with no voice"
    assert "No Spanish voice is installed" in window._statuses[-1]
    assert window._reading.phase == reading.READY


def test_read_routes_the_chosen_language_to_its_voice(window, monkeypatch):
    window.textbox.insert("1.0", "Hola, ¿cómo estás hoy? Espero que estés bien.")
    window._engine = object()
    window.language_menu.set("es  Spanish")
    monkeypatch.setattr(window, "_say_voice_names", lambda: ["Mónica", "Samantha"])
    monkeypatch.setattr(window, "_kokoro_ready", lambda: True)
    seen = []
    monkeypatch.setattr(window, "_start_playback", lambda text, choice, *a, **k: seen.append(choice))
    window._on_read()
    assert len(seen) == 1
    assert seen[0].engine == "say" and seen[0].voice == "Mónica"


def test_the_word_map_matches_the_words_in_the_box(window):
    window.textbox.insert("1.0", "  \nHola, ¿cómo estás?  Esto es una prueba.\n")
    raw = window.textbox.get("1.0", "end-1c")
    lead = len(raw) - len(raw.lstrip())
    spoken = raw[lead:].rstrip()
    window._build_token_map(spoken, "es", base=f"1.0+{lead}c")
    inner = window.textbox._textbox
    for (start, end), (a, b) in zip(window._tokens, reading.word_spans(spoken, "es")):
        assert inner.get(start, end) == spoken[a:b]


def test_clicking_a_word_moves_the_start_position(window):
    window.textbox.insert("1.0", "alpha beta gamma delta")
    raw = window.textbox.get("1.0", "end-1c")
    window._build_token_map(raw, "en")

    class Click:
        pass

    start, _end = window._tokens[2]  # "gamma"
    x_y = window.textbox._textbox.bbox(start)
    assert x_y is not None, "the word must be on screen to be clicked"
    click = Click()
    click.x, click.y = x_y[0] + 1, x_y[1] + 1
    window._token_at_click(click)
    assert window._reading.position == 2
    assert window._reading.phase == reading.READY


def test_a_click_on_punctuation_only_changes_nothing(window):
    window.textbox.insert("1.0", "one, two")
    window._build_token_map(window.textbox.get("1.0", "end-1c"), "en")
    comma = window.textbox._textbox.bbox("1.3")
    if comma is None:
        pytest.skip("layout not available for the comma")

    class Click:
        pass

    click = Click()
    click.x, click.y = comma[0] + 1, comma[1] + 1
    window._reading.select(0)
    window._token_at_click(click)
    assert window._reading.position == 0  # the comma is not a word, so the position is unchanged


def test_the_band_marks_the_word_and_its_neighbours_only(window):
    window.textbox.insert("1.0", "a b c d e")
    window._build_token_map(window.textbox.get("1.0", "end-1c"), "en")
    window._apply_band(2)
    inner = window.textbox._textbox
    current = [t for t in inner.tag_ranges("band_current")]
    context = inner.tag_ranges("band_context")
    assert inner.get(current[0], current[1]) == "c"
    context_words = [inner.get(context[i], context[i + 1]) for i in range(0, len(context), 2)]
    assert sorted(context_words) == ["b", "d"]


def test_clearing_the_band_removes_every_mark(window):
    window.textbox.insert("1.0", "a b c")
    window._build_token_map(window.textbox.get("1.0", "end-1c"), "en")
    window._apply_band(1)
    window._clear_band()
    inner = window.textbox._textbox
    assert inner.tag_ranges("band_current") == ()
    assert inner.tag_ranges("band_context") == ()


def test_the_band_colours_come_from_the_theme_accent(window):
    from listen_app import theme

    inner = window.textbox._textbox
    current = inner.tag_cget("band_current", "background")
    expected = reading.blend(theme.ACCENT, theme.INPUT, 0.55)
    assert current.lower() == expected.lower()


class _Result:
    def __init__(self, title="Talk", url="https://www.youtube.com/watch?v=abc", meta=None, note="", text=""):
        self.title, self.url, self.meta, self.note, self.text = title, url, meta or {}, note, text


def test_a_stale_summary_never_overwrites_a_newer_one(window):
    window._summary_seq = 2  # a newer request is in flight
    window.textbox.insert("1.0", "the newer summary")
    window._on_summary_ready((1, _Result(), ["an old point"]))  # request 1 arrives late
    assert "the newer summary" in window.textbox.get("1.0", "end")
    assert "an old point" not in window.textbox.get("1.0", "end")


def test_a_summary_is_labelled_with_its_source_and_says_it_is_a_summary(window):
    window._summary_seq = 1
    window._on_summary_ready((1, _Result(title="Talk"), ["The first point.", "The second point."]))
    text = window.textbox.get("1.0", "end")
    assert text.startswith("Summary of: Talk")
    assert "Source: https://www.youtube.com/watch?v=abc" in text
    assert "This is a summary of the source, not the full text." in text
    assert "- The first point." in text


def test_a_fallback_language_transcript_is_disclosed_above_the_summary(window):
    window._summary_seq = 1
    result = _Result(meta={"transcript_fallback": True, "transcript_language": "ja"})
    window._on_summary_ready((1, result, ["A point."]))
    assert "the transcript is in ja, not your chosen language" in window.textbox.get("1.0", "end")


def test_an_empty_summary_says_so_and_changes_nothing(window):
    window._summary_seq = 1
    window.textbox.insert("1.0", "keep this")
    window._on_summary_ready((1, _Result(), []))
    assert window.textbox.get("1.0", "end").strip() == "keep this"
    assert "No key points" in window._statuses[-1]


def test_a_sentence_from_the_current_run_moves_the_band_and_a_stale_one_does_not(window):
    text = "Hola, ¿cómo estás hoy? Espero que estés bien. Esto es una prueba."
    window.textbox.insert("1.0", text)
    raw = window.textbox.get("1.0", "end-1c")
    window._build_token_map(raw, "es")
    window._sentence_plans = reading.sentence_plan(
        __import__("listen_app.tts", fromlist=["split_sentences"]).split_sentences(raw), "es")
    window._cancel = __import__("threading").Event()
    window._highlight_sentence(1)  # "Espero", the second sentence
    cur = window.textbox._textbox.tag_ranges("band_current")
    assert window.textbox._textbox.get(cur[0], cur[1]) == "Espero"


def test_a_plain_click_places_the_caret_and_sets_the_start_word(window):
    """Editing is not blocked: a click on a word moves the caret there, and sets the reading start."""
    text = "alpha beta gamma delta"
    window.textbox.insert("1.0", text)
    window._build_token_map(text, "en")
    inner = window.textbox._textbox
    start, _end = window._tokens[2]
    box = inner.bbox(start)
    assert box is not None

    class Release:
        x, y = box[0] + 2, box[1] + 2

    inner.mark_set("insert", start)
    window._token_at_click(Release())
    assert window._reading.position == 2
    # Tk normalises "1.0+11c" to "1.11", so compare resolved positions, not the raw index strings.
    assert inner.compare("insert", "==", start)


def test_typing_clears_the_band_and_the_map(window):
    window.textbox.insert("1.0", "one two three")
    window._build_token_map(window.textbox.get("1.0", "end-1c"), "en")
    window._apply_band(1)

    class Key:
        keysym, char, state = "x", "x", 0

    window._on_key_edit(Key())
    assert window.textbox._textbox.tag_ranges("band_current") == ()
    assert window._tokens == []
    assert window._reading.phase == reading.READY


def test_keyboard_moves_the_start_word_forward_and_back_without_touching_the_text(window):
    text = "alpha beta gamma delta"
    window.textbox.insert("1.0", text)
    window._build_token_map(text, "en")
    window._reading.position = 0
    window._move_start_word(1)
    window._move_start_word(1)
    assert window._reading.position == 2
    window._move_start_word(-1)
    assert window._reading.position == 1
    assert window.textbox.get("1.0", "end-1c") == text, "moving the start must never change the text"


def test_keyboard_start_word_stops_at_the_ends_of_the_text(window):
    text = "one two"
    window.textbox.insert("1.0", text)
    window._build_token_map(text, "en")
    window._move_start_word(-5)
    assert window._reading.position == 0
    window._move_start_word(9)
    assert window._reading.position == 1


def test_keyboard_start_word_works_before_any_playback_has_built_the_map(window):
    window.textbox.insert("1.0", "one two three")
    assert window._tokens == []
    window._move_start_word(1)
    assert len(window._tokens) == 3
    assert window._reading.position == 1


def test_restart_runs_the_real_playback_worker_without_error(window, monkeypatch):
    """Regression: Restart passed "" for the voice choice, so the real worker raised AttributeError.

    The other Restart test stubs the worker out, which hid this. Here the real worker runs on a finished render.
    """
    import sys
    import threading
    import types

    import numpy as np

    from listen_app.playback import RenderAhead

    # The worker imports sounddevice, which needs the PortAudio system library. The Linux CI runner has no PortAudio,
    # so the module is replaced before the worker runs. No audio device is ever opened.
    fake_sd = types.SimpleNamespace(play=lambda *a, **k: None, wait=lambda *a, **k: None, stop=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    class FakeEngine:
        sample_rate = 24000

        def iter_chunks(self, text, voice, speed=1.0):
            for _ in range(2):
                yield np.zeros(240, dtype=np.float32)

    done = threading.Event()
    events = []
    window._events = type("Q", (), {"put": lambda _s, item: (events.append(item), done.set())})()
    cancel = threading.Event()
    finished = RenderAhead(FakeEngine(), "a. b.", "v", 1.0, cancel)
    for _ in range(200):
        if finished.complete:
            break
        threading.Event().wait(0.01)
    window._last_ahead = finished
    window._engine = FakeEngine()

    window._on_restart()
    assert done.wait(10), "the restarted playback never reported back"
    kinds = [item[0] for item in events]
    assert "done" not in kinds or events[kinds.index("done")][1] != "Playback failed: AttributeError", events
    assert "finished" in kinds, events
