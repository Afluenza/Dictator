"""Local notes store: one JSON file in the user's home folder. No network, no accounts.

Each note is a dict: id, markup (see notes_format), color, pinned, minimized, created,
modified. Writes go to a temp file and are then renamed, so a crash cannot leave a
half-written file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

# Muted palette, matched to the Sticky Notepad swatches the Commander shared.
NOTE_COLORS = {
    "olive": "#A9A57E",
    "mustard": "#B3975F",
    "sage": "#94AE92",
    "slate": "#8D9AAB",
    "mauve": "#9A88AE",
    "rose": "#A88686",
}
DEFAULT_COLOR = "mauve"
# Colour names used before the palette change, mapped on load so old notes still open.
LEGACY_COLORS = {"yellow": "mustard", "green": "sage", "blue": "slate", "pink": "rose",
                 "purple": "mauve", "grey": "olive"}
MIN_OPACITY, MAX_OPACITY = 0.3, 1.0


def default_path() -> Path:
    return Path(os.environ.get("LISTEN_APP_NOTES", Path.home() / ".listen-app" / "notes.json"))


class NotesStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self._notes: dict[str, dict] = {}
        self._load()

    # ----- persistence --------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._notes = {n["id"]: n for n in data.get("notes", [])}
        for note in self._notes.values():
            note["color"] = LEGACY_COLORS.get(note["color"], note["color"])
            if note["color"] not in NOTE_COLORS:
                note["color"] = DEFAULT_COLOR
            note.setdefault("opacity", MAX_OPACITY)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "notes": list(self._notes.values())}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".notes-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ----- queries ---------------------------------------------------------
    def all(self) -> list[dict]:
        """Newest modified first, pinned notes ahead of the rest."""
        return sorted(self._notes.values(), key=lambda n: (not n["pinned"], -n["modified"]))

    def get(self, note_id: str) -> dict:
        return self._notes[note_id]

    # ----- writes -------------------------------------------------------------
    def create(self, markup: str = "", color: str = DEFAULT_COLOR) -> dict:
        now = time.time()
        note = {
            "id": uuid.uuid4().hex,
            "markup": markup,
            "color": color if color in NOTE_COLORS else DEFAULT_COLOR,
            "pinned": False,
            "minimized": False,
            "opacity": MAX_OPACITY,
            "created": now,
            "modified": now,
        }
        self._notes[note["id"]] = note
        self._save()
        return note

    def update(self, note_id: str, **fields) -> dict:
        allowed = {"markup", "color", "pinned", "minimized", "opacity"}
        unknown = set(fields) - allowed
        if unknown:
            raise KeyError(f"cannot set {sorted(unknown)}")
        if "color" in fields and fields["color"] not in NOTE_COLORS:
            raise ValueError(f"unknown colour {fields['color']!r}")
        if "opacity" in fields and not MIN_OPACITY <= fields["opacity"] <= MAX_OPACITY:
            raise ValueError(f"opacity must be between {MIN_OPACITY} and {MAX_OPACITY}")
        note = self._notes[note_id]
        note.update(fields)
        note["modified"] = time.time()
        self._save()
        return note

    def delete(self, note_id: str) -> None:
        self._notes.pop(note_id)
        self._save()
