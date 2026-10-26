"""social_read: the four honest states, one per route. Fixtures only — no live network here.

The fixtures in tests/fixtures/social are real replies captured from each route on 2026-10-18,
trimmed in size but not rewritten: the Instagram sign-in page as Jina returned it, the X abuse
error body, the YouTube oEmbed JSON, the GitHub repo, readme and issue payloads, a V2EX topic
and hot list, and a real RSS feed.
"""

import os
from pathlib import Path

import pytest

from listen_app.social_read import (
    BLOCKED,
    EMPTY,
    LOGIN_WALL,
    NEEDS_APPROVAL,
    OK,
    classify,
    http_get,
    parse_feed,
    read,
    read_text,
)

FIX = Path(__file__).parent / "fixtures" / "social"


def body_of(name: str) -> bytes:
    return FIX.joinpath(name).read_bytes()


def serves(mapping: dict, default=(200, "text/html", b"")):
    """A transport that answers by the LONGEST matching URL prefix, so routes cannot shadow each other."""
    prefixes = sorted(mapping, key=len, reverse=True)

    def transport(url, headers=None, timeout=None):
        for prefix in prefixes:
            if url.startswith(prefix):
                return mapping[prefix]
        return default
    return transport


def always(reply):
    return lambda url, headers=None, timeout=None: reply


def test_malformed_link_is_refused_before_any_request():
    """A link with no host or a non-web scheme is invalid input, not a blocked site, and no route runs."""
    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        return 200, "text/html", b"<html></html>"

    for bad in ["https://", "ftp://x", "not a url", ""]:
        result = read(bad, transport=transport)
        assert result.state == EMPTY, bad
        assert result.text == ""
        assert "not a valid web link" in result.note, bad
    assert calls == []


# ----- classification -------------------------------------------------------
@pytest.mark.parametrize("url,kind", [
    ("https://example.com/post", "web"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://github.com/Panniantong/Agent-Reach", "github"),
    ("https://www.v2ex.com/t/1249800", "v2ex"),
    ("https://hnrss.org/frontpage", "rss"),
    ("https://blog.example.com/feed.xml", "rss"),
    ("https://www.reddit.com/r/python/", "login-only"),
    ("https://x.com/elonmusk", "login-only"),
    ("https://www.instagram.com/instagram/", "login-only"),
    ("https://www.bilibili.com/video/BV1GJ411x7h7", "login-only"),
])
def test_classify_picks_one_kind_per_host(url, kind):
    assert classify(url) == kind


# ----- login-only platforms: nothing is fetched at all ----------------------
@pytest.mark.parametrize("url,label", [
    ("https://www.reddit.com/r/python/", "Reddit"),
    ("https://x.com/elonmusk", "X"),
    ("https://www.instagram.com/instagram/", "Instagram"),
    ("https://www.bilibili.com/video/BV1GJ411x7h7", "Bilibili"),
    ("https://www.facebook.com/someone", "Facebook"),
    ("https://www.linkedin.com/in/someone", "LinkedIn"),
])
def test_login_only_platforms_need_approval_and_make_no_request(url, label):
    calls = []

    def transport(u, headers=None, timeout=None):
        calls.append(u)
        return 200, "text/plain", b"should never be fetched"

    result = read(url, transport=transport)
    assert result.state == NEEDS_APPROVAL
    assert label in result.failure_message()
    assert result.text == ""
    assert calls == []  # no cookie, no account header, no request of any kind


def test_login_only_read_text_raises_extract_error():
    from listen_app.extract import ExtractError

    with pytest.raises(ExtractError, match="needs your approval"):
        read_text("https://www.reddit.com/r/python/", transport=always((200, "", b"")))


# ----- state: ok ------------------------------------------------------------
def test_web_page_reads_the_captured_jina_reply():
    page = (200, "text/plain", body_of("jina_page.txt"))
    result = read("https://en.wikipedia.org/wiki/Text-to-speech",
                  transport=serves({"https://r.jina.ai/": page}, default=(404, "", b"nope")))
    assert result.state == OK
    assert result.route == "Jina Reader"
    assert result.title == "Speech synthesis"
    assert "artificial production of human" in result.text


def test_web_page_prefers_a_direct_read_over_jina():
    html = ("<html><body><article><p>" + "Direct body sentence with enough words to count. " * 4 +
            "</p></article></body></html>").encode()
    calls = []

    def transport(url, headers=None, timeout=None):
        calls.append(url)
        return 200, "text/html", html

    result = read("https://example.com/article", transport=transport)
    assert result.state == OK
    assert result.route == "direct + trafilatura"
    assert calls == ["https://example.com/article"]  # Jina is not called once direct text came back


@pytest.fixture
def no_live_socket(monkeypatch):
    """Any real connection from a fixture test fails here, instead of depending on the machine's network.

    The IP-block cooldown is module state, so it is cleared too: a block recorded by one test must not
    make the next fixture read report "cooling down" instead of the transcript it was given.
    """
    import socket

    import listen_app.transcript as tr

    tr.reset_cooldown()
    attempts = []

    def refuse(self, address, *args, **kwargs):
        attempts.append(address)
        raise OSError("fixture test attempted a live connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    yield attempts
    tr.reset_cooldown()
    assert attempts == [], "a fixture test reached the network"


def _youtube_transport(**extra):
    mapping = {
        "https://www.youtube.com/oembed": (200, "application/json", body_of("oembed.json")),
        "https://www.youtube.com/watch?v=": (200, "text/html", body_of("youtube_watch_page.html")),
        "https://r.jina.ai/": (403, "application/json", body_of("jina_blocked.json")),
    }
    mapping.update(extra)
    return serves(mapping)


class _Track:
    def __init__(self, language, generated, *lines):
        self.language, self.generated, self.snippets = language, generated, list(lines)


class _Source:
    def __init__(self, tracks):
        self._tracks = tracks

    def tracks(self, video_id):
        return self._tracks


def _use_captions(monkeypatch, tracks):
    import listen_app.transcript as tr

    monkeypatch.setattr(tr, "YouTubeCaptions", lambda: _Source(tracks))


def test_youtube_without_a_transcript_is_an_honest_empty_state_not_the_description(monkeypatch, no_live_socket):
    """The description is never presented as the video's content. With no transcript, the read says so."""
    _use_captions(monkeypatch, [])
    result = read("https://www.youtube.com/watch?v=dQw4w9WgXcQ", transport=_youtube_transport())
    assert result.state == EMPTY
    assert result.text == ""
    assert "Never Gonna Give You Up" not in result.text
    assert "no caption tracks" in result.note
    assert result.meta["transcript_state"] == "unavailable"


def test_youtube_full_transcript_is_the_content_with_title_and_metadata(monkeypatch, no_live_socket):
    _use_captions(monkeypatch, [_Track("en", False, "Never gonna give you up.", "Never gonna let you down.")])
    result = read("https://www.youtube.com/watch?v=dQw4w9WgXcQ", transport=_youtube_transport())
    assert result.state == OK
    assert "Never gonna give you up. Never gonna let you down." in result.text
    assert result.meta["transcript_language"] == "en"
    assert result.meta["transcript_generated"] is False
    assert result.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_fallback_language_is_disclosed_in_the_result(monkeypatch, no_live_socket):
    _use_captions(monkeypatch, [_Track("ja", True, "こんにちは")])
    result = read("https://www.youtube.com/watch?v=dQw4w9WgXcQ", transport=_youtube_transport())
    assert result.state == OK
    assert result.meta["transcript_fallback"] is True
    assert "fallback" in result.note


def test_youtube_retrieval_failure_is_blocked_not_empty(monkeypatch, no_live_socket):
    import listen_app.transcript as tr

    class Blocked:
        def tracks(self, video_id):
            raise type("IpBlocked", (Exception,), {})("blocked")

    monkeypatch.setattr(tr, "YouTubeCaptions", Blocked)
    result = read("https://www.youtube.com/watch?v=dQw4w9WgXcQ", transport=_youtube_transport())
    assert result.state == BLOCKED
    assert result.text == ""
    assert "IpBlocked" in result.note


def test_fixture_reads_never_open_a_socket_to_youtube(monkeypatch, no_live_socket):
    """Regression (R33): a fixture read must not reach YouTube through the caption library either."""
    _use_captions(monkeypatch, [_Track("en", False, "fixture words only")])
    assert read("https://www.youtube.com/watch?v=dQw4w9WgXcQ", transport=_youtube_transport()).state == OK


@pytest.mark.skipif(not os.environ.get("LISTEN_APP_LIVE_NETWORK"),
                    reason="live YouTube check is opt-in: set LISTEN_APP_LIVE_NETWORK=1")
def test_live_youtube_transcript_route_opt_in():
    """Explicit live check. Not run by default and not run in CI: it reaches YouTube for real."""
    import listen_app.transcript as tr

    result = tr.fetch_transcript("dQw4w9WgXcQ", "en")
    assert result.state in (tr.OK, tr.UNAVAILABLE, tr.FAILED)


def test_youtube_short_link_is_resolved_to_the_same_video(monkeypatch, no_live_socket):
    _use_captions(monkeypatch, [_Track("en", False, "the same video, read through the short link")])
    result = read("https://youtu.be/dQw4w9WgXcQ", transport=_youtube_transport())
    assert result.state == OK
    assert "the same video" in result.text


def test_github_repo_reads_metadata_and_readme():
    transport = serves({
        "https://api.github.com/repos/Panniantong/Agent-Reach/readme": (200, "text/plain",
                                                                       body_of("github_readme.md")),
        "https://api.github.com/repos/Panniantong/Agent-Reach": (200, "application/json",
                                                                 body_of("github_repo.json")),
    })
    result = read("https://github.com/Panniantong/Agent-Reach", transport=transport)
    assert result.state == OK
    assert result.title == "Panniantong/Agent-Reach"
    assert "Agent Reach" in result.text
    assert result.route == "GitHub API + README"


def test_github_issue_reads_the_issue_body():
    transport = serves({
        "https://api.github.com/repos/Panniantong/Agent-Reach/issues/1": (200, "application/json",
                                                                          body_of("github_issue.json")),
        "https://api.github.com/repos/Panniantong/Agent-Reach": (200, "application/json",
                                                                 body_of("github_repo.json")),
    })
    result = read("https://github.com/Panniantong/Agent-Reach/issues/1", transport=transport)
    assert result.state == OK
    assert "hope get access to linkedin" in result.title
    assert "official APIs are not supported anymore" in result.text
    assert result.route == "GitHub issues API"


def test_v2ex_topic_reads_the_post():
    transport = serves({"https://www.v2ex.com/api/topics/show.json": (200, "application/json",
                                                                      body_of("v2ex_show.json"))})
    result = read("https://www.v2ex.com/t/1249800", transport=transport)
    assert result.state == OK
    assert result.title
    assert "Replies:" in result.text


def test_v2ex_hot_list_reads_as_a_list():
    transport = serves({"https://www.v2ex.com/api/topics/hot.json": (200, "application/json",
                                                                     body_of("v2ex_hot.json"))})
    result = read("https://www.v2ex.com/", transport=transport)
    assert result.state == OK
    assert result.text.startswith("V2EX hot topics")
    assert result.text.count("\n- ") >= 2


def test_rss_feed_is_parsed_without_an_extra_package():
    transport = serves({"https://hnrss.org/frontpage": (200, "application/rss+xml", body_of("feed.xml"))})
    result = read("https://hnrss.org/frontpage", transport=transport)
    assert result.state == OK
    assert result.text.count("\n- ") >= 3


# ----- state: blocked -------------------------------------------------------
def test_blocked_page_reports_blocked_and_never_carries_a_summary():
    transport = serves({"https://r.jina.ai/": (403, "application/json", body_of("jina_blocked.json"))},
                       default=(403, "text/plain", b"forbidden"))
    result = read("https://example.com/blocked-post", transport=transport)
    assert result.state == BLOCKED
    assert "AbuseAlleviationError" in result.title
    assert result.text == ""
    assert "refused anonymous reads" in result.failure_message()


@pytest.mark.parametrize("status", [401, 403, 412, 429, 451])
def test_every_blocked_status_maps_to_blocked(status):
    result = read("https://example.com/a", transport=always((status, "text/plain", b"nope")))
    assert result.state == BLOCKED


def test_unreachable_host_is_empty_not_blocked():
    result = read("https://example.com/a", transport=always((0, "", b"timed out")))
    assert result.state == EMPTY
    assert "No readable text at" in result.failure_message()
    assert "timed out" in result.failure_message()  # the reason travels with the message


# ----- state: login-wall ----------------------------------------------------
def test_a_sign_in_page_for_an_unlisted_host_is_a_wall_not_content():
    wall = serves({"https://r.jina.ai/": (200, "text/plain", body_of("jina_login_wall.txt"))},
                  default=(200, "text/html", b"<html><body>tiny</body></html>"))
    result = read("https://example.com/login", transport=wall)
    assert result.state == LOGIN_WALL
    assert result.text == ""
    assert "sign-in page" in result.failure_message()


def test_instagram_stops_before_any_route_is_tried():
    wall = serves({"https://r.jina.ai/": (200, "text/plain", body_of("jina_login_wall.txt"))})
    result = read("https://www.instagram.com/instagram/", transport=wall)
    assert result.state == NEEDS_APPROVAL
    assert result.route == "none (login-only platform)"


def test_a_long_page_that_mentions_logging_in_is_not_a_wall():
    html = ("<html><body><article><p>" + "Real article sentence about the topic. " * 90 +
            "Log in to comment below.</p></article></body></html>").encode()
    result = read("https://example.com/long", transport=always((200, "text/html", html)))
    assert result.state == OK
    assert "Real article sentence" in result.text


# ----- state: empty ---------------------------------------------------------
def test_a_200_with_no_text_is_empty():
    result = read("https://example.com/", transport=always((200, "text/html", b"<html><body></body></html>")))
    assert result.state == EMPTY


def test_feed_that_is_not_xml_is_empty():
    parsed = parse_feed("<html>not a feed</html>", "https://example.com/feed.xml")
    assert parsed.state == EMPTY
    assert parsed.text == ""


def test_empty_feed_is_empty():
    parsed = parse_feed('<?xml version="1.0"?><rss><channel><title>t</title></channel></rss>')
    assert parsed.state == EMPTY
    assert parsed.note == "feed has no items"


# ----- http_get contract ----------------------------------------------------
def test_http_get_returns_the_status_instead_of_raising(monkeypatch):
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert http_get("https://example.com")[0] == 403


def test_read_never_raises_on_a_broken_route():
    result = read("https://example.com/a", transport=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert result.state == EMPTY


# ----- hand-off to the summariser -------------------------------------------
def test_ok_text_is_exactly_what_the_summariser_receives():
    page = (200, "text/plain", body_of("jina_page.txt"))
    text = read_text("https://en.wikipedia.org/wiki/Text-to-speech",
                     transport=serves({"https://r.jina.ai/": page}, default=(404, "", b"nope")))
    from listen_app.summarize import key_points

    assert key_points(text)


# ----- a missing page is a missing page -------------------------------------
# A 404 page is not an article. Wikipedia answers a missing article with HTTP 404 and a
# "does not have an article with this exact name" page, which trafilatura extracts as real
# text, so the old code graded a missing page `ok` and let the summariser write key points
# from the help text. Fixtures are the real replies captured on 2026-10-18.
def test_a_404_page_is_empty_not_ok():
    """The status alone settles it: a 404 is not content, whatever text the error page carries."""
    result = read("https://en.wikipedia.org/wiki/Kokoro_(TTS)",
                  transport=serves({"https://en.wikipedia.org/wiki/Kokoro_(TTS)":
                                    (404, "text/html", body_of("soft404_wikipedia.html"))},
                                   default=(404, "", b"")))
    assert result.state == EMPTY
    assert result.text == ""


def test_a_404_note_names_the_status():
    result = read("https://example.com/gone",
                  transport=serves({"https://example.com/gone": (404, "text/html", b"<html>x</html>")},
                                   default=(404, "", b"")))
    assert result.state == EMPTY
    assert "404" in result.note


def test_a_soft_404_is_empty_even_when_the_status_is_200():
    """Some sites answer a missing page with HTTP 200 and a "does not exist" body."""
    html = ("<html><head><title>Missing page</title></head><body><article><p>"
            "We could not find the page you asked for. Check the spelling of the address and "
            "try the search box instead of guessing the address again.</p></article></body></html>"
            ).encode()
    result = read("https://example.com/missing",
                  transport=serves({"https://example.com/missing": (200, "text/html", html)},
                                   default=(404, "", b"")))
    assert result.state == EMPTY
    assert result.text == ""


def test_jina_404_warning_is_not_read_as_ok():
    """Jina answers HTTP 200 and puts "Target URL returned error 404" in its own header block."""
    page = (200, "text/plain", body_of("jina_soft404.txt"))
    result = read("https://en.wikipedia.org/wiki/Kokoro_(TTS)",
                  transport=serves({"https://r.jina.ai/": page}, default=(404, "", b"nope")))
    assert result.state == EMPTY
    assert result.text == ""


def test_a_refused_jina_does_not_turn_a_404_into_blocked():
    """Direct says the page is missing (404); Jina then refuses anonymous queries (401).
    The honest answer is that the page is missing, not that the site blocked us."""
    jina_refused = (401, "application/json",
                    b'{"data":null,"code":401,"name":"AuthenticationRequiredError",'
                    b'"readableMessage":"blocked from anonymous queries"}')
    result = read("https://en.wikipedia.org/wiki/Kokoro_(TTS)",
                  transport=serves({"https://en.wikipedia.org/wiki/Kokoro_(TTS)":
                                    (404, "text/html", body_of("soft404_wikipedia.html")),
                                    "https://r.jina.ai/": jina_refused},
                                   default=(404, "", b"")))
    assert result.state == EMPTY
    assert "404" in result.note


def test_a_real_wikipedia_article_still_reads():
    """The guard above must not eat real articles."""
    html = ("<html><head><title>Speech synthesis - Wikipedia</title></head><body><article><p>"
            "Speech synthesis is the artificial production of human speech. A computer system "
            "used for this purpose is called a speech synthesizer, and can be implemented in "
            "software or hardware products.</p></article></body></html>").encode()
    result = read("https://en.wikipedia.org/wiki/Text-to-speech",
                  transport=serves({"https://en.wikipedia.org/wiki/Text-to-speech": (200, "text/html", html)},
                                   default=(404, "", b"")))
    assert result.state == OK
    assert "artificial production of human speech" in result.text


# ----- an error status is an error page, whatever the body says ---------------
# The 404 guard above only covered {404, 410}. A 503 outage page, a 400 and a 418 answer with a
# long, wordy body too, and trafilatura extracts all of them, so those error pages were graded
# `ok` and would have been summarised into a note as the post.
ERROR_PAGE = ("<html><head><title>503 Service Unavailable</title></head><body><h1>Service "
              "Unavailable</h1><p>The server is temporarily unable to service your request due to "
              "maintenance downtime or capacity problems. Please try again later, and if the "
              "problem continues contact the site administrator with the reference number shown "
              "at the bottom of this page.</p></body></html>").encode()


@pytest.mark.parametrize("status", [400, 402, 418, 500, 502, 503, 504])
def test_an_error_status_is_empty_not_ok(status):
    result = read("https://example.com/a", transport=always((status, "text/html", ERROR_PAGE)))
    assert result.state == EMPTY
    assert result.text == ""
    assert str(status) in result.note


@pytest.mark.parametrize("status", [401, 403, 412, 429, 451])
def test_error_floor_does_not_steal_the_blocked_statuses(status):
    """Widening the error guard must not reclassify a refusal as a plain empty."""
    result = read("https://example.com/a", transport=always((status, "text/plain", b"nope")))
    assert result.state == BLOCKED


def test_a_200_page_is_unaffected_by_the_error_floor():
    html = ("<html><head><title>Fine</title></head><body><p>"
            "This page loaded normally and has plenty of ordinary sentences to read. " * 4
            + "</p></body></html>").encode()
    result = read("https://example.com/fine",
                  transport=serves({"https://example.com/fine": (200, "text/html", html)}))
    assert result.state == OK


def test_jina_own_error_status_is_empty():
    """Jina itself answering 5xx is an error page, not the target's text."""
    result = read("https://example.com/a",
                  transport=serves({"https://r.jina.ai/": (503, "text/plain", b"upstream down")},
                                   default=(503, "text/html", ERROR_PAGE)))
    assert result.state == EMPTY
    assert result.text == ""
