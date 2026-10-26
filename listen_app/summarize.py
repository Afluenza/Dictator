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


def _pick(sentences: list[str], scored: dict[int, float], count: int) -> list[int]:
    """Indexes of the chosen sentences, in order.

    The source is cut into `count` equal sections and the best-scoring sentence in each is kept, so a long
    source is covered from start to end. A sentence whose text already appeared is skipped, so a repeated
    caption line is never listed twice. A short source with fewer sections than points is covered as well.
    """
    chosen: list[int] = []
    seen: set[str] = set()
    total = len(sentences)
    # A sentence with no word in common with any other is off-topic: its score is exactly 1.0, so it is
    # never a key point while a shared-vocabulary sentence exists. Only when none exists does the fallback
    # below keep the top sentences, so a short or unrelated text still gets a summary.
    eligible = {i: s for i, s in scored.items() if s > 1.0}
    for section in range(count):
        lo, hi = section * total // count, (section + 1) * total // count
        best = None
        for index in range(lo, hi):
            if index not in eligible or " ".join(sentences[index].lower().split()) in seen:
                continue
            if best is None or eligible[index] > eligible[best]:
                best = index
        if best is not None:
            chosen.append(best)
            seen.add(" ".join(sentences[best].lower().split()))
    if not chosen:
        ranked = sorted(scored, key=lambda i: scored[i], reverse=True)
        chosen = sorted(ranked[:count])
    return sorted(chosen)


def key_points(text: str, count: int = DEFAULT_POINTS) -> list[str]:
    """Return up to `count` key sentences, in original order, each shortened if very long."""
    sentences = split_sentences(text)
    if not sentences:
        return []
    frequency = Counter(w for s in sentences for w in _words(s))
    scored: dict[int, float] = {}
    for index, sentence in enumerate(sentences):
        words = _words(sentence)
        if not words:
            continue
        # Average frequency per word, so one long sentence does not win just by length.
        scored[index] = sum(frequency[w] for w in words) / len(words)
    chosen = _pick(sentences, scored, count)
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
