import json

import pytest

from listen_app.notes_store import DEFAULT_COLOR, NOTE_COLORS, NotesStore


def test_create_persists_and_reloads(tmp_path):
    path = tmp_path / "notes.json"
    store = NotesStore(path)
    note = store.create("# Hello", color="sage")
    reloaded = NotesStore(path)
    assert reloaded.get(note["id"])["markup"] == "# Hello"
    assert reloaded.get(note["id"])["color"] == "sage"


def test_legacy_colour_names_and_missing_opacity_load(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "notes": [
        {"id": "a", "markup": "x", "color": "yellow", "pinned": False, "minimized": False,
         "created": 1, "modified": 1}]}), encoding="utf-8")
    note = NotesStore(path).get("a")
    assert note["color"] == "mustard"
    assert note["opacity"] == 1.0


def test_unknown_colour_falls_back_to_default(tmp_path):
    note = NotesStore(tmp_path / "n.json").create("x", color="magenta")
    assert note["color"] == DEFAULT_COLOR


def test_update_changes_fields_and_refreshes_modified(tmp_path):
    store = NotesStore(tmp_path / "n.json")
    note = store.create("a")
    before = note["modified"]
    store.update(note["id"], pinned=True, minimized=True, markup="b")
    got = store.get(note["id"])
    assert got["pinned"] and got["minimized"] and got["markup"] == "b"
    assert got["modified"] >= before


def test_update_rejects_unknown_field_and_colour(tmp_path):
    store = NotesStore(tmp_path / "n.json")
    note = store.create("a")
    with pytest.raises(KeyError):
        store.update(note["id"], owner="someone")
    with pytest.raises(ValueError):
        store.update(note["id"], color="magenta")


def test_pinned_notes_sort_first(tmp_path):
    store = NotesStore(tmp_path / "n.json")
    old = store.create("old")
    store.create("new")
    store.update(old["id"], pinned=True)
    assert store.all()[0]["id"] == old["id"]


def test_delete_removes_the_note(tmp_path):
    path = tmp_path / "n.json"
    store = NotesStore(path)
    note = store.create("gone")
    store.delete(note["id"])
    assert NotesStore(path).all() == []


def test_file_is_valid_json_with_version(tmp_path):
    path = tmp_path / "n.json"
    NotesStore(path).create("x")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["notes"]) == 1


def test_no_temp_files_left_behind(tmp_path):
    store = NotesStore(tmp_path / "n.json")
    store.create("a")
    store.create("b")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["n.json"]


def test_every_colour_is_a_hex_value():
    assert all(c.startswith("#") and len(c) == 7 for c in NOTE_COLORS.values())
