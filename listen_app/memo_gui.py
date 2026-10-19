"""Voice memo control: press Record, speak, press Stop. The memo is saved and transcribed locally.

Recording runs on the sounddevice callback thread. Transcription runs on a worker thread,
so the window does not freeze. Tk widgets are only touched from the Tk thread via `after`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from listen_app import theme
from listen_app.memos import Recorder, Transcriber, save_transcript, save_wav

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "whisper-small.en"
MEMO_DIR = Path.home() / ".listen-app" / "memos"
IDLE_LABEL = "Record memo"


class MemoControl(ctk.CTkFrame):
    """A Record/Stop button and a status line. Calls `on_text` with the transcript when done."""

    def __init__(self, master, on_text: Callable[[str], None]) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_text = on_text
        self._recorder: Recorder | None = None
        self._started = 0.0
        self._transcriber: Transcriber | None = None
        # The label stays "Record memo" (a test and the probes look it up by name); the icon and
        # colour carry the state, so recording cannot be missed.
        self.button = ctk.CTkButton(self, text=IDLE_LABEL, width=140, image=theme.icon("record", size=14),
                                    command=self._toggle, **theme.button("secondary"))
        self.button.pack(side="left", padx=2)
        self.status = ctk.CTkLabel(self, text="", text_color=theme.TEXT_DIM, anchor="w",
                                   font=theme.font(theme.SIZE_SMALL))
        self.status.pack(side="left", fill="x", expand=True, padx=8)

    def _toggle(self) -> None:
        if self._recorder is None:
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        try:
            self._recorder = Recorder()
            self._recorder.start()
        except Exception as err:  # microphone missing or permission refused
            self._recorder = None
            self.status.configure(text=f"Mic error: {err}")
            return
        self._started = time.monotonic()
        self.button.configure(text="Stop", image=theme.icon("stop", size=14, color=theme.ON_ACCENT),
                              **theme.button("danger"))
        self._tick()

    def _tick(self) -> None:
        if self._recorder is None:
            return
        self.status.configure(text=f"Recording {time.monotonic() - self._started:0.0f}s")
        self.after(500, self._tick)

    def _stop(self) -> None:
        recorder, self._recorder = self._recorder, None
        samples = recorder.stop()
        self.button.configure(text=IDLE_LABEL, image=theme.icon("record", size=14), **theme.button("secondary"))
        if samples.size == 0:
            self.status.configure(text="Nothing recorded")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        wav = save_wav(samples, MEMO_DIR / f"memo-{stamp}.wav")
        self.status.configure(text="Transcribing...")
        threading.Thread(target=self._transcribe, args=(wav,), daemon=True).start()

    def _transcribe(self, wav: Path) -> None:
        try:
            if self._transcriber is None:
                self._transcriber = Transcriber(MODEL_DIR)
            text = self._transcriber.transcribe_file(wav)
            save_transcript(wav, text)
            self.after(0, lambda: self._done(text))
        except Exception as err:
            self.after(0, lambda: self.status.configure(text=f"Transcribe error: {err}"))

    def _done(self, text: str) -> None:
        self.status.configure(text="Memo added" if text else "No speech found")
        if text:
            self._on_text(text)
