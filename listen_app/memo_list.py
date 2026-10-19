"""Memos window: every saved voice memo, newest first, with its transcript.

Each row shows the time, a short preview of the transcript, and three actions: play the
recording, add its transcript to a note, or send it to the Reminders panel. Recordings and
transcripts come from listen_app.memos; this window only reads and plays them.

Playback runs on a worker thread so the window does not freeze. Tk is only touched from the
Tk thread, via `after`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from listen_app import theme
from listen_app.memo_gui import MEMO_DIR
from listen_app.memos import list_memos

PREVIEW_CHARS = 60

# Row geometry for the virtual list: measured off the packed rows this replaced
# (readthrough/round14/row_pitch.py), so the list looks the same and costs the same per row
# however many memos there are.
ROW_H = 72
GAP = 4


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """A transcript shortened for a row. An empty transcript reads as not transcribed yet."""
    text = " ".join(text.split())
    if not text:
        return "No transcript"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stamp_label(created: float) -> str:
    return time.strftime("%a %d %b, %H:%M", time.localtime(created))


def play_wav(path: Path) -> None:
    """Play one saved memo on the default output. Blocks until it ends; call from a worker thread."""
    import sounddevice as sd
    import soundfile as sf

    samples, rate = sf.read(str(path), dtype="float32", always_2d=False)
    sd.play(samples, rate)
    sd.wait()


class MemosWindow(ctk.CTkToplevel):
    """Lists the memos in MEMO_DIR. `on_to_note` gets the transcript; `on_to_reminders` gets it too."""

    def __init__(self, master, on_to_note: Callable[[str], None], on_to_reminders: Callable[[str], None],
                 folder: Path = MEMO_DIR) -> None:
        super().__init__(master)
        self.title("Memos")
        self.geometry("460x520")
        self.configure(**theme.body())
        self._folder = folder
        self._on_to_note = on_to_note
        self._on_to_reminders = on_to_reminders
        self._playing = False

        top = ctk.CTkFrame(self, fg_color=theme.CHROME, corner_radius=0)
        top.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(top, text="Memos", text_color=theme.INK, font=theme.font(theme.SIZE_SUB, "bold")
                     ).pack(side="left", padx=12, pady=8)
        self.count_label = ctk.CTkLabel(top, text="", text_color=theme.INK, font=theme.font(theme.SIZE_SMALL))
        self.count_label.pack(side="left", padx=4)
        self.status = ctk.CTkLabel(self, text="", text_color=theme.TEXT_DIM, anchor="w", font=theme.font(theme.SIZE_SMALL))
        self.status.pack(fill="x", padx=16, pady=(0, 4))

        self.list_frame = theme.ScrollList(self, row_height=ROW_H, gap=GAP)
        self.list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.refresh()

    def refresh(self) -> None:
        memos = list_memos(self._folder)
        self.count_label.configure(text=f"{len(memos)} memo{'s' if len(memos) != 1 else ''}")
        if not memos:
            self.list_frame.set_items([])
            self._show_empty("No memos yet. Record one in a note.")
            return
        self._clear_empty()
        self.list_frame.set_items(memos, self._make_row, self._update_row)

    def _show_empty(self, message: str) -> None:
        """Say the list is empty. A virtual list has no rows to hang a message on."""
        self._clear_empty()
        self._empty = ctk.CTkLabel(self.list_frame.body, text=message, text_color=theme.TEXT_DIM,
                                   font=theme.font(theme.SIZE_BODY))
        self._empty.pack(pady=24)

    def _clear_empty(self) -> None:
        empty = getattr(self, "_empty", None)
        if empty is not None and empty.winfo_exists():
            empty.destroy()
        self._empty = None

    def _make_row(self, parent) -> ctk.CTkFrame:
        """One empty row with its widgets in place. The row is never rebuilt once it exists."""
        row = ctk.CTkFrame(parent, fg_color=theme.SURFACE, corner_radius=theme.RADIUS)
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=8)
        row.stamp = ctk.CTkLabel(info, text="", anchor="w", text_color=theme.TEXT,
                                 font=theme.font(theme.SIZE_BODY, "bold"))
        row.stamp.pack(anchor="w")
        row.preview = ctk.CTkLabel(info, text="", anchor="w", font=theme.font(theme.SIZE_SMALL))
        row.preview.pack(anchor="w")
        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.pack(side="right", padx=6, pady=6)
        row.play = ctk.CTkButton(actions, text="", width=30, image=theme.icon("play", size=13),
                                 command=lambda: self._play(row.memo), **theme.button("secondary", small=True))
        row.play.pack(side="left", padx=2)
        row.to_note = ctk.CTkButton(actions, text="Note", width=52, image=theme.icon("note", size=13),
                                    command=lambda: self._to_note(row.memo), **theme.button("secondary", small=True))
        row.to_note.pack(side="left", padx=2)
        row.to_reminders = ctk.CTkButton(actions, text="Reminders", width=86, image=theme.icon("reminder", size=13),
                                         command=lambda: self._to_reminders(row.memo),
                                         **theme.button("secondary", small=True))
        row.to_reminders.pack(side="left", padx=2)
        return row

    def _update_row(self, row: ctk.CTkFrame, memo: dict) -> None:
        row.memo = memo
        row.stamp.configure(text=stamp_label(memo["created"]))
        row.preview.configure(text=preview(memo["text"]),
                              text_color=theme.TEXT if memo["text"] else theme.TEXT_DIM)
        # A memo without a transcript has nothing to send, so both actions follow the text.
        state = "normal" if memo["text"] else "disabled"
        row.to_note.configure(state=state)
        row.to_reminders.configure(state=state)

    def _play(self, memo: dict) -> None:
        if self._playing:
            self.status.configure(text="Already playing")
            return
        self._playing = True
        self.status.configure(text=f"Playing {stamp_label(memo['created'])}")
        threading.Thread(target=self._play_worker, args=(memo["wav"],), daemon=True).start()

    def _play_worker(self, wav: Path) -> None:
        try:
            play_wav(wav)
            self.after(0, lambda: self._played(None))
        except Exception as err:  # no output device, or a corrupt file
            self.after(0, lambda: self._played(str(err)))

    def _played(self, error: str | None) -> None:
        self._playing = False
        self.status.configure(text=f"Playback failed: {error}" if error else "Finished")

    def _to_note(self, memo: dict) -> None:
        self._on_to_note(memo["text"])
        self.status.configure(text="Added to a note")

    def _to_reminders(self, memo: dict) -> None:
        self._on_to_reminders(memo["text"])
