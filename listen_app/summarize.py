"""Extractive key-point summary, local and offline.

Each sentence is scored by the frequency of its content words across the whole text,
then the top sentences are returned in their original order as bullet points.
No model, no network. The method is deliberately simple and is shown as a summary,
not as a rewrite of the text.
"""

from __future__ import annotations

import re
from collections import Counter

from listen_app.notes_format import plain_to_markup
from listen_app.tts import split_sentences

STOPWORDS = frozenset(
    """a an and are as at be but by for from has have he her his i if in is it its of on or
    our she so that the their them then there these they this to was we were what when which
    who will with you your not no do does did been being than into also can could would should
    may might just about over more most some such only very too s t""".split()
)
_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", re.UNICODE)
# A script written without spaces (Chinese, Japanese, Korean) has no words to count, so a run of
# these characters is scored as overlapping two-character shingles instead. Without this a whole
# Chinese or Japanese page produced no words, so no points, so "No key points found in the text".
_DENSE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
DEFAULT_POINTS = 5
MAX_POINT_CHARS = 240


def _words(sentence: str) -> list[str]:
    lowered = sentence.lower()
    words = [w for w in _WORD.findall(lowered) if w not in STOPWORDS and len(w) > 2]
    if words:
        return words
    return [run[i:i + 2] for run in _DENSE.findall(lowered) for i in range(len(run) - 1)]


def key_points(text: str, count: int = DEFAULT_POINTS) -> list[str]:
    """Return up to `count` key sentences, in original order, each shortened if very long."""
    sentences = split_sentences(text)
    if not sentences:
        return []
    frequency = Counter(w for s in sentences for w in _words(s))
    scored = []
    for index, sentence in enumerate(sentences):
        words = _words(sentence)
        if not words:
            continue
        # Average frequency per word, so one long sentence does not win just by length.
        score = sum(frequency[w] for w in words) / len(words)
        scored.append((score, index))
    chosen = sorted(index for _score, index in sorted(scored, reverse=True)[:count])
    points = []
    for index in chosen:
        sentence = sentences[index]
        if len(sentence) > MAX_POINT_CHARS:
            sentence = sentence[: MAX_POINT_CHARS - 1].rsplit(" ", 1)[0] + "…"
        points.append(sentence)
    return points


def as_markup(points: list[str], title: str = "Key points", source: str = "") -> str:
    """Note markup: a heading, then one bullet per point, then the source if there is one.

    Every point and the source are escaped as literal text, so a sentence that happens to
    contain a bracket ("arr[0]", a speaker tag, a bracketed URL) reaches the note unchanged.
    """
    if not points:
        return ""
    lines = [f"## {title}", *(f"- {plain_to_markup(p)}" for p in points)]
    if source:
        lines.append("")
        lines.append(f"- Source: {plain_to_markup(source)}")
    return "\n".join(lines)
