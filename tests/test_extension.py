"""The browser extension: what it is allowed to ask for, and what it actually extracts.

The extraction tests are not assertions about source text. Each one writes a small page
description, runs extension/extract.js in Node against a fake DOM, and reads the decision the
script really made. That means a change to the selector order or the paragraph scoring is caught
by behaviour, not by a comment going stale.

Node is not a dependency of the app, so these skip when it is absent — the same rule the Kokoro
tests follow for the model files.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "extension"
MANIFEST = EXT / "manifest.json"
NODE_CANDIDATES = [
    shutil.which("node"),
    str(Path.home() / ".nvm" / "versions" / "node" / "v24.21.0" / "bin" / "node"),
]


def node() -> str:
    for candidate in NODE_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("node is not installed, so the extraction cannot be executed here")


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ----- the manifest asks for the least it can -------------------------------
def test_it_is_manifest_v3():
    assert manifest()["manifest_version"] == 3


def test_it_asks_for_no_broad_host_access():
    """The extension reads the tab the user is on, only when they press the button.

    `activeTab` + `scripting` grant exactly that. A `<all_urls>` or "tabs" permission would grant
    standing access to every page instead, which this extension does not need.
    """
    granted = set(manifest().get("permissions", []))
    assert granted <= {"activeTab", "scripting", "storage"}, granted
    assert "tabs" not in granted
    hosts = manifest().get("host_permissions", [])
    assert all(host.startswith(("http://127.0.0.1", "http://localhost")) for host in hosts), hosts


def test_it_never_asks_for_cookies_or_webrequest():
    """The bridge must not be able to see a logged-in session, and no request is intercepted."""
    forbidden = {"cookies", "webRequest", "webRequestBlocking", "history", "downloads", "management"}
    assert forbidden.isdisjoint(manifest().get("permissions", []))


def test_every_file_the_manifest_names_exists():
    data = manifest()
    named = [data["action"]["default_popup"], data["background"]["service_worker"]]
    named += list(data.get("icons", {}).values())
    for name in named:
        assert (EXT / name).is_file(), name


def test_the_popup_and_worker_are_real_scripts():
    """An extension whose popup script does not parse fails silently in the browser."""
    for name in ["popup.js", "client.js", "background.js", "extract.js"]:
        path = EXT / name
        check = subprocess.run([node(), "--check", str(path)], capture_output=True, text=True)
        assert check.returncode == 0, f"{name}: {check.stderr[:400]}"


def test_the_bridge_contract_matches_the_python_side():
    """The extension's header, endpoint and payload cap are the bridge's, not copies that drifted."""
    from listen_app import bridge as py_bridge

    client = (EXT / "client.js").read_text(encoding="utf-8")
    assert py_bridge.TOKEN_HEADER in client
    assert f"127.0.0.1:{py_bridge.DEFAULT_PORT}" in client
    extract = (EXT / "extract.js").read_text(encoding="utf-8")
    assert str(py_bridge.MAX_TEXT) in extract, "extract.js must trim to the bridge's cap"


def test_the_extension_makes_no_request_to_a_third_party():
    """Every fetch in the extension is loopback. A remote URL here would break the local-only rule."""
    for name in ["client.js", "extract.js", "popup.js", "background.js"]:
        for line in (EXT / name).read_text(encoding="utf-8").splitlines():
            code = line.strip()
            if code.startswith(("*", "//", "/*")):  # a comment is prose, not a call
                continue
            if "fetch(" in code or "XMLHttpRequest" in code:
                assert "127.0.0.1" in code or "ENDPOINT" in code, f"{name}: {code}"


def test_the_content_script_does_not_use_cookies_or_storage_of_the_page():
    source = (EXT / "extract.js").read_text(encoding="utf-8")
    for forbidden in ["document.cookie", "localStorage", "sessionStorage", "indexedDB"]:
        assert forbidden not in source, forbidden


# ----- what it extracts, executed for real ---------------------------------
HARNESS = r"""
import { readFileSync } from "node:fs";
class El {
  constructor(spec, parent) {
    this.tag = (spec.tag || "div").toLowerCase();
    this.attrs = spec.attrs || {};
    this.text = spec.text || "";
    this.own = spec.own || "";
    this.hidden = !!spec.hidden;
    this.children = (spec.children || []).map((c) => new El(c, this));
    this.parentElement = parent;
    this.id = this.attrs.id || "";
    this.classes = (this.attrs.class || "").split(/\s+/).filter(Boolean);
  }
  get innerText() {
    if (this.hidden) return "";
    if (this.own) return this.own;
    if (this.text) return this.text;
    return this.children.map((c) => c.innerText).join("\n");
  }
  get textContent() {
    if (this.hidden) return "";
    if (this.own) return this.own;
    if (this.text) return this.text;
    return this.children.map((c) => c.textContent).join(" ");
  }
  getBoundingClientRect() { return this.hidden ? { width: 0, height: 0 } : { width: 100, height: 100 }; }
  matches(sel) {
    const attr = sel.match(/^\[([\w-]+)="([^"]*)"\]$/);
    if (attr) return this.attrs[attr[1]] === attr[2];
    if (sel.startsWith(".")) return this.classes.includes(sel.slice(1));
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    return this.tag === sel.toLowerCase();
  }
  querySelectorAll(sel) {
    const parts = sel.split(",").map((p) => p.trim()).filter(Boolean);
    const out = [];
    const walk = (node) => {
      if (node !== this && parts.some((p) => node.matches(p))) out.push(node);
      for (const child of node.children) walk(child);
    };
    walk(this);
    return out;
  }
}
const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));
const root = new El({ tag: "body", ...(scenario.body || {}) }, null);
globalThis.document = { title: scenario.title || "A page", body: root, activeElement: null,
  querySelectorAll: (s) => root.querySelectorAll(s) };
globalThis.location = { href: scenario.url || "https://example.com/a", hostname: scenario.host || "example.com" };
globalThis.window = {
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  getSelection: () => scenario.selection
    ? { rangeCount: 1, isCollapsed: false, toString: () => scenario.selection }
    : { rangeCount: 0, isCollapsed: true, toString: () => "" },
};
const source = readFileSync(process.argv[3], "utf8");
const dictatorExtract = new Function(`${source}\nreturn dictatorExtract;`)();
console.log(JSON.stringify(dictatorExtract(scenario.options || {})));
"""


def extract(scenario: dict, tmp_path: Path) -> dict:
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    page = tmp_path / "page.json"
    page.write_text(json.dumps(scenario), encoding="utf-8")
    run = subprocess.run([node(), str(harness), str(page), str(EXT / "extract.js")],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    return json.loads(run.stdout)


def para(text, **extra) -> dict:
    return {"tag": "p", "text": text, **extra}


def test_a_selection_wins_over_the_whole_page(tmp_path):
    result = extract({
        "url": "https://example.com/thread", "host": "example.com",
        "selection": "the comment I actually meant",
        "body": {"children": [{"tag": "article", "children": [para("A long article body. " * 40)]}]},
    }, tmp_path)
    assert result["how"] == "selection"
    assert result["text"] == "the comment I actually meant"


def test_page_mode_ignores_the_selection(tmp_path):
    result = extract({
        "url": "https://example.com/thread", "host": "example.com",
        "selection": "the comment I actually meant",
        "options": {"preferSelection": False},
        "body": {"children": [{"tag": "article", "children": [para("A long article body. " * 40)]}]},
    }, tmp_path)
    assert result["how"] == "main"
    assert "long article body" in result["text"]


def test_a_social_post_is_read_from_its_own_container_not_the_sidebar(tmp_path):
    result = extract({
        "url": "https://x.com/someone/status/1", "host": "x.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"class": "sidebar"}, "text": "Home Explore Notifications " * 20},
            {"tag": "div", "attrs": {"data-testid": "tweetText"}, "text": "Just shipped the bridge."},
        ]},
    }, tmp_path)
    assert result["how"] == "post"
    assert result["text"] == "Just shipped the bridge."


def test_a_reddit_comment_thread_keeps_the_post_body(tmp_path):
    result = extract({
        "url": "https://www.reddit.com/r/python/comments/x/y", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"class": "comment"}, "text": "an unrelated comment " * 30},
            {"tag": "div", "attrs": {"data-testid": "post-content"}, "text": "My post about sockets."},
        ]},
    }, tmp_path)
    assert result["how"] == "post"
    assert result["text"] == "My post about sockets."


def test_an_article_page_uses_its_main_region(tmp_path):
    result = extract({
        "url": "https://example.com/news", "host": "example.com",
        "body": {"children": [
            {"tag": "nav", "text": "Menu " * 200},
            {"tag": "article", "children": [para("The real story, with enough words to count as prose. " * 6)]},
        ]},
    }, tmp_path)
    assert result["how"] == "main"
    assert "The real story" in result["text"]
    assert "Menu" not in result["text"]


def test_a_bare_page_falls_back_to_the_densest_prose_block(tmp_path):
    result = extract({
        "url": "https://example.com/bare", "host": "example.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"class": "links"},
             "text": "click here " * 200},
            {"tag": "div", "attrs": {"class": "prose"},
             "children": [para("This is the body of the piece and it is what the reader wants. " * 4)]},
        ]},
    }, tmp_path)
    assert result["how"] == "page"
    assert "the body of the piece" in result["text"]


def test_an_empty_page_reports_no_text_so_the_caller_can_say_so(tmp_path):
    result = extract({"url": "https://example.com/blank", "host": "example.com",
                      "body": {"children": [{"tag": "div", "text": ""}]}}, tmp_path)
    assert result["text"] == ""
    assert result["how"] == ""


def test_a_huge_page_is_trimmed_to_the_bridge_cap(tmp_path):
    from listen_app.bridge import MAX_TEXT

    result = extract({
        "url": "https://example.com/long", "host": "example.com",
        "body": {"children": [{"tag": "article", "children": [para("word " * (MAX_TEXT))]}]},
    }, tmp_path)
    assert result["truncated"] is True
    assert len(result["text"]) == MAX_TEXT


def test_the_url_and_title_travel_with_the_text(tmp_path):
    result = extract({
        "url": "https://example.com/post", "host": "example.com", "title": "A real post",
        "selection": "selected words here",
        "body": {"children": []},
    }, tmp_path)
    assert result["url"] == "https://example.com/post"
    assert result["title"] == "A real post"


def test_white_space_is_tidied_not_sent_raw(tmp_path):
    result = extract({
        "url": "https://example.com/w", "host": "example.com",
        "selection": "  line one   with   runs\n\n\n\n  line two  ",
        "body": {"children": []},
    }, tmp_path)
    assert result["text"] == "line one with runs\n\nline two"


def test_a_link_list_does_not_beat_the_prose_block(tmp_path):
    """A page whose links outnumber its prose must not return the link list as the article.

    Regression, found in Chromium: the wrapper <html> ties the prose block on score and was
    scanned first, so the whole body (link directory included) won. The document root is a
    wrapper, never the content, so it is excluded from the scan.
    """
    result = extract({
        "url": "https://example.com/dir", "host": "example.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"id": "m"}, "children": [
                {"tag": "p", "children": [{"tag": "a", "text": "Directory entry alpha link words"}]},
                {"tag": "p", "children": [{"tag": "a", "text": "Directory entry beta link words"}]},
            ]},
            {"tag": "div", "attrs": {"id": "prose"}, "children": [
                para("The real prose paragraph that the reader wants to keep, long enough to score. " * 4),
            ]},
        ]},
    }, tmp_path)
    assert "The real prose paragraph" in result["text"]
    assert "Directory entry alpha" not in result["text"]



def test_the_text_of_a_closed_or_empty_page_is_not_invented(tmp_path):
    """A page with nothing readable still reports no text, so the caller says so."""
    result = extract({"url": "https://example.com/empty2", "host": "example.com",
                      "body": {"children": [{"tag": "img"}]}}, tmp_path)
    assert result["text"] == ""
    assert result["how"] == ""


def test_a_bot_challenge_page_is_refused_not_read_as_the_article(tmp_path):
    """A short bot-check page must not be sent as if it were the article.

    Regression, found live: a Cloudflare "Performing security verification" page (HTTP 403) was
    extracted as 256 characters of page text and would have been sent to Listen.
    """
    result = extract({
        "url": "https://example.com/blocked", "host": "example.com",
        "body": {"children": [
            {"tag": "h1", "text": "example.com"},
            {"tag": "p", "text": "Performing security verification. This website uses a security "
                                 "service to protect against malicious bots."},
        ]},
    }, tmp_path)
    assert result["how"] == "wall"
    assert result["text"] == ""


def test_an_article_that_only_mentions_the_phrase_is_still_read(tmp_path):
    """A real article that quotes the challenge wording has several paragraphs, so it is kept."""
    body = [para("Sites often show a page saying Performing security verification before a reader "
                 "gets in, which many readers find annoying when they only want the article. " * 2)]
    body += [para("Another real paragraph of article prose that keeps the page an article. " * 4)
             for _ in range(3)]
    result = extract({
        "url": "https://example.com/discuss", "host": "example.com",
        "body": {"children": [{"tag": "article", "children": body}]},
    }, tmp_path)
    assert result["how"] != "wall"
    assert "Another real paragraph" in result["text"]


def test_a_user_selection_on_a_challenge_page_is_still_sent(tmp_path):
    """The user chose the text, so a selection is never refused by the challenge guard."""
    result = extract({
        "url": "https://example.com/blocked", "host": "example.com",
        "selection": "a sentence the user picked",
        "body": {"children": [{"tag": "p", "text": "Performing security verification."}]},
    }, tmp_path)
    assert result["how"] == "selection"
    assert result["text"] == "a sentence the user picked"


def test_a_missing_wikipedia_article_is_refused_not_read_as_the_article(tmp_path):
    """A page that does not exist must not be sent as if it were the article.

    Regression, found live in Chromium: en.wikipedia.org/wiki/Kokoro_(TTS) (no such article) was
    extracted as 1211 characters of "does not have an article" text with how "main". Wikipedia marks
    the missing page with #noarticletext, and a real article does not carry it.
    """
    result = extract({
        "url": "https://en.wikipedia.org/wiki/Kokoro_(TTS)", "host": "en.wikipedia.org",
        "body": {"children": [
            {"tag": "article", "children": [
                {"tag": "div", "attrs": {"id": "noarticletext"},
                 "text": "Wikipedia does not have an article with this exact name."},
                para("Look for Kokoro (TTS) on one of Wikipedia's sister projects. " * 4),
            ]},
        ]},
    }, tmp_path)
    assert result["how"] == "missing"
    assert result["text"] == ""


def test_a_real_wikipedia_article_is_still_read(tmp_path):
    """A real article has no #noarticletext marker, so it is kept."""
    result = extract({
        "url": "https://en.wikipedia.org/wiki/Speech_synthesis", "host": "en.wikipedia.org",
        "body": {"children": [{"tag": "article", "children": [
            para("Speech synthesis is the artificial production of human speech. " * 6)]}]},
    }, tmp_path)
    assert result["how"] == "main"
    assert "artificial production of human speech" in result["text"]


def test_wikipedia_interface_menus_are_not_read_as_the_article(tmp_path):
    """The article's own text must come first, without the page's menus in front of it.

    Found live in Opera GX, Wikipedia "Speech synthesis": the <main> element starts with the
    language count, the Article/Talk/Read/Edit tabs and the Appearance controls, then the article.
    The reader sent all of it as the page, so the note began with menu labels.
    """
    menus = ["54 languages", "Article", "Talk", "Read", "Edit", "View history", "Tools",
             "Appearance hide", "Text", "Small", "Standard", "Large", "Width", "Wide", "Color"]
    result = extract({
        "url": "https://en.wikipedia.org/wiki/Speech_synthesis", "host": "en.wikipedia.org",
        "body": {"children": [{"tag": "main", "children": [
            *[{"tag": "span", "text": label} for label in menus],
            {"tag": "div", "attrs": {"id": "mw-content-text"}, "children": [
                para("Speech synthesis is the artificial production of human speech. " * 6)]},
        ]}]},
    }, tmp_path)
    assert result["how"] == "post"  # the Wikipedia selector, which runs before the generic <main> scan
    assert result["text"].startswith("Speech synthesis is the artificial production of human speech")
