"""Builds the notes windows on a real Tk display. Skipped where no display is available."""
import pytest

tk = pytest.importorskip("tkinter")


def _root():
    try:
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        return root
    except Exception as exc:  # no display
        pytest.skip(f"no display: {exc}")


def test_notes_window_lists_creates_and_saves(tmp_path):
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        store.create("# First")
        win = NotesWindow(root, store)
        win.new_note()
        assert len(store.all()) == 2
        win.destroy()
    finally:
        root.destroy()


def test_editor_round_trips_markup_and_saves_on_change(tmp_path):
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("[b]bold[/b] text")
        win = NotesWindow(root, store)
        win.open_note(note["id"])
        editor = win._editors[note["id"]].editor
        assert editor.markup() == "[b]bold[/b] text"
        editor.text.insert("end", " more")
        editor._changed()
        assert store.get(note["id"])["markup"] == "[b]bold[/b] text more"
        win.destroy()
    finally:
        root.destroy()


def test_sticky_pin_and_minimise_persist(tmp_path):
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("sticky")
        win = NotesWindow(root, store)
        win.pop_out(note["id"])
        sticky = win._stickies[note["id"]]
        sticky._toggle_pin()
        assert store.get(note["id"])["pinned"] is True
        sticky._toggle_minimize()
        assert store.get(note["id"])["minimized"] is True
        sticky._toggle_minimize()
        assert store.get(note["id"])["minimized"] is False
        win.destroy()
    finally:
        root.destroy()


def test_colour_change_reaches_both_open_windows(tmp_path):
    """A colour picked in either window repaints the other one, and the list row."""
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NOTE_COLORS, NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("colour")
        win = NotesWindow(root, store)
        win.open_note(note["id"])
        win.pop_out(note["id"])
        editor_window, sticky = win._editors[note["id"]], win._stickies[note["id"]]

        sticky._set_color("sage")
        assert store.get(note["id"])["color"] == "sage"
        assert editor_window.editor.text.cget("bg") == NOTE_COLORS["sage"]

        editor_window._set_color("rose")
        assert sticky.title_bar.cget("fg_color") == NOTE_COLORS["rose"]
        assert sticky.body.text.cget("bg") == NOTE_COLORS["rose"]
        win.destroy()
    finally:
        root.destroy()


def test_sticky_shows_the_colour_picker_while_minimised(tmp_path):
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("mini")
        win = NotesWindow(root, store)
        win.pop_out(note["id"])
        sticky = win._stickies[note["id"]]
        sticky._toggle_minimize()
        assert sticky.body.winfo_ismapped() == 0
        assert sticky.style_button.winfo_exists()
        sticky._open_style()  # the style popover still opens while minimised
        assert sticky._style_popover is not None and sticky._style_popover.winfo_exists()
        win.destroy()
    finally:
        root.destroy()


def test_deleting_a_note_closes_its_editor_and_sticky(tmp_path):
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("doomed")
        win = NotesWindow(root, store)
        win.open_note(note["id"])
        win.pop_out(note["id"])
        win._editors[note["id"]]._delete()
        assert store.all() == []
        assert note["id"] not in win._editors
        assert note["id"] not in win._stickies
        win.refresh()  # must not raise on a note that is gone
        win.destroy()
    finally:
        root.destroy()
