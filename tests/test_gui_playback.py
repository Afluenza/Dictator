"""Playback tests for render-ahead playback. No audio device is opened."""
import math
import threading
import time

import numpy as np
import pytest

from listen_app import playback
from listen_app.playback import RenderAhead, play_buffered, start_threshold


class FakeEngine:
    def __init__(self, chunks, sample_rate=24000, delay=0.0, on_render=None):
        self.chunks = chunks
        self.sample_rate = sample_rate
        self.delay = delay
        self.on_render = on_render
        self.rendered = []

    def iter_chunks(self, text, voice, speed=1.0):
        for index, chunk in enumerate(self.chunks):
            if self.on_render:
                self.on_render(index)
            time.sleep(self.delay)
            self.rendered.append(index)
            yield np.full(4, chunk, dtype=np.float32)


def _run(engine, cancel, play, text="a. b. c.", gap_s=0.0):
    ahead = RenderAhead(engine, text, "v", 1.0, cancel)
    return play_buffered(ahead, cancel, threading.Lock(), play=play, wait=lambda: None, gap_s=gap_s)


# ----- start rule ----------------------------------------------------------

def test_short_text_renders_whole_before_playing():
    # Text that renders within the budget is never started early.
    assert start_threshold(total_audio_s=30.0, rate=0.5) == math.inf


def test_long_text_starts_with_a_lead_when_rendering_is_slow():
    threshold = start_threshold(total_audio_s=600.0, rate=1.5)
    assert playback.MIN_LEAD_S <= threshold < 600.0


def test_slower_rendering_needs_a_bigger_lead():
    fast = start_threshold(total_audio_s=600.0, rate=1.1)
    slow = start_threshold(total_audio_s=600.0, rate=1.8)
    assert slow > fast


def test_lead_matches_the_render_deficit():
    # A renderer 3x slower than real time must be 2/3 of the text ahead: 60 * (3-1)/3 + 3 s safety.
    assert start_threshold(total_audio_s=60.0, rate=3.0) == pytest.approx(43.0)


def test_lead_that_reaches_the_whole_text_means_wait_for_the_end():
    # At 20x slower, the lead covers the whole 60 s text, so playback waits for the end.
    assert start_threshold(total_audio_s=60.0, rate=20.0) == math.inf


# ----- playback order and stop -----------------------------------------------

def test_playback_order_and_gaps():
    played = []
    gap = np.zeros(2, dtype=np.float32)
    engine = FakeEngine([1.0, 2.0, 3.0], sample_rate=4)
    completed = _run(engine, threading.Event(), lambda a: played.append(a.copy()), gap_s=0.5)

    assert completed is True
    expected = [np.full(4, 1.0), gap, np.full(4, 2.0), gap, np.full(4, 3.0)]
    assert len(played) == len(expected)
    for got, want in zip(played, expected):
        assert np.array_equal(got, want), (got, want)


def test_stop_during_render_plays_nothing():
    cancel = threading.Event()
    played = []
    engine = FakeEngine([1.0, 2.0, 3.0], delay=0.5)
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    completed = _run(engine, cancel, played.append)
    timer.join()

    assert completed is False
    assert played == []


def test_stop_between_sentences_stops_after_current_one():
    cancel = threading.Event()
    played = []

    def play(audio):
        played.append(float(audio[0]))
        cancel.set()

    completed = _run(FakeEngine([1.0, 2.0, 3.0]), cancel, play)
    assert completed is False
    assert played == [1.0]


def test_render_error_is_raised_to_the_caller():
    class Broken(FakeEngine):
        def iter_chunks(self, text, voice, speed=1.0):
            yield np.zeros(2, dtype=np.float32)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _run(Broken([0]), threading.Event(), lambda a: None)


# ----- no automatic stop, render-ahead, low CPU --------------------------------

def test_slow_renderer_waits_and_never_ends_playback_early(monkeypatch):
    # Force the streamed path with a small lead, so the renderer is slower than the player.
    monkeypatch.setattr(playback, "FULL_RENDER_BUDGET_S", 0.0)
    monkeypatch.setattr(playback, "MIN_LEAD_S", 2.0)
    monkeypatch.setattr(playback, "SAFETY_S", 1.0)
    cancel = threading.Event()
    played = []
    engine = FakeEngine([float(i) for i in range(8)], sample_rate=4, delay=0.3)

    ahead = RenderAhead(engine, " ".join(["Sentence."] * 8), "v", 1.0, cancel)
    completed = play_buffered(ahead, cancel, threading.Lock(), play=lambda a: played.append(float(a[0])),
                              wait=lambda: None, gap_s=0.0)

    assert completed is True
    assert played == [float(i) for i in range(8)]
    assert ahead.underruns > 0  # the player did wait for the renderer, and carried on anyway


def test_renderer_pauses_when_far_ahead_of_the_listener(monkeypatch):
    monkeypatch.setattr(playback, "FULL_RENDER_BUDGET_S", 0.0)
    monkeypatch.setattr(playback, "MIN_LEAD_S", 1.0)
    monkeypatch.setattr(playback, "SAFETY_S", 0.0)
    monkeypatch.setattr(playback, "AHEAD_CAP_S", 3.0)
    cancel = threading.Event()
    # Each chunk is 1 s of audio and takes 50 ms to render. Playback is marked started before
    # rendering begins, so the cap applies from the first chunk. The listener never moves.
    engine = FakeEngine([1.0] * 20, sample_rate=4, delay=0.05)

    ahead = RenderAhead(engine, " ".join(["Sentence."] * 20), "v", 1.0, cancel)
    ahead.mark_started()
    time.sleep(0.5)

    assert 3 <= len(engine.rendered) <= 4, engine.rendered  # cap is 3 s, so it pauses after about 3 chunks
    cancel.set()


def test_rewind_replays_the_finished_audio_without_rendering_again():
    cancel = threading.Event()
    engine = FakeEngine([1.0, 2.0, 3.0], sample_rate=4)
    ahead = RenderAhead(engine, "a. b. c.", "v", 1.0, cancel)
    first, second = [], []
    assert play_buffered(ahead, cancel, threading.Lock(), play=lambda a: first.append(float(a[0])),
                         wait=lambda: None, gap_s=0.0) is True
    renders_after_first_run = len(engine.rendered)

    replay_cancel = threading.Event()
    ahead.rewind(replay_cancel)
    assert play_buffered(ahead, replay_cancel, threading.Lock(), play=lambda a: second.append(float(a[0])),
                         wait=lambda: None, gap_s=0.0) is True

    assert second == first == [1.0, 2.0, 3.0]
    assert len(engine.rendered) == renders_after_first_run  # the replay reused the cached audio


def test_rewind_refuses_before_the_render_has_finished():
    cancel = threading.Event()
    engine = FakeEngine([1.0, 2.0, 3.0], delay=0.2)
    ahead = RenderAhead(engine, "a. b. c.", "v", 1.0, cancel)
    with pytest.raises(RuntimeError, match="finished render"):
        ahead.rewind(threading.Event())
    cancel.set()


def test_start_plans_against_the_worst_sentence_not_just_the_average(monkeypatch):
    # A slow sentence later in the text must raise the planned rate, so the start lead covers it.
    # Budget and floors are lowered so the planner gives a finite lead instead of "render whole text".
    monkeypatch.setattr(playback, "FULL_RENDER_BUDGET_S", 0.0)
    monkeypatch.setattr(playback, "MIN_LEAD_S", 1.0)
    monkeypatch.setattr(playback, "SAFETY_S", 0.0)
    cancel = threading.Event()
    engine = FakeEngine([1.0] * 6, sample_rate=4)
    ahead = RenderAhead(engine, " ".join(["Sentence."] * 6), "v", 1.0, cancel)
    with ahead._cond:
        ahead._rendered_s = 3.0
        ahead._render_wall_s = 0.3  # average rate 0.1
        ahead._rendered_chars = 27
        ahead._worst_rate = 2.0     # one sentence rendered at 2x real time: planning must use this
    with ahead._cond:
        threshold_worst, _ = ahead._planned_threshold_locked()
        ahead._worst_rate = 0.0
        threshold_avg, _ = ahead._planned_threshold_locked()
    cancel.set()
    assert threshold_worst > threshold_avg
    assert threshold_worst < float("inf")


def test_progress_reports_a_message_before_audio_is_ready():
    cancel = threading.Event()
    engine = FakeEngine([1.0, 2.0], delay=0.2)
    ahead = RenderAhead(engine, "a. b.", "v", 1.0, cancel)
    fraction, message = ahead.progress()
    assert 0.0 <= fraction <= 1.0
    assert message.startswith("Preparing audio")
    cancel.set()


def test_first_sound_for_a_ten_minute_text_is_under_a_minute_at_the_measured_rate():
    # Measured on this Mac: 0.597 wall s per audio s (readthrough/bench_engines.log). The planner adds
    # RATE_MARGIN and the worst-sentence rate. The wall wait before first sound is threshold * measured rate.
    measured = 0.597
    threshold = start_threshold(total_audio_s=600.0, rate=measured * playback.RATE_MARGIN)
    assert threshold * measured <= 60.0


def test_cancelled_render_is_not_marked_complete():
    # A render stopped by the user is partial. Marking it complete would let a Restart replay part of the text.
    cancel = threading.Event()
    engine = FakeEngine([1.0] * 20, delay=0.05)
    ahead = RenderAhead(engine, " ".join(["Sentence."] * 20), "v", 1.0, cancel)
    time.sleep(0.15)
    cancel.set()
    time.sleep(0.4)
    assert ahead.complete is False
