"""Render-ahead playback for the Listen window. Opens no audio device.

Rules:
- Text that renders whole within FULL_RENDER_BUDGET_S of wall time is rendered entirely
  before the first sound.
- Longer text starts once enough audio is rendered that the renderer stays ahead of the
  listener. The lead is computed from the render speed measured on this machine.
- Once playback starts it ends only when the text is finished or the user presses Stop.
  If rendering falls behind, the player waits for the next sentence. It never ends on its own.
- While playing, the renderer pauses once it is AHEAD_CAP_S ahead of the listener, so it
  does not burn CPU rendering audio nobody needs yet. It resumes as playback advances.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

import numpy as np

from listen_app.tts import SENTENCE_GAP_S, split_sentences

FULL_RENDER_BUDGET_S = 20.0  # wall time; text that renders this fast is rendered whole before playing
MIN_LEAD_S = 10.0  # audio that must be ready before playback starts, for long text
SAFETY_S = 3.0  # extra audio lead when rendering is slower than real time
AHEAD_CAP_S = 30.0  # while playing, the renderer pauses this far ahead of the listener
EARLY_SENTENCES = 3  # the start decision waits for this many sentences, so the speed estimate settles
RATE_MARGIN = 1.25  # plan for a render 25% slower than measured, so a slow passage later in the text cannot stall playback
POLL_S = 0.1


def start_threshold(total_audio_s: float, rate: float) -> float:
    """Audio seconds that must be rendered before playback may start.

    `rate` is wall seconds spent rendering per second of audio. Returns math.inf when the
    whole text has to be rendered first, in which case playback waits for the end of the text.
    """
    if total_audio_s * rate <= FULL_RENDER_BUDGET_S:
        return math.inf
    lead = total_audio_s * max(0.0, rate - 1.0) / rate + SAFETY_S
    threshold = max(MIN_LEAD_S, lead)
    return math.inf if threshold >= total_audio_s else threshold


class RenderAhead:
    """Renders one text on a single worker thread and hands the chunks to the player in order."""

    def __init__(self, engine: Any, text: str, voice: str, speed: float, cancel: threading.Event) -> None:
        self.sample_rate = engine.sample_rate
        self._cancel = cancel
        self._sentences = split_sentences(text)
        self._total_chars = sum(len(s) for s in self._sentences) or 1
        self._cond = threading.Condition()
        self._chunks: list[np.ndarray] = []
        self._rendered_s = 0.0
        self._rendered_chars = 0
        self._render_wall_s = 0.0
        self._worst_rate = 0.0  # slowest single sentence so far; a later passage can be slower than the opening
        self._played_s = 0.0
        self._started = False
        self._underruns = 0
        self._done = False
        self._error: BaseException | None = None
        threading.Thread(target=self._run, args=(engine, text, voice, speed), daemon=True).start()

    @property
    def underruns(self) -> int:
        """How many times the player had to wait for rendering. Playback never ended because of it."""
        return self._underruns

    @property
    def complete(self) -> bool:
        """True once every sentence is rendered. Replays then need no rendering at all."""
        with self._cond:
            return self._done

    def rewind(self, cancel: threading.Event) -> None:
        """Replay the rendered audio from the start with a new cancel event. Only valid once complete."""
        if not self.complete:
            raise RuntimeError("rewind needs a finished render")
        with self._cond:
            self._cancel = cancel
            self._started = False
            self._played_s = 0.0
            self._underruns = 0

    # ----- renderer thread -------------------------------------------------
    def _run(self, engine: Any, text: str, voice: str, speed: float) -> None:
        try:
            chunks = engine.iter_chunks(text, voice, speed)
            while self._wait_for_room():
                t0 = time.perf_counter()
                chunk = next(chunks, None)
                wall = time.perf_counter() - t0
                if chunk is None:
                    break
                with self._cond:
                    index = len(self._chunks)
                    self._chunks.append(chunk)
                    seconds = len(chunk) / self.sample_rate
                    self._rendered_s += seconds
                    self._render_wall_s += wall
                    if seconds > 0:
                        self._worst_rate = max(self._worst_rate, wall / seconds)
                    if index < len(self._sentences):
                        self._rendered_chars += len(self._sentences[index])
                    self._cond.notify_all()
        except BaseException as exc:  # handed to the player, which re-raises it on its own thread
            with self._cond:
                self._error = exc
                self._cond.notify_all()
            return
        with self._cond:
            # A cancelled render is partial, so it must not count as complete (rewind would replay part of the text).
            self._done = not self._cancel.is_set()
            self._cond.notify_all()

    def _wait_for_room(self) -> bool:
        """Block while the renderer is far enough ahead of the listener. False once cancelled."""
        with self._cond:
            while not self._cancel.is_set() and self._ahead_limit_reached():
                self._cond.wait(POLL_S)
            return not self._cancel.is_set()

    def _ahead_limit_reached(self) -> bool:
        if not self._started:
            return False  # before playback starts, render freely up to the start threshold
        return self._rendered_s - self._played_s >= AHEAD_CAP_S

    # ----- estimates (call with the lock held) ----------------------------
    def _estimate_locked(self) -> tuple[float, float]:
        """Return (rate, total_audio_s). Total is extrapolated from the characters rendered so far."""
        rate = self._render_wall_s / self._rendered_s if self._rendered_s > 0 else 0.0
        if self._rendered_chars:
            total = self._rendered_s * self._total_chars / self._rendered_chars
        else:
            total = self._rendered_s
        return rate, total

    def _planned_threshold_locked(self) -> tuple[float, float]:
        """(start threshold, total audio). Plans against the slower of the average and the worst sentence, plus margin."""
        rate, total = self._estimate_locked()
        planned = max(rate, self._worst_rate) * RATE_MARGIN
        return start_threshold(total, planned), total

    def _ready_locked(self) -> bool:
        if self._done:
            return True
        if len(self._chunks) < min(EARLY_SENTENCES, len(self._sentences)):
            return False
        threshold, _ = self._planned_threshold_locked()
        return self._rendered_s >= threshold

    # ----- player side ----------------------------------------------------
    def wait_for_start(self, on_progress: Callable[[float, str], None] | None = None) -> bool:
        """Block until playback may start. False if Stop came first. Re-raises a render error."""
        while True:
            with self._cond:
                if self._error is not None:
                    raise self._error
                if self._cancel.is_set():
                    return False
                if self._ready_locked():
                    return True
                self._cond.wait(POLL_S)
            if on_progress is not None:
                on_progress(*self.progress())

    def progress(self) -> tuple[float, str]:
        """Fraction (0 to 1) and message for the loading panel."""
        with self._cond:
            if self._done:
                return 1.0, "Voice ready."
            if not self._chunks:
                return 0.0, "Preparing audio..."
            threshold, total = self._planned_threshold_locked()
            rate, _ = self._estimate_locked()
            if math.isfinite(threshold):
                eta = max(0.0, threshold - self._rendered_s) * rate
                fraction = min(1.0, self._rendered_s / threshold)
                return fraction, (
                    f"Preparing audio: {self._rendered_s:.0f} of {threshold:.0f} s ready, "
                    f"about {eta:.0f} s to go."
                )
            count = len(self._chunks)
            total_sentences = len(self._sentences) or count
            eta = max(0.0, total - self._rendered_s) * rate
            return min(1.0, count / total_sentences), (
                f"Preparing audio: {count} of {total_sentences} sentences, about {eta:.0f} s to go."
            )

    def chunk(self, index: int) -> np.ndarray | None:
        """Chunk `index`, waiting for the renderer if needed. None once the text is finished or cancelled."""
        with self._cond:
            if index >= len(self._chunks) and not self._done and not self._cancel.is_set():
                self._underruns += 1
            while index >= len(self._chunks) and not self._done and self._error is None and not self._cancel.is_set():
                self._cond.wait(POLL_S)
            if index < len(self._chunks):
                return self._chunks[index]
            if self._error is not None:
                raise self._error
            return None

    def mark_started(self) -> None:
        with self._cond:
            self._started = True

    def mark_played(self, samples: int) -> None:
        """Tell the renderer how far the listener has got, so it can pause when far enough ahead."""
        with self._cond:
            self._played_s += samples / self.sample_rate
            self._cond.notify_all()


def play_buffered(
    ahead: RenderAhead,
    cancel: threading.Event,
    lock: threading.Lock,
    play: Callable[[np.ndarray], None],
    wait: Callable[[], None],
    on_start: Callable[[], None] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    gap_s: float = SENTENCE_GAP_S,
) -> bool:
    """Play the rendered chunks in order, with a gap between them. Returns False if Stop came first.

    `play` runs under `lock`, the same lock Stop holds while it calls sd.stop(), so a Stop cannot
    slip in between the cancel check and the start of playback.
    """
    if not ahead.wait_for_start(on_progress):
        return False
    gap = np.zeros(int(gap_s * ahead.sample_rate), dtype=np.float32)
    started = False
    index = 0
    while True:
        chunk = ahead.chunk(index)
        if chunk is None:
            break
        index += 1
        if len(chunk) == 0:
            continue
        for piece in ([chunk] if not started or len(gap) == 0 else [gap, chunk]):
            with lock:
                if cancel.is_set():
                    return False
                if not started:
                    started = True
                    ahead.mark_started()
                    if on_start is not None:
                        on_start()
                play(piece)
            wait()
            ahead.mark_played(len(piece))
    return not cancel.is_set()
