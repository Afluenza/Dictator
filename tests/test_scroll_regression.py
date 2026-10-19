"""Regression: long lists must build and redraw quickly.

Two separate causes, both fixed:

1. CTkScrollableFrame in customtkinter 6.0.0 recurses inside its own scrollbar redraw. A list of
   about 120 notes never returned, and 300 memos hung the Memos window. theme.ScrollList avoids
   that with a plain tk.Scrollbar.
2. The FIRST draw still grew faster than the row count. CTk widgets placed inside a shown canvas
   cost about 52 s at 300 rows, while plain Tk rows in the same canvas cost 0.14 s
   (readthrough/round13/round13-list-backend.md). ScrollList is now a virtual list: it keeps a fixed
   pool of row slots and re-points them on scroll, so a 10,000-row list builds the same number of
   widgets as a 20-row one.

Both tests below fail if either cause returns, and a regression is reported rather than hung: each
runs under a wall-clock alarm.
"""

import signal
import time

import pytest

tk = pytest.importorskip("tkinter")

LIMIT_S = 5.0  # generous: the fixed build takes well under a second at 300 rows
ALARM_S = 60  # wall-clock backstop, so a regressed build fails instead of blocking the suite


class _Timeout(Exception):
    pass


def _timebox(seconds: int):
    """Fail the test rather than hang it. Best effort: a native Tcl call is only interrupted on return."""

    def _fire(_signum, _frame):
        raise _Timeout()

    if not hasattr(signal, "SIGALRM"):  # Windows: no alarm; the timed asserts still run
        return None
    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    return previous


def _unbox(previous) -> None:
    if previous is None:
        return
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def _root():
    try:
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        return root
    except Exception as exc:  # no display
        pytest.skip(f"no display: {exc}")


def test_notes_list_redraws_quickly_at_120_notes(tmp_path):
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    previous = _timebox(ALARM_S)
    try:
        store = NotesStore(tmp_path / "notes.json")
        for i in range(120):
            store.create(f"# Note {i}\nbody {i}")
        start = time.monotonic()
        win = NotesWindow(root, store)
        root.update()  # a shown window, which is the condition the slowdown appears under
        elapsed = time.monotonic() - start
        assert elapsed < LIMIT_S, f"Notes list took {elapsed:.1f}s at 120 notes"
        win.destroy()
    except _Timeout:
        pytest.fail(f"the 120-note Notes list did not finish building within {ALARM_S}s")
    finally:
        _unbox(previous)
        root.destroy()


def test_memos_window_redraws_quickly_at_300_memos(tmp_path):
    import numpy as np

    from listen_app.memo_list import MemosWindow
    from listen_app.memos import save_transcript, save_wav

    for i in range(300):
        wav = save_wav(np.zeros(160, dtype=np.float32), tmp_path / f"memo-{i:04d}.wav")
        save_transcript(wav, f"memo {i}")
    root = _root()
    previous = _timebox(ALARM_S)
    try:
        start = time.monotonic()
        win = MemosWindow(root, on_to_note=lambda t: None, on_to_reminders=lambda t: None, folder=tmp_path)
        root.update()  # a shown window, which is the condition the slowdown appears under
        elapsed = time.monotonic() - start
        assert elapsed < LIMIT_S, f"Memos window took {elapsed:.1f}s at 300 memos"
        win.destroy()
    except _Timeout:
        pytest.fail(f"the 300-memo Memos window did not finish building within {ALARM_S}s")
    finally:
        _unbox(previous)
        root.destroy()


def _notes_window(root, store, notes: int):
    from listen_app.notes_gui import NotesWindow

    for i in range(notes):
        store.create(f"# Note {i:04d}\nbody {i}")
    win = NotesWindow(root, store)
    root.update()
    return win


def test_a_long_list_builds_only_the_rows_in_view(tmp_path):
    """The widget count must follow the window, not the list: that is what makes it virtual."""
    from listen_app.notes_store import NotesStore

    root = _root()
    previous = _timebox(ALARM_S)
    try:
        win = _notes_window(root, NotesStore(tmp_path / "notes.json"), 1000)
        rows = win.list_frame.winfo_children()
        assert 0 < len(rows) < 40, f"built {len(rows)} row widgets for 1000 notes"
        win.destroy()
    except _Timeout:
        pytest.fail(f"the 1000-note list did not finish building within {ALARM_S}s")
    finally:
        _unbox(previous)
        root.destroy()


def test_scrolling_repoints_rows_instead_of_growing_the_pool(tmp_path):
    """After scrolling, the visible rows must match the tail of the list, with no new widgets."""
    from listen_app.notes_gui import short_title
    from listen_app.notes_store import NotesStore

    root = _root()
    previous = _timebox(ALARM_S)
    try:
        store = NotesStore(tmp_path / "notes.json")
        win = _notes_window(root, store, 200)
        built = len(win.list_frame.winfo_children())
        order = [short_title(n["markup"], 34) for n in store.all()]
        win.list_frame._canvas.yview_moveto(1.0)
        root.update()
        visible = win.list_frame.winfo_children()
        assert len(visible) <= built, "the row pool grew while scrolling"
        assert [r.title.cget("text") for r in visible] == order[-len(visible):], "rows are out of order"
        assert visible[-1].note["id"] == store.all()[-1]["id"], "the last row is not the last note"
        win.destroy()
    except _Timeout:
        pytest.fail(f"scrolling the 200-note list did not finish within {ALARM_S}s")
    finally:
        _unbox(previous)
        root.destroy()


def test_a_ticked_reminder_line_stays_ticked_after_scrolling_away():
    """Tick state lives in the panel, not in a row widget, because rows are re-pointed."""
    from listen_app.reminders_panel import RemindersWindow

    root = _root()
    previous = _timebox(ALARM_S)
    try:
        lines = [f"task {i}" for i in range(60)]
        win = RemindersWindow(root, initial_text="\n".join(lines))
        root.update()
        assert win.chosen_lines() == lines
        win._ticks[0] = False  # as the tick box does when the user clicks the first line
        win.list_frame._canvas.yview_moveto(1.0)
        root.update()
        assert win.chosen_lines() == lines[1:], "an unticked line came back ticked after scrolling"
        win.list_frame._canvas.yview_moveto(0.0)
        root.update()
        assert win.list_frame.winfo_children()[0].box.get() == 0, "the first tick box lost its state"
        assert win.chosen_lines() == lines[1:]
        win.destroy()
    except _Timeout:
        pytest.fail(f"scrolling the 60-line Reminders list did not finish within {ALARM_S}s")
    finally:
        _unbox(previous)
        root.destroy()


def test_summary_dialog_rows_scroll_and_target_the_note_they_show(tmp_path):
    """These rows used to be packed into the list frame, so they never scrolled at all."""
    from listen_app import theme
    from listen_app.notes_gui import NotesWindow
    from listen_app.notes_store import NotesStore

    root = _root()
    previous = _timebox(ALARM_S)
    try:
        store = NotesStore(tmp_path / "notes.json")
        for i in range(120):
            store.create(f"# Target {i:04d}")
        win = NotesWindow(root, store)
        win.offer_summary("## key point")
        root.update()
        dialog = win._summary_dialog
        scroll = [w for w in dialog.winfo_children() if isinstance(w, theme.ScrollList)][0]
        assert scroll.winfo_children(), "the summary dialog built no rows"
        scroll._canvas.yview_moveto(1.0)
        root.update()
        row = scroll.winfo_children()[-1]
        target = row.note_id
        assert target, "the last visible summary row is not pointing at a note"
        row.button.invoke()
        root.update()
        assert "key point" in store.get(target)["markup"], "the summary did not land on the row's note"
        win.destroy()
    except _Timeout:
        pytest.fail(f"scrolling the summary dialog did not finish within {ALARM_S}s")
    finally:
        _unbox(previous)
        root.destroy()
