from listen_app.summarize import MAX_POINT_CHARS, as_markup, key_points


def test_empty_text_has_no_points():
    assert key_points("") == []
    assert key_points("   \n  ") == []


def test_returns_at_most_count_points_in_original_order():
    text = ". ".join(f"Sentence number {i} about dogs and cats" for i in range(10)) + "."
    points = key_points(text, count=3)
    assert len(points) == 3
    positions = [text.index(p.rstrip(".")) for p in points]
    assert positions == sorted(positions)


def test_frequent_topic_sentences_win_over_an_off_topic_one():
    text = (
        "Solar panels convert sunlight into electricity. "
        "Solar energy is cheap and solar power keeps growing. "
        "The weather was nice on Tuesday. "
        "Installing solar panels at home lowers electricity bills."
    )
    points = key_points(text, count=2)
    assert all("weather" not in p for p in points)
    assert any("olar" in p for p in points)


def test_very_long_point_is_shortened():
    long_sentence = "word " * 200 + "end."
    points = key_points(long_sentence, count=1)
    assert len(points[0]) <= MAX_POINT_CHARS


def test_markup_has_heading_and_bullets():
    markup = as_markup(["First point.", "Second point."])
    assert markup == "## Key points\n- First point.\n- Second point."
    assert as_markup([]) == ""

def test_brackets_in_the_source_text_survive_into_the_note():
    """A summary of real text must keep the text's own brackets.

    Before the fix, as_markup() copied the sentence straight into note markup, so a
    literal "[b]" in the source text was read back by notes_format as an inline style
    tag and vanished from the note. An array index like "arr[0]" lost its "[0]".
    """
    from listen_app.notes_format import parse

    text = (
        "Use the tag [b] to make text bold in the editor. "
        "An array index is written arr[0] in most languages. "
        "A backslash \\ starts an escape sequence. "
        "Never write [/b] without opening it first."
    )
    points = key_points(text, count=4)
    markup = as_markup(points, source="https://example.com/a[1]?q=[x]")
    plain, _styles = parse(markup)
    for sentence in points:
        assert sentence in plain, f"{sentence!r} did not survive into the note"
    assert "https://example.com/a[1]?q=[x]" in plain


def test_a_repeated_sentence_is_not_repeated_as_a_bullet():
    """Captions and pages often repeat a line; the summary must list the idea once."""
    text = " ".join(["Net metering credits owners for surplus power sent back to the grid."] * 6
                    + ["Batteries let households use stored energy after dark."])
    points = key_points(text, count=4)
    assert len(points) == len(set(points)), points


def test_points_cover_the_whole_source_not_one_cluster():
    """A long source with ten distinct topics must not put every bullet in the first few topics."""
    topics = [
        "Solar panels convert sunlight into electricity using photovoltaic cells.",
        "Battery storage lets households use stored energy after dark.",
        "Grid operators balance supply and demand every second.",
        "Installation costs have fallen sharply over the past decade.",
        "Net metering credits owners for surplus power sent back to the grid.",
        "Maintenance is minimal because panels have no moving parts.",
        "Insurance and roof condition affect the total cost of ownership.",
        "Local permits can add weeks to the schedule before work starts.",
        "Cold climates reduce output but snow can reflect extra light.",
        "Financing options include loans, leases and power purchase agreements.",
    ]
    text = " ".join(topics * 3)
    points = key_points(text, count=5)
    covered = {topics.index(p) for p in points if p in topics}
    assert len(covered) >= 4, (covered, points)
