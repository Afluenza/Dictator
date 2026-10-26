"""Reading model: language detection, segmentation, highlight maths, and the playback state machine.

Pure logic, so no Tk and no audio device. The window's wiring is tested separately.
"""
import pytest

from listen_app import reading as r


# ----- language detection --------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("The quick brown fox jumps over the lazy dog and that is the end of this story.", "en"),
        ("El perro corre por el parque y los niños juegan con una pelota para siempre.", "es"),
        ("Le chat dort dans la maison et les enfants jouent pour une journée dans le jardin.", "fr"),
        ("Der Hund läuft über die Wiese und die Kinder spielen mit einem Ball auf dem Feld.", "de"),
        ("Il cane corre nel parco e i bambini giocano con una palla anche questo pomeriggio.", "it"),
        ("O cachorro corre no parque e as crianças brincam com uma bola para você também.", "pt"),
        ("Собака бежит через парк, и дети играют с мячом весь день в этом городе.", "ru"),
        ("これは日本語のテキストです。ねこがすきです。", "ja"),
        ("이것은 한국어 텍스트입니다. 고양이를 좋아합니다.", "ko"),
        ("这是一个中文句子，我们在公园里散步。", "zh"),
    ],
)
def test_detects_the_language_from_script_or_function_words(text, expected):
    assert r.detect_language(text) == expected


def test_kana_outranks_han_so_japanese_is_not_called_chinese():
    assert r.detect_language("東京の天気はとても良いです。") == "ja"


def test_empty_or_unidentifiable_text_defaults_to_english():
    assert r.detect_language("") == "en"
    assert r.detect_language("12345 !!!") == "en"


def test_manual_override_beats_detection():
    text = "Der Hund läuft über die Wiese und die Kinder spielen mit einem Ball."
    assert r.resolve_language(text, "fr") == "fr"
    assert r.resolve_language(text, r.AUTO) == "de"


def test_unknown_override_falls_back_to_detection_instead_of_crashing():
    assert r.resolve_language("El perro corre por el parque.", "xx") == "es"


# ----- segmentation ----------------------------------------------------------------
def test_spaced_languages_split_on_words_and_keep_apostrophes():
    text = "Don't stop, mañana: 3 cats!"
    spans = r.word_spans(text, "en")
    assert [text[a:b] for a, b in spans] == ["Don't", "stop", "mañana", "3", "cats"]


def test_japanese_and_chinese_are_segmented_per_character_not_per_space():
    text = "東京は大きい"
    spans = r.word_spans(text, "ja")
    assert [text[a:b] for a, b in spans] == list("東京は大きい")
    assert len(r.word_spans("我们去公园", "zh")) == 5


def test_punctuation_and_whitespace_are_never_tokens():
    assert r.word_spans("... — ! \n\t ?", "en") == []


def test_numbers_and_accented_letters_are_words():
    text = "Año 2026: café, naïve."
    assert [text[a:b] for a, b in r.word_spans(text, "es")] == ["Año", "2026", "café", "naïve"]


# ----- highlight maths -----------------------------------------------------------
def test_sentence_plans_give_global_token_indices_across_sentences():
    plans = r.sentence_plan(["One two.", "Three."], "en")
    assert [p.first_token for p in plans] == [0, 2]
    assert [p.count for p in plans] == [2, 1]


def test_token_for_moves_through_a_sentence_in_proportion_to_word_length():
    plans = r.sentence_plan(["a bbbbbbbbbb ccc."], "en")  # "bbbbbbbbbb" is long, so it takes most of the time
    assert r.token_for(plans, 0, 0.0) == 0
    assert r.token_for(plans, 0, 0.2) == 1
    assert r.token_for(plans, 0, 0.95) == 2


def test_token_for_returns_none_for_punctuation_only_sentences():
    plans = r.sentence_plan(["...", "Hi."], "en")
    assert r.token_for(plans, 0, 0.5) is None
    assert r.token_for(plans, 1, 0.5) == plans[1].first_token


def test_token_for_clamps_out_of_range_fractions():
    plans = r.sentence_plan(["one two"], "en")
    assert r.token_for(plans, 0, -5.0) == 0
    assert r.token_for(plans, 0, 9.0) == 1


def test_reading_band_is_the_word_and_one_neighbour_each_side():
    assert r.band(5, 10) == (4, 7)
    assert r.band(0, 10) == (0, 2)  # no word before the first
    assert r.band(9, 10) == (8, 10)  # no word after the last
    assert r.band(3, 1) == (0, 1)  # a one-word text is just that word


def test_band_clamps_a_position_past_the_end():
    assert r.band(99, 4) == (2, 4)
    assert r.band(0, 0) == (0, 0)


# ----- playback state ----------------------------------------------------------
def test_read_then_natural_finish_offers_restart_and_new_only():
    state = r.ReadingState()
    state.read()
    state.finish()
    assert r.controls(state) == {"read": False, "stop": False, "restart": True, "new": True}


def test_stopping_by_hand_never_offers_restart_or_new():
    state = r.ReadingState()
    state.read()
    state.stop(spoken_token=4)
    assert r.controls(state) == {"read": True, "stop": False, "restart": False, "new": False}


def test_stop_keeps_the_position_so_read_resumes_there():
    state = r.ReadingState()
    state.read()
    state.stop(spoken_token=7)
    assert state.read() == 7


def test_restart_begins_at_the_top_and_restores_active_controls():
    state = r.ReadingState()
    state.read()
    state.finish()
    assert state.restart() == 0
    assert r.controls(state) == {"read": False, "stop": True, "restart": False, "new": False}


def test_a_click_while_playing_stops_speech_and_moves_the_start_position():
    state = r.ReadingState()
    state.read()
    before = state.session
    state.select(12)
    assert state.phase == r.READY
    assert state.session > before  # the old speech session is now stale
    assert state.read() == 12


def test_a_click_after_completion_restores_read_and_stop_controls():
    state = r.ReadingState()
    state.read()
    state.finish()
    state.select(3)
    assert r.controls(state)["read"] is True
    assert r.controls(state)["restart"] is False


def test_editing_after_completion_exits_the_completed_state():
    state = r.ReadingState()
    state.read()
    state.finish()
    state.edit()
    assert r.controls(state) == {"read": True, "stop": False, "restart": False, "new": False}
    assert state.position == 0


def test_editing_during_playback_invalidates_the_running_session():
    state = r.ReadingState()
    state.read()
    running = state.session
    state.edit()
    assert state.session != running  # stale highlights and queued audio from the old text are ignored


def test_new_clears_completion_and_position():
    state = r.ReadingState()
    state.read()
    state.stop(spoken_token=5)
    state.new()
    assert state.position == 0 and state.phase == r.READY


def test_stop_when_not_playing_changes_nothing():
    state = r.ReadingState()
    session = state.session
    state.stop(spoken_token=3)
    assert state.session == session and state.position == 0


def test_finish_when_not_playing_is_ignored():
    # A late "finished" event from an old session must not flip a stopped or edited state into FINISHED.
    state = r.ReadingState()
    state.read()
    state.stop(spoken_token=2)
    state.finish()
    assert state.phase == r.STOPPED


# ----- voice routing -------------------------------------------------------------
ALL_SAY = ["Samantha", "Mónica", "Paulina", "Thomas", "Amélie", "Anna", "Alice", "Luciana", "Joana",
           "Milena", "Kyoko", "Yuna", "Tingting"]


def test_english_uses_kokoro_when_it_is_loaded():
    choice = r.choose_voice("en", ALL_SAY, kokoro_ready=True)
    assert choice.engine == "kokoro" and choice.voice is None


def test_english_falls_back_to_macos_and_says_so_when_kokoro_is_missing():
    choice = r.choose_voice("en", ALL_SAY, kokoro_ready=False)
    assert choice.engine == "say" and choice.voice == "Samantha"
    assert "Kokoro is not loaded" in choice.note


@pytest.mark.parametrize("lang", ["es", "fr", "de", "it", "pt", "ru", "ja", "ko", "zh"])
def test_each_non_english_language_gets_a_native_say_voice(lang):
    choice = r.choose_voice(lang, ALL_SAY, kokoro_ready=True)
    assert choice.engine == "say"
    assert choice.voice in r.LANGUAGES[lang][1]


def test_a_language_with_no_installed_voice_is_refused_never_given_an_english_voice():
    # Only English voices are installed: Spanish must not be spoken with one.
    choice = r.choose_voice("es", ["Samantha", "Alex"], kokoro_ready=True)
    assert choice.engine == "" and choice.voice is None
    assert "No Spanish voice is installed" in choice.note


def test_an_unsupported_language_code_is_refused():
    choice = r.choose_voice("xx", ALL_SAY, kokoro_ready=True)
    assert choice.engine == "" and choice.voice is None


def test_no_voice_at_all_on_english_is_refused_not_silent():
    choice = r.choose_voice("en", [], kokoro_ready=False)
    assert choice.engine == "" and choice.voice is None


def test_the_supported_list_is_ten_languages_including_english():
    assert len(r.LANGUAGES) == 10
    assert "en" in r.LANGUAGES


# ----- colour ------------------------------------------------------------------
def test_reading_band_text_meets_wcag_aa_on_both_themes():
    # The band text is the theme's own TEXT colour over the band fill. Check both the light and dark pairs.
    dark_text, dark_body = (242, 240, 245), (36, 31, 44)
    assert r.contrast_ratio(dark_text, dark_body) >= 4.5
    light_text, light_body = (28, 22, 34), (250, 249, 252)
    assert r.contrast_ratio(light_text, light_body) >= 4.5


def test_blend_keeps_the_accent_hue_at_partial_opacity():
    assert r.blend("#3478F6", "#241F2C", 1.0) == "#3478f6"
    assert r.blend("#3478F6", "#241F2C", 0.0) == "#241f2c"
    mid = r.blend("#3478F6", "#241F2C", 0.5)
    assert mid != "#3478f6" and mid != "#241f2c"
