"""The Dictation window: choose where speech is transcribed, and store the key that does it.

Three states, one saved setting:
- Auto:           the local Whisper model when one is installed, otherwise cloud if a key is saved.
- On this Mac:    the local model, and say so plainly when it is not installed.
- In the cloud:   the OpenAI-compatible /audio/transcriptions endpoint, with the user's own key.

The key is written to the OS keychain through `keyring` when that is available, else to a
0600 file. It is never shown back: the field starts empty, a saved key is reported as "saved",
and no code path in this window can read it into a widget. Nothing here is spoken, pasted or
logged, and no request is made — saving a key is a local write.

Colours and fonts come from listen_app.theme only; there are no hex values in this file.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from listen_app import dictate, theme

_ENGINE_LABELS = {
    "auto": "Automatic",
    "local": "On this Mac",
    "cloud": "In the cloud",
}
_LABEL_TO_ENGINE = {label: engine for engine, label in _ENGINE_LABELS.items()}


class DictationWindow(ctk.CTkToplevel):
    """Pick the engine, save or remove the cloud key, and see what will actually run."""

    def __init__(self, master, on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.title("Dictation")
        self.geometry("520x430")
        self.resizable(False, False)
        self.configure(**theme.body())
        self.transient(master)
        self._on_changed = on_changed or (lambda: None)
        self._settings = dictate.load_settings()

        ctk.CTkLabel(self, text="Speech to text", text_color=theme.TEXT,
                     font=theme.font(theme.SIZE_TITLE, "bold")).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(self, text="Where your voice is transcribed. The microphone stays in the app.",
                     text_color=theme.TEXT_DIM, font=theme.font(theme.SIZE_SMALL)
                     ).pack(anchor="w", padx=20, pady=(0, 14))

        picker = ctk.CTkFrame(self, **theme.surface())
        picker.pack(fill="x", padx=16)
        ctk.CTkLabel(picker, text="Engine", text_color=theme.TEXT,
                     font=theme.font(theme.SIZE_SUB, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        self.engine_menu = ctk.CTkOptionMenu(
            picker, values=list(_ENGINE_LABELS.values()), width=200, corner_radius=theme.RADIUS,
            fg_color=theme.SURFACE_HI, button_color=theme.SURFACE_HI, button_hover_color=theme.SURFACE_HOVER,
            text_color=theme.TEXT, font=theme.font(theme.SIZE_BODY), dropdown_fg_color=theme.SURFACE,
            dropdown_hover_color=theme.ACCENT, dropdown_text_color=theme.TEXT, command=self._pick_engine,
        )
        self.engine_menu.set(_ENGINE_LABELS[self._settings["engine"]])
        self.engine_menu.pack(anchor="w", padx=14, pady=(0, 10))
        self.engine_note = ctk.CTkLabel(picker, text="", text_color=theme.TEXT_DIM, justify="left",
                                        wraplength=440, font=theme.font(theme.SIZE_SMALL))
        self.engine_note.pack(anchor="w", padx=14, pady=(0, 12))

        cloud = ctk.CTkFrame(self, **theme.surface())
        cloud.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(cloud, text="Cloud address and key", text_color=theme.TEXT,
                     font=theme.font(theme.SIZE_SUB, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        self.url_entry = ctk.CTkEntry(cloud, **theme.field())
        self.url_entry.insert(0, self._settings["cloud_url"])
        self.url_entry.pack(fill="x", padx=14, pady=(0, 6))
        self.key_entry = ctk.CTkEntry(cloud, show="*", placeholder_text="Paste your API key")
        self.key_entry.configure(**theme.field())
        self.key_entry.configure(placeholder_text_color=theme.TEXT_DIM)
        self.key_entry.pack(fill="x", padx=14, pady=(0, 8))
        row = ctk.CTkFrame(cloud, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 12))
        self.save_key_button = ctk.CTkButton(row, text="Save key", width=110, command=self._save_key,
                                            **theme.button("primary"))
        self.save_key_button.pack(side="left")
        self.clear_key_button = ctk.CTkButton(row, text="Remove key", width=118, command=self._clear_key,
                                              **theme.button("secondary"))
        self.clear_key_button.pack(side="left", padx=8)
        self.key_state = ctk.CTkLabel(row, text="", text_color=theme.TEXT_DIM,
                                      font=theme.font(theme.SIZE_SMALL))
        self.key_state.pack(side="left", padx=6)

        self.status = ctk.CTkLabel(self, text="", anchor="w", text_color=theme.TEXT_DIM,
                                   justify="left", wraplength=470, font=theme.font(theme.SIZE_SMALL))
        self.status.pack(anchor="w", padx=20, pady=(12, 4))
        self.close_button = ctk.CTkButton(self, text="Done", width=96, command=self.destroy,
                                          **theme.button("secondary"))
        self.close_button.pack(anchor="e", padx=20, pady=(0, 16))
        self._refresh()

    # ----- state ---------------------------------------------------------
    def _refresh(self, message: str = "") -> None:
        """Rewrite the note and key state from the saved settings and where the key lives right now."""
        self._settings = dictate.load_settings()
        engine = self._settings["engine"]
        self.engine_menu.set(_ENGINE_LABELS[engine])
        self.engine_note.configure(text=self._explain(engine))
        self.key_state.configure(text="key saved" if dictate.has_key() else "no key saved")
        self.status.configure(text=message or self._summary())

    def _explain(self, engine: str) -> str:
        local_ready = (dictate.model_dir() / "model.bin").exists()
        if engine == "local":
            return ("The model on this machine." if local_ready
                    else "Not installed. Put a faster-whisper model in ~/listen-app/models/whisper-small.en.")
        if engine == "cloud":
            return "Audio is sent to the address below. Nothing else leaves the machine."
        return ("The local model when it is installed, otherwise the cloud when a key is saved."
                + ("" if local_ready else " No local model is installed right now."))

    def _summary(self) -> str:
        try:
            choice = dictate.make_engine(settings=self._settings)
        except dictate.DictateError as exc:
            return str(exc)
        kind = dictate.engine_name(choice)
        if kind == "cloud":
            return f"Ready. Dictation will run in the cloud at {self._settings['cloud_url']}."
        return "Ready. Dictation will run on this machine."

    # ----- handlers ------------------------------------------------------
    def _pick_engine(self, label: str) -> None:
        engine = _LABEL_TO_ENGINE.get(label, "auto")
        try:
            dictate.save_settings(engine=engine)
        except (ValueError, OSError) as exc:
            self.status.configure(text=f"Could not save that: {exc}")
            return
        self._refresh()
        self._on_changed()

    def _save_key(self) -> None:
        key = self.key_entry.get().strip()
        if not key:
            self.status.configure(text="Paste the key first.")
            return
        try:
            url = dictate.check_cloud_url(self.url_entry.get())
            where = dictate.save_key(key)
            dictate.save_settings(cloud_url=url)
        except (ValueError, dictate.DictateError, OSError) as exc:
            self.status.configure(text=str(exc))
            return
        self.key_entry.delete(0, "end")  # the key is never kept in a widget after it is saved
        self._refresh(f"Key saved in the {'OS keychain' if where == 'keychain' else 'app folder (0600)'}.")
        self._on_changed()

    def _clear_key(self) -> None:
        dictate.clear_key()
        self._refresh("Key removed.")
        self._on_changed()

    # ----- for tests and for a headless check ----------------------------
    def report(self) -> dict:
        """What the window is showing, without needing a screenshot."""
        return {
            "engine": self._settings["engine"],
            "engine_label": self.engine_menu.get(),
            "key_saved": dictate.has_key(),
            "key_state": self.key_state.cget("text"),
            "url": self._settings["cloud_url"],
            "note": self.engine_note.cget("text"),
            "status": self.status.cget("text"),
            "key_field": self.key_entry.get(),
        }
