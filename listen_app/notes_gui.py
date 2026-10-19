"""Notes window: a list of notes, a rich-text editor per note, and mini sticky pop-outs.

Each note opens in its own editor window. A note can be popped out as a small
sticky window that can be pinned on top, recoloured, minimised to its title, or
closed. Everything saves to NotesStore on each change. Formatting uses the tags in
listen_app.notes_format; the editor shows them as Tk text tags.

The NotesWindow owns every open window for a note, so a colour picked in either the
editor or the sticky repaints both, and deleting a note closes its windows.

Tk objects are only touched from the Tk thread. This module needs no network.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from listen_app import notes_format as nf
from listen_app import theme
from listen_app.notes_store import DEFAULT_COLOR, MAX_OPACITY, MIN_OPACITY, NOTE_COLORS, NotesStore

HEADING_SIZES = {"h1": theme.SIZE_TITLE, "h2": theme.SIZE_HEAD, "h3": theme.SIZE_SUB}
BODY_SIZE = theme.SIZE_BODY
MINI_SIZE = (280, 240)
MINI_TITLE_SIZE = (280, 34)
STICKY_TITLE_LIMIT = 22  # the title strip is ~280 px wide, so a long title is shortened with an ellipsis


def short_title(markup: str, limit: int = 28) -> str:
    """The note's first line, shortened so it cannot run under the buttons beside it."""
    text = nf.title_of(markup).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _ColorMenu(ctk.CTkOptionMenu):
    """Colour picker for one note. The choice is routed through NotesWindow."""

    def __init__(self, master, color: str, on_pick: Callable[[str], None]) -> None:
        super().__init__(
            master, values=list(NOTE_COLORS), width=104, height=28, corner_radius=theme.RADIUS,
            fg_color=theme.SURFACE_HI, button_color=theme.SURFACE_HI, button_hover_color=theme.SURFACE_HOVER,
            text_color=theme.TEXT, font=theme.font(theme.SIZE_SMALL), dropdown_fg_color=theme.SURFACE,
            dropdown_hover_color=theme.ACCENT, dropdown_text_color=theme.TEXT, command=on_pick,
        )
        self.set(color)


class _Editor(ctk.CTkFrame):
    """Rich-text editing area with a toolbar. Styles are Tk tags; saved as note markup."""

    def __init__(self, master, store: NotesStore, note: dict, on_change: Callable[[], None]) -> None:
        super().__init__(master, fg_color="transparent")
        self.store = store
        self.note = note
        self.on_change = on_change
        self._suspend = False
        self._build_toolbar()
        self.text = tk.Text(self, wrap="word", undo=True, relief="flat", padx=10, pady=8,
                            bg=NOTE_COLORS.get(note["color"], NOTE_COLORS[DEFAULT_COLOR]), fg=theme.INK,
                            insertbackground=theme.INK, selectbackground=theme.ACCENT, selectforeground=theme.TEXT,
                            highlightthickness=0, font=(theme.FAMILY, BODY_SIZE))
        self.text.pack(fill="both", expand=True)
        self._configure_tags()
        self._load(note["markup"])
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyRelease>", lambda _e: self._reapply_block_tags())

    # ----- toolbar ---------------------------------------------------------
    def _build_toolbar(self) -> None:
        """Small chips: headings as bold H1-H3, the bullet as a drawn icon, and B/I/U/S in their own style."""
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(4, 0))
        self.toolbar = bar
        self._buttons = {}
        for label, icon_name, action, face in (
            ("H1", None, lambda: self.block("h1"), theme.font(theme.SIZE_HEAD, "bold")),
            ("H2", None, lambda: self.block("h2"), theme.font(theme.SIZE_SUB, "bold")),
            ("H3", None, lambda: self.block("h3"), theme.font(theme.SIZE_BODY, "bold")),
            ("", "bullet", lambda: self.block("bullet"), None),
            ("B", None, lambda: self.inline("bold"), theme.font(theme.SIZE_SMALL, "bold")),
            ("I", None, lambda: self.inline("italic"), theme.font(theme.SIZE_SMALL, "italic")),
            ("U", None, lambda: self.inline("underline"), theme.font(theme.SIZE_SMALL, "underline")),
            ("S", None, lambda: self.inline("strike"), theme.font(theme.SIZE_SMALL, "overstrike")),
        ):
            btn = ctk.CTkButton(
                bar, text=label, width=28 if icon_name is None else 30, command=action,
                image=theme.icon(icon_name, size=14) if icon_name else None, **theme.button("secondary", small=True),
            )
            if face is not None:
                btn.configure(font=face)
            btn.pack(side="left", padx=1)
            self._buttons[label or "bullet"] = btn

    def apply_color(self, color: str) -> None:
        """Repaint the text area for a colour chosen anywhere in the app."""
        self.text.configure(bg=NOTE_COLORS[color])

    # ----- tags -------------------------------------------------------------
    def _configure_tags(self) -> None:
        for name, size in HEADING_SIZES.items():
            self.text.tag_configure(name, font=theme.font(size, "bold"))
        self.text.tag_configure("bullet", lmargin1=18, lmargin2=18)
        self.text.tag_configure("bold", font=theme.font(BODY_SIZE, "bold"))
        self.text.tag_configure("italic", font=theme.font(BODY_SIZE, "italic"))
        self.text.tag_configure("underline", underline=True)
        self.text.tag_configure("strike", overstrike=True)

    def _load(self, markup: str) -> None:
        plain, styles = nf.parse(markup)
        self._suspend = True
        self.text.delete("1.0", "end")
        self.text.insert("1.0", plain)
        for name, spans in nf.styles_to_ranges(styles).items():
            for start, end in spans:
                self.text.tag_add(name, f"1.0+{start}c", f"1.0+{end}c")
        self._reapply_block_tags()
        self._suspend = False
        self.text.edit_reset()

    def _reapply_block_tags(self) -> None:
        for name in ("h1", "h2", "h3", "bullet"):
            self.text.tag_remove(name, "1.0", "end")
        lines = self.text.get("1.0", "end-1c").split("\n")
        for number, line in enumerate(lines, start=1):
            kind = nf.block_kind(line)
            if kind != "p":
                self.text.tag_add(kind, f"{number}.0", f"{number}.end")

    # ----- formatting actions ---------------------------------------------------
    def inline(self, name: str) -> None:
        try:
            start, end = self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            return  # nothing selected
        if self.text.tag_nextrange(name, start, end):
            self.text.tag_remove(name, start, end)
        else:
            self.text.tag_add(name, start, end)
        self._changed()

    def block(self, kind: str) -> None:
        """Apply a line prefix (H1-H3 or bullet) to each line the selection touches.

        Choosing the style a line already has removes it, so the toolbar toggles.
        """
        prefixes = {"h1": "# ", "h2": "## ", "h3": "### ", "bullet": "- "}
        first_line = int(self.text.index("insert").split(".")[0])
        try:
            last_line = int(self.text.index("sel.last").split(".")[0])
        except tk.TclError:
            last_line = first_line
        self._suspend = True
        for line in range(first_line, last_line + 1):
            text = self.text.get(f"{line}.0", f"{line}.end")
            current = nf.block_kind(text)  # bullet, h1, h2, h3 or p; prefixes[] maps each back to its prefix
            if current != "p":
                self.text.delete(f"{line}.0", f"{line}.{len(prefixes[current])}")
            if current != kind:
                self.text.insert(f"{line}.0", prefixes[kind])
        self._suspend = False
        self._changed()

    # ----- save -----------------------------------------------------------------
    def _on_modified(self, _event=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if not self._suspend:
                self._changed()

    def _changed(self) -> None:
        self._reapply_block_tags()
        self.store.update(self.note["id"], markup=self.markup())
        self.on_change()

    def markup(self) -> str:
        plain = self.text.get("1.0", "end-1c")
        styles = [frozenset() for _ in plain]
        for name in nf.INLINE:
            for start, end in self._ranges(name):
                for index in range(start, end):
                    styles[index] = styles[index] | {name}
        return nf.dump(plain, styles)

    def _ranges(self, name: str) -> list[tuple[int, int]]:
        spans = []
        for first, last in zip(self.text.tag_ranges(name)[0::2], self.text.tag_ranges(name)[1::2]):
            spans.append((self._offset(first), self._offset(last)))
        return spans

    def _offset(self, index) -> int:
        return len(self.text.get("1.0", index))


class NoteEditorWindow(ctk.CTkToplevel):
    """Full editor for one note, with colour and a Pop Out button."""

    def __init__(self, master, store: NotesStore, note: dict, on_pop_out: Callable[[dict], None],
                 on_change: Callable[[], None], on_color: Callable[[str, str], None] | None = None,
                 on_delete: Callable[[str, object], None] | None = None) -> None:
        super().__init__(master)
        self.store = store
        self.note_id = note["id"]
        self.title(nf.title_of(note["markup"]))
        self.geometry("460x440")
        self.configure(**theme.body())
        self._on_change = on_change
        self._on_pop_out = on_pop_out
        self._on_color = on_color
        self._on_delete = on_delete

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 0))
        self.pop_out_button = ctk.CTkButton(
            top, text="Pop Out", width=88, image=theme.icon("note", size=14),
            command=lambda: self._on_pop_out(self.store.get(self.note_id)), **theme.button("secondary"),
        )
        self.pop_out_button.pack(side="left", padx=(0, 4))
        self.color_menu = _ColorMenu(top, note["color"], self._set_color)
        self.color_menu.pack(side="left", padx=4)
        self.delete_button = ctk.CTkButton(
            top, text="Delete", width=78, image=theme.icon("trash", size=14, color=theme.ON_ACCENT),
            command=self._delete, **theme.button("danger"),
        )
        self.delete_button.pack(side="right", padx=(4, 0))
        self.reminders_button = ctk.CTkButton(
            top, text="To Reminders", width=118, image=theme.icon("reminder", size=14),
            command=self._to_reminders, **theme.button("secondary"),
        )
        self.reminders_button.pack(side="right", padx=4)

        from listen_app.memo_gui import MemoControl

        # The memo strip claims the bottom first; an expanding editor packed before it would squeeze it out.
        self.memo = MemoControl(self, on_text=self._add_memo_text)
        self.memo.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.editor = _Editor(self, store, note, self._changed)
        self.editor.pack(fill="both", expand=True, padx=8, pady=8)
        self.editor.text.focus_set()

    def _add_memo_text(self, text: str) -> None:
        """Append a voice-memo transcript as a new line at the end of the note."""
        self.editor.text.insert("end", ("\n" if self.editor.text.get("1.0", "end-1c").strip() else "") + text)
        self._changed()

    def _changed(self) -> None:
        self.sync_title()
        self._on_change()

    def sync_title(self) -> None:
        """Re-read the title from the store. The text itself is never reloaded mid-typing."""
        note = self.store.get(self.note_id)
        self.title(nf.title_of(note["markup"]))

    def _set_color(self, color: str) -> None:
        """Set the colour here, and in any other open window for this note."""
        if self._on_color is not None:
            self._on_color(self.note_id, color)
            return
        self.store.update(self.note_id, color=color)
        self.apply_color(color)
        self._on_change()

    def apply_color(self, color: str) -> None:
        self.color_menu.set(color)
        self.editor.apply_color(color)

    def _delete(self) -> None:
        """Delete the note, then close every window showing it before the list refreshes."""
        self.store.delete(self.note_id)
        if self._on_delete is not None:
            self._on_delete(self.note_id, self)  # closes the sticky, drops both entries, refreshes
        else:
            self._on_change()
        self.destroy()

    def _to_reminders(self) -> None:
        """Export each line of this note to the Reminders test list, and show the count read back."""
        from listen_app.reminders_export import TEST_LIST, RemindersError, count_in_list, export_to_reminders

        try:
            created = export_to_reminders(self.store.get(self.note_id)["markup"], TEST_LIST)
            total = count_in_list(TEST_LIST)
            self.title(f"Sent {created} to Reminders ({total} in {TEST_LIST})")
        except RemindersError as err:
            self.title(f"Reminders failed: {err}")


STICKY_BODY = theme.BODY  # dark body under a tinted title bar, as in the Sticky Notepad reference


class _IconButton(ctk.CTkButton):
    """Flat, small title-bar button drawn from a theme icon. set_active tints it when its state is on.

    The icon is dark (theme.INK) because the bar it sits on is a light note tint.
    """

    def __init__(self, master, icon_name: str, command: Callable[[], None], tip: str) -> None:
        super().__init__(master, text="", image=theme.icon(icon_name, color=theme.INK, size=16), width=30, height=26,
                         corner_radius=theme.RADIUS_SMALL, fg_color="transparent",
                         hover_color=theme.CHROME_HOVER, command=command)
        self.tip = tip

    def set_active(self, active: bool) -> None:
        self.configure(fg_color=theme.CHROME_HOVER if active else "transparent")


class _StylePopover(ctk.CTkToplevel):
    """Colour swatches and an opacity slider for one sticky. Changes apply at once and persist."""

    def __init__(self, master, store: NotesStore, note_id: str, on_color: Callable[[str], None]) -> None:
        super().__init__(master)
        self.overrideredirect(True)
        self.store = store
        self.note_id = note_id
        self._on_color = on_color
        self.configure(fg_color=theme.SURFACE)
        ctk.CTkLabel(self, text="Color", text_color=theme.TEXT, font=theme.font(theme.SIZE_SUB, "bold")
                     ).pack(anchor="w", padx=14, pady=(12, 4))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=12)
        current = store.get(note_id)["color"]
        self._swatches = {}
        for name, hex_value in NOTE_COLORS.items():
            btn = ctk.CTkButton(row, text="✓" if name == current else "", width=28, height=28, corner_radius=14,
                                fg_color=hex_value, hover_color=hex_value, text_color=theme.INK,
                                font=theme.font(theme.SIZE_SMALL, "bold"),
                                command=lambda n=name: self._pick(n))
            btn.pack(side="left", padx=3)
            self._swatches[name] = btn
        ctk.CTkLabel(self, text="Opacity", text_color=theme.TEXT, font=theme.font(theme.SIZE_SUB, "bold")
                     ).pack(anchor="w", padx=14, pady=(12, 4))
        self.slider = ctk.CTkSlider(self, from_=MIN_OPACITY, to=MAX_OPACITY, width=200, command=self._opacity,
                                    button_color=theme.TEXT, button_hover_color=theme.TEXT,
                                    progress_color=theme.ACCENT, fg_color=theme.SURFACE_HI)
        self.slider.set(store.get(note_id).get("opacity", MAX_OPACITY))
        self.slider.pack(padx=14, pady=(0, 14))
        self.bind("<Escape>", lambda _e: self.destroy())

    def _pick(self, name: str) -> None:
        for key, btn in self._swatches.items():
            btn.configure(text="✓" if key == name else "")
        self._on_color(name)

    def _opacity(self, value: float) -> None:
        self.store.update(self.note_id, opacity=round(float(value), 2))
        master = self.master
        if hasattr(master, "attributes"):
            master.attributes("-alpha", round(float(value), 2))


class StickyWindow(ctk.CTkToplevel):
    """A small always-visible note. Pin keeps it on top; the minimise button shrinks it to its title."""

    def __init__(self, master, store: NotesStore, note: dict, on_change: Callable[[], None],
                 on_color: Callable[[str, str], None] | None = None) -> None:
        super().__init__(master)
        self.store = store
        self.note_id = note["id"]
        self._on_change = on_change
        self._on_color = on_color
        self._expanded_size = MINI_SIZE
        self.overrideredirect(False)
        self.resizable(True, True)
        self.configure(fg_color=STICKY_BODY)
        self.minsize(MINI_SIZE[0], MINI_TITLE_SIZE[1])
        self.attributes("-alpha", note.get("opacity", 1.0))
        # Title bar: close, pin, style popover, minimise on the left; title beside them.
        bar = ctk.CTkFrame(self, fg_color=NOTE_COLORS[note["color"]], height=34, corner_radius=0)
        bar.pack(fill="x")
        self.title_bar = bar
        self.close_button = _IconButton(bar, "close", self._close, "Close")
        self.close_button.pack(side="left", padx=2)
        self.pin_button = _IconButton(bar, "pin", self._toggle_pin, "Stay on top")
        self.pin_button.pack(side="left", padx=2)
        self.style_button = _IconButton(bar, "appearance", self._open_style, "Colour and opacity")
        self.style_button.pack(side="left", padx=2)
        self.min_button = _IconButton(bar, "minus", self._toggle_minimize, "Minimise")
        self.min_button.pack(side="left", padx=2)
        self.title_label = ctk.CTkLabel(bar, text=short_title(note["markup"], STICKY_TITLE_LIMIT), anchor="w",
                                        text_color=theme.INK, font=theme.font(theme.SIZE_BODY, "bold"))
        self.title_label.pack(side="left", padx=4)
        self.body = _Editor(self, store, note, self._body_changed)
        self.body.pack(fill="both", expand=True, padx=6, pady=6)
        self._style_popover = None

        self.geometry(f"{MINI_SIZE[0]}x{MINI_SIZE[1]}")
        self._apply_pin(note["pinned"])
        if note["minimized"]:
            self._minimize(persist=False)

    # ----- colour -----------------------------------------------------------
    def _set_color(self, color: str) -> None:
        """Recolour this note through the notes window, so the editor window follows."""
        if self._on_color is not None:
            self._on_color(self.note_id, color)
            return
        self.store.update(self.note_id, color=color)
        self.apply_color(color)
        self._on_change()

    def apply_color(self, color: str) -> None:
        self.title_bar.configure(fg_color=NOTE_COLORS[color])
        self.configure(fg_color=STICKY_BODY)
        self.body.apply_color(color)

    def _open_style(self) -> None:
        """Open the colour and opacity popover under the style button, one at a time."""
        if self._style_popover is not None and self._style_popover.winfo_exists():
            self._style_popover.destroy()
            self._style_popover = None
            return
        popover = _StylePopover(self, self.store, self.note_id, self._set_color)
        x, y = self.style_button.winfo_rootx(), self.style_button.winfo_rooty() + 28
        popover.geometry(f"+{x}+{y}")
        self._style_popover = popover

    # ----- pin, minimise, close -----------------------------------------------
    def _apply_pin(self, pinned: bool) -> None:
        self.attributes("-topmost", bool(pinned))
        self.pin_button.set_active(bool(pinned))

    def _toggle_pin(self) -> None:
        pinned = not self.store.get(self.note_id)["pinned"]
        self.store.update(self.note_id, pinned=pinned)
        self._apply_pin(pinned)
        self._on_change()

    def _toggle_minimize(self) -> None:
        if self.store.get(self.note_id)["minimized"]:
            self._restore()
        else:
            self._minimize(persist=True)

    def _minimize(self, persist: bool) -> None:
        self._expanded_size = (self.winfo_width(), self.winfo_height()) if self.winfo_width() > 1 else MINI_SIZE
        self.body.pack_forget()
        self.geometry(f"{MINI_TITLE_SIZE[0]}x{MINI_TITLE_SIZE[1]}")
        self.min_button.set_active(True)
        if persist:
            self.store.update(self.note_id, minimized=True)
            self._on_change()

    def _restore(self) -> None:
        self.body.pack(fill="both", expand=True, padx=6, pady=6)
        self.geometry(f"{self._expanded_size[0]}x{self._expanded_size[1]}")
        self.min_button.set_active(False)
        self.store.update(self.note_id, minimized=False)
        self._on_change()

    def _body_changed(self) -> None:
        self.sync_title()
        self._on_change()

    def sync_title(self) -> None:
        self.title_label.configure(text=short_title(self.store.get(self.note_id)["markup"], STICKY_TITLE_LIMIT))

    def _close(self) -> None:
        """Close the sticky only. The note stays in the store and in the list."""
        self.destroy()


class NotesWindow(ctk.CTkToplevel):
    """The list of notes: open to edit, pop out as a sticky, or delete. New note at the top."""

    def __init__(self, master, store: NotesStore | None = None) -> None:
        super().__init__(master)
        self.title("Notes")
        self.geometry("460x520")
        self.configure(**theme.body())
        self.store = store or NotesStore()
        self._editors: dict[str, NoteEditorWindow] = {}
        self._stickies: dict[str, StickyWindow] = {}
        # One pin image per window, shared by every list row. It is built on first use and belongs
        # to this window: a Tk image cannot outlive the interpreter that made it.
        self._pin_icon: ctk.CTkImage | None = None

        # Lavender strip, matching the note title bars: the window's own identity colour.
        top = ctk.CTkFrame(self, fg_color=theme.CHROME, corner_radius=0)
        top.pack(fill="x", padx=12, pady=(12, 6))
        self.new_button = ctk.CTkButton(top, text="New note", width=112, image=theme.icon("plus", size=14, color=theme.INK),
                                        command=self.new_note, fg_color=theme.CHROME_HOVER,
                                        hover_color=theme.CHROME, text_color=theme.INK,
                                        corner_radius=theme.RADIUS, height=32, font=theme.font(theme.SIZE_BODY, "bold"))
        self.new_button.pack(side="left", padx=8, pady=6)
        self.count_label = ctk.CTkLabel(top, text="", text_color=theme.INK, font=theme.font(theme.SIZE_SUB))
        self.count_label.pack(side="left", padx=4)
        self.reminders_button = ctk.CTkButton(top, text="Reminders", width=118, height=28,
                                              image=theme.icon("reminder", size=13, color=theme.INK),
                                              command=self.open_reminders, fg_color=theme.CHROME_HOVER,
                                              hover_color=theme.CHROME, text_color=theme.INK,
                                              corner_radius=theme.RADIUS, font=theme.font(theme.SIZE_SMALL, "bold"))
        self.reminders_button.pack(side="right", padx=(4, 8), pady=6)
        self.memos_button = ctk.CTkButton(top, text="Memos", width=100, height=28,
                                          image=theme.icon("mic", size=13, color=theme.INK),
                                          command=self.open_memos, fg_color=theme.CHROME_HOVER,
                                          hover_color=theme.CHROME, text_color=theme.INK,
                                          corner_radius=theme.RADIUS, font=theme.font(theme.SIZE_SMALL, "bold"))
        self.memos_button.pack(side="right", padx=4, pady=6)

        self.list_frame = theme.ScrollList(self, row_height=46, gap=4)
        self.list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.refresh()

    def refresh(self) -> None:
        notes = self.store.all()
        self.count_label.configure(text=f"{len(notes)} note{'s' if len(notes) != 1 else ''}")
        self.list_frame.set_items(notes, self._make_row, self._update_row)
        self._sync_titles()

    def _make_row(self, parent) -> ctk.CTkFrame:
        """One empty note row. The virtual list re-points these, so they are built once."""
        row = ctk.CTkFrame(parent, fg_color=theme.SURFACE, corner_radius=theme.RADIUS)
        # The colour chip carries the note's tint, so the row still reads as that note.
        row.chip = ctk.CTkFrame(row, width=6, height=34, corner_radius=3)
        row.chip.pack(side="left", padx=(6, 4), pady=6)
        # A whisper, not a badge. Packed before the title, because an expanding label claims the
        # space first and would push anything packed after it into the middle of the row. The slot
        # is always packed and only its image changes, so showing a pin cannot reorder the row.
        row.pin = ctk.CTkLabel(row, text="", image=None)
        row.pin.pack(side="left", padx=(0, 4))
        row.title = ctk.CTkLabel(row, text="", anchor="w", text_color=theme.TEXT,
                                 font=theme.font(theme.SIZE_BODY, "bold"))
        row.title.pack(side="left", fill="x", expand=True, padx=4, pady=8)
        row.title.bind("<Button-1>", lambda _e: self.open_note(row.note["id"]))
        row.pop_out = ctk.CTkButton(row, text="Pop out", width=70, image=theme.icon("note", size=13),
                                    command=lambda: self.pop_out(row.note["id"]),
                                    **theme.button("secondary", small=True))
        row.pop_out.pack(side="right", padx=2, pady=6)
        row.edit = ctk.CTkButton(row, text="Edit", width=58, command=lambda: self.open_note(row.note["id"]),
                                 **theme.button("secondary", small=True))
        row.edit.pack(side="right", padx=2, pady=6)
        return row

    def _update_row(self, row: ctk.CTkFrame, note: dict) -> None:
        row.note = note
        row.chip.configure(fg_color=NOTE_COLORS[note["color"]])
        row.pin.configure(image=self._pin() if note["pinned"] else None)
        row.title.configure(text=short_title(note["markup"], 34))

    def _pin(self) -> ctk.CTkImage:
        if self._pin_icon is None:
            self._pin_icon = theme.icon("pin", size=13, color=theme.CHROME)
        return self._pin_icon

    def _sync_titles(self) -> None:
        """Refresh the titles of open windows after a note changed elsewhere."""
        for note_id, window in list(self._editors.items()) + list(self._stickies.items()):
            if not window.winfo_exists():
                continue
            try:
                window.sync_title()
            except KeyError:  # the note was deleted a moment ago; its windows close next
                continue

    def set_note_color(self, note_id: str, color: str) -> None:
        """Store a colour and repaint every open window for that note."""
        self.store.update(note_id, color=color)
        for window in (self._editors.get(note_id), self._stickies.get(note_id)):
            if window is not None and window.winfo_exists():
                window.apply_color(color)
        self.refresh()

    def _note_deleted(self, note_id: str, closing: object | None = None) -> None:
        """Close the windows left open for a note the user just deleted."""
        for registry in (self._editors, self._stickies):
            window = registry.pop(note_id, None)
            if window is not None and window is not closing and window.winfo_exists():
                window.destroy()
        self.refresh()

    def offer_summary(self, markup: str) -> None:
        """Let the user add key points to a new note or to an existing one."""
        if getattr(self, "_summary_dialog", None) is not None and self._summary_dialog.winfo_exists():
            self._summary_dialog.destroy()
        dialog = ctk.CTkToplevel(self)
        dialog.title("Add key points to a note")
        dialog.geometry("360x420")
        dialog.configure(**theme.body())
        dialog.transient(self)
        self._summary_dialog = dialog
        ctk.CTkLabel(dialog, text="Add the key points to:", text_color=theme.TEXT,
                     font=theme.font(theme.SIZE_SUB, "bold")).pack(padx=16, pady=(14, 8), anchor="w")
        ctk.CTkButton(dialog, text="New note", image=theme.icon("plus", size=14),
                      command=lambda: self._add_summary(markup, None, dialog), **theme.button("primary")
                      ).pack(padx=16, pady=4, fill="x")
        scroll = theme.ScrollList(dialog)
        scroll.pack(fill="both", expand=True, padx=16, pady=8)
        notes = self.store.all()
        # Row order and titles are read once, so a re-pointed row never re-reads the store.
        ids = [note["id"] for note in notes]
        titles = {note["id"]: nf.title_of(note["markup"]) for note in notes}
        scroll.set_items(
            ids,
            lambda parent: self._summary_row(
                parent, lambda note_id: self._add_summary(markup, note_id, dialog)
            ),
            lambda row, note_id: self._summary_update(row, note_id, titles),
        )

    @staticmethod
    def _summary_row(parent, make_command) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.note_id = None
        # The button is built once and re-pointed, so its action reads the id the row holds now.
        row.button = ctk.CTkButton(row, text="", anchor="w", command=lambda: make_command(row.note_id),
                                   **theme.button("secondary"))
        row.button.pack(fill="x")
        return row

    @staticmethod
    def _summary_update(row: ctk.CTkFrame, note_id: str, titles: dict) -> None:
        row.note_id = note_id
        row.button.configure(text=titles[note_id])

    def _add_summary(self, markup: str, note_id: str | None, dialog) -> None:
        """Append the key points to an existing note, or create a new note holding them."""
        if note_id is None:
            note = self.store.create(markup)
        else:
            existing = self.store.get(note_id)["markup"]
            joiner = "\n\n" if existing.strip() else ""
            note = self.store.update(note_id, markup=existing + joiner + markup)
        dialog.destroy()
        self._summary_dialog = None
        self.refresh()
        self.open_note(note["id"])

    def open_memos(self) -> None:
        """Open the Memos window once; a second press lifts it. A transcript can go to a note or Reminders."""
        from listen_app.memo_list import MemosWindow

        if getattr(self, "_memos", None) is None or not self._memos.winfo_exists():
            self._memos = MemosWindow(self, on_to_note=self.memo_to_note, on_to_reminders=self.memo_to_reminders)
        else:
            self._memos.lift()

    def open_reminders(self, text: str = "") -> None:
        """Open the Reminders window, pre-filled with `text` if given."""
        from listen_app.reminders_panel import RemindersWindow

        if getattr(self, "_reminders", None) is None or not self._reminders.winfo_exists():
            self._reminders = RemindersWindow(self, initial_text=text)
        else:
            if text:
                self._reminders.load_text(text)
            self._reminders.lift()

    def memo_to_note(self, text: str) -> None:
        """Add a memo transcript as a new note, as literal text.

        A transcript is untrusted text: "[Music]", a name in brackets, or "arr[0]" spoken
        aloud would otherwise be read as a style tag and silently dropped from the note.
        """
        note = self.store.create(nf.plain_to_markup(text))
        self.refresh()
        self.open_note(note["id"])

    def memo_to_reminders(self, text: str) -> None:
        self.open_reminders(text)

    def new_note(self) -> None:
        note = self.store.create("")
        self.refresh()
        self.open_note(note["id"])

    def open_note(self, note_id: str) -> None:
        existing = self._editors.get(note_id)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        self._editors[note_id] = NoteEditorWindow(
            self, self.store, self.store.get(note_id), self.pop_out, self.refresh,
            on_color=self.set_note_color, on_delete=self._note_deleted,
        )

    def pop_out(self, note: dict | str) -> None:
        note_id = note["id"] if isinstance(note, dict) else note
        existing = self._stickies.get(note_id)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        self._stickies[note_id] = StickyWindow(
            self, self.store, self.store.get(note_id), self.refresh, on_color=self.set_note_color,
        )
        self.refresh()
