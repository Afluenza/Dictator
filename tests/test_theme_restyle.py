"""Regression tests for the restyle: every window takes its look from theme.py, and the pieces fit.

These assert behaviour a screenshot alone cannot: that no window carries a raw colour of its own,
that the loaded look module is the one the windows use, and that the added controls are present.
"""

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


def test_theme_is_the_only_place_with_hex_colours():
    """A window that hardcodes a colour drifts from the shared style.

    theme.py owns the chrome. notes_store.py owns the note palette, which is note data rather
    than chrome, so it is allowed its own swatches.
    """
    import re
    from pathlib import Path

    import listen_app

    circled = re.compile(r"#[0-9A-Fa-f]{6}\b")
    folder = Path(listen_app.__file__).parent
    owners = {"theme.py", "notes_store.py"}
    offenders = {
        path.name: circled.findall(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.py"))
        if path.name not in owners and circled.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, f"raw hex outside theme.py: {offenders}"


def test_theme_offers_every_icon_it_advertises():
    from listen_app import theme

    for name in theme.ICON_NAMES:
        assert theme.icon(name).cget("size") == (theme.ICON_SIZE, theme.ICON_SIZE)


def test_short_title_keeps_the_buttons_clear():
    from listen_app.notes_gui import STICKY_TITLE_LIMIT, short_title

    assert short_title("# Shopping list") == "Shopping list"
    assert short_title("") == "Untitled note"  # notes_format.title_of supplies this fallback
    long_title = short_title("Call the plumber about the kitchen sink today please", STICKY_TITLE_LIMIT)
    assert len(long_title) == STICKY_TITLE_LIMIT and long_title.endswith("…")


def test_editor_keeps_its_toolbar_and_memo_strip_visible(tmp_path):
    """The memo strip is packed before the editor at the bottom, so neither can push the other out."""
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("check the layout")
        win = NotesWindow(root, store)
        win.geometry("460x440")
        win.update()
        editor_window = win.open_note(note["id"]) or win._editors[note["id"]]
        root.update()

        toolbar = editor_window.editor.toolbar
        assert [b.cget("text") for b in toolbar.winfo_children()] == ["H1", "H2", "H3", "", "B", "I", "U", "S"]
        assert toolbar.winfo_ismapped() and editor_window.memo.winfo_ismapped()
        assert editor_window.editor.text.winfo_ismapped()
        # The memo strip must sit below the editing area, not over it.
        assert editor_window.memo.winfo_y() >= editor_window.editor.winfo_y()
        win.destroy()
    finally:
        root.destroy()


def test_notes_list_row_shows_the_note_colour_and_a_pin(tmp_path):
    from listen_app import theme
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NOTE_COLORS, NotesStore

    import customtkinter as ctk

    root = _root()
    try:
        store = NotesStore(tmp_path / "notes.json")
        note = store.create("pinned note", color="sage")
        store.update(note["id"], pinned=True)
        win = NotesWindow(root, store)
        root.update()
        row = win.list_frame.winfo_children()[0]
        chips = [c for c in row.winfo_children() if c.cget("fg_color") == NOTE_COLORS["sage"]]
        assert chips, "the note's colour is not shown on its list row"
        assert row.cget("fg_color") == theme.SURFACE
        # The pin is drawn as an icon (no text); the title of the row always carries text.
        icon_labels = [c for c in row.winfo_children() if isinstance(c, ctk.CTkLabel) and c.cget("text") == ""]
        assert icon_labels, "a pinned note shows no pin on its row"
        win.destroy()
    finally:
        root.destroy()


def test_disabled_buttons_do_not_look_live():
    """customtkinter dims only the label; set_enabled also flattens the fill."""
    from listen_app import theme
    import customtkinter as ctk

    root = _root()
    try:
        button = ctk.CTkButton(root, text="Stop", **theme.button("secondary"))
        theme.set_enabled(button, "secondary", False)
        assert button.cget("state") == "disabled" and button.cget("fg_color") == theme.DISABLED_FILL
        theme.set_enabled(button, "secondary", True)
        assert button.cget("state") == "normal" and button.cget("fg_color") == theme.SURFACE_HI
    finally:
        root.destroy()
