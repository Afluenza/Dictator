"""Listen app window: three text inputs, a speed slider, a voice picker, and Read/Stop.

Inputs:
- Upload File: .txt, .md, .pdf, or .docx, read by listen_app.extract.
- Paste Link: the URL is fetched and its main text extracted, on a worker thread.
- Add Text: uses whatever is in the text box.

No cloud import, login, or server. Engine loading, link fetching, and rendering run off
the Tk thread; widgets are only touched from the Tk thread, via a queue polled with `after`.
Read renders ahead (see listen_app.playback): a loading bar shows the wait before audio starts,
and playback then runs to the end of the text unless the user presses Stop.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from listen_app.extract import ExtractError, UnsupportedInput, extract_file, validate_url
from listen_app.playback import RenderAhead, play_buffered
from listen_app import theme
from listen_app.tts import MAX_SPEED, MIN_SPEED, clamp_speed

POLL_MS = 100


class ListenApp(ctk.CTk):
    def __init__(self, engine_factory: Callable[[], object]) -> None:
        super().__init__()
        self.title("Listen")
        self.geometry("720x560")
        self.configure(**theme.body())
        self._engine = None
        self._busy = False
        self._pending_status = ""
        self._loaded_source = ""  # the URL the text in the box ACTUALLY came from; set only on a load that worked
        self._events: queue.Queue = queue.Queue()
        self._cancel: threading.Event | None = None
        self._play_lock = threading.Lock()
        self._last_ahead: RenderAhead | None = None  # finished render, kept so Restart replays without rendering
        self._popup: ctk.CTkToplevel | None = None
        self._notes = None  # the Notes window, created on first use

        self._build()
        threading.Thread(target=self._load_engine, args=(engine_factory,), daemon=True).start()
        self.after(POLL_MS, self._poll)

    # ----- layout -------------------------------------------------------
    def _build(self) -> None:
        """Header strip, three inputs, the text well, then the voice/speed/Read controls and status."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color=theme.CHROME, corner_radius=0)
        header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        ctk.CTkLabel(header, text="Listen", text_color=theme.INK, font=theme.font(theme.SIZE_TITLE, "bold")
                     ).pack(side="left", padx=14, pady=8)
        self.notes_button = ctk.CTkButton(
            header, text="Notes", width=84, image=theme.icon("note", size=14, color=theme.INK),
            command=self._on_notes, fg_color=theme.CHROME_HOVER, hover_color=theme.CHROME,
            text_color=theme.INK, corner_radius=theme.RADIUS, height=32, font=theme.font(theme.SIZE_BODY),
        )
        self.notes_button.pack(side="right", padx=(4, 10), pady=6)
        self.summarize_button = ctk.CTkButton(
            header, text="Summarize to Notes", width=164, image=theme.icon("sparkle", size=14, color=theme.INK),
            command=self._on_summarize, fg_color=theme.CHROME_HOVER, hover_color=theme.CHROME,
            text_color=theme.INK, corner_radius=theme.RADIUS, height=32, font=theme.font(theme.SIZE_BODY),
        )
        self.summarize_button.pack(side="right", padx=4, pady=6)

        inputs = ctk.CTkFrame(self, **theme.surface())
        inputs.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")
        inputs.grid_columnconfigure(3, weight=1)
        ctk.CTkButton(inputs, text="Upload File", image=theme.icon("file", size=14), command=self._on_upload,
                      **theme.button("secondary")).grid(row=0, column=0, padx=8, pady=8)
        ctk.CTkButton(inputs, text="Paste Link", image=theme.icon("globe", size=14), command=self._on_link,
                      **theme.button("secondary")).grid(row=0, column=1, padx=8, pady=8)
        ctk.CTkButton(inputs, text="Add Text", image=theme.icon("text", size=14), command=self._on_add_text,
                      **theme.button("secondary")).grid(row=0, column=2, padx=8, pady=8)
        self.url_entry = ctk.CTkEntry(inputs, placeholder_text="https://...", **theme.field())
        self.url_entry.configure(placeholder_text_color=theme.TEXT_DIM)
        self.url_entry.grid(row=0, column=3, padx=8, pady=8, sticky="ew")

        self.textbox = ctk.CTkTextbox(self, wrap="word", **theme.field())
        self.textbox.configure(scrollbar_button_color=theme.SURFACE_HI,
                               scrollbar_button_hover_color=theme.SURFACE_HOVER)
        self.textbox.grid(row=2, column=0, padx=16, pady=8, sticky="nsew")

        controls = ctk.CTkFrame(self, **theme.surface())
        controls.grid(row=3, column=0, padx=16, pady=(8, 16), sticky="ew")
        controls.grid_columnconfigure(1, weight=1)
        self.voice_menu = ctk.CTkOptionMenu(
            controls, values=["loading..."], width=150, corner_radius=theme.RADIUS,
            fg_color=theme.SURFACE_HI, button_color=theme.SURFACE_HI, button_hover_color=theme.SURFACE_HOVER,
            text_color=theme.TEXT, font=theme.font(theme.SIZE_BODY), dropdown_fg_color=theme.SURFACE,
            dropdown_hover_color=theme.ACCENT, dropdown_text_color=theme.TEXT,
        )
        self.voice_menu.grid(row=0, column=0, padx=8, pady=8)
        self.speed_label = ctk.CTkLabel(controls, text="Speed 1.00x", text_color=theme.TEXT,
                                        font=theme.font(theme.SIZE_BODY))
        self.speed_label.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        self.speed = ctk.CTkSlider(
            controls,
            from_=MIN_SPEED,
            to=MAX_SPEED,
            number_of_steps=int(round((MAX_SPEED - MIN_SPEED) / 0.1)),
            command=self._on_speed,
            button_color=theme.TEXT,
            button_hover_color=theme.TEXT,
            progress_color=theme.ACCENT,
            fg_color=theme.SURFACE_HI,
        )
        self.speed.set(1.0)
        self.speed.grid(row=0, column=2, padx=8, pady=8, sticky="ew")
        self.read_button = ctk.CTkButton(controls, text="Read", width=84, image=theme.icon("play", size=14,
                                          color=theme.ON_ACCENT), command=self._on_read, state="disabled",
                                          **theme.button("primary"))
        self.read_button.grid(row=0, column=3, padx=8, pady=8)
        self.stop_button = ctk.CTkButton(controls, text="Stop", width=84, image=theme.icon("stop", size=13),
                                         command=self._on_stop, state="disabled", **theme.button("secondary"))
        self.stop_button.grid(row=0, column=4, padx=8, pady=8)

        self.status = ctk.CTkLabel(self, text="Loading voice engine...", anchor="w", text_color=theme.TEXT_DIM,
                                   font=theme.font(theme.SIZE_SUB))
        self.status.grid(row=4, column=0, padx=18, sticky="ew")

        self.progress = ctk.CTkProgressBar(self, progress_color=theme.ACCENT, fg_color=theme.SURFACE_HI,
                                           corner_radius=theme.RADIUS_SMALL)
        self.progress.set(0)
        self.progress.grid(row=5, column=0, padx=16, pady=(6, 16), sticky="ew")
        self._set_buttons(read=False, stop=False)  # nothing is live until the engine is ready

    # ----- engine -------------------------------------------------------
    def _load_engine(self, factory: Callable[[], object]) -> None:
        try:
            self._events.put(("engine", factory()))
        except Exception as exc:  # reported in the window, not raised in a thread
            self._events.put(("engine_error", f"{type(exc).__name__}: {exc}"))

    # ----- Tk-thread event handling ----------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                self._handle(*self._events.get_nowait())
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll)

    def _handle(self, kind: str, payload: object, source: str = "") -> None:
        if kind == "engine":
            self._engine = payload
            names = payload.voice_names()
            default = getattr(payload, "default_voice", names[0])
            self.voice_menu.configure(values=names)
            self.voice_menu.set(default if default in names else names[0])
            self._set_buttons(read=True, stop=False)
            self.status.configure(text=f"Ready. {len(names)} voices.")
        elif kind == "engine_error":
            self.status.configure(text=f"Voice engine failed to load: {payload}")
        elif kind == "status":
            self.status.configure(text=str(payload))
        elif kind == "progress":
            fraction, message = payload
            self.progress.set(max(0.0, min(1.0, float(fraction))))
            self.status.configure(text=str(message))
        elif kind == "text":
            self._set_text(str(payload))
            self._loaded_source = source  # committed with the text, so a failed load can never leave a stale label
            self.status.configure(text=self._pending_status or "Text ready")
        elif kind == "load_failed":
            # The box may still hold the previous piece of text. Clear it, so Summarize can never
            # label old text with the URL that just failed.
            self._set_text("")
            self._loaded_source = ""
            self.status.configure(text=str(payload))
        elif kind == "finished":
            self._finish(payload)
        elif kind == "done":
            self._busy = False
            self.progress.set(0)
            self._set_buttons(read=True, stop=False)
            self.status.configure(text=str(payload))
        else:
            raise ValueError(f"unknown event {kind!r}")

    def _set_buttons(self, read: bool, stop: bool) -> None:
        """Show which control is live. A disabled button flattens, so it cannot be mistaken for an active one."""
        theme.set_enabled(self.read_button, "primary", read)
        theme.set_enabled(self.stop_button, "secondary", stop)

    # ----- finished popup ------------------------------------------------------
    def _finish(self, ahead: RenderAhead) -> None:
        """Playback reached the end of the text. Only Stop or an error ends it early, and those do not reach here."""
        self._busy = False
        self._last_ahead = ahead
        self.progress.set(0)
        self._set_buttons(read=True, stop=False)
        self.status.configure(text="Finished.")
        self._show_finished_popup()

    def _show_finished_popup(self) -> None:
        popup = ctk.CTkToplevel(self)
        popup.title("Finished")
        popup.geometry("340x170")
        popup.resizable(False, False)
        popup.configure(**theme.body())
        popup.transient(self)
        ctk.CTkLabel(popup, text="Finished reading.", text_color=theme.TEXT,
                     font=theme.font(theme.SIZE_HEAD, "bold")).pack(padx=24, pady=(28, 4))
        ctk.CTkLabel(popup, text="Replay this text, or start a new one.", text_color=theme.TEXT_DIM,
                     font=theme.font(theme.SIZE_SMALL)).pack(padx=24, pady=(0, 14))
        row = ctk.CTkFrame(popup, fg_color="transparent")
        row.pack(pady=(0, 12))
        ctk.CTkButton(row, text="Restart", width=110, image=theme.icon("play", size=14, color=theme.ON_ACCENT),
                      command=lambda: self._close_popup(popup, restart=True), **theme.button("primary")
                      ).pack(side="left", padx=8)
        ctk.CTkButton(row, text="New", width=110, command=lambda: self._close_popup(popup, restart=False),
                      **theme.button("secondary")).pack(side="left", padx=8)
        popup.protocol("WM_DELETE_WINDOW", lambda: self._close_popup(popup, restart=False))
        self._popup = popup
        popup.after(100, popup.grab_set)

    def _close_popup(self, popup: ctk.CTkToplevel, restart: bool) -> None:
        """Restart replays the cached audio. New clears the text and the cache for a new playback."""
        popup.grab_release()
        popup.destroy()
        self._popup = None
        if restart and self._last_ahead is not None:
            self._start_playback("", "", 1.0, reuse=self._last_ahead)
            return
        self._last_ahead = None
        self._set_text("")
        self.status.configure(text="Ready. Enter new text.")

    # ----- input handlers ----------------------------------------------------
    def _set_text(self, text: str) -> None:
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", text)

    def _on_upload(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            filetypes=[("Documents", "*.txt *.md *.pdf *.docx"), ("All files", "*.*")]
        )
        if not path:
            return
        self._extract_async(lambda: extract_file(path), f"Loaded {Path(path).name}", "Reading file...")

    def _on_link(self) -> None:
        """Any http(s) link: a page, a YouTube video, a GitHub repo or issue, V2EX, or a feed.

        listen_app.social_read picks the free anonymous route for the link's kind and returns
        one honest state. A login-only platform (Reddit, X, Instagram, ...) is reported as
        needing approval and is never fetched; a blocked or sign-in page is never read as a post.
        """
        from listen_app.social_read import read_text

        try:
            url = validate_url(self.url_entry.get())
        except ValueError as exc:
            self.status.configure(text=str(exc))
            return
        self._extract_async(lambda: read_text(url), f"Loaded {url}", "Reading link...", source_url=url)

    def _extract_async(self, job: Callable[[], str], ok_status: str, busy_status: str,
                       source_url: str = "") -> None:
        """Run an extractor off the Tk thread. The result comes back through the event queue."""
        self.status.configure(text=busy_status)
        self._pending_status = ok_status

        def worker() -> None:
            try:
                self._events.put(("text", job(), source_url))
            except (UnsupportedInput, ExtractError, ValueError, OSError, UnicodeDecodeError) as exc:
                self._events.put(("load_failed", str(exc)))
            except Exception as exc:  # reported in the window, not raised in a thread
                self._events.put(("load_failed", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_add_text(self) -> None:
        if not self.textbox.get("1.0", "end").strip():
            self.status.configure(text="Type or paste text in the box first")
        else:
            self.status.configure(text="Text ready")

    def _on_summarize(self) -> None:
        """Summarise the loaded text into key points, then let the user add them to a note.

        When the text came from a link, the note opens with the source it came from, so a
        summary of a post can always be traced back to the post.
        """
        from listen_app.summarize import as_markup, key_points

        text = self.textbox.get("1.0", "end").strip()
        if not text:
            self.status.configure(text="Load some text first, then summarise it")
            return
        markup = as_markup(key_points(text), source=self._loaded_source)
        if not markup:
            self.status.configure(text="No key points found in the text")
            return
        self._on_notes()
        self._notes.offer_summary(markup)
        self.status.configure(text="Key points ready. Choose a note in the Notes window.")

    def _on_notes(self) -> None:
        """Open the notes window once; a second press lifts the existing one."""
        from listen_app.notes_gui import NotesWindow

        if self._notes is None or not self._notes.winfo_exists():
            self._notes = NotesWindow(self)
        else:
            self._notes.lift()

    def _on_speed(self, value: float) -> None:
        self.speed_label.configure(text=f"Speed {clamp_speed(value):.2f}x")

    # ----- read / stop ------------------------------------------------------------
    def _on_read(self) -> None:
        text = self.textbox.get("1.0", "end").strip()
        if self._busy or self._engine is None:
            return
        if not text:
            self.status.configure(text="Nothing to read")
            return
        voice = self.voice_menu.get()
        speed = clamp_speed(self.speed.get())
        self._start_playback(text, voice, speed, reuse=None)

    def _start_playback(self, text: str, voice: str, speed: float, reuse: RenderAhead | None) -> None:
        """Start a render-and-play run, or replay `reuse` (a finished render) without rendering again."""
        self._busy = True
        self._set_buttons(read=False, stop=True)
        self.status.configure(text="Preparing audio..." if reuse is None else "Restarting...")
        self.progress.set(0)
        self._cancel = threading.Event()
        threading.Thread(
            target=self._read_worker, args=(text, voice, speed, self._cancel, reuse), daemon=True
        ).start()

    def _read_worker(
        self, text: str, voice: str, speed: float, cancel: threading.Event, reuse: RenderAhead | None
    ) -> None:
        try:
            import sounddevice as sd

            rate = self._engine.sample_rate
            if reuse is None:
                ahead = RenderAhead(self._engine, text, voice, speed, cancel)
            else:
                ahead = reuse
                ahead.rewind(cancel)
            completed = play_buffered(
                ahead,
                cancel,
                self._play_lock,
                play=lambda audio: sd.play(audio, rate),
                wait=sd.wait,
                on_start=lambda: self._events.put(("status", "Playing...")),
                on_progress=lambda fraction, message: self._events.put(("progress", (fraction, message))),
            )
            if completed:
                self._events.put(("finished", ahead))
            else:
                self._events.put(("done", "Stopped."))
        except Exception as exc:
            self._events.put(("done", f"Playback failed: {type(exc).__name__}: {exc}"))

    def _on_stop(self) -> None:
        """Stop is the only way playback ends early. It is pressed by the user, never triggered by the app."""
        import sounddevice as sd

        with self._play_lock:
            if self._cancel is not None:
                self._cancel.set()
            sd.stop()
        self._set_buttons(read=False, stop=False)


def smoke(engine_factory: Callable[[], object], timeout_s: float = 120.0) -> dict:
    """Build the window, wait for the engine, check the voice menu, then close. Returns a report."""
    import time

    app = ListenApp(engine_factory)
    report = {"engine_ready": False, "voices": 0, "error": None}
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            app.update()
            if app._engine is not None or app.status.cget("text").startswith("Voice engine failed"):
                break
            time.sleep(0.05)
        report["engine_ready"] = app._engine is not None
        report["voices"] = len(app.voice_menu.cget("values"))
        report["error"] = None if report["engine_ready"] else app.status.cget("text")
    finally:
        app.destroy()
    return report


if __name__ == "__main__":
    import argparse

    from listen_app.tts import make_kokoro

    parser = argparse.ArgumentParser(description="Open the Listen window.")
    parser.add_argument("--smoke", action="store_true", help="load the engine, print a report, and close")
    args = parser.parse_args()
    if args.smoke:
        print(smoke(make_kokoro))
    else:
        ListenApp(make_kokoro).mainloop()
