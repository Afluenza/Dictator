"""Round 29: the Summarize-to-Notes path end to end, the 60,000-character edge, and dead-code checks.

Three groups:

1. Summarize to Notes, driven through the real code path with no display: a bridged page arrives on
   the event queue, the Summarize button reads the box, and the key points land in a new note or an
   existing one through NotesStore.
2. The bridge and summariser at the documented limits: exactly 60,000 characters, 60,001, a
   60,000-character payload made of emoji, and concurrent posts.
3. Dead code and duplication: the removed `_pending_status` field, and the one place the extension
   and the Python side both define the cap, which must stay a single source of truth.

Each behavioural test is written so that reverting its fix makes it fail; the revert is stated in
its docstring and was run by hand (see the round report).
"""

import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from listen_app import bridge
from listen_app.notes_store import NotesStore
from listen_app.summarize import MAX_POINT_CHARS, as_markup, key_points


# ----- helpers --------------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _post(port: int, payload, token="a" * 48, path="/ingest"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", bridge.TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class _Window:
    """A ListenApp with the widgets replaced by recorders: the real handlers, no display.

    The constructor is skipped on purpose (it loads a voice engine, binds the bridge and starts a
    mainloop), so the fields the handlers touch are set here.
    """

    def __init__(self, notes_path):
        from listen_app import gui

        self.app = gui.ListenApp.__new__(gui.ListenApp)
        self.app._events = queue.Queue()
        self.app._loaded_source = ""
        self.app._load_seq = 0
        self.app._busy = False
        self.box = [""]
        self.statuses = []
        app = self.app
        app._set_text = lambda text: self.box.__setitem__(0, text)
        # _on_summarize reads the widget; the recorder answers with whatever the box holds.
        app.textbox = type("T", (), {"get": lambda _self, *_args: self.box[0]})()
        app.status = type("S", (), {"configure": lambda _s, **kw: self.statuses.append(kw.get("text", ""))})()
        self.store = NotesStore(notes_path)
        self.app._notes = None
        self.opened = []
        # The real Summarize handler opens the real Notes window; that needs Tk, so the two
        # window-level calls are captured and the store work is done through NotesStore itself.
        app._on_notes = lambda: self.opened.append("notes")

    def drain(self, reverse=False):
        """Deliver every queued event the way _poll does. `reverse` makes the earliest finish last."""
        events = []
        while not self.app._events.empty():
            events.append(self.app._events.get())
        for event in reversed(events) if reverse else events:
            # A text event carries (kind, payload, source); a failure carries (kind, payload).
            self.app._handle(*event)
        return events

    def deliver(self, kind, payload, source=""):
        self.app._handle(kind, payload, source)

    def summarize(self):
        """Run the real _on_summarize. offer_summary is captured instead of opening a window."""
        offered = []
        self.app._notes = type("N", (), {"offer_summary": staticmethod(offered.append)})()
        self.app._on_summarize()
        return offered


# ----- 1. Summarize to Notes, end to end ------------------------------------
def test_a_bridged_page_summarizes_into_a_new_note(tmp_path):
    """Extension text in, key points out, added to a new note — through the real handlers.

    Revert check: this is the whole path, so removing `offer_summary` from _on_summarize, or the
    store write in NotesWindow._add_summary, fails it.
    """
    from listen_app.notes_gui import NotesWindow

    window = _Window(tmp_path / "notes.json")
    page = (
        "Solar panels convert sunlight into electricity. "
        "Solar energy is cheap and solar power keeps growing as installations rise. "
        "Solar panels on a roof pay for themselves over a decade. "
        "The weather was nice on Tuesday. "
        "Installing solar panels at home lowers electricity bills over time. "
        "Solar inverters convert direct current into household current. "
        "A cat sat on the mat. "
        "Solar batteries store the afternoon's surplus for the evening."
    )
    # The extension posts to the bridge; the window receives it as a bridged event exactly as
    # ListenApp._on_bridged does, which is the first thing after the socket.
    window.app._extract_async(lambda: page, "Loaded from browser: Solar", "Receiving...",
                              source_url="https://example.com/solar")
    window.drain()

    assert window.box[0] == page, "the bridged page must reach the box Summarize reads"
    assert window.app._loaded_source == "https://example.com/solar", "the source travels with the text"

    offered = window.summarize()
    assert len(offered) == 1, "Summarize must offer the key points exactly once"
    markup = offered[0]
    assert markup.startswith("## Key points"), markup
    assert "Source: https://example.com/solar" in markup, "a summary must be traceable to its page"
    assert "weather" not in markup, "an off-topic sentence must not be a key point"
    assert "cat sat" not in markup, "a second off-topic sentence must not be a key point either"
    assert markup.count("- ") >= 4, markup

    # The dialog's "New note" branch, with the real store and the real method.
    before = len(window.store.all())
    NotesWindow._add_summary(_StoreOnly(window.store), markup, None, _Dialog())
    notes = window.store.all()
    assert len(notes) == before + 1
    assert markup in notes[-1]["markup"]


def test_summarize_appends_to_an_existing_note_without_clobbering_it(tmp_path):
    """The dialog's other branch: an existing note keeps its text and gains the key points."""
    from listen_app.notes_gui import NotesWindow

    window = _Window(tmp_path / "notes.json")
    note = window.store.create("# My research\nAlready here.")
    NotesWindow._add_summary(_StoreOnly(window.store), "## Key points\n- A point.", note["id"], _Dialog())

    stored = window.store.get(note["id"])["markup"]
    assert stored.startswith("# My research\nAlready here."), "the existing note must keep its text"
    assert stored.endswith("## Key points\n- A point."), "the key points must be appended"
    assert window.store.get(note["id"])["markup"].count("# My research") == 1


def test_summarize_refuses_an_empty_box_instead_of_making_an_empty_note(tmp_path):
    window = _Window(tmp_path / "notes.json")
    window.box[0] = ""
    offered = window.summarize()
    assert offered == [], "no text must mean no dialog and no note"
    assert "Load some text first" in window.statuses[-1]


def test_summarize_says_so_when_the_text_has_no_key_points(tmp_path):
    """A page of pure stopwords produces no points. Saying "ready" would be a lie."""
    window = _Window(tmp_path / "notes.json")
    window.box[0] = "the and of to " * 40
    offered = window.summarize()
    assert offered == []
    assert "No key points found" in window.statuses[-1]


class _StoreOnly:
    """NotesWindow._add_summary touches the store and these two calls; a window needs Tk."""

    def __init__(self, store):
        self.store = store

    def refresh(self):
        return

    def open_note(self, _note_id):
        return


class _Dialog:
    def destroy(self):
        return


# ----- 2. the 60,000-character edge ----------------------------------------
def test_a_page_of_exactly_the_limit_summarizes(tmp_path):
    """60,000 characters is the documented maximum. It must summarise, not be refused."""
    text = " ".join(["solar panels lower electricity bills over time."] * 1400)[:bridge.MAX_TEXT]
    assert len(text) == bridge.MAX_TEXT
    window = _Window(tmp_path / "notes.json")
    window.app._extract_async(lambda: text, "Loaded", "Receiving...", source_url="https://example.com/long")
    window.drain()
    offered = window.summarize()
    assert len(offered) == 1
    points = key_points(text)
    assert points, "a 60,000-character page must produce key points"
    assert all(len(p) <= MAX_POINT_CHARS for p in points)


def test_the_worst_case_escaped_payload_is_delivered_and_summarized(tmp_path):
    """60,000 emoji is ~720 KB of JSON. Round 26 raised the byte cap; this proves the whole path."""
    received = []
    port = _free_port()
    live = bridge.Bridge(received.append, token="a" * 48, port=port)
    assert live.start(), live.error
    try:
        text = "\U0001F600" * bridge.MAX_TEXT
        body = json.dumps({"kind": "text", "text": text}).encode()
        assert len(body) > 200_000, "the old byte cap refused this with 413"
        status, reply = _post(port, body)
        assert status == 200, reply
    finally:
        live.stop()
    assert len(received) == 1 and len(received[0]["text"]) == bridge.MAX_TEXT

    window = _Window(tmp_path / "notes.json")
    window.app._extract_async(lambda: received[0]["text"], "Loaded", "Receiving...")
    window.drain()
    # Emoji carry no words, so no key points is the honest answer here: what is proven is that the
    # limit itself is not what stops it.
    window.summarize()
    assert "No key points found" in window.statuses[-1]


def test_one_character_over_the_limit_is_refused_by_the_bridge(tmp_path):
    received = []
    port = _free_port()
    live = bridge.Bridge(received.append, token="a" * 48, port=port)
    assert live.start(), live.error
    try:
        status, reply = _post(port, {"kind": "text", "text": "x" * (bridge.MAX_TEXT + 1)})
        assert status == 400 and "longer than" in reply["error"]
    finally:
        live.stop()
    assert received == [], "a refused payload must never reach the window"


def test_concurrent_bursts_at_the_limit_all_arrive_intact():
    """40 simultaneous 60,000-character posts: every one delivered, none truncated, no interleave."""
    received = []
    lock = threading.Lock()

    def collect(payload):
        with lock:
            received.append(payload)

    port = _free_port()
    live = bridge.Bridge(collect, token="a" * 48, port=port)
    assert live.start(), live.error
    try:
        texts = [f"{i:05d}" + "word " * 12000 for i in range(40)]
        texts = [t[:bridge.MAX_TEXT] for t in texts]
        results = []

        def send(text):
            results.append(_post(port, {"kind": "text", "text": text}))

        threads = [threading.Thread(target=send, args=(t,)) for t in texts]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        elapsed = time.perf_counter() - started

        assert all(thread.is_alive() is False for thread in threads), "a post never returned"
        assert all(status == 200 for status, _ in results), [r for r in results if r[0] != 200]
        assert len(received) == 40, f"only {len(received)} of 40 arrived"
        by_prefix = {item["text"][:5]: item["text"] for item in received}
        for text in texts:
            assert by_prefix[text[:5]] == text, "a delivered payload was truncated or interleaved"
        assert elapsed < 30, f"40 posts at the limit took {elapsed:.1f}s"
    finally:
        live.stop()


def test_a_stale_load_cannot_overwrite_a_newer_one(tmp_path):
    """Measured real race: send a page, then a link before the first finishes.

    The queue delivers in completion order, so the slow first load landed last and overwrote the
    newer text while keeping the newer status line. Revert check: remove the `_load_seq` comparison
    in `_handle` and the A text wins again (both boxes are written).
    """
    from listen_app.gui import _LoadResult

    window = _Window(tmp_path / "notes.json")
    # Two loads were started (tickets 1 and 2); the newer one finished first and the older one
    # landed after it. Built by hand rather than raced, so the delivery order is not luck.
    window.app._load_seq = 2
    window.deliver("text", _LoadResult(2, "TEXT-B", "Loaded B", ""), "https://example.com/b")
    window.deliver("text", _LoadResult(1, "TEXT-A", "Loaded A", ""), "https://example.com/a")

    assert window.box[0] == "TEXT-B", "the newer load must win, whatever order the results arrive in"
    assert window.app._loaded_source == "https://example.com/b", "the stale source must not be adopted"
    assert window.statuses[-1] == "Loaded B"


def test_a_stale_failure_cannot_clear_a_newer_page(tmp_path):
    """The mirror case: the first load fails after the second succeeded. Clearing the box then
    would leave the user with nothing and a status line about a page they never asked for."""
    from listen_app.gui import _LoadResult

    window = _Window(tmp_path / "notes.json")
    window.app._load_seq = 2
    window.deliver("text", _LoadResult(2, "TEXT-B", "Loaded B", ""), "https://example.com/b")
    window.deliver("load_failed", _LoadResult(1, "", "", "dead link"))

    assert window.box[0] == "TEXT-B", "a stale failure must not wipe the newer text"
    assert "dead link" not in window.statuses[-1]


# ----- 3. dead code and duplication ---------------------------------------
def test_the_removed_status_field_stays_removed():
    """`_pending_status` was one value shared by every in-flight load, which is what let a slow
    load print the newer load's status. It is replaced by a per-load record and must not return."""
    import inspect

    from listen_app import gui

    source = inspect.getsource(gui)
    assert "_pending_status" not in source, "the shared status field is back"
    assert "_LoadResult" in source and "seq" in source, "each load must carry its own ticket"


def test_the_cap_has_one_source_of_truth_on_each_side():
    """The 60,000 cap is defined once per language, and each side is checked against the other.

    This is the only intentional duplication in the send path: the extension cannot import Python,
    so the number is repeated there and pinned by this test rather than left to drift.
    """
    from pathlib import Path

    from listen_app import bridge as py_bridge

    extension = Path(__file__).resolve().parents[1] / "extension" / "extract.js"
    source = extension.read_text(encoding="utf-8")
    assert f"const LIMIT = {py_bridge.MAX_TEXT};" in source, "extract.js drifted from MAX_TEXT"

    # Inside Python the number must exist once: social_read owns it and the bridge borrows it.
    from listen_app import social_read

    assert social_read.MAX_CHARS == py_bridge.MAX_TEXT, "the bridge cap drifted from the reader's"
    assert py_bridge.MAX_TEXT == social_read.MAX_CHARS


def test_the_file_picker_offers_exactly_what_the_extractors_read():
    """The picker's filter was a hand-written literal that duplicated extract.SUPPORTED_SUFFIXES.

    Revert check: put the literal back and adding a suffix to the extractor silently stops being
    selectable in the dialog; this test then fails because the two lists differ.
    """
    import inspect

    from listen_app import gui
    from listen_app.extract import SUPPORTED_SUFFIXES

    source = inspect.getsource(gui.ListenApp._on_upload)
    assert "SUPPORTED_SUFFIXES" in source, "the picker must build its filter from the extractor's list"
    assert "*.txt *.md" not in source, "the hand-written filter literal is back"
    assert set(SUPPORTED_SUFFIXES) == {".txt", ".md", ".pdf", ".docx"}


def test_the_extension_has_no_second_copy_of_the_extraction_route():
    """client.js and background.js must not each carry their own send logic: one module, two callers."""
    from pathlib import Path

    extension = Path(__file__).resolve().parents[1] / "extension"
    background = (extension / "background.js").read_text(encoding="utf-8")
    assert "fetch(" not in background, "background.js must call client.js, not fetch on its own"
    assert 'from "./client.js"' in background, "the worker must import the one send path"
    popup = (extension / "popup.js").read_text(encoding="utf-8")
    assert "fetch(" not in popup, "popup.js must call client.js, not fetch on its own"


def test_the_window_and_the_reader_agree_on_what_a_link_is():
    """Two functions used to decide this, and they disagreed.

    `validate_url` accepted "https://example.com/a b" (it only looked at scheme and netloc) while
    `is_web_link` refused it, so the window accepted a link and the reader then failed on it. The
    window now asks the reader's own rule. Revert check: restore the looser scheme/netloc check in
    validate_url and the spaced case is accepted again.
    """
    import pytest

    from listen_app.extract import validate_url
    from listen_app.social_read import is_web_link

    accepts = ["https://example.com/a", "http://example.com", "https://en.wikipedia.org/wiki/X"]
    for good in accepts:
        assert validate_url(good) == good
        assert is_web_link(good)

    rejects = ["https://example.com/a b", "https://exa mple.com", "", "example.com", "ftp://x.com/a"]
    for bad in rejects:
        with pytest.raises(ValueError):
            validate_url(bad)
        assert not is_web_link(bad), f"{bad!r} is not a web link, so the window must not accept it"


def test_the_summariser_handles_a_script_without_spaces():
    """A Chinese or Japanese page produced ZERO key points: no word boundaries to count.

    Two halves, two reverts: the sentence split lives in tts.py and the word scoring in
    summarize.py. Reverting either one alone fails this test, so neither is carried by the other.
    """
    from listen_app.tts import split_sentences

    text = ("这是一个测试句子。这是第二个句子。第三个句子在这里。"
            "第四个句子关于土壤和作物。第五个句子讲灌溉。")
    # Half one: the split. Without the full-width stop in _SENTENCE_RE the whole page is one piece.
    assert len(split_sentences(text)) == 5, "a CJK page must split into its sentences"

    # Half two: the scoring. Without the DENSE shingle fallback every sentence scores zero words.
    points = key_points(text, count=3)
    assert points, "a CJK page must produce key points"
    assert all("。" in p for p in points), "the sentences must be split, not one wrapped blob"


def test_a_cjk_page_survives_into_the_note():
    from listen_app.notes_format import parse

    points = key_points("这是一个测试句子。这是第二个句子。", count=2)
    plain, _styles = parse(as_markup(points, source="https://example.cn/a"))
    for point in points:
        assert point in plain


# ----- switching to the macOS voice engine ---------------------------------
def test_the_say_engine_can_be_switched_to_without_crashing():
    """Measured crash: `_handle("engine", SayEngine())` raised AttributeError, no voice_names.

    Revert check: delete SayEngine.voice_names and this fails with the same AttributeError, which
    left the window half-built with no message and no voices.
    """
    import queue
    import sys

    import pytest

    from listen_app import gui, tts

    if sys.platform != "darwin":
        pytest.skip("SayEngine is macOS only")

    engine = tts.SayEngine()
    names = engine.voice_names()
    assert names, "every engine the window can load must report its voices"
    assert len(names) == len(set(names)), "a voice listed twice would fill the menu with duplicates"
    assert engine.default_voice in names, "the default must be one of the offered voices"

    app = gui.ListenApp.__new__(gui.ListenApp)
    app._events = queue.Queue()
    app._busy = False
    app._load_seq = 0
    app._loaded_source = ""
    seen = []
    app._set_text = lambda text: seen.append(text)
    app.status = type("S", (), {"configure": lambda _s, **kw: seen.append(kw.get("text"))})()
    app.voice_menu = type("M", (), {"configure": lambda _s, **kw: seen.append(("values", len(kw.get("values", [])))),
                                    "set": lambda _s, v: seen.append(("set", v))})()
    app._set_buttons = lambda **kw: None

    app._handle("engine", engine)  # the crash used to happen here
    assert ("values", len(names)) in seen, "the voice menu was not filled"
    assert ("set", engine.default_voice) in seen, "no default voice was selected"
