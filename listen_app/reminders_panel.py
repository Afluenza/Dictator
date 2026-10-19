"""Reminders panel: send chosen lines of text to a Reminders test list, then read the count back.

The panel is a thin layer over listen_app.reminders_export. Every write goes through
export_to_reminders, which refuses any list that is not one of the two test lists before it
runs AppleScript. The panel adds no write path of its own.

Each non-empty line of the text box becomes one reminder. A line can be unticked so only
the chosen lines are sent. The Send step reads the list back and shows the count, so a
silent failure is visible.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from listen_app import theme
from listen_app.reminders_export import (
    TEST_LIST,
    TEST_LISTS,
    RemindersError,
    count_in_list,
    export_to_reminders,
    reminder_titles,
)

# Row geometry for the virtual list, measured off the packed ticks this replaced
# (readthrough/round14/row_pitch.py).
ROW_H = 24
GAP = 3


def build_markup(lines: list[str]) -> str:
    """Join the chosen lines back into note markup, one reminder per line."""
    return "\n".join(line for line in lines if line.strip())


class RemindersWindow(ctk.CTkToplevel):
    """Shows text as one row per line, with a tick box each, and a list picker limited to the test lists."""

    def __init__(self, master, initial_text: str = "", on_status: Callable[[str], None] | None = None) -> None:
        super().__init__(master)
        self.title("Reminders")
        self.geometry("460x520")
        self.configure(**theme.body())
        self._on_status = on_status
        self._rows: list = []  # (row, title) in list order, including rows not in view
        self._ticks: list[bool] = []  # tick state per line, so scrolling cannot lose it

        top = ctk.CTkFrame(self, fg_color=theme.CHROME, corner_radius=0)
        top.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(top, text="Reminders", text_color=theme.INK, font=theme.font(theme.SIZE_SUB, "bold")
                     ).pack(side="left", padx=12, pady=8)
        self.list_menu = ctk.CTkOptionMenu(
            top, values=list(TEST_LISTS), width=132, height=28, corner_radius=theme.RADIUS,
            fg_color=theme.SURFACE_HI, button_color=theme.SURFACE_HI, button_hover_color=theme.SURFACE_HOVER,
            text_color=theme.TEXT, font=theme.font(theme.SIZE_SMALL), dropdown_fg_color=theme.SURFACE,
            dropdown_hover_color=theme.ACCENT, dropdown_text_color=theme.TEXT,
        )
        self.list_menu.set(TEST_LIST)
        self.list_menu.pack(side="right", padx=10)

        self.status = ctk.CTkLabel(self, text="Tick the lines to send. Only the two test lists are writable.",
                                   text_color=theme.TEXT_DIM, anchor="w", font=theme.font(theme.SIZE_SMALL))
        self.status.pack(fill="x", padx=16, pady=(0, 4))

        self.list_frame = theme.ScrollList(self, row_height=ROW_H, gap=GAP)
        self.list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.send_button = ctk.CTkButton(self, text="Send to Reminders", image=theme.icon("reminder", size=14),
                                         command=self.send, **theme.button("primary"))
        self.send_button.pack(fill="x", padx=12, pady=(0, 12))
        self.load_text(initial_text)

    def load_text(self, text: str) -> None:
        """Replace the rows with one tick box per non-empty line of `text`."""
        # The tick state lives here, not in the widgets: a virtual list re-points its rows as you
        # scroll, so a row scrolling out of view must not lose whether the user ticked it.
        self._rows = [(None, title) for title in reminder_titles(text)]
        self._ticks = [True] * len(self._rows)
        self.list_frame.set_items(list(range(len(self._rows))), self._make_row, self._update_row)

    def _make_row(self, parent) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.box = ctk.CTkCheckBox(row, text="", font=theme.font(theme.SIZE_BODY),
                                  text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                                  border_color=theme.SURFACE_HI, checkmark_color=theme.ON_ACCENT)
        row.box.pack(anchor="w", fill="x", padx=8)
        return row

    def _update_row(self, row: ctk.CTkFrame, index: int) -> None:
        """Point a tick row at its line, and show the tick the user set for that line."""
        row.index = index
        title = self._rows[index][1]
        row.box.configure(text=title, command=lambda i=index: self._toggle(i, row.box.get()))
        if self._ticks[index]:
            row.box.select()
        else:
            row.box.deselect()

    def _toggle(self, index: int, ticked) -> None:
        self._ticks[index] = bool(ticked)

    def chosen_lines(self) -> list[str]:
        return [self._rows[i][1] for i in range(len(self._rows)) if self._ticks[i]]

    def send(self) -> None:
        """Write the ticked lines to the chosen test list, then read the count back."""
        lines = self.chosen_lines()
        if not lines:
            self.status.configure(text="Nothing ticked")
            return
        target = self.list_menu.get()
        try:
            created = export_to_reminders(build_markup(lines), target)
            total = count_in_list(target)
        except RemindersError as err:
            self._report(f"Reminders failed: {err}")
            return
        self._report(f"Sent {created} to {target} ({total} in list)")

    def _report(self, message: str) -> None:
        self.status.configure(text=message)
        if self._on_status is not None:
            self._on_status(message)
