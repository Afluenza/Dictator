"""Round 37: the buffer fixes from rea/buffer-audit.md, each held by a test that fails on the old code.

The headline defect was one PortAudio stream opened and torn down per sentence: measured at about 265 ms
of silent dead air per seam on this machine. These tests count stream opens through a fake sd module, so
they prove the fix on all three platforms with no audio device.
"""

from __future__ import annotations

import sys
import threading
import time

import numpy as np
import pytest

from listen_app import playback
from listen_app.playback import RenderAhead, StreamPlayer, play_buffered
from tests.fixtures.fake_sounddevice import make_sd


class FakeEngine:
    """Yields one chunk per sentence, each `frames` samples of a constant value."""

    def __init__(self, values, sample_rate=24000, frames=240, delay=0.0, gate=None):
        self.values = values
        self.sample_rate = sample_rate
        self.frames = frames
        self.delay = delay
        self.gate = gate  # an Event: the renderer blocks before each chunk until it is set
        self.rendered = []

    def iter_chunks(self, text, voice, speed=1.0):
        for index, value in enumerate(self.values):
            if self.gate is not None:
                self.gate.wait(5)
            time.sleep(self.delay)
            self.rendered.append(index)
            yield np.full(self.frames, value, dtype=np.float32)


# ----- Defect 1 and 6.2: one stream per run -----------------------------------

def test_one_output_stream_for_the_whole_run():
    """The guard for Defect 1. Before the fix this opened a stream per sentence (23 for 12 sentences)."""
    sd = make_sd()
    player = StreamPlayer(sd, 24000)
    ahead = RenderAhead(FakeEngine([1.0, 2.0, 3.0, 4.0]), "a. b. c. d.", "v", 1.0, threading.Event())

    player.open()
    try:
        assert play_buffered(ahead, threading.Event(), threading.Lock(), play=player.write, gap_s=0.0) is True
    finally:
        player.close()

    assert len(sd.streams) == 1, f"expected one stream for the run, opened {len(sd.streams)}"
    assert sd.streams[0].started and sd.streams[0].closed


def test_every_sentence_and_gap_goes_to_that_one_stream_in_order():
    sd = make_sd()
    player = StreamPlayer(sd, 4)
    ahead = RenderAhead(FakeEngine([1.0, 2.0], sample_rate=4, frames=4), "a. b.", "v", 1.0, threading.Event())

    player.open()
    try:
        play_buffered(ahead, threading.Event(), threading.Lock(), play=player.write, gap_s=0.5)
    finally:
        player.close()

    written = np.concatenate(sd.streams[0].written)
    # sentence, gap, sentence: the gap is written into the SAME stream, not left as an open/close seam
    assert list(written) == [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0]


# ----- Defect 2: a dry card is reported, not hidden ---------------------------

def test_device_underruns_are_counted_and_surfaced():
    sd = make_sd()
    player = StreamPlayer(sd, 4)
    ahead = RenderAhead(FakeEngine([1.0, 2.0], sample_rate=4, frames=4), "a. b.", "v", 1.0, threading.Event())
    player.open()
    sd.streams[0].dry_on_write = 1  # the card wanted samples and there were none, on the first write
    try:
        play_buffered(ahead, threading.Event(), threading.Lock(), play=player.write, gap_s=0.0)
    finally:
        player.close()
    assert player.underruns == 1
    assert ahead.underruns == 0  # the renderer never made the player wait, so these are different numbers


def test_the_finished_status_names_the_device_gaps():
    """The window used to discard sd.wait()'s CallbackFlags, so a run with drops looked clean."""
    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    statuses: list[str] = []
    app.status = type("S", (), {"configure": lambda _s, **kw: statuses.append(kw.get("text", ""))})()
    app.progress = type("P", (), {"set": lambda _s, _v: None})()
    app._reading = type("R", (), {"finish": lambda _s: None})()
    app._clear_band = lambda: None
    app._set_buttons = lambda read, stop, finished=False: None
    app._finish((object(), 0, 2))
    assert "ran dry 2 time(s)" in statuses[-1], statuses


def test_a_clean_run_says_nothing_about_drops():
    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    statuses: list[str] = []
    app.status = type("S", (), {"configure": lambda _s, **kw: statuses.append(kw.get("text", ""))})()
    app.progress = type("P", (), {"set": lambda _s, _v: None})()
    app._reading = type("R", (), {"finish": lambda _s: None})()
    app._clear_band = lambda: None
    app._set_buttons = lambda read, stop, finished=False: None
    app._finish((object(), 0, 0))
    assert "ran dry" not in statuses[-1]
    assert statuses[-1].startswith("Finished reading.")


# ----- Defect 3: the cap cannot discard the lead the start decision planned ---

def test_render_ahead_cap_follows_the_planned_lead(monkeypatch):
    """A slow machine plans a lead bigger than AHEAD_CAP_S; the gate used to throw that lead away."""
    monkeypatch.setattr(playback, "FULL_RENDER_BUDGET_S", 0.0)
    monkeypatch.setattr(playback, "MIN_LEAD_S", 10.0)
    monkeypatch.setattr(playback, "SAFETY_S", 0.0)
    monkeypatch.setattr(playback, "AHEAD_CAP_S", 30.0)
    ahead = RenderAhead(FakeEngine([1.0] * 4), "a. b. c. d.", "v", 1.0, threading.Event())
    with ahead._cond:
        ahead._rendered_s = 100.0
        ahead._render_wall_s = 300.0  # renders at 3x real time, so the plan asks for a large lead
        ahead._rendered_chars = 10
        ahead._worst_rate = 3.0
    cancel = threading.Event()
    try:
        with ahead._cond:
            planned, _ = ahead._planned_threshold_locked()
        assert planned > playback.AHEAD_CAP_S, planned  # the premise: the plan exceeds the floor
        ahead.mark_started()
        assert ahead._ahead_cap_s == pytest.approx(planned)
        assert ahead._ahead_cap_s > playback.AHEAD_CAP_S
    finally:
        cancel.set()


def test_the_floor_still_applies_when_the_plan_is_small(monkeypatch):
    monkeypatch.setattr(playback, "FULL_RENDER_BUDGET_S", 0.0)
    ahead = RenderAhead(FakeEngine([1.0] * 3), "a. b. c.", "v", 1.0, threading.Event())
    with ahead._cond:
        ahead._rendered_s = 100.0
        ahead._render_wall_s = 1.0  # renders 100x faster than real time
        ahead._rendered_chars = 10
    ahead.mark_started()
    assert ahead._ahead_cap_s == playback.AHEAD_CAP_S  # never below the floor, so a fast machine does not hoard


# ----- Defect 4: what a long listen actually holds ---------------------------

def _drain(ahead, cancel, played_seconds, sample_rate=24000, frames=24000):
    """Play the whole render at the given audio length per chunk, marking each as heard."""
    index = 0
    while True:
        chunk = ahead.chunk(index)
        if chunk is None:
            return
        index += 1
        ahead.mark_played(len(chunk))


def test_played_audio_is_downcast_so_a_long_listen_holds_half(monkeypatch):
    """96,000 bytes per audio second is exact. Past COMPACT_AFTER_S the heard audio is kept as int16."""
    monkeypatch.setattr(playback, "COMPACT_AFTER_S", 0.0)
    ahead = RenderAhead(FakeEngine([0.5] * 4, frames=2400), "a. b. c. d.", "v", 1.0, threading.Event())
    for _ in range(200):
        if ahead.complete:
            break
        time.sleep(0.01)
    full = ahead.retained_bytes
    _drain(ahead, threading.Event(), 0)
    assert ahead.retained_bytes == pytest.approx(full / 2, rel=0.02), (full, ahead.retained_bytes)
    assert ahead.replayable is True
    # a downcast chunk still arrives as float32 samples at the right level
    back = ahead.chunk(0)
    assert back.dtype == np.float32
    assert float(back[0]) == pytest.approx(0.5, abs=1e-3)


def test_retention_is_bounded_and_restart_then_renders_again(monkeypatch):
    monkeypatch.setattr(playback, "COMPACT_AFTER_S", 0.0)
    monkeypatch.setattr(playback, "RETAIN_MAX_BYTES", 1)  # force release on the first chunk handled
    ahead = RenderAhead(FakeEngine([0.5] * 3, frames=2400), "a. b. c.", "v", 1.0, threading.Event())
    for _ in range(200):
        if ahead.complete:
            break
        time.sleep(0.01)
    _drain(ahead, threading.Event(), 0)
    assert ahead.replayable is False, "released audio cannot be replayed"
    with pytest.raises(RuntimeError, match="released"):
        ahead.rewind(threading.Event())


def test_the_short_text_path_keeps_every_chunk(monkeypatch):
    """Below COMPACT_AFTER_S nothing is touched: Restart still replays without rendering again."""
    monkeypatch.setattr(playback, "COMPACT_AFTER_S", 1e9)
    ahead = RenderAhead(FakeEngine([0.5] * 3, frames=240), "a. b. c.", "v", 1.0, threading.Event())
    for _ in range(200):
        if ahead.complete:
            break
        time.sleep(0.01)
    before = ahead.retained_bytes
    _drain(ahead, threading.Event(), 0)
    assert ahead.retained_bytes == before
    assert ahead.replayable is True


# ----- Defect 5: an underrun is a wait that happened --------------------------

def test_asking_before_the_renderer_caught_up_is_not_an_underrun():
    """Before the fix this counted the request itself, so the metric fired on a fast renderer too."""
    ahead = RenderAhead(FakeEngine([1.0]), "a.", "v", 1.0, threading.Event())
    for _ in range(200):
        if ahead.complete:
            break
        time.sleep(0.01)
    assert ahead.chunk(0) is not None
    assert ahead.underruns == 0


def _waiting_run(monkeypatch, cancel):
    """A render held behind a shut gate, with a player thread asking for the un-rendered sentence 1."""
    monkeypatch.setattr(playback, "FULL_RENDER_BUDGET_S", 0.0)
    monkeypatch.setattr(playback, "MIN_LEAD_S", 0.0)
    monkeypatch.setattr(playback, "SAFETY_S", 0.0)
    gate = threading.Event()  # held shut, so the renderer cannot get ahead of the player
    ahead = RenderAhead(FakeEngine([1.0, 2.0, 3.0, 4.0], frames=4, gate=gate), "a. b. c. d.", "v", 1.0, cancel)
    ahead.mark_started()
    result = {}
    waiter = threading.Thread(target=lambda: result.update(chunk=ahead.chunk(1)))
    waiter.start()
    time.sleep(0.2)  # the player is provably waiting: the gate is still shut
    return ahead, gate, result, waiter


def test_a_wait_that_ends_in_audio_counts_one_underrun(monkeypatch):
    """Positive control: the metric must fire when the listener really was starved and then fed."""
    ahead, gate, result, waiter = _waiting_run(monkeypatch, threading.Event())
    assert ahead.underruns == 0, "the wait has not ended yet, so nothing may be counted"
    gate.set()
    waiter.join(10)
    assert result["chunk"] is not None
    assert ahead.underruns == 1


def test_a_wait_ended_by_stop_is_not_an_underrun(monkeypatch):
    """The real difference from the old code, which counted the request itself.

    Before the fix the increment sat before the wait loop, so this case reported an underrun even
    though no audio was ever missing from the listener's ear: Stop had already ended the run.
    The count now means "starved and then served", which is the gap a listener actually hears.
    """
    cancel = threading.Event()
    ahead, gate, result, waiter = _waiting_run(monkeypatch, cancel)
    cancel.set()  # Stop arrives while the player is waiting for the next sentence
    waiter.join(10)
    gate.set()
    assert result["chunk"] is None
    assert ahead.underruns == 0


# ----- the player closes the stream on Stop ----------------------------------

def test_stop_aborts_the_open_stream_immediately():
    sd = make_sd()
    player = StreamPlayer(sd, 24000)
    player.open()
    player.stop()
    stream = sd.streams[0]
    assert stream.aborted and stream.closed
    assert not stream.stopped  # abort, not stop: Stop must not wait for the buffered audio to drain
