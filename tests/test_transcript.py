"""Full YouTube transcript selection and assembly. Every test uses a fake caption source: no socket is opened."""
import socket

import pytest

from listen_app import transcript as t


class FakeSource:
    def __init__(self, tracks=None, error=None):
        self._tracks = tracks or []
        self._error = error
        self.calls = 0

    def tracks(self, video_id):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._tracks


class IpBlocked(Exception):
    pass


class TranscriptsDisabled(Exception):
    pass


class NoTranscriptFound(Exception):
    pass


@pytest.fixture(autouse=True)
def _no_live_network_and_no_cooldown(monkeypatch):
    """A test that reaches a real socket fails here, and a cooldown from one test cannot leak into another."""

    def refuse(self, address, *args, **kwargs):
        raise OSError(f"fixture test attempted a live connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    t.reset_cooldown()
    yield
    t.reset_cooldown()


def track(language, generated, *lines):
    return t.Track(language=language, generated=generated, snippets=list(lines))


# ----- track choice ------------------------------------------------------------
def test_the_readers_language_is_preferred_over_another():
    tracks = [track("fr", False, "bonjour"), track("en", False, "hello")]
    chosen, fallback = t.pick_track(tracks, "en")
    assert chosen.language == "en" and fallback is False


def test_a_regional_code_matches_its_base_language():
    tracks = [track("de", False, "hallo")]
    chosen, fallback = t.pick_track(tracks, "de-DE")
    assert chosen.language == "de" and fallback is False


def test_manual_captions_beat_generated_in_the_same_language():
    tracks = [track("en", True, "auto words"), track("en", False, "creator words")]
    chosen, _ = t.pick_track(tracks, "en")
    assert chosen.generated is False


def test_a_missing_language_falls_back_and_says_so():
    tracks = [track("fr", False, "bonjour")]
    chosen, fallback = t.pick_track(tracks, "en")
    assert chosen.language == "fr"
    assert fallback is True


def test_no_tracks_chooses_nothing():
    assert t.pick_track([], "en") == (None, False)


# ----- assembly ----------------------------------------------------------------
def test_snippets_are_joined_in_order_into_readable_text():
    text = t.join_snippets(["Hello", "there", "friend."])
    assert text == "Hello there friend."


def test_repeated_speech_is_kept_but_an_identical_adjacent_snippet_is_dropped():
    # "no no no" is three real snippets of speech; the same line twice in a row is an overlap artefact.
    assert t.join_snippets(["no", "no", "no", "go"]) == "no no no go"
    assert t.join_snippets(["the whole line.", "the whole line.", "next."]) == "the whole line. next."
    # A two-word repeat is real speech, so it is kept.
    assert t.join_snippets(["same line.", "same line.", "next."]) == "same line. same line. next."


def test_long_transcripts_are_cut_into_paragraphs_at_sentence_ends_without_losing_words():
    snippets = [f"Sentence number {i} is here and it keeps going." for i in range(60)]
    text = t.join_snippets(snippets)
    assert "\n\n" in text
    rejoined = " ".join(p.replace("\n", " ") for p in text.split("\n\n"))
    assert rejoined.split() == " ".join(snippets).split()  # every word, in the same order


def test_empty_snippets_are_skipped():
    assert t.join_snippets(["", "   ", "real words"]) == "real words"


# ----- fetch outcomes ------------------------------------------------------------
def test_a_full_transcript_in_the_readers_language_is_ok():
    source = FakeSource([track("en", False, "Never gonna give you up.", "Never gonna let you down.")])
    result = t.fetch_transcript("dQw4w9WgXcQ", "en", source)
    assert result.state == t.OK
    assert result.text == "Never gonna give you up. Never gonna let you down."
    assert result.language == "en" and result.fallback is False
    assert "creator-provided en transcript" in result.note


def test_a_fallback_language_is_labelled_in_the_note():
    source = FakeSource([track("ja", True, "こんにちは")])
    result = t.fetch_transcript("abc", "en", source)
    assert result.state == t.OK
    assert result.fallback is True
    assert "fallback: en was not offered" in result.note
    assert result.available == ["ja"]


def test_available_languages_are_reported_for_the_reader_to_choose_from():
    source = FakeSource([track("en", True, "a"), track("es", False, "b"), track("de", True, "c")])
    result = t.fetch_transcript("abc", "es", source)
    assert result.available == ["de", "en", "es"]
    assert result.language == "es"


def test_disabled_transcripts_are_unavailable_not_failed():
    source = FakeSource(error=TranscriptsDisabled("off"))
    result = t.fetch_transcript("abc", "en", source)
    assert result.state == t.UNAVAILABLE
    assert "TranscriptsDisabled" in result.note
    assert result.text == ""


def test_no_transcript_in_any_language_is_unavailable_not_failed():
    result = t.fetch_transcript("abc", "en", FakeSource(error=NoTranscriptFound("none")))
    assert result.state == t.UNAVAILABLE


def test_a_video_with_no_caption_tracks_is_unavailable():
    result = t.fetch_transcript("abc", "en", FakeSource([]))
    assert result.state == t.UNAVAILABLE
    assert "no caption tracks" in result.note


def test_an_empty_caption_track_is_unavailable_not_ok():
    result = t.fetch_transcript("abc", "en", FakeSource([track("en", False, "", "   ")]))
    assert result.state == t.UNAVAILABLE


def test_a_network_error_is_failed_so_it_is_never_mistaken_for_no_transcript():
    result = t.fetch_transcript("abc", "en", FakeSource(error=TimeoutError("slow")))
    assert result.state == t.FAILED
    assert "TimeoutError" in result.note


def test_an_ip_block_fails_and_starts_a_cooldown_so_the_next_video_is_not_sent():
    blocked = FakeSource(error=IpBlocked("blocked"))
    first = t.fetch_transcript("one", "en", blocked)
    assert first.state == t.FAILED and "IpBlocked" in first.note

    untouched = FakeSource([track("en", False, "would work")])
    second = t.fetch_transcript("two", "en", untouched)
    assert second.state == t.FAILED
    assert "cooling down" in second.note
    assert untouched.calls == 0  # the second video was never requested


def test_no_live_socket_is_opened_by_these_tests():
    # Reaching the real library from here would fail on the socket refusal in the autouse fixture.
    result = t.fetch_transcript("abc", "en", FakeSource([track("en", False, "fixture words")]))
    assert result.state == t.OK


def test_a_three_hour_transcript_is_kept_whole_through_the_read_ceiling():
    """Regression: a 400,000-character ceiling cut real long lectures. A 3-hour talk is about 194,000 characters."""
    from listen_app import social_read as sr

    snippet = "This is a sentence of a long lecture about the subject at hand, with a number "
    snippets = [f"{snippet}{i}." for i in range(6000)]  # about 480,000 characters, longer than the old ceiling
    result = t.fetch_transcript("longvid", "en", FakeSource([track("en", False, *snippets)]))
    whole = sr._trim_full(result.text)
    assert "safety ceiling" not in whole
    assert whole.rstrip().endswith("5999.")
    assert len(whole) >= 470_000


def test_a_runaway_transcript_is_still_bounded_and_says_so():
    from listen_app import social_read as sr

    huge = "word " * (sr.MAX_TRANSCRIPT_CHARS // 4)
    text = sr._trim_full(huge)
    assert len(text) <= sr.MAX_TRANSCRIPT_CHARS + 200
    assert "safety ceiling" in text
