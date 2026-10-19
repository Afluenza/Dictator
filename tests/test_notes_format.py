from listen_app.notes_format import UNTITLED, block_kind, dump, parse, title_of


def test_plain_text_round_trips_with_no_styles():
    plain, styles = parse("hello world")
    assert plain == "hello world"
    assert all(s == frozenset() for s in styles)
    assert dump(plain, styles) == "hello world"


def test_inline_tags_set_styles_and_strip_from_text():
    plain, styles = parse("a [b]bold[/b] [i]it[/i] [u]u[/u] [s]s[/s]")
    assert plain == "a bold it u s"
    assert styles[2] == frozenset({"bold"})
    assert styles[plain.index("i")] == frozenset({"italic"})
    assert styles[plain.index("u")] == frozenset({"underline"})
    assert styles[plain.index("s")] == frozenset({"strike"})


def test_overlapping_styles_round_trip():
    markup = "[b]bold [i]both[/i][/b] plain"
    plain, styles = parse(markup)
    assert plain == "bold both plain"
    # dump may nest the closing tags in a different order; the meaning must be identical.
    assert parse(dump(plain, styles)) == (plain, styles)


def test_literal_brackets_and_backslashes_survive_a_round_trip():
    plain = "use [b] literally and a \\ slash"
    styles = [frozenset()] * len(plain)
    markup = dump(plain, styles)
    assert parse(markup)[0] == plain


def test_styles_do_not_run_across_a_line_break():
    markup = "[b]one\ntwo[/b]"
    plain, styles = parse(markup)
    assert plain == "one\ntwo"
    assert frozenset({"bold"}) in styles
    assert styles[plain.index("t")] == frozenset()  # line two is not bold


def test_unclosed_tag_is_closed_at_the_end_of_its_line():
    plain, styles = parse("[i]open")
    assert styles[0] == frozenset({"italic"})
    assert dump(plain, styles) == "[i]open[/i]"


def test_block_kind_reads_prefixes_in_the_right_order():
    assert block_kind("### h") == "h3"
    assert block_kind("## h") == "h2"
    assert block_kind("# h") == "h1"
    assert block_kind("- item") == "bullet"
    assert block_kind("plain") == "p"
    assert block_kind("#nospace") == "p"


def test_title_skips_blank_lines_and_block_prefixes():
    assert title_of("\n\n## Shopping list\n- milk") == "Shopping list"
    assert title_of("[b]Bold title[/b]") == "Bold title"
    assert title_of("   \n ") == UNTITLED


def test_title_is_shortened():
    long = "x" * 200
    assert len(title_of(long)) == 60
    assert title_of(long).endswith("…")


def test_styles_to_ranges_and_back_are_inverse():
    from listen_app.notes_format import ranges_to_styles, styles_to_ranges

    markup = "[b]ab[/b]c[i]de[/i]"
    plain, styles = parse(markup)
    ranges = styles_to_ranges(styles)
    assert ranges["bold"] == [(0, 2)]
    assert ranges["italic"] == [(3, 5)]
    assert ranges_to_styles(len(plain), ranges) == styles

def test_plain_to_markup_keeps_brackets_and_backslashes_literal():
    """Untrusted text (a transcript, a summary) must survive being stored as note markup."""
    from listen_app.notes_format import plain_to_markup

    text = "He said [b] out loud and the path is C:\\temp. Also arr[0] and [/b]."
    plain, styles = parse(plain_to_markup(text))
    assert plain == text
    assert all(s == frozenset() for s in styles)
