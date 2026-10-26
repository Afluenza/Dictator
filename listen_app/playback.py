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
- The player opens ONE output stream for the whole run. Opening a stream per sentence cost about
  265 ms of dead air at every seam on this machine (measured in rea/buffer-audit.md), which is the
  silent stutter the listener heard between sentences.
"""

from __future__ import annotations

import math
import queue
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
COMPACT_AFTER_S = 120.0  # audio already played past this is downcast to int16, so a long listen holds half as much
RETAIN_MAX_BYTES = 64 * 1024 * 1024  # retained audio for a finished render; past it the oldest played chunks are released


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
        self._ahead_cap_s = AHEAD_CAP_S  # raised to the planned lead at start, so the gate cannot discard it
        self._underruns = 0
        self._done = False
        self._replayable = True  # False once played audio is released; Restart then renders the text again
        self._handled = 0  # first chunk whose released/compacted form has been decided
        self._handled_end_s = 0.0  # audio seconds up to and including the last chunk handled
        self._retained_bytes = 0  # bytes held in _chunks right now
        self._error: BaseException | None = None
        threading.Thread(target=self._run, args=(engine, text, voice, speed), daemon=True).start()

    @property
    def underruns(self) -> int:
        """How many times the player had to wait for rendering. Playback never ended because of it."""
        return self._underruns

    @property
    def replayable(self) -> bool:
        """True while the whole render is still held, so Restart can replay it without rendering again."""
        with self._cond:
            return self._replayable

    @property
    def retained_bytes(self) -> int:
        """Bytes held for this render. One audio second of float32 at 24 kHz is 96,000 bytes."""
        with self._cond:
            return self._retained_bytes

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
            if not self._replayable:
                raise RuntimeError("rewind needs the retained audio, which was released for a long text")
            self._cancel = cancel
            self._started = False
            self._played_s = 0.0
            self._underruns = 0
            self._handled = 0
            self._handled_end_s = 0.0

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
                    self._retained_bytes += chunk.nbytes
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
        return self._rendered_s - self._played_s >= self._ahead_cap_s

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
            waited = False
            while index >= len(self._chunks) and not self._done and self._error is None and not self._cancel.is_set():
                waited = True
                self._cond.wait(POLL_S)
            if waited and index < len(self._chunks):
                # Counted only once the wait is over and the chunk arrived: a player that merely asked
                # before the renderer caught up did not wait, so it is not an underrun.
                self._underruns += 1
            if index < len(self._chunks):
                found = self._chunks[index]
                if found is None:
                    raise RuntimeError("this render's audio was released; render the text again to replay it")
                if found.dtype != np.float32:
                    return found.astype(np.float32) / 32767.0  # released chunks are held as int16
                return found
            if self._error is not None:
                raise self._error
            return None

    def mark_started(self) -> None:
        """Freeze the render-ahead limit for this run.

        The start decision may plan a lead larger than AHEAD_CAP_S (a slow machine plans 37-167 s of lead
        for a long text, see start_threshold). The gate kept the 30 s floor and discarded the lead the start
        decision had just waited for, so the cap follows the plan instead: max(floor, planned lead).
        """
        with self._cond:
            self._started = True
            threshold, _ = self._planned_threshold_locked()
            if math.isfinite(threshold):
                self._ahead_cap_s = max(AHEAD_CAP_S, threshold)

    def mark_played(self, samples: int) -> None:
        """Tell the renderer how far the listener has got, so it can pause when far enough ahead, and release
        the audio already heard (see _release_played_locked)."""
        with self._cond:
            self._played_s += samples / self.sample_rate
            self._release_played_locked()
            self._cond.notify_all()

    def _release_played_locked(self) -> None:
        """Shrink what the listener has already passed. Chunks are kept so Restart replays without rendering.

        96,000 bytes per audio second means a 30-minute text holds about 173 MB for the session. Past
        COMPACT_AFTER_S the played chunks are downcast to int16 (half), and past RETAIN_MAX_BYTES they are
        dropped: the render is then no longer replayable, and Restart renders the text again instead.
        """
        if self._played_s < COMPACT_AFTER_S:
            return
        while self._handled < len(self._chunks):
            chunk = self._chunks[self._handled]
            end_s = self._handled_end_s + len(chunk) / self.sample_rate
            if end_s > self._played_s:
                break
            if self._retained_bytes > RETAIN_MAX_BYTES:
                self._chunks[self._handled] = None
                self._retained_bytes -= chunk.nbytes
                self._replayable = False
            elif chunk.dtype != np.int16:
                compact = (np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16)
                self._retained_bytes -= chunk.nbytes - compact.nbytes
                self._chunks[self._handled] = compact
            self._handled_end_s = end_s
            self._handled += 1


class StreamPlayer:
    """One output stream for a whole playback run, written to directly.

    `sd.play()` builds a stream, plays it and tears it down on every call: measured on this machine that is
    about 265 ms of silent dead air at each sentence seam (rea/buffer-audit.md), which is the stutter the
    listener heard. Writing to one stream that stays open is audio-true instead.

    `write()` blocks until the device has taken the audio, so it is also the clock: the caller's
    loops stay in step with what is actually heard, with no queue and no timer.
    """

    def __init__(self, sd: Any, sample_rate: int) -> None:
        self._sd = sd
        self._rate = sample_rate
        self._stream: Any = None
        self.underruns = 0  # device side: times the card wanted samples and there were none

    def open(self) -> None:
        self._stream = self._sd.OutputStream(samplerate=self._rate, channels=1, dtype="float32")
        self._stream.start()

    def write(self, audio: np.ndarray) -> None:
        stream = self._stream
        if stream is None:
            raise RuntimeError("the output stream is not open")
        if stream.write(np.ascontiguousarray(audio, dtype=np.float32)):
            self.underruns += 1  # the device ran dry: this one IS an audible gap, reported to the user

    def close(self) -> None:
        """Let the audio already buffered finish, then close. Used at the natural end of a run."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def stop(self) -> None:
        """Drop what is still buffered and close now, from any thread. Used by the Stop button."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()
        finally:
            stream.close()


def play_buffered(
    ahead: RenderAhead,
    cancel: threading.Event,
    lock: threading.Lock,
    play: Callable[[np.ndarray], None],
    on_start: Callable[[], None] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    gap_s: float = SENTENCE_GAP_S,
    on_chunk: Callable[[int], None] | None = None,
) -> bool:
    """Play the rendered chunks in order, with a gap between them. Returns False if Stop came first.

    `play(audio)` hands the samples to the device and returns once they have been heard, so this loop runs
    at listening speed. The lock is held only for the Stop check and the first-sound bookkeeping, never
    across `play`: Stop must be able to interrupt a blocking write from the Tk thread.
    `on_chunk(index)` runs as each sentence begins, so the reading highlight can follow the speech.
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
        sentence = index
        index += 1
        if len(chunk) == 0:
            continue
        for piece in ([chunk] if not started or len(gap) == 0 else [gap, chunk]):
            just_started = False
            with lock:
                if cancel.is_set():
                    return False
                if not started:
                    started = just_started = True
                    ahead.mark_started()
            if just_started and on_start is not None:
                on_start()
            if on_chunk is not None and piece is chunk:
                on_chunk(sentence)  # the sentence is about to be heard, so the highlight moves now
            try:
                play(piece)
            except Exception:
                if cancel.is_set():
                    return False  # Stop interrupted the write; that is not a playback failure
                raise
            ahead.mark_played(len(piece))
    return not cancel.is_set()
