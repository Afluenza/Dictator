"""Tests for the memo library and the Memos / Reminders panels. No microphone, model or Reminders write needed."""

import os
import time

import pytest

from listen_app.memo_list import preview
from listen_app.memos import list_memos, save_transcript, save_wav, transcript_path
from listen_app.reminders_panel import build_markup
import numpy as np


def _memo(folder, stamp: str, text: str | None, mtime: float):
    wav = save_wav(np.zeros(1600, dtype=np.float32), folder / f"memo-{stamp}.wav")
    os.utime(wav, (mtime, mtime))
    if text is not None:
        save_transcript(wav, text)
    return wav


def test_transcript_sidecar_sits_beside_the_wav(tmp_path):
    wav = save_wav(np.zeros(16, dtype=np.float32), tmp_path / "memo-1.wav")
    path = save_transcript(wav, "buy milk")
    assert path == transcript_path(wav) == tmp_path / "memo-1.txt"
    assert path.read_text(encoding="utf-8") == "buy milk"


def test_list_memos_is_newest_first_and_reads_transcripts(tmp_path):
    now = time.time()
    _memo(tmp_path, "a", "older note", now - 100)
    _memo(tmp_path, "b", "newer note", now - 10)
    _memo(tmp_path, "c", None, now - 50)  # not transcribed yet
    memos = list_memos(tmp_path)
    assert [m["text"] for m in memos] == ["newer note", "", "older note"]


def test_list_memos_ignores_non_memo_files_and_missing_folder(tmp_path):
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert list_memos(tmp_path) == []
    assert list_memos(tmp_path / "does-not-exist") == []


def test_preview_collapses_whitespace_and_shortens():
    assert preview("  buy   milk \n eggs ") == "buy milk eggs"
    assert preview("") == "No transcript"
    long = preview("word " * 40, limit=20)
    assert len(long) == 20 and long.endswith("…")


def test_build_markup_keeps_only_ticked_non_empty_lines():
    assert build_markup(["milk", "", "  ", "eggs"]) == "milk\neggs"
    assert build_markup([]) == ""


def test_reminders_panel_refuses_a_real_list_through_the_export_path():
    """The panel has no write path of its own, so a real list is refused by the shared guard."""
    from listen_app.reminders_export import RemindersError, export_to_reminders

    with pytest.raises(RemindersError):
        export_to_reminders("milk", "Chores")
