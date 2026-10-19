"""Read a social or web post with free, anonymous routes only, then hand the text to the app.

Routes (from rea/agent-reach-research.md, probe-verified 2026-10-18):
- general web page: trafilatura direct, and Jina Reader (https://r.jina.ai/<url>) when that fails
- YouTube: oEmbed for title and author, the watch-page description, and English captions via youtube-transcript-api
- GitHub public: the REST API for repo metadata, a README, or an issue or pull body
- V2EX: the public topics API
- RSS/Atom: parsed with the standard library, no extra package

Login-only platforms (Reddit, X, Bilibili, Instagram, Facebook, Xiaohongshu, LinkedIn) are
reported as needing the Commander's approval, and are not fetched at all. This module never
reads, stores, or sends a cookie, token or account header, and calls no hosted or paid API.

Every read returns exactly one state, and never dresses one up as another:
    ok              real content with the fields a summary needs
    blocked         HTTP 401/403/412/429/451, or the route's own abuse error
    login-wall      HTTP 200 whose body is a sign-in page, not the post
    needs-approval  a login-only platform, so nothing was fetched
    empty           the route answered but carried no usable text
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

try:  # MIT, no key, no account. Missing install means no transcript, never a crash.
    from youtube_transcript_api import (
        IpBlocked,
        NoTranscriptFound,
        TranscriptsDisabled,
        YouTubeTranscriptApi,
    )
except ImportError:  # pragma: no cover - the dependency is pinned in pyproject.toml
    YouTubeTranscriptApi = None
    NoTranscriptFound = TranscriptsDisabled = IpBlocked = Exception

TIMEOUT_S = 25
MAX_CHARS = 60_000  # a post longer than this is trimmed, and the note says so
MIN_CONTENT_CHARS = 40
USER_AGENT = "listen-app/0.1 (+local, anonymous)"

OK = "ok"
BLOCKED = "blocked"
LOGIN_WALL = "login-wall"
NEEDS_APPROVAL = "needs-approval"
EMPTY = "empty"

JINA_PREFIX = "https://r.jina.ai/"

# Login-only platforms: reporting one of these as read is worse than not reading it.
LOGIN_ONLY = {
    "reddit.com": "Reddit",
    "x.com": "X",
    "twitter.com": "X",
    "bilibili.com": "Bilibili",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "xiaohongshu.com": "Xiaohongshu",
    "linkedin.com": "LinkedIn",
}
BLOCKED_STATUS = {401, 403, 412, 429, 451}
LOGIN_MARKERS = ("log into", "log in to", "sign in to", "sign in to continue")
# A missing page is an error page, not a post. Matched on the link-stripped visible text only,
# and only under 600 characters, so a real article that mentions a missing page mid-way
# through is not mistaken for one.
NOT_FOUND_MARKERS = ("page not found", "does not have an article", "could not find the page",
                     "we couldn't find that page", "no such page", "404 not found",
                     "this page is not available", "the page you requested")
NOT_FOUND_MAX_VISIBLE = 600
MISSING_STATUS = {404, 410}
# Every other error status is an error page too. 400, 402, 418 and all 5xx answer with a
# long, wordy body that trafilatura extracts happily, so without this a 503 outage page
# would be summarised into a note as if it were the post.
ERROR_STATUS_FLOOR = 400
FEED_SUFFIXES = (".rss", ".xml", ".atom")
FEED_HOSTS = ("hnrss.org", "feeds.bbci.co.uk")


@dataclass
class ReadResult:
    """One honest answer about one URL. `state` is the verdict; `text` is never invented."""

    state: str
    kind: str
    url: str
    route: str = ""
    title: str = ""
    text: str = ""
    note: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == OK

    def source_line(self) -> str:
        """One line naming where the text came from, for the note the summary lands in."""
        return f"Source: {self.title or self.url} — {self.url} ({self.route})"

    def failure_message(self) -> str:
        """What the reader sees when there is nothing to read. Never a fake success."""
        if self.state == NEEDS_APPROVAL:
            return (f"{self.note} needs your approval: it is login-only, and this app uses no "
                    "cookies or accounts. Nothing was fetched.")
        if self.state == BLOCKED:
            return f"{self.note or self.url} refused anonymous reads ({self.route}). {self.title}".strip()
        if self.state == LOGIN_WALL:
            return f"{self.note} returned a sign-in page, not the post. Nothing to read."
        if self.note:
            # The route may know why there is nothing (a stub title, a feed with no items).
            return f"No readable text at {self.url}: {self.note}"
        return f"No readable text found at {self.url}"


# ----- transport ------------------------------------------------------------
def http_get(url: str, headers: dict | None = None, timeout: float = TIMEOUT_S) -> tuple[int, str, bytes]:
    """One anonymous GET. Returns (status, content-type, body); an error status is returned, not raised."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.headers.get_content_type() if exc.headers else ""), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, "", str(exc).encode("utf-8", "replace")


# ----- classification -------------------------------------------------------
def host_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def classify(url: str) -> str:
    """One of: login-only, youtube, github, v2ex, rss, web."""
    host, path = host_of(url), urlparse(url).path.lower()
    if login_only_label(url):
        return "login-only"
    if host in ("youtube.com", "youtu.be", "m.youtube.com"):
        return "youtube"
    if host == "github.com":
        return "github"
    if host == "v2ex.com":
        return "v2ex"
    if path.endswith(FEED_SUFFIXES) or "/feed" in path or "/rss" in path or host in FEED_HOSTS:
        return "rss"
    return "web"


def login_only_label(url: str) -> str:
    host = host_of(url)
    for login_host, label in LOGIN_ONLY.items():
        if host == login_host or host.endswith("." + login_host):
            return label
    return ""


# ----- the read -------------------------------------------------------------
def is_web_link(url: str) -> bool:
    """An http(s) link with a real host. Anything else is invalid input, not a site that refused us."""
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc.strip()) and " " not in url.strip()


def read(url: str, transport=http_get) -> ReadResult:
    """Read one URL through the free anonymous route for its kind. A bad state is returned, not raised."""
    if not is_web_link(url):
        return ReadResult(EMPTY, "web", url, "none (not a web link)", note="not a valid web link")
    kind = classify(url)
    if kind == "login-only":
        return ReadResult(NEEDS_APPROVAL, "login-only", url, "none (login-only platform)",
                          note=login_only_label(url) or host_of(url))
    handler = {"youtube": _read_youtube, "github": _read_github, "v2ex": _read_v2ex,
               "rss": _read_feed, "web": _read_web}[kind]
    try:
        return handler(url, transport)
    except Exception as exc:  # a route failing is a state, not a crash in the window
        return ReadResult(EMPTY, kind, url, kind, note=f"{type(exc).__name__}: {exc}")


def read_text(url: str, transport=http_get) -> str:
    """The post text for the app, or an ExtractError naming the honest reason there is none."""
    from listen_app.extract import ExtractError

    result = read(url, transport=transport)
    if not result.ok:
        raise ExtractError(result.failure_message())
    return result.text


# ----- web pages ------------------------------------------------------------
def _read_web(url: str, transport) -> ReadResult:
    """Direct read first: it is local, instant and has no rate limit. Jina Reader is the fallback."""
    direct = _direct_text(url, transport)
    if direct.ok:
        return direct
    jina = _jina(url, transport)
    if jina.ok:
        jina.note = f"read via Jina Reader (direct fetch was {direct.state})"
        return jina
    if direct.state == EMPTY and direct.note.endswith("(no such page)"):
        return direct  # the page is missing; a refused Jina fallback does not change that
    if jina.state == LOGIN_WALL or direct.state == LOGIN_WALL:
        return jina if jina.state == LOGIN_WALL else direct  # a sign-in page outranks a plain empty
    if jina.state == BLOCKED:
        # Both routes refused: keep whichever one explained why, so the reader is told the reason.
        return jina if len(jina.title) > len(direct.title) else direct
    return direct


def _direct_text(url: str, transport) -> ReadResult:
    status, _ctype, body = transport(url, None, TIMEOUT_S)
    if status in MISSING_STATUS:
        return ReadResult(EMPTY, "web", url, "direct",
                          note=f"{host_of(url)} returned HTTP {status} (no such page)")
    if status in BLOCKED_STATUS:
        return ReadResult(BLOCKED, "web", url, "direct", title=_error_message(body), note=host_of(url))
    if status >= ERROR_STATUS_FLOOR:
        return ReadResult(EMPTY, "web", url, "direct",
                          note=f"{host_of(url)} returned HTTP {status} (error page, not content)")
    if status == 0 or not body:
        return ReadResult(EMPTY, "web", url, "direct", note=body.decode("utf-8", "replace")[:200])
    raw = body.decode("utf-8", "replace")
    if _looks_like_login_wall(raw):
        return ReadResult(LOGIN_WALL, "web", url, "direct", note=host_of(url))
    try:
        from listen_app.extract import extract_html

        text = extract_html(raw, url)
    except Exception as exc:  # trafilatura refused the page
        return ReadResult(EMPTY, "web", url, "direct", note=f"{type(exc).__name__}: {exc}")
    if _looks_like_missing_page(text):
        return ReadResult(EMPTY, "web", url, "direct",
                          note=f"{host_of(url)} returned a no-such-page notice, not an article")
    if len(text) < MIN_CONTENT_CHARS:
        return ReadResult(EMPTY, "web", url, "direct", note=host_of(url))
    return ReadResult(OK, "web", url, "direct + trafilatura",
                      title=_html_title(raw) or host_of(url), text=_trim(text))


def _jina(url: str, transport) -> ReadResult:
    status, _ctype, body = transport(JINA_PREFIX + url, None, TIMEOUT_S)
    if status in BLOCKED_STATUS:
        return ReadResult(BLOCKED, "web", url, "Jina Reader", title=_error_message(body), note=host_of(url))
    if status == 0:
        return ReadResult(EMPTY, "web", url, "Jina Reader", note=body.decode("utf-8", "replace")[:200])
    raw = body.decode("utf-8", "replace")
    abuse = _json_error(raw)
    if abuse:
        return ReadResult(BLOCKED, "web", url, "Jina Reader", title=abuse, note=host_of(url))
    if status >= ERROR_STATUS_FLOOR:
        return ReadResult(EMPTY, "web", url, "Jina Reader", note=f"Jina Reader returned HTTP {status}")
    missing = _jina_missing_status(raw)
    if missing:
        return ReadResult(EMPTY, "web", url, "Jina Reader",
                          note=f"Jina reported HTTP {missing} for the target page")
    if _looks_like_login_wall(raw):
        return ReadResult(LOGIN_WALL, "web", url, "Jina Reader", note=host_of(url))
    text = _jina_body(raw)
    if len(text) < MIN_CONTENT_CHARS:
        return ReadResult(EMPTY, "web", url, "Jina Reader", note=host_of(url))
    return ReadResult(OK, "web", url, "Jina Reader", title=_jina_title(raw) or host_of(url), text=_trim(text))


# ----- YouTube --------------------------------------------------------------
def _read_youtube(url: str, transport) -> ReadResult:
    """oEmbed gives a real title and author; a transcript is added only when one truly exists."""
    oembed = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"})
    status, _ctype, body = transport(oembed, None, TIMEOUT_S)
    meta = {}
    if status == 200:
        try:
            meta = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            meta = {}
    title, author = str(meta.get("title") or ""), str(meta.get("author_name") or "")
    if status in BLOCKED_STATUS and not title:
        return ReadResult(BLOCKED, "youtube", url, "YouTube oEmbed",
                          title=_error_message(body), note="youtube.com")

    parts, notes = [], []
    if title:
        parts.append(f"{title} — {author}" if author else title)
    description, description_note = _youtube_description(url, transport)
    if description:
        parts.append(description)
        notes.append("description from the watch page")
    elif description_note:
        notes.append(description_note)
    transcript, transcript_note = _youtube_transcript(url)
    if transcript:
        parts.append(transcript)
        notes.append("transcript via youtube-transcript-api")
    else:
        notes.append(transcript_note)
    if not description:
        page = _jina(url, transport)
        if page.ok:
            parts.append(page.text)
            notes.append("page text via Jina Reader")
        elif page.state == BLOCKED:
            notes.append("page text blocked")

    text = "\n\n".join(p for p in parts if p.strip())
    if len(text) < MIN_CONTENT_CHARS or len(parts) < 2:
        # Only the oEmbed title line: real, but a title is not a post. Say so instead of
        # summarising a headline as if it were the content.
        detail = "; ".join(n for n in notes if n) or "no title or transcript found"
        return ReadResult(EMPTY, "youtube", url, "YouTube oEmbed", title=title,
                          note=f"found the video ({title or 'untitled'}) but no body text — {detail}")
    return ReadResult(OK, "youtube", url, "YouTube oEmbed", title=title or url, text=_trim(text),
                      note="; ".join(n for n in notes if n), meta=meta)


_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def _youtube_description(url: str, transport) -> tuple[str, str]:
    """The video description from the public watch page.

    The page's own JSON carries `shortDescription`, the same text a logged-out visitor sees.
    No account is used, and no extra package is needed for this.
    """
    match = _VIDEO_ID.search(url)
    if not match:
        return "", "no video id in the link"
    status, _ctype, body = transport("https://www.youtube.com/watch?v=" + match.group(1),
                                     {"Accept-Language": "en"}, TIMEOUT_S)
    if status in BLOCKED_STATUS:
        return "", f"watch page refused ({status})"
    if status != 200 or not body:
        return "", "watch page unavailable"
    raw = body.decode("utf-8", "replace")
    match = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', raw)
    if not match:
        return "", "watch page had no description"
    try:
        description = json.loads('"' + match.group(1) + '"')
    except ValueError:
        return "", "watch page description was unreadable"
    description = re.sub(r"https?://\S+", "", description)  # promo links are not prose
    description = re.sub(r"\n{2,}", "\n", description).strip()
    return (description, "") if len(description) >= MIN_CONTENT_CHARS else ("", "description too short")


# After YouTube blocks this IP, every further call deepens the block. Pause transcript calls
# for a while. Measured on 2026-10-18 after a burst of test requests: IpBlocked on every video.
TRANSCRIPT_COOLDOWN_S = 15 * 60
_transcript_blocked_until = 0.0


def _start_transcript_cooldown() -> None:
    global _transcript_blocked_until
    _transcript_blocked_until = time.monotonic() + TRANSCRIPT_COOLDOWN_S


def _youtube_transcript(url: str) -> tuple[str, str]:
    """English captions through youtube-transcript-api: anonymous, no key, no cookie, no yt-dlp.

    Round 15 measurement: yt-dlp was not installed on the Mac, so the old branch never ran.
    This route returned real transcripts for 3 of 5 videos in about 1 s each. The other two were
    an honest "no transcript" and "unavailable", which are reported as notes, not as text.
    """
    match = _VIDEO_ID.search(url)
    if not match or YouTubeTranscriptApi is None:
        return "", "no transcript route on this machine (youtube-transcript-api missing)"
    if time.monotonic() < _transcript_blocked_until:
        return "", "transcript paused: YouTube blocked this IP, cooling down"
    try:
        fetched = YouTubeTranscriptApi().fetch(match.group(1), languages=["en"])
    except NoTranscriptFound:
        return "", "no English transcript on this video"
    except TranscriptsDisabled:
        return "", "transcripts are disabled on this video"
    except IpBlocked as exc:
        _start_transcript_cooldown()
        return "", f"transcript unavailable ({type(exc).__name__}): YouTube blocked this IP"
    except Exception as exc:  # the library reports network and unavailable states by type
        return "", f"transcript unavailable ({type(exc).__name__})"
    text = " ".join(s.text.strip() for s in fetched.snippets if s.text.strip())
    return (text, "") if text else ("", "transcript was empty")


# ----- GitHub, V2EX, feeds --------------------------------------------------
_GITHUB = re.compile(r"^/([^/]+)/([^/]+)(?:/(?:issues|pull)/(\d+))?")


def _read_github(url: str, transport) -> ReadResult:
    match = _GITHUB.match(urlparse(url).path)
    if not match:
        return ReadResult(EMPTY, "github", url, "GitHub API", note="not a repo, issue or pull link")
    owner, repo, number = match.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}"
    accept = {"Accept": "application/vnd.github+json"}
    status, _ctype, body = transport(api, accept, TIMEOUT_S)
    if status in BLOCKED_STATUS:
        return ReadResult(BLOCKED, "github", url, "GitHub API", title=_error_message(body),
                          note="api.github.com")
    if status != 200:
        return ReadResult(EMPTY, "github", url, "GitHub API",
                          note=_error_message(body) or f"HTTP {status}")
    meta = json.loads(body.decode("utf-8", "replace"))
    full = meta.get("full_name") or f"{owner}/{repo}"
    parts = [f"{full} — {meta.get('description') or 'no description'}"]
    if meta.get("language"):
        parts.append(f"Language: {meta['language']} · Stars: {meta.get('stargazers_count')}")

    route, title, text_body = "GitHub API", full, ""
    if number:
        status, _ctype, body = transport(f"{api}/issues/{number}", accept, TIMEOUT_S)
        if status == 200:
            issue = json.loads(body.decode("utf-8", "replace"))
            title = f"{full} #{issue.get('number')} — {issue.get('title')}"
            parts.append(f"State: {issue.get('state')} · by {(issue.get('user') or {}).get('login')}")
            text_body = (issue.get("body") or "").strip()
            route = "GitHub issues API"
    else:
        status, _ctype, body = transport(f"{api}/readme", {"Accept": "application/vnd.github.raw"},
                                         TIMEOUT_S)
        if status == 200:
            text_body = body.decode("utf-8", "replace").strip()
            route = "GitHub API + README"
    if text_body:
        parts.append(text_body)
    text = _trim("\n\n".join(p for p in parts if p and p.strip()))
    if len(text) < MIN_CONTENT_CHARS:
        return ReadResult(EMPTY, "github", url, route, title=title, note="no description or body")
    return ReadResult(OK, "github", url, route, title=title, text=text)


_V2EX_TOPIC = re.compile(r"^/t/(\d+)")


def _read_v2ex(url: str, transport) -> ReadResult:
    match = _V2EX_TOPIC.match(urlparse(url).path)
    api = (f"https://www.v2ex.com/api/topics/show.json?id={match.group(1)}" if match
           else "https://www.v2ex.com/api/topics/hot.json")
    status, _ctype, body = transport(api, {"User-Agent": "agent-reach/1.0"}, TIMEOUT_S)
    if status in BLOCKED_STATUS:
        return ReadResult(BLOCKED, "v2ex", url, "V2EX API", title=_error_message(body), note="v2ex.com")
    if status != 200:
        return ReadResult(EMPTY, "v2ex", url, "V2EX API", note=f"HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    if match and isinstance(data, list) and data:
        topic = data[0]
        text = _trim(f"{topic.get('title')}\n\n{topic.get('content') or ''}\n\n"
                     f"Replies: {topic.get('replies')}")
        return ReadResult(OK, "v2ex", url, "V2EX topic API", title=str(topic.get("title") or ""),
                          text=text)
    if isinstance(data, list) and data:
        lines = [f"- {t.get('title')} ({t.get('url')})" for t in data if t.get("title")]
        if lines:
            return ReadResult(OK, "v2ex", url, "V2EX hot API", title="V2EX hot topics",
                              text=_trim("V2EX hot topics\n\n" + "\n".join(lines)))
    return ReadResult(EMPTY, "v2ex", url, "V2EX API", note="no topics returned")


def _read_feed(url: str, transport) -> ReadResult:
    status, _ctype, body = transport(url, None, TIMEOUT_S)
    if status in BLOCKED_STATUS:
        return ReadResult(BLOCKED, "rss", url, "feed", title=_error_message(body), note=host_of(url))
    if status != 200 or not body:
        return ReadResult(EMPTY, "rss", url, "feed", note=f"HTTP {status}")
    return parse_feed(body.decode("utf-8", "replace"), url)


def parse_feed(xml: str, url: str = "") -> ReadResult:
    """RSS 2.0 or Atom with the standard library only. Namespaces are ignored, not required."""
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError as exc:
        return ReadResult(EMPTY, classify(url) if url else "rss", url, "feed",
                          note=f"not a feed: {exc}")
    title = _find_text(root, "title") or host_of(url)
    items = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        item_title = _find_text(node, "title")
        summary = (_find_text(node, "description") or _find_text(node, "summary")
                   or _find_text(node, "content"))
        link = _find_text(node, "link")
        if not link:
            for child in node:
                if child.tag.rsplit("}", 1)[-1] == "link":
                    link = child.get("href") or ""
                    break
        line = "- " + (item_title or "(untitled)")
        if link:
            line += f" ({link})"
        if summary:
            line += " " + _strip_html(summary)[:300]
        items.append(line)
    if not items:
        return ReadResult(EMPTY, "rss", url, "feed", title=title, note="feed has no items")
    return ReadResult(OK, "rss", url, "feed", title=title, text=_trim(f"{title}\n\n" + "\n".join(items)))


def _find_text(node, name: str) -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name and (child.text or "").strip():
            return child.text.strip()
    return ""


# ----- shared helpers -------------------------------------------------------
def _trim(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit(" ", 1)[0] + "\n\n[trimmed]"


def _jina_title(raw: str) -> str:
    match = re.search(r"^Title:\s*(.+)$", raw, re.M)
    return match.group(1).strip() if match else ""


def _html_title(raw: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    return _strip_html(match.group(1)) if match else ""


def _jina_body(raw: str) -> str:
    """Drop Jina's header block and markdown decoration, keeping the post text."""
    body = raw.split("Markdown Content:", 1)[-1] if "Markdown Content:" in raw else raw
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", body)  # images carry no words
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)  # keep link text, drop the URL
    body = re.sub(r"^\s*[*\-+]\s*\[[ xX]\]\s*", "", body, flags=re.M)  # empty tick boxes
    body = re.sub(r"\n{3,}", "\n\n", body)
    return re.sub(r"[ \t]{2,}", " ", body).strip()


def _jina_missing_status(raw: str) -> str:
    """Jina keeps HTTP 200 and reports the target page's error in its own header block."""
    header = raw.split("Markdown Content:", 1)[0]
    match = re.search(r"Target URL returned error[ ]+([0-9]{3})", header, re.I)
    return match.group(1) if match else ""


def _looks_like_missing_page(text: str) -> bool:
    """True when the VISIBLE words are short and read as a no-such-page notice."""
    visible = _strip_html(_drop_link_targets(text))
    if len(visible) >= NOT_FOUND_MAX_VISIBLE:
        return False
    lowered = visible.lower()
    return any(marker in lowered for marker in NOT_FOUND_MARKERS)


def _looks_like_login_wall(raw: str) -> bool:
    """A page with almost no visible words that only asks the reader to sign in is a wall.

    Judged on the VISIBLE text, not the byte count: an HTML page is mostly markup, so a long
    real article must not be misread as a wall just because it has a "log in to comment" footer.
    """
    visible = _strip_html(_drop_link_targets(raw))
    if len(visible) >= 500:
        return False
    lowered = visible.lower()
    return any(marker in lowered for marker in LOGIN_MARKERS)


def _drop_link_targets(text: str) -> str:
    """Remove markdown/HTML URL targets, so a page's bytes of links are not mistaken for words."""
    text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", text)  # markdown image or link
    return re.sub(r"(?:https?|blob):[^\s\"')]+", " ", text)


def _json_error(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return ""
    try:
        data = json.loads(stripped)
    except ValueError:
        return ""
    if isinstance(data, dict) and data.get("data") is None and data.get("code"):
        return str(data.get("readableMessage") or data.get("message") or data.get("name") or "")
    return ""


def _error_message(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    as_json = _json_error(text)
    return as_json[:200] if as_json else _strip_html(text)[:200]


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
