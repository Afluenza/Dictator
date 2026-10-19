"""bridge: a loopback-only door with a token, and two payload kinds and no others.

These talk to a real listening socket on 127.0.0.1 — the same code path the extension uses —
so the gates are proven by traffic, not by reading the source. Nothing here needs a display.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from listen_app import bridge


@pytest.fixture
def pair():
    """A live bridge on an ephemeral port, with the payloads it received."""
    received: list[dict] = []
    port = _free_port()
    live = bridge.Bridge(received.append, token="a" * 48, port=port)
    assert live.start(), live.error
    try:
        yield live, received, port
    finally:
        live.stop()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def post(port: int, payload, token="a" * 48, headers=None, path="/ingest", raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", bridge.TOKEN_HEADER: token, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)


# ----- it delivers ----------------------------------------------------------
def test_bridged_text_reaches_the_app(pair):
    live, received, port = pair
    status, body, _ = post(port, {"kind": "text", "text": "A post body.", "url": "https://example.com/a",
                                  "title": "A post"})
    assert status == 200 and body["ok"] is True and body["chars"] == len("A post body.")
    assert received == [{"kind": "text", "text": "A post body.", "url": "https://example.com/a",
                         "title": "A post"}]


def test_bridged_link_is_delivered_as_a_link_not_fetched(pair):
    live, received, port = pair
    status, body, _ = post(port, {"kind": "url", "url": "https://en.wikipedia.org/wiki/Speech_synthesis"})
    assert status == 200 and body["kind"] == "url"
    assert received[0] == {"kind": "url", "url": "https://en.wikipedia.org/wiki/Speech_synthesis",
                           "title": ""}


def test_ping_answers_so_the_extension_can_show_connected(pair):
    live, _received, port = pair
    with urllib.request.urlopen(f"{live.endpoint}/ping", timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read())["app"] == "dictator"


# ----- the token is the door -------------------------------------------------
def test_a_wrong_token_is_refused_and_nothing_is_delivered(pair):
    live, received, port = pair
    status, body, _ = post(port, {"kind": "text", "text": "nope"}, token="b" * 48)
    assert status == 401
    assert received == []


def test_a_missing_token_is_refused(pair):
    live, received, port = pair
    request = urllib.request.Request(f"http://127.0.0.1:{port}/ingest", data=b'{"kind":"text","text":"x"}',
                                     method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 401
    assert received == []


def test_the_token_is_compared_in_constant_time():
    """A length-or-prefix shortcut would leak the code one character at a time."""
    import inspect

    source = inspect.getsource(bridge.make_handler)
    assert "hmac.compare_digest" in source


# ----- the local-only gate ---------------------------------------------------
def test_a_non_local_peer_is_refused(monkeypatch, pair):
    live, received, port = pair
    monkeypatch.setattr(bridge, "is_local_peer", lambda address: False)
    status, body, _ = post(port, {"kind": "text", "text": "nope"})
    assert status == 403 and body["error"] == "local only"
    assert received == []


def test_a_host_header_that_is_not_loopback_is_refused(pair):
    """A page that rebinds DNS arrives with its own hostname. The Host header is the tell."""
    live, received, port = pair
    status, body, _ = post(port, {"kind": "text", "text": "nope"}, headers={"Host": "evil.example"})
    assert status == 403
    assert received == []


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "127.0.0.1"])
def test_loopback_host_headers_are_accepted(host):
    assert bridge.host_is_loopback(host)


def test_it_binds_the_loopback_address_only(pair):
    live, _received, port = pair
    assert live._server.server_address[0] == "127.0.0.1"
    assert bridge.HOST == "127.0.0.1"


# ----- only two kinds of payload --------------------------------------------
@pytest.mark.parametrize("payload", [
    {"kind": "file", "path": "/etc/passwd"},
    {"kind": "text"},
    {"kind": "text", "text": ""},
    {"kind": "text", "text": 42},
    {"kind": "url", "url": "file:///etc/passwd"},
    {"kind": "url", "url": "javascript:alert(1)"},
    {"kind": "url", "url": "not a link"},
    {"kind": "url"},
    {"text": "no kind"},
    ["a list"],
])
def test_anything_but_text_or_a_web_link_is_refused(pair, payload):
    live, received, port = pair
    status, body, _ = post(port, payload)
    assert status == 400, body
    assert received == []


def test_a_body_that_is_not_json_is_refused(pair):
    live, received, port = pair
    status, _body, _ = post(port, None, raw=b"<html>hello</html>")
    assert status == 400
    assert received == []


def test_an_oversized_text_is_refused_not_truncated(pair):
    live, received, port = pair
    status, body, _ = post(port, {"kind": "text", "text": "x" * (bridge.MAX_TEXT + 1)})
    assert status == 400 and "longer than" in body["error"]
    assert received == []


def test_a_form_post_is_refused_before_it_is_read(pair):
    """A plain web page can POST cross-origin without CORS; it only gets here with the token,
    and the JSON content type is a second gate."""
    live, received, port = pair
    status, _body, _ = post(port, {"kind": "text", "text": "x"}, headers={"Content-Type": "text/plain"})
    assert status == 415
    assert received == []


def test_an_unknown_path_is_a_404(pair):
    live, received, port = pair
    status, _body, _ = post(port, {"kind": "text", "text": "x"}, path="/shell")
    assert status == 404
    assert received == []


# ----- CORS ----------------------------------------------------------------
def test_a_web_page_gets_no_cors_grant_but_an_extension_does(pair, monkeypatch):
    live, _received, port = pair
    _status, _body, headers = post(port, {"kind": "text", "text": "x"},
                                   headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in headers

    _status, _body, headers = post(port, {"kind": "text", "text": "x"},
                                   headers={"Origin": "chrome-extension://abcdefghijklmnop"})
    assert headers.get("Access-Control-Allow-Origin") == "chrome-extension://abcdefghijklmnop"


def test_preflight_is_answered_for_an_extension(pair):
    live, _received, port = pair
    request = urllib.request.Request(f"http://127.0.0.1:{port}/ingest", method="OPTIONS", headers={
        "Origin": "chrome-extension://abcdefghijklmnop",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": bridge.TOKEN_HEADER,
    })
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 204
        assert bridge.TOKEN_HEADER in response.headers["Access-Control-Allow-Headers"]


# ----- pairing -------------------------------------------------------------
def test_the_saved_token_is_reused_across_launches(tmp_path):
    config = tmp_path / "bridge.json"
    first = bridge.load_token(config)
    assert len(first) == 48
    assert bridge.load_token(config) == first
    assert oct(config.stat().st_mode)[-3:] == "600"


def test_a_missing_or_broken_config_gets_a_fresh_token(tmp_path):
    config = tmp_path / "bridge.json"
    config.write_text("{not json", encoding="utf-8")
    assert len(bridge.load_token(config)) == 48
    config.write_text(json.dumps({"token": "short"}), encoding="utf-8")
    assert len(bridge.load_token(config)) == 48


def test_the_code_is_grouped_but_compared_ungrouped():
    live = bridge.Bridge(lambda payload: None, token="abcdef0123456789" * 3, port=0)
    assert live.code.split(" ")[0] == "abcd"
    assert bridge.ungroup_code(live.code) == live.token


def test_a_taken_port_reports_instead_of_crashing():
    taken = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", taken))
    blocker.listen(1)
    try:
        live = bridge.Bridge(lambda payload: None, token="c" * 48, port=taken)
        assert live.start() is False
        assert "unavailable" in live.error
        assert live.running is False
    finally:
        blocker.close()


def test_stop_releases_the_port(pair):
    live, _received, port = pair
    live.stop()
    assert live.running is False
    again = bridge.Bridge(lambda payload: None, token="d" * 48, port=port)
    assert again.start(), again.error
    again.stop()


def test_concurrent_posts_are_all_delivered_once():
    """The extension retries; the bridge must not drop or duplicate a burst."""
    received: list[dict] = []
    lock = threading.Lock()

    def collect(payload):
        with lock:
            received.append(payload)

    port = _free_port()
    live = bridge.Bridge(collect, token="e" * 48, port=port)
    assert live.start(), live.error
    statuses: list[int] = []
    try:
        threads = [threading.Thread(target=lambda i=i: statuses.append(
            post(port, {"kind": "text", "text": f"note {i}"}, token="e" * 48)[0])) for i in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        live.stop()
    assert statuses == [200] * 24
    assert sorted(p["text"] for p in received) == sorted(f"note {i}" for i in range(24))
