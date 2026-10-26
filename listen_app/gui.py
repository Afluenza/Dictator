"""Listen app window: three text inputs, a speed slider, a voice picker, and Read/Stop.

Inputs:
- Upload File: .txt, .md, .pdf, or .docx, read by listen_app.extract.
- Paste Link: the URL is fetched and its main text extracted, on a worker thread.
- Add Text: uses whatever is in the text box.
- The browser extension: listen_app.bridge listens on 127.0.0.1 for one paired extension and
  drops a page or its selected text into this window, so nothing has to be copied across by hand.

No cloud import, login, or server. Engine loading, link fetching, and rendering run off
the Tk thread; widgets are only touched from the Tk thread, via a queue polled with `after`.
Read renders ahead (see listen_app.playback): a loading bar shows the wait before audio starts,
and playback then runs to the end of the text unless the user presses Stop.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from listen_app.extract import (SUPPORTED_SUFFIXES, ExtractError, UnsupportedInput, extract_file,
                                validate_url)
from listen_app import reading, theme
from listen_app.playback import RenderAhead, StreamPlayer, play_buffered
from listen_app.tts import MAX_SPEED, MIN_SPEED, clamp_speed, labelled_voices, voice_id_for

POLL_MS = 100


@dataclass(frozen=True)
class _LoadResult:
    """One extractor's outcome, carrying the ticket of the load that asked for it.

    `seq` is 0 for a result constructed by hand (the tests do this), which is always applied.
    """

    seq: int
    text: str = ""
    status: str = ""
    error: str = ""


class ListenApp(ctk.CTk):
    def __init__(self, engine_factory: Callable[[], object]) -> None:
        super().__init__()
        self.title("Listen")
        self.geometry("840x600")  # wide enough for the voice, language, speed, Read, Stop and Dictate row
        self.configure(**theme.body())
        self._engine = None
        self._busy = False
        self._load_seq = 0  # bumped per load, so a slower earlier load cannot overwrite a newer one
        self._loaded_source = ""  # the URL the text in the box ACTUALLY came from; set only on a load that worked
        self._events: queue.Queue = queue.Queue()
        self._cancel: threading.Event | None = None
        self._play_lock = threading.Lock()
        self._player: StreamPlayer | None = None  # the open output stream, so Stop can abort it from here
        self._last_ahead: RenderAhead | None = None  # finished render, kept so Restart replays without rendering
        self._reading = reading.ReadingState()  # one phase and one position for the whole window (see reading.py)
        self._notes = None  # the Notes window, created on first use
        self._dictation_window = None  # the Dictation settings window, created on first use
        self._bridge = None  # the loopback listener the browser extension posts to
        self._bridge_error = ""
        self._dictation = None  # the mic session, created on first use
        self._paste = True  # paste a dictation into the focused field; the app never pastes on its own
        self._dictation_engine = ""  # "local" | "cloud", set when the engine is actually built

        self._build()
        threading.Thread(target=self._load_engine, args=(engine_factory,), daemon=True).start()
        self._start_bridge()
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
        self.summarize_page_button = ctk.CTkButton(
            header, text="Summarize this page to Listen", width=218, image=theme.icon("sparkle", size=14, color=theme.INK),
            command=self._on_summarize_page, fg_color=theme.CHROME_HOVER, hover_color=theme.CHROME,
            text_color=theme.INK, corner_radius=theme.RADIUS, height=32, font=theme.font(theme.SIZE_BODY),
        )
        self.summarize_page_button.pack(side="right", padx=4, pady=6)
        self.browser_button = ctk.CTkButton(
            header, text="Browser", width=104, image=theme.icon("globe", size=14, color=theme.INK),
            command=self._on_browser, fg_color=theme.CHROME_HOVER, hover_color=theme.CHROME,
            text_color=theme.INK, corner_radius=theme.RADIUS, height=32, font=theme.font(theme.SIZE_BODY),
        )
        self.browser_button.pack(side="right", padx=4, pady=6)
        self.dictation_button = ctk.CTkButton(
            header, text="Dictation", width=112, image=theme.icon("mic", size=14, color=theme.INK),
            command=self._on_dictation_settings, fg_color=theme.CHROME_HOVER, hover_color=theme.CHROME,
            text_color=theme.INK, corner_radius=theme.RADIUS, height=32, font=theme.font(theme.SIZE_BODY),
        )
        self.dictation_button.pack(side="right", padx=4, pady=6)

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
        self._configure_band_colours()
        # A click still places the cursor and selects text as before. The click only also sets the reading
        # start when it lands on a word; editing is never blocked (see _token_at_click).
        self.textbox._textbox.bind("<ButtonRelease-1>", self._token_at_click, add="+")
        self.textbox._textbox.bind("<Key>", self._on_key_edit, add="+")
        # Keyboard equivalent of a click: Option+Left/Right move the start word, Option+Return starts Read from it.
        # Plain arrows stay with the caret, so ordinary editing is untouched.
        self.textbox._textbox.bind("<Option-Left>", lambda _e: self._move_start_word(-1), add="+")
        self.textbox._textbox.bind("<Option-Right>", lambda _e: self._move_start_word(1), add="+")
        self.textbox._textbox.bind("<Option-Return>", lambda _e: self._on_read(), add="+")
        self.textbox._textbox.bind("<Alt-Left>", lambda _e: self._move_start_word(-1), add="+")
        self.textbox._textbox.bind("<Alt-Right>", lambda _e: self._move_start_word(1), add="+")
        self.textbox._textbox.bind("<Alt-Return>", lambda _e: self._on_read(), add="+")

        controls = ctk.CTkFrame(self, **theme.surface())
        controls.grid(row=3, column=0, padx=16, pady=(8, 16), sticky="ew")
        # The speed slider is column 2 and takes the spare width, so the fixed controls keep their size.
        controls.grid_columnconfigure(2, weight=1)
        self.voice_menu = ctk.CTkOptionMenu(
            controls, values=["loading..."], width=150, corner_radius=theme.RADIUS,
            fg_color=theme.SURFACE_HI, button_color=theme.SURFACE_HI, button_hover_color=theme.SURFACE_HOVER,
            text_color=theme.TEXT, font=theme.font(theme.SIZE_BODY), dropdown_fg_color=theme.SURFACE,
            dropdown_hover_color=theme.ACCENT, dropdown_text_color=theme.TEXT,
        )
        self.voice_menu.grid(row=0, column=0, padx=8, pady=8)
        # Language: Auto-detect, or a fixed language. The chosen language decides the voice (see reading.choose_voice).
        self.language_menu = ctk.CTkOptionMenu(
            controls, values=[reading.AUTO, *(f"{code}  {label}" for code, (label, _v) in reading.LANGUAGES.items())],
            width=150, corner_radius=theme.RADIUS, fg_color=theme.SURFACE_HI, button_color=theme.SURFACE_HI,
            button_hover_color=theme.SURFACE_HOVER, text_color=theme.TEXT, font=theme.font(theme.SIZE_BODY),
            dropdown_fg_color=theme.SURFACE, dropdown_hover_color=theme.ACCENT, dropdown_text_color=theme.TEXT,
            command=lambda _choice: self._on_language_changed(),
        )
        self.language_menu.set(reading.AUTO)
        self.language_menu.grid(row=0, column=6, padx=(8, 0), pady=8)
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
        self.dictate_button = ctk.CTkButton(
            controls, text="Dictate", width=96, image=theme.icon("mic", size=14),
            command=self._on_dictate, **theme.button("secondary"))
        self.dictate_button.grid(row=0, column=5, padx=(8, 0), pady=8)
        # Finished controls. They are shown only after natural completion, never after a Stop, and they replace the
        # old separate popup, so every completion action stays in this window.
        self.restart_button = ctk.CTkButton(controls, text="Restart", width=96,
                                            image=theme.icon("play", size=14, color=theme.ON_ACCENT),
                                            command=self._on_restart, **theme.button("primary"))
        self.new_button = ctk.CTkButton(controls, text="New", width=84, command=self._on_new,
                                        **theme.button("secondary"))
        self.restart_button.grid(row=1, column=3, padx=8, pady=(0, 8))
        self.new_button.grid(row=1, column=4, padx=8, pady=(0, 8))
        self.restart_button.grid_remove()
        self.new_button.grid_remove()

        self.status = ctk.CTkLabel(self, text="Loading voice engine...", anchor="w", text_color=theme.TEXT_DIM,
                                   font=theme.font(theme.SIZE_SUB))
        self.status.grid(row=4, column=0, padx=18, sticky="ew")

        self.progress = ctk.CTkProgressBar(self, progress_color=theme.ACCENT, fg_color=theme.SURFACE_HI,
                                           corner_radius=theme.RADIUS_SMALL)
        self.progress.set(0)
        self.progress.grid(row=5, column=0, padx=16, pady=(6, 16), sticky="ew")
        self._set_buttons(read=False, stop=False)  # nothing is live until the engine is ready
        # Cmd/Ctrl+Shift+D anywhere in the window toggles dictation, so it is reachable from the
        # keyboard without the window having focus stolen by a global shortcut.
        self.bind_all("<Command-Shift-d>", lambda _event: self._on_dictate())
        self.bind_all("<Control-Shift-d>", lambda _event: self._on_dictate())

    # ----- engine -------------------------------------------------------
    def _load_engine(self, factory: Callable[[], object]) -> None:
        try:
            self._events.put(("engine", factory()))
        except Exception as exc:  # reported in the window, not raised in a thread
            self._events.put(("engine_error", f"{type(exc).__name__}: {exc}"))

    # ----- Tk-thread event handling ----------------------------------------
    def _start_bridge(self) -> None:
        """Open the loopback listener the browser extension posts to.

        A port already in use is reported in the status line and the app carries on; it never
        blocks the window. The payload callback runs on a request thread, so it only ever puts
        an event on the queue — Tk is still touched from the Tk thread alone.
        """
        from listen_app.bridge import Bridge

        bridge = Bridge(lambda payload: self._events.put(("bridged", payload)),
                        on_dictate=self._dictate_toggle,
                        engine_label=self._dictation_engine_name)
        if not bridge.start():
            self._bridge_error = bridge.error
            self.status.configure(text=f"Browser bridge off: {bridge.error}")
            return
        self._bridge = bridge

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
            default = getattr(payload, "default_voice", names[0] if names else "")
            # The menu shows short labels ("Heart"), while the engine keeps the real ID ("af_heart").
            # Built from `names` each time, so a label can never outlive the voice list it came from.
            self._voice_labels = labelled_voices(names)
            self.voice_menu.configure(values=list(self._voice_labels.values()))
            self.voice_menu.set(self._voice_labels.get(default, next(iter(self._voice_labels.values()), "")))
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
            # Two loads can overlap (send a page, then a link before the first finishes). The
            # queue delivers them in completion order, so a slow earlier load used to land last
            # and overwrite the newer text while keeping the newer status line. Drop a stale one.
            loaded = payload if isinstance(payload, _LoadResult) else _LoadResult(0, str(payload), "", "")
            if loaded.seq and loaded.seq != self._load_seq:
                return
            self._set_text(loaded.text)
            self._loaded_source = source  # committed with the text, so a failed load can never leave a stale label
            self.status.configure(text=loaded.status or "Text ready")
        elif kind == "load_failed":
            failed = payload if isinstance(payload, _LoadResult) else _LoadResult(0, "", "", str(payload))
            if failed.seq and failed.seq != self._load_seq:
                return  # a stale failure must not clear text a newer load just delivered
            # The box may still hold the previous piece of text. Clear it, so Summarize can never
            # label old text with the URL that just failed.
            self._set_text("")
            self._loaded_source = ""
            self.status.configure(text=failed.error)
        elif kind == "finished":
            self._finish(payload)
        elif kind == "bridged":
            self._on_bridged(payload)
        elif kind == "dictated":
            self._on_dictated(str(payload))
        elif kind == "dictation_state":
            self._set_dictating(bool(payload))
        elif kind == "sentence":
            cancel, sentence = payload
            # A sentence from a run that was stopped or replaced must not move the band of the current run.
            if cancel is not self._cancel or self._cancel.is_set():
                return
            self._highlight_sentence(sentence)
        elif kind == "summary_ready":
            self._on_summary_ready(payload)
        elif kind == "summary_failed":
            seq, message = payload
            if seq == getattr(self, "_summary_seq", 0):
                self.status.configure(text=f"No summary: {message}")
        elif kind == "done":
            self._busy = False
            self.progress.set(0)
            self._clear_band()
            self._set_buttons(read=True, stop=False)
            self.status.configure(text=str(payload))
        else:
            raise ValueError(f"unknown event {kind!r}")

    def _set_buttons(self, read: bool, stop: bool, finished: bool = False) -> None:
        """Show which control is live. A disabled button flattens, so it cannot be mistaken for an active one.

        `finished` shows Restart and New in place of Read and Stop. It is set only after natural completion.
        """
        theme.set_enabled(self.read_button, "primary", read)
        theme.set_enabled(self.stop_button, "secondary", stop)
        if finished:
            self.restart_button.grid()
            self.new_button.grid()
            self.read_button.grid_remove()
            self.stop_button.grid_remove()
        else:
            self.restart_button.grid_remove()
            self.new_button.grid_remove()
            self.read_button.grid()
            self.stop_button.grid()

    # ----- completion (in this window; no popup) -------------------------------
    def _finish(self, pay) -> None:
        """Playback reached the end of the text by itself. Restart and New appear here, in this window.

        `pay` is (the finished render, times the renderer had to wait, times the sound card ran dry).
        The device count is reported, because a card that ran dry is an audible gap the app used to hide.
        """
        ahead, render_waits, device_gaps = pay if isinstance(pay, tuple) else (pay, 0, 0)
        self._busy = False
        self._last_ahead = ahead
        self._reading.finish()
        self._clear_band()
        self.progress.set(0)
        self._set_buttons(read=False, stop=False, finished=True)
        message = "Finished reading. Restart to hear it again, or New for new text."
        if device_gaps:
            message += f" The sound card ran dry {device_gaps} time(s), so some audio may have been dropped."
        self.status.configure(text=message)

    def _on_restart(self) -> None:
        """Replay the finished text from the top, reusing the cached audio, with no new render and no popup."""
        if self._busy or self._last_ahead is None:
            return
        self._reading.restart()
        self._busy = True
        self._set_buttons(read=False, stop=True)
        self.status.configure(text="Restarting...")
        self.progress.set(0)
        self._cancel = threading.Event()
        threading.Thread(target=self._read_worker,
                         args=("", None, 1.0, self._cancel, self._last_ahead, ""), daemon=True).start()

    def _on_new(self) -> None:
        """Clear the text and the completion state, then put the cursor in the box for new input."""
        self._last_ahead = None
        self._reading.new()
        self._set_text("")
        self._loaded_source = ""
        self._set_buttons(read=True, stop=False)
        self.status.configure(text="Ready. Enter new text.")
        self.textbox.focus_set()

    # ----- input handlers ----------------------------------------------------
    def _set_text(self, text: str) -> None:
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", text)
        self._reset_highlight_map()

    # ----- reading highlight and click-to-start ----------------------------------
    # The reading view is the one text box. Words are found by reading.word_spans for the chosen language, and their
    # Tk character offsets are kept so a click can be mapped back to a token index.
    def _reset_highlight_map(self) -> None:
        """Forget the word map. Called on every text change so a stale map never points into new text."""
        self._tokens: list[tuple[str, str]] = []  # (tk start index, tk end index) per token, in reading order
        self._token_lang = ""
        self._sentence_plans: list = []
        self._clear_band()

    def _build_token_map(self, text: str, lang: str, base: str = "1.0") -> None:
        """Index every word of `text`, placed at Tk index `base`, so a click and the highlight both resolve in O(1).

        `text` is the exact string that is spoken, and `base` is where it begins in the box. The offsets are
        character counts from that point, which is why the text and its base must come from the same read.
        """
        self._tokens = []
        origin = base if base.startswith("1.0") else "1.0"
        offset = int(origin.split("+")[1].rstrip("c")) if "+" in origin else 0
        for start, end in reading.word_spans(text, lang):
            self._tokens.append((f"1.0+{offset + start}c", f"1.0+{offset + end}c"))
        self._token_lang = lang

    def _apply_band(self, token: int | None) -> None:
        """Highlight the spoken word strongly, and its neighbours lightly. Uses the theme's accent, never a fixed colour."""
        self._clear_band()
        if token is None or not self._tokens:
            return
        first, last = reading.band(token, len(self._tokens))
        for index in range(first, last):
            start, end = self._tokens[index]
            self.textbox._textbox.tag_add("band_context" if index != token else "band_current", start, end)
        self.textbox._textbox.see(self._tokens[token][0])

    def _clear_band(self) -> None:
        inner = self.textbox._textbox
        inner.tag_remove("band_context", "1.0", "end")
        inner.tag_remove("band_current", "1.0", "end")

    def _configure_band_colours(self) -> None:
        """Tag colours from the theme's own tokens. The accent is blended into the well so text stays readable."""
        inner = self.textbox._textbox
        context = reading.blend(theme.ACCENT, theme.INPUT, 0.22)
        current = reading.blend(theme.ACCENT, theme.INPUT, 0.55)
        inner.tag_configure("band_context", background=context, foreground=theme.TEXT)
        inner.tag_configure("band_current", background=current, foreground=theme.ON_ACCENT)
        inner.tag_raise("band_current")

    def _on_key_edit(self, event) -> None:
        """Typing changes the text, so any speech of the old text stops and the next Read uses the new text.

        Only text-changing keys count. Arrow keys, Shift, Tab and copy shortcuts leave the text and the position alone.
        """
        navigation = ("Shift_L", "Shift_R", "Control_L", "Control_R", "Command", "Option_L", "Option_R",
                      "Left", "Right", "Up", "Down", "Home", "End", "Tab", "Escape")
        if event.keysym in navigation or event.state & 0x8:  # 0x8: Command or Alt held, so a shortcut, not typing
            return
        editing_key = event.keysym in ("BackSpace", "Delete", "Return")
        printable = bool(event.char) and event.char.isprintable()
        if not (printable or editing_key):
            return
        if self._busy:
            self._on_stop()
        if self._reading.phase != reading.READY or self._reading.position:
            self._reading.edit()
            self._set_buttons(read=True, stop=False)
        self._clear_band()
        self._tokens = []  # the map is rebuilt from the new text when Read next starts

    def _move_start_word(self, step: int) -> str:
        """Move the reading start one word forward or back, from the keyboard. Returns "break" to keep the caret still.

        Same result as a click: speech stops, the start word is set, and Read begins there. With no word map yet
        the first press builds it from the box, so the keyboard works before any playback has happened.
        """
        if not self._tokens:
            raw = self.textbox.get("1.0", "end-1c")
            lead = len(raw) - len(raw.lstrip())
            spoken = raw[lead:].rstrip()
            if not spoken:
                return "break"
            lang = reading.resolve_language(spoken, self._language_choice())
            self._build_token_map(spoken, lang, base=f"1.0+{lead}c")
        if self._busy:
            self._on_stop()
        target = min(max(self._reading.position + step, 0), len(self._tokens) - 1)
        self._reading.select(target)
        self._apply_band(target)
        self.status.configure(text=f"Reading will start at word {target + 1}. Press Read or Option+Return.")
        self._set_buttons(read=True, stop=False)
        return "break"

    def _highlight_sentence(self, sentence: int) -> None:
        """Move the band to the start of the sentence being spoken (the first word of that sentence).

        Within a sentence the band stays on its first word. Neither engine reports word boundaries, so
        per-word movement inside a sentence is an estimate, and the band is only exact at sentence starts.
        """
        plans = getattr(self, "_sentence_plans", [])
        if not (0 <= sentence < len(plans)) or plans[sentence].count == 0:
            return
        token = plans[sentence].first_token
        self._apply_band(token)

    def _token_at_click(self, event) -> None:
        """A click on a word moves the reading position there. A click that is not on a word is ordinary editing."""
        if self._busy:
            self._on_stop()
        index = self.textbox._textbox.index(f"@{event.x},{event.y}")
        for token, (start, end) in enumerate(self._tokens):
            if self.textbox._textbox.compare(index, ">=", start) and self.textbox._textbox.compare(index, "<", end):
                self._reading.select(token)
                self._apply_band(None)
                self.status.configure(text="Reading will start from the word you chose. Press Read.")
                self._set_buttons(read=True, stop=False)
                return

    def _on_upload(self) -> None:
        from tkinter import filedialog

        # The filter is built from the extractors' own suffix list, so a format the app can read is
        # never missing from the picker (and a format it cannot read is never offered).
        path = filedialog.askopenfilename(
            filetypes=[("Documents", " ".join(f"*{s}" for s in SUPPORTED_SUFFIXES)), ("All files", "*.*")]
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

    def _on_bridged(self, payload: dict) -> None:
        """A page or its selected text arrived from the browser extension.

        The payload was already narrowed by listen_app.bridge: `text` carries the page's own text
        and the URL it came from, `url` carries only a link. A link goes through exactly the same
        social_read route as Paste Link — free anonymous routes, login-only hosts never fetched —
        so the extension cannot reach anything the window would not.
        """
        if payload["kind"] == "text":
            title = payload.get("title") or ""
            self._extract_async(lambda: payload["text"], f"Loaded from browser: {title}" if title
                                else "Loaded from browser", "Receiving from browser...",
                                source_url=payload.get("url", ""))
            return
        from listen_app.social_read import read_text

        url = payload["url"]
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
        self._extract_async(lambda: read_text(url), f"Loaded {url}", "Reading the link you sent...",
                            source_url=url)

    def _on_browser(self) -> None:
        """Whether the extension is listening, and the pairing code it needs. Nothing is sent anywhere."""
        if self._bridge is None:
            self.status.configure(text=f"Browser bridge off: {self._bridge_error or 'not started'}. "
                                       "Close the other copy of Listen and reopen this one.")
            return
        from listen_app.bridge_dialog import show_pairing

        show_pairing(self, self._bridge.code, self._bridge.endpoint)

    def _extract_async(self, job: Callable[[], str], ok_status: str, busy_status: str,
                       source_url: str = "") -> None:
        """Run an extractor off the Tk thread. The result comes back through the event queue."""
        self.status.configure(text=busy_status)
        self._load_seq += 1
        seq = self._load_seq  # this load's ticket; a later load bumps the counter and wins

        def worker() -> None:
            try:
                self._events.put(("text", _LoadResult(seq, job(), ok_status, ""), source_url))
            except (UnsupportedInput, ExtractError, ValueError, OSError, UnicodeDecodeError) as exc:
                self._events.put(("load_failed", _LoadResult(seq, "", "", str(exc))))
            except Exception as exc:  # reported in the window, not raised in a thread
                self._events.put(("load_failed", _LoadResult(seq, "", "", f"{type(exc).__name__}: {exc}")))

        threading.Thread(target=worker, daemon=True).start()

    def _on_add_text(self) -> None:
        if not self.textbox.get("1.0", "end").strip():
            self.status.configure(text="Type or paste text in the box first")
        else:
            self.status.configure(text="Text ready")

    # ----- dictation -----------------------------------------------------------
    def _session(self):
        """The mic session, built on first use. Its callbacks only queue events, never touch Tk."""
        if self._dictation is None:
            from listen_app.dictate import Session

            self._dictation = Session(
                on_text=lambda text: self._events.put(("dictated", text)),
                on_status=lambda message: self._events.put(("status", message)),
            )
        return self._dictation

    def _dictation_engine_name(self) -> str:
        """Which engine built the last transcript, for the popup and the status line. Never a key.

        Called from the bridge's request thread, so it only reads an attribute and never builds
        a session: opening a microphone because a browser asked a question would be wrong.
        """
        # __dict__, not getattr: a Tk widget's __getattr__ delegates to self.tk and recurses forever
        # on a name the widget does not have, which is exactly the case before the first dictation.
        session = self.__dict__.get("_dictation")
        return session.engine_name if session is not None else ""

    def _dictate_toggle(self) -> bool:
        """Start or stop listening. Safe to call from a request thread: only the queue is touched.

        The state event is what keeps the button honest when the extension's hotkey — not the
        button — started the microphone. Measured without it: the bridge answered recording: true
        while the button still read Dictate.
        """
        session = self._session()
        if session.recording:
            session.stop()
            self._events.put(("dictation_state", False))
            return False
        try:
            session.start()
        except Exception as exc:  # reported in the window, never raised into the bridge thread
            self._events.put(("status", str(exc)))
            self._events.put(("dictation_state", False))
            return False
        self._events.put(("dictation_state", True))
        return True

    def _on_dictate(self) -> None:
        """The Dictate button: listen, then put what was said where the cursor already is.

        Recording is stopped from the same button (it reads Stop while listening). The mic itself
        runs off the Tk thread, and the transcript arrives back through the event queue.
        """
        if self._busy:
            self.status.configure(text="Stop the reading first, then dictate.")
            return
        self._dictate_toggle()  # the relabel and the "Listening" line arrive as a state event

    def _set_dictating(self, recording: bool) -> None:
        self.dictate_button.configure(
            text="Stop" if recording else "Dictate",
            image=theme.icon("stop" if recording else "mic", size=14),
        )
        if recording:
            self.status.configure(text="Listening. Press Stop when you are done.")

    def _on_dictated(self, text: str) -> None:
        """A transcript came back: show it in the box, then paste it at the cursor if asked to."""
        self._set_dictating(False)
        if not text:
            return
        self._set_text(text)
        self._loaded_source = ""  # it is speech, not a page: nothing to attribute
        # Which engine produced this transcript belongs in the status line: local and cloud
        # differ in privacy and in who was paid for the call, so the user must not have to guess.
        engine = self._dictation_engine_name()
        by = f" ({engine})" if engine and engine != "unknown" else ""
        if not self._paste:
            self.status.configure(text=f"Dictated into the box{by}.")
            return
        from listen_app.paste import PasteError, paste_text

        try:
            paste_text(text)
        except PasteError as exc:
            # The text is in the box either way, so this is a partial success and says so.
            self.status.configure(text=f"Dictated into the box{by}; could not paste at the cursor: {exc}")
            return
        self.status.configure(text=f"Dictated and pasted at the cursor{by}.")

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

    def _on_summarize_page(self) -> None:
        """"Summarize this page to Listen": fetch the whole source, then put a labelled summary in the reading view.

        The source is read first with the same route as Paste Link, so a YouTube video is summarised from its full
        transcript and never from its description or comments. The summary scores sentences across the whole source,
        so nothing past the first part is dropped. A partial or unavailable source is disclosed before the summary.
        """
        from listen_app.summarize import key_points
        from listen_app.social_read import read

        url = self.url_entry.get().strip()
        try:
            url = validate_url(url)
        except ValueError as exc:
            self.status.configure(text=str(exc))
            return
        if self._busy:
            self.status.configure(text="Stop the reading first, then summarise.")
            return
        self.status.configure(text="Reading the whole source before summarising...")
        self._summary_seq = getattr(self, "_summary_seq", 0) + 1
        seq = self._summary_seq

        def worker() -> None:
            result = read(url)
            if not result.ok:
                self._events.put(("summary_failed", (seq, result.failure_message())))
                return
            points = key_points(result.text)
            self._events.put(("summary_ready", (seq, result, points)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_summary_ready(self, payload) -> None:
        """Put the summary in the box, labelled as a summary, with its source and any disclosure above it."""
        seq, result, points = payload
        if seq != getattr(self, "_summary_seq", 0):
            return  # a newer summary request has started; this one is stale and must not overwrite it
        if not points:
            self.status.configure(text="No key points found in the source.")
            return
        lines = [f"Summary of: {result.title or result.url}", f"Source: {result.url}"]
        if result.meta.get("transcript_fallback"):
            lines.append(f"Note: the transcript is in {result.meta.get('transcript_language')}, not your chosen language.")
        if result.note and "fallback" not in result.note:
            lines.append(f"Note: {result.note}.")
        lines.append("")
        lines.append("This is a summary of the source, not the full text.")
        lines.append("")
        lines.extend(f"- {point}" for point in points)
        self._set_text("\n".join(lines))
        self._loaded_source = result.url
        self._reading.edit()
        self._set_buttons(read=True, stop=False)
        self.status.configure(text="Summary ready. Press Read to hear it, or edit it first.")

    def _on_dictation_settings(self) -> None:
        """Open the Dictation window once; a second press lifts the existing one."""
        from listen_app.dictation_gui import DictationWindow

        if self._dictation_window is None or not self._dictation_window.winfo_exists():
            self._dictation_window = DictationWindow(self, on_changed=self._dictation_changed)
        else:
            self._dictation_window.lift()

    def _dictation_changed(self) -> None:
        """A setting changed: drop the cached engine so the next dictation uses the new choice."""
        if self._dictation is not None:
            self._dictation.reset()
        self.status.configure(text=f"Dictation: {self._dictation_label()}.")

    def _dictation_label(self) -> str:
        """What dictation will use, in one plain phrase with no key in it."""
        from listen_app import dictate

        return dictate.describe()

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
    def _on_language_changed(self) -> None:
        """The language menu changed. Speech from here on uses the new language; anything playing stops first."""
        if self._busy:
            self._on_stop()
        self._reading.edit()
        self.status.configure(text=f"Language set to {self.language_menu.get().strip()}.")

    def _on_read(self) -> None:
        text = self.textbox.get("1.0", "end").strip()
        if self._busy or self._engine is None:
            return
        if not text:
            self.status.configure(text="Nothing to read")
            return
        lang = reading.resolve_language(text, self._language_choice())
        choice = reading.choose_voice(lang, self._say_voice_names(), kokoro_ready=self._kokoro_ready())
        if not choice.engine:
            # No voice can speak this language. Say so and stop: never play it in a different language, and never
            # show a successful playback state for speech that cannot start.
            self.status.configure(text=choice.note)
            return
        speed = clamp_speed(self.speed.get())
        # The map and the spoken text come from ONE string, with offsets from the box's own start. Stripping the
        # text first shifted every token by the leading whitespace (measured: 3 characters), so the band
        # would have highlighted the wrong word.
        raw = self.textbox.get("1.0", "end-1c")
        lead = len(raw) - len(raw.lstrip())
        spoken = raw[lead:].rstrip()
        self._build_token_map(spoken, lang, base=f"1.0+{lead}c")
        # The renderer speaks one sentence at a time, split by the same rule, so the plan maps sentence -> first word.
        from listen_app.tts import split_sentences

        self._sentence_plans = reading.sentence_plan(split_sentences(spoken), lang)
        self._reading.read()
        self._start_playback(text, choice, speed, reuse=None)

    def _language_choice(self) -> str:
        label = self.language_menu.get().strip()
        if label == reading.AUTO:
            return reading.AUTO
        return label.split()[0]

    def _kokoro_ready(self) -> bool:
        """True only when the loaded engine is Kokoro. Both engines share a sample rate, so the type is the test."""
        from listen_app.tts import KokoroEngine

        return isinstance(self._engine, KokoroEngine)

    def _selected_voice(self) -> str:
        """The Kokoro voice ID the menu is showing, or "" to let the engine use its own default."""
        try:
            chosen = self.voice_menu.get()
        except Exception:  # a stub or half-built menu must not stop speech
            return ""
        return voice_id_for(chosen, getattr(self, "_voice_labels", {}))

    def _say_voice_names(self) -> list[str]:
        """The macOS voices available, read once. A machine without `say` returns none, which is reported."""
        if not hasattr(self, "_say_voices_cache"):
            from listen_app.tts import SayEngine

            try:
                self._say_voices_cache = SayEngine().voice_names()
            except RuntimeError:
                self._say_voices_cache = []
        return self._say_voices_cache

    def _start_playback(self, text: str, choice, speed: float, reuse: RenderAhead | None) -> None:
        """Start a render-and-play run. `choice` says which engine and voice speak the text."""
        self._busy = True
        self._set_buttons(read=False, stop=True)
        self.status.configure(text=choice.note if reuse is None else "Restarting...")
        self.progress.set(0)
        self._cancel = threading.Event()
        threading.Thread(
            target=self._read_worker,
            args=(text, choice, speed, self._cancel, reuse, self._selected_voice()), daemon=True
        ).start()

    def _read_worker(
        self, text: str, choice, speed: float, cancel: threading.Event, reuse: RenderAhead | None,
        kokoro_voice: str = "",
    ) -> None:
        try:
            import sounddevice as sd

            from listen_app.tts import SayEngine

            engine = self._engine if choice is None or choice.engine == "kokoro" else SayEngine()
            # The menu's voice was read on the Tk thread and passed in; the other languages name their own.
            voice = kokoro_voice if choice is None or choice.engine == "kokoro" else choice.voice
            rate = engine.sample_rate
            if reuse is not None and not reuse.replayable:
                reuse = None  # a long text let its played audio go; Restart renders it again
                self._events.put(("status", "Restarting: rendering the text again..."))
            if reuse is None:
                ahead = RenderAhead(engine, text, voice, speed, cancel)
            else:
                ahead = reuse
                ahead.rewind(cancel)
            # ONE stream for the whole run. Opening a stream per sentence added about 265 ms of silence
            # at every seam on this machine (rea/buffer-audit.md); writing to one stream is audio-true.
            player = StreamPlayer(sd, rate)
            # The lock is taken only to publish the player and to open the stream, never around play_buffered:
            # play_buffered takes the same non-reentrant lock itself, and Stop must be able to reach in.
            with self._play_lock:
                if cancel.is_set():
                    self._events.put(("done", "Stopped."))
                    return
                self._player = player
                player.open()
            try:
                completed = play_buffered(
                    ahead,
                    cancel,
                    self._play_lock,
                    play=player.write,
                    on_start=lambda: self._events.put(("status", "Playing...")),
                    on_progress=lambda fraction, message: self._events.put(("progress", (fraction, message))),
                    on_chunk=lambda sentence: self._events.put(("sentence", (cancel, sentence))),
                )
            finally:
                player.close()
                with self._play_lock:
                    if self._player is player:
                        self._player = None
            if completed:
                self._events.put(("finished", (ahead, ahead.underruns, player.underruns)))
            else:
                self._events.put(("done", "Stopped."))
        except Exception as exc:
            self._events.put(("done", f"Playback failed: {type(exc).__name__}: {exc}"))

    def _on_stop(self) -> None:
        """Stop is the only way playback ends early. It is pressed by the user, never triggered by the app.

        The text and the reading position are kept, so Read resumes from the word being spoken.
        """
        with self._play_lock:
            if self._cancel is not None:
                self._cancel.set()
            player = self._player
            if player is not None:
                player.stop()  # aborts the open stream, which is what ends the audio now
        self._reading.stop()
        self._set_buttons(read=True, stop=False)


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
