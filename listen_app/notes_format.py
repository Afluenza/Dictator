"""Note body format: plain text with simple inline tags, stored as a string.

Block styles are literal line prefixes, so the file stays readable:
    "# " heading 1, "## " heading 2, "### " heading 3, "- " bullet.

Inline styles are explicit open/close tags:
    [b]bold[/b]  [i]italic[/i]  [u]underline[/u]  [s]strikethrough[/s]

A literal "[" or "\\" is written as "\\[" or "\\\\". Tags never span a line, so a
broken tag cannot leak into the next line. This module is pure Python with no GUI.
"""

from __future__ import annotations

INLINE = ("bold", "italic", "underline", "strike")
_OPEN = {"bold": "[b]", "italic": "[i]", "underline": "[u]", "strike": "[s]"}
_CLOSE = {"bold": "[/b]", "italic": "[/i]", "underline": "[/u]", "strike": "[/s]"}
_TOKENS = {**{v: (k, True) for k, v in _OPEN.items()}, **{v: (k, False) for k, v in _CLOSE.items()}}
_ESCAPABLE = "[\\"
_BLOCKS = (("### ", "h3"), ("## ", "h2"), ("# ", "h1"), ("- ", "bullet"))
UNTITLED = "Untitled note"


def parse(markup: str) -> tuple[str, list[frozenset]]:
    """Return (plain text, inline style set for each plain character)."""
    chars: list[str] = []
    styles: list[frozenset] = []
    active: set[str] = set()
    i = 0
    while i < len(markup):
        ch = markup[i]
        if ch == "\\" and i + 1 < len(markup) and markup[i + 1] in _ESCAPABLE:
            chars.append(markup[i + 1])
            styles.append(frozenset(active))
            i += 2
            continue
        if ch == "[":
            end = markup.find("]", i)
            token = markup[i : end + 1] if end != -1 else ""
            if token in _TOKENS:
                name, on = _TOKENS[token]
                (active.add if on else active.discard)(name)
                i = end + 1
                continue
        if ch == "\n":
            active.clear()  # styles never run across a line break
        chars.append(ch)
        styles.append(frozenset(active))
        i += 1
    return "".join(chars), styles


def dump(plain: str, styles: list[frozenset]) -> str:
    """Build the markup string from plain text and per-character style sets."""
    out: list[str] = []
    active: set[str] = set()
    for ch, style in zip(plain, styles):
        if ch == "\n":
            out.extend(_CLOSE[n] for n in INLINE if n in active)
            active.clear()
            out.append("\n")
            continue
        out.extend(_CLOSE[n] for n in INLINE if n in active and n not in style)
        out.extend(_OPEN[n] for n in INLINE if n in style and n not in active)
        active = set(style) & set(INLINE)
        out.append("\\" + ch if ch in _ESCAPABLE else ch)
    out.extend(_CLOSE[n] for n in INLINE if n in active)
    return "".join(out)


def plain_to_markup(text: str) -> str:
    """Wrap untrusted plain text so it stores as literal text with no styling.

    A transcript or a summarised sentence can carry a "[" of its own: a speaker tag,
    an array index like arr[0], a URL with brackets. Written raw into markup, parse()
    would read that "[" as an inline tag and drop it from the note. Escaping every "["
    and "\\" here keeps the text exactly as it arrived.
    """
    return dump(text, [frozenset()] * len(text))


def block_kind(line: str) -> str:
    """Block style of one line: h1, h2, h3, bullet, or p (plain)."""
    for prefix, kind in _BLOCKS:
        if line.startswith(prefix):
            return kind
    return "p"


def title_of(markup: str, limit: int = 60) -> str:
    """First non-empty line, with its block prefix removed, as a short title."""
    plain, _ = parse(markup)
    for line in plain.split("\n"):
        text = line
        for prefix, _kind in _BLOCKS:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        text = text.strip()
        if text:
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return UNTITLED


def styles_to_ranges(styles: list[frozenset]) -> dict[str, list[tuple[int, int]]]:
    """Turn per-character style sets into [start, end) character ranges for each inline style."""
    ranges: dict[str, list[tuple[int, int]]] = {name: [] for name in INLINE}
    for name in INLINE:
        start = None
        for index, style in enumerate(styles):
            if name in style and start is None:
                start = index
            elif name not in style and start is not None:
                ranges[name].append((start, index))
                start = None
        if start is not None:
            ranges[name].append((start, len(styles)))
    return ranges


def ranges_to_styles(length: int, ranges: dict[str, list[tuple[int, int]]]) -> list[frozenset]:
    """Inverse of styles_to_ranges."""
    styles: list[set] = [set() for _ in range(length)]
    for name, spans in ranges.items():
        for start, end in spans:
            for index in range(max(0, start), min(length, end)):
                styles[index].add(name)
    return [frozenset(s) for s in styles]
