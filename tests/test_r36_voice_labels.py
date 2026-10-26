"""Tests for the voice labels: the menu shows "Heart", the engine still gets "af_heart".

Revert check: restore `voice_label` to return `name` unchanged and the label tests fail; restore the
menu to `values=names` and the round-trip tests fail.
"""

from __future__ import annotations

import queue

import pytest

from listen_app import tts


# ----- the label function -------------------------------------------------------
@pytest.mark.parametrize("voice_id, expected", [
    ("af_heart", "Heart"),
    ("bm_george", "George"),
    ("zf_xiaobei", "Xiaobei"),
    ("Samantha", "Samantha"),      # a macOS `say` voice has no prefix and is already a name
    ("", ""),                      # the engine's own default, which is not a menu entry
    ("nounderscore", "nounderscore"),  # no prefix to strip, so it is left exactly as given
])
def test_voice_label_strips_the_prefix_and_capitalises(voice_id, expected):
    assert tts.voice_label(voice_id) == expected


def test_a_shared_name_is_reported_rather_than_merged():
    # The real Kokoro list has Santa in three IDs and Dora, Alex and Alpha in two, so stripping the
    # prefix alone would put two different voices behind one label. Those keep their prefix.
    names = ["af_santa", "am_santa", "em_santa", "ef_dora", "pf_dora", "af_alpha", "hf_alpha", "af_heart"]
    assert tts.voice_label("af_santa", names) == "Af Santa"
    assert tts.voice_label("em_santa", names) == "Em Santa"
    assert tts.voice_label("ef_dora", names) == "Ef Dora"
    assert tts.voice_label("af_heart", names) == "Heart"  # no collision, plain name
    labels = list(tts.labelled_voices(names).values())
    assert len(set(labels)) == len(labels), "two voices must never share one menu label"


def test_every_label_in_a_real_voice_list_is_unique():
    # The shipped Kokoro vocabulary: 54 IDs, four names repeated across languages.
    names = ["af_alloy", "af_heart", "af_santa", "am_santa", "em_santa", "bf_emma", "bm_george",
             "ef_dora", "pf_dora", "em_alex", "pm_alex", "hf_alpha", "jf_alpha", "zf_xiaobei"]
    mapping = tts.labelled_voices(names)
    assert len(mapping) == len(names)
    assert len(set(mapping.values())) == len(names), f"duplicate labels: {mapping}"
    assert all("_" not in label for label in mapping.values())


def test_the_say_engine_names_come_through_untouched():
    names = ["Samantha", "Mónica", "Amélie"]
    mapping = tts.labelled_voices(names)
    assert mapping == {name: name for name in names}


# ----- display label back to engine ID ------------------------------------------
def test_a_selected_label_maps_back_to_the_real_id():
    mapping = tts.labelled_voices(["af_heart", "bm_george", "em_santa", "am_santa"])
    assert tts.voice_id_for("Heart", mapping) == "af_heart"
    assert tts.voice_id_for("George", mapping) == "bm_george"
    assert tts.voice_id_for("Em Santa", mapping) == "em_santa"
    assert tts.voice_id_for("Am Santa", mapping) == "am_santa"


def test_an_unknown_label_is_not_guessed_into_a_voice():
    mapping = tts.labelled_voices(["af_heart", "bm_george"])
    assert tts.voice_id_for("Nobody", mapping) == ""


# ----- the window's menu --------------------------------------------------------
def _window_with_menu():
    """ListenApp without its constructor: it loads a voice engine and binds the bridge."""
    from listen_app import gui

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._events = queue.Queue()
    app._busy = False
    app._load_seq = 0
    app._loaded_source = ""
    state: dict = {"values": [], "shown": ""}
    app._set_buttons = lambda **kw: None
    app.status = type("S", (), {"configure": lambda _s, **kw: None})()
    app.voice_menu = type("M", (), {
        "configure": lambda _s, **kw: state.__setitem__("values", kw.get("values", [])),
        "set": lambda _s, v: state.__setitem__("shown", v),
        "get": lambda _s: state["shown"],
    })()
    return app, state


class _Engine:
    def __init__(self, names, default):
        self._names, self.default_voice = names, default

    def voice_names(self):
        return list(self._names)


def test_the_menu_shows_short_labels_and_still_names_the_real_voice():
    app, state = _window_with_menu()
    app._handle("engine", _Engine(["af_heart", "bm_george"], "af_heart"))
    assert state["values"] == ["Heart", "George"]
    assert state["shown"] == "Heart"
    assert app._selected_voice() == "af_heart", "the default must still resolve to its real ID"

    state["shown"] = "George"  # what the Commander sees after choosing the second entry
    assert app._selected_voice() == "bm_george"


def test_a_colliding_engine_default_still_selects_the_right_voice():
    app, state = _window_with_menu()
    app._handle("engine", _Engine(["am_santa", "em_santa"], "em_santa"))
    assert state["values"] == ["Am Santa", "Em Santa"]
    assert state["shown"] == "Em Santa"
    assert app._selected_voice() == "em_santa"


def test_a_say_engine_menu_is_unchanged_and_selects_the_say_voice():
    app, state = _window_with_menu()
    app._handle("engine", _Engine(["Samantha", "Mónica"], "Samantha"))
    assert state["values"] == ["Samantha", "Mónica"]
    assert app._selected_voice() == "Samantha"


def test_an_engine_with_no_voices_does_not_raise():
    app, state = _window_with_menu()
    app._handle("engine", _Engine([], ""))
    assert state["values"] == []
