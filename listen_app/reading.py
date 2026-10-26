"""The reading model for the Listen window: languages, word segmentation, playback state, highlight maths.

Pure Python, with no Tk and no audio, so every rule here is unit tested and the window only drives it.

Languages: English plus nine other languages (see LANGUAGES). Kokoro's grapheme-to-phoneme step is
English-only (misaki returned unknown symbols for Spanish, Japanese and Russian), so only English uses
Kokoro. The other languages use macOS `say`, which ships native voices for them.

Playback phases (one state machine, no other flags):
    ready     nothing is playing; Read starts from `position`
    playing   speech is running; Stop is the only control that ends it early
    stopped   the user pressed Stop; `position` is kept, so Read resumes there
    finished  the text ran to its end naturally; Restart and New are offered, nothing else
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Initial language list. Selection basis: languages with a native macOS `say` voice on this Mac,
# covering the major writing systems (Latin, Cyrillic, Han, Kana, Hangul). This is NOT a ranking and
# not a claim of a global top 10. Arabic and Hindi are excluded for now: Arabic needs right-to-left
# layout this window has not verified, and no Hindi voice is guaranteed on every Mac.
LANGUAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    # code: (label, preferred `say` voices, in order)
    "en": ("English", ("Samantha",)),
    "es": ("Spanish", ("Mónica", "Paulina")),
    "fr": ("French", ("Thomas", "Amélie")),
    "de": ("German", ("Anna",)),
    "it": ("Italian", ("Alice",)),
    "pt": ("Portuguese", ("Luciana", "Joana")),
    "ru": ("Russian", ("Milena",)),
    "ja": ("Japanese", ("Kyoko",)),
    "ko": ("Korean", ("Yuna",)),
    "zh": ("Chinese (Mandarin)", ("Tingting",)),
}
AUTO = "auto"  # the language menu's first entry: detect from the text

_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
_SAMPLE_CHARS = 5000

# Function words that identify a Latin-script language. Words shared between languages (la, en, a, con,
# die) are left out, so they cannot vote for the wrong one.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the and is of that with for are this from was have".split()),
    "es": frozenset("el los las que por una para pero también está".split()),
    "fr": frozenset("le les et est des une pour dans qui pas nous vous".split()),
    "de": frozenset("der und das ist nicht mit auf ein eine ich sie".split()),
    "it": frozenset("il che non per sono gli della anche questo molto".split()),
    "pt": frozenset("não uma para com os são mas você isso também".split()),
}

# One token per character for Japanese and Chinese, which have no spaces. Every other language is
# space-separated, so a token is a run of letters and digits.
_CJK_CHAR = r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]"
_WORD = r"[^\W_]+(?:['\u2019][^\W_]+)*"
_TOKEN_SPACED = re.compile(_WORD)
_TOKEN_CJK = re.compile(f"{_CJK_CHAR}|{_WORD}")
PER_CHARACTER = frozenset({"ja", "zh"})


def detect_language(text: str) -> str:
    """Best guess at the language of `text`. A heuristic: the manual language menu always overrides it.

    Script first (kana means Japanese, Hangul means Korean, Cyrillic means Russian, Han alone means
    Chinese), then the most common function words for the Latin languages. Anything it cannot tell
    apart is English.
    """
    sample = text[:_SAMPLE_CHARS]
    if _KANA.search(sample):
        return "ja"
    if _HANGUL.search(sample):
        return "ko"
    han = len(_HAN.findall(sample))
    cyrillic = len(_CYRILLIC.findall(sample))
    latin = len(_LATIN.findall(sample))
    if han and han >= latin:
        return "zh"
    if cyrillic and cyrillic >= latin:
        return "ru"
    words = re.findall(r"[^\W\d_]+", sample.lower())
    scores = {code: sum(1 for w in words if w in stop) for code, stop in _STOPWORDS.items()}
    best = max(scores, key=lambda code: scores[code])
    return best if scores[best] else "en"


def resolve_language(text: str, choice: str) -> str:
    """The language to use: the user's menu choice, or the detected one when the choice is AUTO."""
    if choice != AUTO and choice in LANGUAGES:
        return choice
    return detect_language(text)


@dataclass(frozen=True)
class VoiceChoice:
    """Which engine speaks a language, and the voice it uses. `voice` is None when nothing can speak it."""

    lang: str
    engine: str  # "kokoro" (English only) or "say" (macOS), or "" when no engine can speak it
    voice: str | None
    note: str  # one plain sentence the reader sees, so a fallback is never silent


def choose_voice(lang: str, say_voices: list[str], kokoro_ready: bool) -> VoiceChoice:
    """Pick the engine and voice for `lang`. Never returns a voice that cannot speak the language.

    English uses Kokoro when it is loaded, because it is the higher-quality English voice. Every other
    language needs a native macOS `say` voice; a missing voice is reported, not replaced by an English one.
    """
    label = LANGUAGES.get(lang, ("", ()))[0] or lang
    if lang == "en":
        if kokoro_ready:
            return VoiceChoice("en", "kokoro", None, "English voice: Kokoro.")
        if say_voices:
            return VoiceChoice("en", "say", LANGUAGES["en"][1][0] if LANGUAGES["en"][1][0] in say_voices
                               else say_voices[0], "English voice: macOS (Kokoro is not loaded).")
        return VoiceChoice("en", "", None, "No English voice is available on this machine.")
    if lang not in LANGUAGES:
        return VoiceChoice(lang, "", None, f"{lang} is not one of the supported languages.")
    for preferred in LANGUAGES[lang][1]:
        if preferred in say_voices:
            return VoiceChoice(lang, "say", preferred, f"{label} voice: {preferred} (macOS).")
    return VoiceChoice(lang, "", None,
                       f"No {label} voice is installed on this Mac. Add one in System Settings > "
                       "Accessibility > Spoken Content, or choose English to hear the text in English.")


def word_spans(text: str, lang: str) -> list[tuple[int, int]]:
    """(start, end) character offsets of every word, in reading order. Punctuation and spaces are not tokens."""
    pattern = _TOKEN_CJK if lang in PER_CHARACTER else _TOKEN_SPACED
    return [m.span() for m in pattern.finditer(text)]


@dataclass(frozen=True)
class SentencePlan:
    """Where one rendered sentence sits in the global token list, and how long each of its tokens is."""

    first_token: int
    lengths: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.lengths)


def sentence_plan(sentences: list[str], lang: str, first_token: int = 0) -> list[SentencePlan]:
    """Map rendered sentences onto the token list. `first_token` is where the first sentence begins."""
    plans: list[SentencePlan] = []
    cursor = first_token
    for sentence in sentences:
        lengths = tuple(end - start for start, end in word_spans(sentence, lang))
        plans.append(SentencePlan(cursor, lengths))
        cursor += len(lengths)
    return plans


def token_for(plans: list[SentencePlan], chunk: int, fraction: float) -> int | None:
    """The global token being spoken, `fraction` (0 to 1) of the way through sentence `chunk`.

    Each token takes time in proportion to its length, because a long word takes longer to say. This is
    an estimate: neither engine reports word boundaries, so the highlight is approximate within a sentence
    and exact at sentence starts. Returns None for a sentence with no tokens (punctuation only).
    """
    if not 0 <= chunk < len(plans) or plans[chunk].count == 0:
        return None
    plan = plans[chunk]
    weights = [max(1, n) for n in plan.lengths]
    target = min(max(fraction, 0.0), 1.0) * sum(weights)
    acc = 0
    for offset, weight in enumerate(weights):
        acc += weight
        if target < acc:
            return plan.first_token + offset
    return plan.first_token + plan.count - 1


def band(token: int, total: int) -> tuple[int, int]:
    """The contextual reading band: the spoken token and one neighbour on each side. Returns [first, last)."""
    if total <= 0:
        return 0, 0
    token = min(max(token, 0), total - 1)
    return max(0, token - 1), min(total, token + 2)


# ----- playback state ----------------------------------------------------------
READY, PLAYING, STOPPED, FINISHED = "ready", "playing", "stopped", "finished"


@dataclass
class ReadingState:
    """One reading position and one phase. `session` changes whenever speech starts, stops, or the text changes.

    Any speech or summary result that carries an older session number is ignored, so a previous text version
    can never be heard or highlighted after an edit.
    """

    phase: str = READY
    position: int = 0  # token index that Read starts from, or resumes at after Stop
    session: int = 0

    def read(self) -> int:
        """Start speaking from `position`. Returns that token index."""
        self.phase = PLAYING
        self.session += 1
        return self.position

    def restart(self) -> int:
        """Start again from the beginning of the text."""
        self.position = 0
        return self.read()

    def stop(self, spoken_token: int | None = None) -> None:
        """The user pressed Stop. Keep the word being spoken, so Read resumes there."""
        if self.phase != PLAYING:
            return
        if spoken_token is not None:
            self.position = spoken_token
        self.phase = STOPPED
        self.session += 1

    def finish(self) -> None:
        """The text ran to its end. The next Restart begins at the top."""
        if self.phase != PLAYING:
            return
        self.phase = FINISHED
        self.position = 0
        self.session += 1

    def select(self, token: int) -> None:
        """A word was clicked or chosen from the keyboard. The next Read starts there, and any speech stops."""
        if self.phase == PLAYING:
            self.session += 1
        self.position = max(0, token)
        self.phase = READY

    def edit(self) -> None:
        """The text changed. Any speech stops and the reading position resets to the beginning.

        Mapping the old position onto edited text is not reliable (a sentence may have moved or vanished),
        so the reset is the honest option.
        """
        self.session += 1
        self.position = 0
        self.phase = READY

    def new(self) -> None:
        """Start a fresh text. Same reset as an edit: position, completion and speech are cleared."""
        self.edit()


def controls(state: ReadingState) -> dict[str, bool]:
    """Which controls are live for this phase. Restart and New appear only after natural completion."""
    if state.phase == FINISHED:
        return {"read": False, "stop": False, "restart": True, "new": True}
    playing = state.phase == PLAYING
    return {"read": not playing, "stop": playing, "restart": False, "new": False}


def contrast_ratio(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two sRGB colours (1.0 to 21.0). Used to check the reading band."""

    def lum(rgb: tuple[int, int, int]) -> float:
        def channel(value: int) -> float:
            v = value / 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        r, g, b = (channel(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((lum(foreground), lum(background)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def blend(foreground: str, background: str, alpha: float) -> str:
    """#rrggbb mix of `foreground` over `background` at `alpha` (0 to 1). Keeps the accent's hue in the band."""
    fg = [int(foreground.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    bg = [int(background.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    mixed = [round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg)]
    return "#" + "".join(f"{c:02x}" for c in mixed)
