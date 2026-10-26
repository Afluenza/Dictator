"""Full YouTube transcripts for Listen: which caption track to use, and how to turn it into readable text.

The caption library is MIT-licensed and needs no key or account. It is reached only through `CaptionSource`,
so tests can inject fixtures and never open a socket. Production uses `YouTubeCaptions`, the real library.

Outcomes are kept apart, because they mean different things to the reader:
    ok           a full transcript was retrieved; `text` is the whole of it, in order
    unavailable  YouTube says there is no transcript (disabled, none in any language, private or removed)
    failed       the request itself failed (network, timeout, IP block); retrying later may work

A failed or unavailable transcript is never replaced by the description or the comments.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

OK = "ok"
UNAVAILABLE = "unavailable"
FAILED = "failed"

TRANSCRIPT_COOLDOWN_S = 15 * 60  # after YouTube blocks this IP, stop calling it for a while (measured 2026-10-18)
_blocked_until = 0.0

# Snippets are joined into paragraphs of roughly this many characters, broken at a sentence end where one exists.
PARAGRAPH_CHARS = 600


@dataclass
class Track:
    """One caption track YouTube offers: its language code, whether a person wrote it, and its snippets."""

    language: str
    generated: bool
    snippets: list[str] = field(default_factory=list)


class CaptionSource(Protocol):
    def tracks(self, video_id: str) -> list[Track]: ...


@dataclass
class TranscriptResult:
    state: str
    text: str = ""
    language: str = ""  # the language code actually used
    generated: bool = False  # True when the track was machine-generated
    fallback: bool = False  # True when the reader's chosen language was not available
    note: str = ""
    available: list[str] = field(default_factory=list)  # every language YouTube offered, for the reader's menu


class YouTubeCaptions:
    """The real caption source: youtube-transcript-api. Imported lazily, so a missing package is a clear note."""

    def tracks(self, video_id: str) -> list[Track]:
        from youtube_transcript_api import YouTubeTranscriptApi

        listing = YouTubeTranscriptApi().list(video_id)
        out: list[Track] = []
        for transcript in listing:
            fetched = transcript.fetch(preserve_formatting=False)
            out.append(Track(
                language=transcript.language_code,
                generated=bool(transcript.is_generated),
                snippets=[s.text for s in fetched.snippets],
            ))
        return out


def pick_track(tracks: list[Track], preferred: str) -> tuple[Track | None, bool]:
    """The track to use, and whether it is a fallback. Manual captions beat generated ones within a language.

    The reader's language is tried first (matching either the exact code or its base, so "en-GB" matches "en").
    Otherwise the first manual track is used, then the first generated one, and the caller shows that it is a fallback.
    """
    if not tracks:
        return None, False
    base = preferred.split("-")[0].lower()

    def rank(track: Track) -> tuple[int, int]:
        same = track.language.lower() == preferred.lower() or track.language.split("-")[0].lower() == base
        return (0 if same else 1, 0 if not track.generated else 1)

    best = min(tracks, key=rank)
    return best, not (best.language.lower() == preferred.lower() or best.language.split("-")[0].lower() == base)


def join_snippets(snippets: list[str]) -> str:
    """Readable paragraphs from caption snippets, in order, with no words dropped.

    Captions split speech at arbitrary points, so the snippets are joined with spaces and cut into paragraphs at
    sentence ends once a paragraph is long enough. A word or short phrase repeated on purpose ("no no no") is kept.
    Only a whole multi-word line that repeats the line before it is dropped: that is the overlap artefact some
    caption tracks produce when two timed windows share a line.
    """
    cleaned: list[str] = []
    for raw in snippets:
        piece = re.sub(r"\s+", " ", (raw or "")).strip()
        if not piece:
            continue
        if cleaned and cleaned[-1] == piece and len(piece.split()) >= 3:
            continue
        cleaned.append(piece)
    paragraphs: list[str] = []
    current = ""
    for piece in cleaned:
        current = f"{current} {piece}".strip() if current else piece
        if len(current) >= PARAGRAPH_CHARS and re.search(r"[.!?。！？]$", current):
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def fetch_transcript(video_id: str, preferred: str, source: CaptionSource | None = None) -> TranscriptResult:
    """The full transcript for `video_id`, in the reader's language when YouTube offers it."""
    global _blocked_until
    if source is None:
        source = YouTubeCaptions()
    if time.monotonic() < _blocked_until:
        return TranscriptResult(FAILED, note="transcript paused: YouTube blocked this IP, cooling down")
    try:
        tracks = source.tracks(video_id)
    except Exception as exc:  # the library reports each state by type; the name says which one
        name = type(exc).__name__
        if name == "IpBlocked":
            _blocked_until = time.monotonic() + TRANSCRIPT_COOLDOWN_S
            return TranscriptResult(FAILED, note="transcript failed (IpBlocked): YouTube blocked this IP")
        if name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable"):
            return TranscriptResult(UNAVAILABLE, note=f"no transcript on this video ({name})")
        return TranscriptResult(FAILED, note=f"transcript failed ({name})")
    available = sorted({t.language for t in tracks})
    track, fallback = pick_track(tracks, preferred)
    if track is None:
        return TranscriptResult(UNAVAILABLE, note="this video has no caption tracks", available=available)
    text = join_snippets(track.snippets)
    if not text:
        return TranscriptResult(UNAVAILABLE, note="the caption track was empty", available=available)
    kind = "auto-generated" if track.generated else "creator-provided"
    note = f"{kind} {track.language} transcript"
    if fallback:
        note += f" (fallback: {preferred} was not offered)"
    return TranscriptResult(OK, text=text, language=track.language, generated=track.generated,
                            fallback=fallback, note=note, available=available)


def reset_cooldown() -> None:
    """Forget a recorded IP block. For tests; production waits the cooldown out."""
    global _blocked_until
    _blocked_until = 0.0
