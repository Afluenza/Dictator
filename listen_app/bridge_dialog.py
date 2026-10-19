"""The pairing window: the code the extension needs, and what the bridge actually accepts.

There is no other setup step. The extension is loaded unpacked once, the code is pasted into its
popup once, and every page after that goes straight into the app. Nothing here opens a socket,
writes a file, or sends a request.
"""

from __future__ import annotations

import customtkinter as ctk

from listen_app import theme

HELP = ("Load the extension once: chrome://extensions, turn on Developer mode, "
        "Choose Load unpacked, pick the extension folder in this app, then paste the code "
        "below into the extension's popup. It stays paired after that.")
LIMITS = ("The bridge listens on this machine only and accepts two things: the page text, or a "
          "link. It never touches cookies, logins, or another host.")


def show_pairing(parent, code: str, endpoint: str) -> None:
    """Open the pairing window on top of `parent`. Closing it changes nothing."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Send pages from your browser")
    dialog.geometry("430x330")
    dialog.resizable(False, False)
    dialog.configure(**theme.body())
    dialog.transient(parent)

    card = ctk.CTkFrame(dialog, **theme.surface())
    card.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(card, text="Browser extension", text_color=theme.TEXT,
                 font=theme.font(theme.SIZE_HEAD, "bold")).pack(padx=16, pady=(14, 2), anchor="w")
    ctk.CTkLabel(card, text=f"Listening on {endpoint}", text_color=theme.TEXT_DIM,
                 font=theme.font(theme.SIZE_SMALL)).pack(padx=16, anchor="w")

    ctk.CTkLabel(card, text="Pairing code", text_color=theme.TEXT_DIM,
                 font=theme.font(theme.SIZE_SMALL)).pack(padx=16, pady=(12, 2), anchor="w")
    code_box = ctk.CTkEntry(card, justify="center", **theme.field(theme.SIZE_SUB))
    code_box.insert(0, code)
    code_box.configure(state="readonly")
    code_box.pack(padx=16, fill="x")

    row = ctk.CTkFrame(card, fg_color="transparent")
    row.pack(padx=16, pady=10, anchor="w")
    ctk.CTkButton(row, text="Copy code", width=110, image=theme.icon("note", size=14),
                  command=lambda: _copy(dialog, code), **theme.button("primary")).pack(side="left")
    ctk.CTkButton(row, text="Close", width=90, command=dialog.destroy,
                  **theme.button("secondary")).pack(side="left", padx=8)

    ctk.CTkLabel(card, text=HELP, text_color=theme.TEXT_DIM, wraplength=380, justify="left",
                 font=theme.font(theme.SIZE_SMALL)).pack(padx=16, anchor="w")
    ctk.CTkLabel(card, text=LIMITS, text_color=theme.TEXT_DIM, wraplength=380, justify="left",
                 font=theme.font(theme.SIZE_SMALL)).pack(padx=16, pady=(8, 14), anchor="w")

    dialog.after(100, dialog.grab_set)


def _copy(dialog, code: str) -> None:
    """The code is not a secret from the person looking at this window — it is theirs to paste."""
    dialog.clipboard_clear()
    dialog.clipboard_append(code)
