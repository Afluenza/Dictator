"""Local bridge: one 127.0.0.1 endpoint, so the browser extension can hand a page to this app.

The extension cannot touch the filesystem, so text has to cross the loopback interface. That is
the entire surface of this module, and it is deliberately narrow:

- it binds 127.0.0.1 only, so nothing on the network can reach it;
- it refuses any peer that is not the local machine, and any Host header that is not loopback
  (a DNS-rebinding page would otherwise arrive carrying its own hostname);
- it requires a token that only this machine holds, compared in constant time;
- it accepts exactly two payload kinds, `text` and `url`. No file paths, no headers, no
  commands, no cookies, nothing the app then re-sends anywhere;
- a link is not fetched here: it is handed to the window, which runs it through the same
  listen_app.social_read route as Paste Link (free anonymous routes only, login-only hosts
  refused before any request).

Nothing is logged; request lines would end up in a log with the payload's URL in them.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from listen_app.social_read import is_web_link

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 200_000  # a payload, not a file: the 60k text cap plus its URL and title
MAX_TEXT = 60_000  # matches social_read.MAX_CHARS, so a bridged page is no larger than a fetched one
MAX_URL = 2_048
TOKEN_HEADER = "X-Dictator-Token"
TOKEN_BYTES = 24  # 48 hex characters
LOCAL_PEERS = {"127.0.0.1", "::1"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
EXTENSION_SCHEMES = ("chrome-extension://", "moz-extension://", "safari-web-extension://")
CONFIG_PATH = Path(os.environ.get("DICTATOR_BRIDGE_CONFIG",
                                  Path.home() / ".listen-app" / "bridge.json"))


class Rejected(Exception):
    """The payload is not one the app will load. The message is safe to show the user."""


# ----- pairing --------------------------------------------------------------
def load_token(path: Path | str = CONFIG_PATH) -> str:
    """The saved pairing code, or a fresh one written with owner-only permissions.

    It is reused across launches so the extension is paired once, not every time the app opens.
    A token that cannot be saved still works for this session; the extension then needs pairing
    again next launch, which is honest rather than silent.
    """
    path = Path(path)
    try:
        saved = json.loads(path.read_text(encoding="utf-8")).get("token")
        if isinstance(saved, str) and len(saved) >= 32:
            return saved
    except (OSError, ValueError, AttributeError):
        pass
    token = secrets.token_hex(TOKEN_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"token": token}), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass  # a read-only home directory only costs a re-pair next launch
    return token


# ----- payload --------------------------------------------------------------
def _clean_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:300]


def _clean_url(value: object) -> str:
    """The page the text came from, if the sender named one that is a real web link."""
    if isinstance(value, str) and len(value) <= MAX_URL and is_web_link(value):
        return value
    return ""


def parse_payload(raw: bytes) -> dict:
    """Validate one payload. Anything the window cannot load is refused here, with a plain reason."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected("body must be JSON")
    if not isinstance(data, dict):
        raise Rejected("body must be a JSON object")
    kind = data.get("kind")
    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise Rejected("text must be a non-empty string")
        if len(text) > MAX_TEXT:
            raise Rejected(f"text is longer than {MAX_TEXT} characters")
        return {"kind": "text", "text": text, "url": _clean_url(data.get("url")),
                "title": _clean_title(data.get("title"))}
    if kind == "url":
        url = data.get("url")
        if not isinstance(url, str) or len(url) > MAX_URL or not is_web_link(url):
            raise Rejected("url must be an http(s) link")
        return {"kind": "url", "url": url, "title": _clean_title(data.get("title"))}
    raise Rejected('kind must be "text" or "url"')


def is_local_peer(address: str) -> bool:
    return address in LOCAL_PEERS


def host_is_loopback(host_header: str) -> bool:
    """`127.0.0.1:8765` or `localhost:8765` only. A rebound DNS name is refused."""
    host = (host_header or "").strip().rsplit(":", 1)[0].strip("[]").lower()
    return host in LOCAL_HOSTS


# ----- server ---------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    """Threaded, loopback-only, with a listen backlog that survives a burst.

    socketserver's default backlog is 5 and it is applied during __init__, so setting it on the
    instance afterwards is too late: a burst of scripted posts lost 17 of 24 connections with
    "connection reset by peer" before any handler ran. Setting it on the class fixes the listen()
    call that actually happens.
    """

    daemon_threads = True
    request_queue_size = 64
    # On Windows SO_REUSEADDR lets a second process bind a port already in use (port hijack),
    # so it is set only where it is the safe restart option.
    allow_reuse_address = os.name != "nt"


def make_handler(token: str, on_payload: Callable[[dict], None]):
    """A request handler bound to one token and one delivery callback."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "DictatorBridge/1"
        sys_version = ""

        # ----- housekeeping
        def log_message(self, *args, **kwargs) -> None:  # no request lines, so no payload URL in a log
            return

        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(payload)

        def _cors(self) -> None:
            """Only a browser extension may read the reply. A web page gets no CORS grant at all."""
            origin = self.headers.get("Origin", "") or ""
            if origin.startswith(EXTENSION_SCHEMES):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        # ----- gates
        def _allowed(self) -> bool:
            if not is_local_peer(self.client_address[0]):
                self._send(403, {"ok": False, "error": "local only"})
                return False
            if not host_is_loopback(self.headers.get("Host", "")):
                self._send(403, {"ok": False, "error": "local only"})
                return False
            return True

        def _authorised(self) -> bool:
            sent = self.headers.get(TOKEN_HEADER, "") or ""
            if not hmac.compare_digest(sent, token):
                self._send(401, {"ok": False, "error": "not paired: paste the code from the app"})
                return False
            return True

        # ----- routes
        def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming, not ours
            if not self._allowed():
                return
            origin = self.headers.get("Origin", "") or ""
            self.send_response(204)
            if origin.startswith(EXTENSION_SCHEMES):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            if self.path.split("?")[0] != "/ping":
                self._send(404, {"ok": False, "error": "no such endpoint"})
                return
            self._send(200, {"ok": True, "app": "dictator"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            if self.path.split("?")[0] != "/ingest":
                self._send(404, {"ok": False, "error": "no such endpoint"})
                return
            if not self._authorised():
                return
            mime = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            if mime != "application/json":
                self._send(415, {"ok": False, "error": "Content-Type must be application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"ok": False, "error": "bad Content-Length"})
                return
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"ok": False, "error": f"body must be 1 to {MAX_BODY} bytes"})
                return
            try:
                payload = parse_payload(self.rfile.read(length))
            except Rejected as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            try:
                on_payload(payload)
            except Exception as exc:  # a broken receiver is reported, never a stack trace
                self._send(500, {"ok": False, "error": type(exc).__name__})
                return
            self._send(200, {"ok": True, "kind": payload["kind"],
                             "chars": len(payload.get("text") or payload.get("url") or "")})

    return Handler


class Bridge:
    """The listening end. `on_payload` runs on a request thread, so it must be thread-safe."""

    def __init__(self, on_payload: Callable[[dict], None], token: str | None = None,
                 port: int = DEFAULT_PORT) -> None:
        self.token = token or load_token()
        self.port = port
        self._on_payload = on_payload
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.error = ""

    def start(self) -> bool:
        """Bind the port. False (with .error set) if it is taken, so the window can say why."""
        if self._server is not None:
            return True
        try:
            server = _Server((HOST, self.port), make_handler(self.token, self._on_payload))
        except OSError as exc:
            self.error = f"port {self.port} is unavailable: {exc.strerror or exc}"
            return False
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="dictator-bridge", daemon=True)
        self._thread.start()
        self.error = ""
        return True

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def endpoint(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def code(self) -> str:
        """The pairing code as the extension wants it: grouped in fours, easy to read and type."""
        return " ".join(self.token[i:i + 4] for i in range(0, len(self.token), 4))


def ungroup_code(code: str) -> str:
    """A pasted code may carry the spaces the app showed. Compare on the bare token."""
    return "".join((code or "").split()).lower()
