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
        check = subprocess.run([node(), "--check", str(path)], capture_output=True, text=True,
                               encoding="utf-8")
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
  // One compound selector: a tag, an #id, .classes, and [attr], [attr=v], [attr*=v], [attr^=v].
  matchesSimple(sel) {
    const bits = sel.replace(/([#.\[])/g, " $1").split(/\s+/).filter(Boolean);
    if (!bits.length) return false;
    const tag = bits[0][0] === "[" || bits[0][0] === "." || bits[0][0] === "#" ? "" : bits[0].toLowerCase();
    if (tag && this.tag !== tag) return false;
    const attrs = bits.slice(tag ? 1 : 0);
    for (const bit of attrs) {
      if (bit.startsWith("#")) { if (this.id !== bit.slice(1)) return false; continue; }
      if (bit.startsWith(".")) { if (!this.classes.includes(bit.slice(1))) return false; continue; }
      const inner = bit.slice(1, -1);
      const sub = inner.match(/^([\w-]+)\*=(["\']?)(.*)\2$/);
      const pre = inner.match(/^([\w-]+)\^=(["\']?)(.*)\2$/);
      const eq = inner.match(/^([\w-]+)=(["\']?)(.*)\2$/);
      const bare = inner.match(/^([\w-]+)$/);
      const value = this.attrs[bare ? bare[1] : (sub || pre || eq)[1]];
      if (value === undefined) return false;
      if (sub && !String(value).includes(sub[3])) return false;
      if (pre && !String(value).startsWith(pre[3])) return false;
      if (eq && String(value) !== eq[3]) return false;
    }
    return true;
  }
  // A descendant chain ("a b c"): the last compound must match, and each earlier one an ancestor.
  matchesChain(sel) {
    const tokens = sel.split(/\s+/).filter(Boolean);
    if (!this.matchesSimple(tokens[tokens.length - 1])) return false;
    let i = tokens.length - 2;
    let node = this.parentElement;
    while (i >= 0) {
      let found = false;
      while (node) {
        if (node.matchesSimple(tokens[i])) { found = true; node = node.parentElement; i -= 1; break; }
        node = node.parentElement;
      }
      if (!found) return false;
    }
    return true;
  }
  querySelectorAll(sel) {
    const parts = sel.split(",").map((p) => p.trim()).filter(Boolean);
    const out = [];
    const walk = (node) => {
      if (node !== this && parts.some((p) => node.matchesChain(p))) out.push(node);
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
    # encoding="utf-8" is load-bearing: without it Python decodes Node's UTF-8 output with the
    # host's own codec, and on Windows (cp1252) an em dash came back mangled, failing the one test
    # that checks a \u2014 escape. Found in CI on windows-latest 2026-10-20.
    run = subprocess.run([node(), str(harness), str(page), str(EXT / "extract.js")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
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


# ----- posts and their conversations, from the live DOM of each site ---------
def test_a_reddit_thread_reads_the_post_and_the_top_comments(tmp_path):
    """A Reddit page is a post plus a conversation; the post alone loses what the reader came for.

    Measured live in Chromium 2026-10-20 on r/Python: the body is in `shreddit-post [slot="text-body"]`
    and each comment in `shreddit-comment [slot="comment"]`. The extractor never read a comment.
    """
    result = extract({
        "url": "https://www.reddit.com/r/Python/comments/x/showcase/", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "shreddit-post", "children": [
                {"tag": "div", "attrs": {"slot": "text-body"}, "text": "Post all of your code here."},
            ]},
            {"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"}, "text": "I built StatLite for monitoring."},
            ]},
            {"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"}, "text": "Pytrithon v1.2.12 is out."},
            ]},
        ]},
    }, tmp_path)
    assert result["how"] == "post"
    assert result["text"].startswith("Post all of your code here.")
    assert "Comments:" in result["text"]
    assert "StatLite" in result["text"] and "Pytrithon" in result["text"]
    # the post stays first, so a note built from it starts with the thing being discussed
    assert result["text"].index("Post all") < result["text"].index("StatLite")


def test_a_reddit_thread_appends_a_repeated_comment_once(tmp_path):
    """A quote-reply repeats the comment it answers; the same text must not arrive twice."""
    reply = "The answer that was quoted inside another reply."
    result = extract({
        "url": "https://www.reddit.com/r/x/comments/y/z/", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "shreddit-post", "children": [
                {"tag": "div", "attrs": {"slot": "text-body"}, "text": "A short post body."}]},
            *[{"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"}, "text": reply}]} for _ in range(4)],
        ]},
    }, tmp_path)
    assert result["text"].count(reply) == 1


def test_a_reddit_thread_stops_at_the_comment_cap(tmp_path):
    """A 500-reply thread must not crowd the post out of the 60,000-character bridge cap."""
    post = "The post body that must survive a huge thread. " * 4
    comments = [{"tag": "shreddit-comment", "children": [
        {"tag": "div", "attrs": {"slot": "comment"}, "text": f"Reply number {i} " + "with words " * 40}]}
        for i in range(40)]
    result = extract({
        "url": "https://www.reddit.com/r/x/comments/y/z/", "host": "www.reddit.com",
        "body": {"children": [{"tag": "shreddit-post", "children": [
            {"tag": "div", "attrs": {"slot": "text-body"}, "text": post}]}, *comments]},
    }, tmp_path)
    assert result["text"].startswith(post.strip())
    assert result["text"].count("Reply number") == 8
    assert len(result["text"]) < 60000


def test_a_reddit_link_post_keeps_the_conversation_and_not_the_sidebar(tmp_path):
    """A link post has a title and no body, and the reply under it is still the point.

    Reproduced 2026-10-21: a post with no [slot="text-body"] made the thread read return "", the
    generic page scan took over, and the note began with the sidebar's community rules with the
    replies mixed in as page text. The title carries the post now, so the conversation is read,
    labelled, and the sidebar is not.
    """
    result = extract({
        "url": "https://www.reddit.com/r/Python/comments/x/link/", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "aside", "children": [
                para(f"Related community rule {i}: keep posts on topic and civil.") for i in range(8)]},
            {"tag": "shreddit-post", "children": [
                {"tag": "h1", "attrs": {"slot": "title"},
                 "text": "What are some good python projects for a beginner?"}]},
            {"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"}, "text": "Try a web scraper with httpx."}]},
        ]},
    }, tmp_path)
    assert result["how"] == "post", result["text"][:160]
    assert result["text"].startswith("What are some good python projects")
    assert "Comments:" in result["text"] and "web scraper" in result["text"]
    assert "Related community rule" not in result["text"]


def test_a_reddit_post_that_reads_empty_still_sends_its_comments(tmp_path):
    """An image post is a title, a picture and a conversation.

    When the post selector finds nothing, the replies must still arrive under their heading
    instead of being dropped along with the post body.
    """
    result = extract({
        "url": "https://www.reddit.com/r/x/comments/y/img/", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "shreddit-post", "children": [{"tag": "img"}]},
            {"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"},
                 "text": "That is a capacitor bank, not a battery pack."}]},
            {"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"},
                 "text": "The label says 400V, so do not touch the terminals."}]},
        ]},
    }, tmp_path)
    assert result["how"] == "post", result["text"][:160]
    assert result["text"].startswith("Comments:")
    assert "capacitor bank" in result["text"] and "400V" in result["text"]


def test_a_youtube_video_reads_the_full_description_not_the_collapsed_box(tmp_path):
    """YouTube collapses the description to ~370 characters behind a "more" button.

    Measured live 2026-10-20: `#description-inline-expander` innerText was 372 characters while the
    page's own player JSON held 1379, so the collapsed box alone loses two thirds of the text.
    """
    full = "This socket programming tutorial shows how to connect multiple clients to a server."
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"id": "description-inline-expander"},
             "text": "This socket programming tutorial shows how to … more"},
            {"tag": "script", "text": 'var ytInitialPlayerResponse = {"videoDetails":'
                                      '{"shortDescription":"' + full + '"}};'},
        ]},
    }, tmp_path)
    assert result["how"] == "video"
    assert result["text"].startswith(full)


def test_a_youtube_description_is_decoded_from_json_not_sent_with_escapes(tmp_path):
    """The description is a JSON string: \\n and \\u escapes must become characters."""
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"Line one\\nLine two \\u2014 dash\\n'
                                      '#python #sockets"}'},
        ]},
    }, tmp_path)
    assert result["how"] == "video"
    assert result["text"] == "Line one\nLine two — dash\n#python #sockets"


def test_a_youtube_video_appends_the_top_comments(tmp_path):
    """The comments under a video are the reaction the reader usually wants summarised."""
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"A description long enough to be read."}'},
            *[{"tag": "ytd-comment-thread-renderer", "children": [
                {"tag": "yt-element", "attrs": {"id": "content-text"},
                 "text": f"Comment {i} about the video, long enough to matter."}]} for i in range(3)],
        ]},
    }, tmp_path)
    assert "Comments:" in result["text"]
    assert result["text"].count("Comment ") == 3


def test_a_youtube_page_without_player_json_still_reads_the_collapsed_box(tmp_path):
    """If the player JSON is absent or unparseable, the visible description is the fallback."""
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"id": "description-inline-expander"},
             "text": "A description that only exists in the visible expander box here."},
        ]},
    }, tmp_path)
    assert result["how"] == "video"
    assert result["text"].startswith("A description that only exists")


def test_a_logged_out_facebook_page_is_refused_not_sent_as_the_post(tmp_path):
    """Logged out, facebook.com is a sign-in card. Sending it puts "Log in" in a note as the post.

    Measured live 2026-10-20: the logged-out page body was 501 characters and held no article.
    """
    result = extract({
        "url": "https://www.facebook.com/", "host": "www.facebook.com",
        "body": {"children": [
            {"tag": "div", "text": "Log in to Facebook. Email or phone number. Password. Log in. "
                                   "Forgotten password? Create new account."},
        ]},
    }, tmp_path)
    assert result["how"] == "login"
    assert result["text"] == ""


def test_a_logged_out_instagram_page_is_refused(tmp_path):
    result = extract({
        "url": "https://www.instagram.com/p/abc/", "host": "www.instagram.com",
        "body": {"children": [{"tag": "div",
                                "text": "Log in to Instagram. Sign up to see photos and videos "
                                        "from your friends."}]},
    }, tmp_path)
    assert result["how"] == "login"
    assert result["text"] == ""


def test_a_logged_out_discord_page_is_refused(tmp_path):
    result = extract({
        "url": "https://discord.com/app", "host": "discord.com",
        "body": {"children": [{"tag": "div", "text": "You must log in to view this. Log in or "
                                                     "create a new account to continue."}]},
    }, tmp_path)
    assert result["how"] == "login"
    assert result["text"] == ""


def test_a_signed_in_facebook_post_is_read(tmp_path):
    """A real post is long and carries no sign-in wording, so the login guard must not touch it."""
    post = "A real post about a build that shipped today, with the details a reader wants. " * 12
    result = extract({
        "url": "https://www.facebook.com/someone/posts/1", "host": "www.facebook.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"role": "article"}, "children": [
                {"tag": "div", "text": post},
                {"tag": "div", "text": "A comment under the post, from a friend who replied."},
            ]},
        ]},
    }, tmp_path)
    assert result["how"] == "post"
    assert "A real post about a build" in result["text"]
    assert "A comment under the post" in result["text"]


def test_an_instagram_post_is_read_from_its_article(tmp_path):
    result = extract({
        "url": "https://www.instagram.com/p/abc/", "host": "www.instagram.com",
        "body": {"children": [{"tag": "article", "text":
                               "A caption long enough to count as the post, with #hashtags and a "
                               "sentence about the trip. A comment from someone under it."}]},
    }, tmp_path)
    assert result["how"] == "post"
    assert "A caption long enough" in result["text"]


def test_a_discord_channel_reads_its_visible_messages(tmp_path):
    """Discord is a message list, not a post: every visible message is content."""
    messages = [{"tag": "div", "attrs": {"class": "messageContent_abc"},
                 "text": f"Message {i} in the channel with the actual conversation text."}
                for i in range(3)]
    result = extract({
        "url": "https://discord.com/channels/1/2", "host": "discord.com",
        "body": {"children": [{"tag": "ol", "attrs": {"data-list-id": "chat-messages"},
                               "children": messages}]},
    }, tmp_path)
    assert result["how"] == "post"
    assert result["text"].count("Message ") == 3


def test_a_non_thread_host_still_reads_its_own_post_container(tmp_path):
    """The thread path must not swallow the plain social hosts."""
    result = extract({
        "url": "https://x.com/someone/status/1", "host": "x.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"data-testid": "tweetText"}, "text": "Just shipped the bridge."}]},
    }, tmp_path)
    assert result["how"] == "post"
    assert result["text"] == "Just shipped the bridge."


def test_a_site_error_404_page_is_not_read_as_an_article(tmp_path):
    """A dead link that answers HTTP 200 with a wordy 404 is refused, not summarised.

    Found live in Chromium 2026-10-20: bbc.com/news/articles/<dead> returned 404 with 2825
    characters of real paragraphs and real navigation, so <main> extracted it as an article.
    A content script never sees the HTTP status, so the wording is the only signal.
    """
    result = extract({
        "url": "https://www.bbc.com/news/articles/c1e0xvz1yvzo", "host": "www.bbc.com",
        "body": {"children": [
            {"tag": "nav", "text": "Home News Sport Business Technology Health Culture"},
            {"tag": "main", "children": [
                {"tag": "h1", "text": "Error 404 - Oops, the page you're looking for is no longer here"},
                para("Try searching for it instead. We can't find the page you're looking for. " * 6),
            ]},
        ]},
    }, tmp_path)
    assert result["how"] == "missing"
    assert result["text"] == ""


def test_an_article_about_404_pages_is_still_read(tmp_path):
    """A real piece that discusses 404 errors is long, so the length ceiling keeps it."""
    body = [para("A good 404 page tells the reader the page is not found and offers a way back. " * 6)
            for _ in range(20)]
    result = extract({
        "url": "https://example.com/blog/designing-a-404-not-found-page", "host": "example.com",
        "body": {"children": [{"tag": "article", "children": body}]},
    }, tmp_path)
    assert result["how"] != "missing"
    assert "A good 404 page tells the reader" in result["text"]


def test_a_logged_out_linkedin_feed_is_refused(tmp_path):
    """Measured live 2026-10-20: linkedin.com/feed while logged out is a 376-character sign-in card."""
    result = extract({
        "url": "https://www.linkedin.com/feed/", "host": "www.linkedin.com",
        "body": {"children": [{"tag": "main", "text":
                               "0 notifications Sign in New to LinkedIn? Join now Sign in with Apple "
                               "or Email or phone Password Forgot password? Keep me signed in"}]},
    }, tmp_path)
    assert result["how"] == "login"
    assert result["text"] == ""


def test_a_logged_out_gmail_tab_is_refused_not_sent_as_page_text(tmp_path):
    """Logging out of Gmail lands you on Google's accounts sign-in card, not on mail.google.com.

    Measured live in Chromium 2026-10-20: mail.google.com and mail.google.com/mail/u/0/ both
    redirect to accounts.google.com/v3/signin/identifier, whose body was 128 characters ("Loading
    Sign in to continue to Gmail Email or phone Forgot email? Next Create account English (United
    States) Help Privacy Terms"). Neither host was in LOGIN_HOSTS, so the card was read as the page.
    """
    for host in ["accounts.google.com", "mail.google.com"]:
        result = extract({
            "url": f"https://{host}/v3/signin/identifier", "host": host,
            "body": {"children": [{"tag": "div", "text":
                                   "Loading Sign in to continue to Gmail Email or phone "
                                   "Forgot email? Next Create account English (United States) "
                                   "Help Privacy Terms"}]},
        }, tmp_path)
        assert result["how"] == "login", host
        assert result["text"] == "", host


def test_a_signed_in_gmail_message_is_read_without_the_folder_chrome(tmp_path):
    """A Gmail message must arrive as the mail, not as the mailbox furniture in front of it.

    Measured live 2026-10-24 on a signed-in mailbox: a message view has exactly one visible
    div.a3s (class "a3s aiL msg-<id>", 4,563 characters), the subject in h2.hP, the sender in
    .gD, and no table[role="grid"]. Both the folder list and the "Print all / In new window /
    Inbox / Search for all messages with label Inbox" toolbar live inside div[role="main"], so the
    generic <main> scan sent them ahead of the mail: 4,758 characters, how "main", note opened with
    "Print all". The message body is read from its own container now.
    """
    result = extract({
        "url": "https://mail.google.com/mail/u/0/#inbox/FMfcgzQhWxfQKjPtxSKhFJqJLvzRkchP",
        "host": "mail.google.com",
        "title": "RAG vs Agentic RAG - someone@gmail.com - Gmail",
        "body": {"children": [
            {"tag": "div", "text": "Skip to content Using Gmail with screen readers Search Try Gemini"},
            {"tag": "div", "text": "Compose Labels Inbox 30,463 Starred Snoozed Sent Drafts 18"},
            {"tag": "div", "attrs": {"role": "main"}, "children": [
                {"tag": "div", "text": "Print all\nIn new window"},
                {"tag": "h2", "attrs": {"class": "hP"}, "text": "RAG vs Agentic RAG"},
                {"tag": "div", "text": "Inbox Search for all messages with label Inbox"},
                {"tag": "span", "attrs": {"class": "gD"}, "text": "ByteByteGo"},
                {"tag": "div", "attrs": {"class": "a3s aiL msg-5230818929322642783"},
                 "text": "ChatGPT can now react to your product's events.\n\n"
                         "ChatGPT can now subscribe to events from your MCP server."},
            ]},
        ]},
    }, tmp_path)
    assert result["how"] == "mail", result["text"][:200]
    # the mail itself, and the two header lines that say what it is
    assert "MCP server" in result["text"]
    assert "RAG vs Agentic RAG" in result["text"]
    assert "ByteByteGo" in result["text"]
    # and none of the furniture that used to open the note
    for furniture in ["Print all", "In new window", "Search for all messages",
                      "Compose", "Starred", "Snoozed", "Try Gemini", "Skip to content"]:
        assert furniture not in result["text"], furniture
    # the subject comes before the body, so a note built from it opens with what the mail is
    assert result["text"].index("RAG vs Agentic RAG") < result["text"].index("MCP server")


def test_a_signed_in_gmail_message_list_is_read_without_the_folder_chrome(tmp_path):
    """The other Gmail page behind the same host: the list, with no message open.

    Measured live 2026-10-24: the list view has 0 div.a3s nodes and one table[role="grid"] holding
    50 tr.zA rows, and the folder chrome is outside that grid. The generic scan read <main>, which
    put "1–50 of 32,321 / Conversations / Primary / Promotions" at the top of the note.
    """
    rows = [{"tag": "tr", "attrs": {"class": "zA zE"},
             "text": f"Sender {i}\nSubject line {i}\n- The first line of message {i}"}
            for i in range(6)]
    result = extract({
        "url": "https://mail.google.com/mail/u/0/#inbox", "host": "mail.google.com",
        "body": {"children": [
            {"tag": "div", "text": "Compose Labels Inbox 30,463 Starred Snoozed Sent Drafts"},
            {"tag": "div", "attrs": {"role": "main"}, "children": [
                {"tag": "div", "text": "1–50 of 32,321 Conversations"},
                {"tag": "table", "attrs": {"role": "grid"}, "children": rows},
            ]},
        ]},
    }, tmp_path)
    assert result["how"] == "mail", result["text"][:200]
    assert "Subject line 0" in result["text"]
    for furniture in ["1–50 of 32,321", "Conversations", "Compose", "Starred"]:
        assert furniture not in result["text"], furniture


def test_a_gmail_message_that_is_still_loading_falls_back_instead_of_sending_nothing(tmp_path):
    """An empty Gmail shell must not become a "mail" answer with no text in it.

    The mail reader runs before the generic scan, so a mailbox page whose containers have not
    mounted yet has to fall through to the rest of the ladder rather than returning "".
    """
    result = extract({
        "url": "https://mail.google.com/mail/u/0/#inbox", "host": "mail.google.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"role": "main"}, "children": [
                para("The mailbox is still loading, and this sentence stands in for whatever the "
                     "page will show once it has mounted its own containers. " * 3)]},
        ]},
    }, tmp_path)
    # "main", not "mail": the mail reader found no container, so the ladder carried on. Any answer
    # with real text is fine here; the failure this guards against is how "mail" with empty text.
    assert result["how"] != "mail", result["how"]
    assert result["text"].strip()
    assert "still loading" in result["text"]


def test_a_real_gmail_mailbox_is_not_refused_as_a_sign_in_wall(tmp_path):
    """The 1500-character wall ceiling must keep a signed-in mailbox out of the refusal.

    No signed-in session was available in this run, so this page stands in for one rather than
    being measured: 120 message rows plus the folder list, far over the ceiling.
    """
    rows = [{"tag": "tr", "text": f"Sender {i} — Subject line {i} — first line of the message body"}
            for i in range(120)]
    result = extract({
        "url": "https://mail.google.com/mail/u/0/#inbox", "host": "mail.google.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"role": "main"}, "children": [
                {"tag": "table", "children": rows},
            ]},
        ]},
    }, tmp_path)
    assert result["how"] != "login"
    assert "Subject line 0" in result["text"]


def test_a_short_x_post_that_mentions_signing_in_is_still_read(tmp_path):
    """The login guard must not refuse a real post just because it is short and says "log in".

    The container check is what separates them: a signed-in page carries the post element, a wall
    does not.
    """
    result = extract({
        "url": "https://x.com/someone/status/2", "host": "x.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"data-testid": "tweetText"},
             "text": "Reminder: you have to log in to that dashboard before it works."},
        ]},
    }, tmp_path)
    assert result["how"] == "post"
    assert result["text"].startswith("Reminder: you have to log in")


def test_the_popup_says_why_a_page_was_refused():
    """A refusal must be named, not reported as an empty page.

    extract.js returns how "login"/"missing"/"wall" with empty text. If the popup only said "found
    no text", a user on a logged-out Facebook tab would keep pressing the button.
    """
    client = (EXT / "client.js").read_text(encoding="utf-8")
    for verdict, phrase in [("login", "sign-in wall"), ("missing", "does not exist"),
                            ("wall", "bot check")]:
        assert f'{verdict}:' in client, verdict
        assert phrase in client, phrase
    # a successful send names what was read, so "Sent video" does not say "Sent main"
    for kind in ["selection", "post", "video", "main"]:
        assert f'{kind}:' in client, kind


def test_a_youtube_page_whose_comments_have_not_loaded_asks_for_a_second_pass(tmp_path):
    """YouTube mounts comments only as they approach the viewport.

    Measured live 2026-10-20: a fully loaded watch page had 0 comment renderers, and 20 once the
    comments section was scrolled to. The first pass therefore asks for a second one, and carries
    the tab's own scroll position so it can be put back.
    """
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"A description long enough to be read."}'},
            {"tag": "ytd-comments", "children": [{"tag": "div", "text": "Comments loading"}]},
        ]},
    }, tmp_path)
    assert result["how"] == "video"
    assert result["revealComments"] is True
    assert result["scrollY"] == 0  # a real 0, not a missing field


def test_a_youtube_page_that_already_has_comments_does_not_ask_for_a_second_pass(tmp_path):
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"A description long enough to be read."}'},
            {"tag": "ytd-comments", "children": [
                {"tag": "ytd-comment-thread-renderer", "children": [
                    {"tag": "yt-element", "attrs": {"id": "content-text"},
                     "text": "A real comment that had already loaded."}]}]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is False
    assert result["scrollY"] is None


def test_a_second_pass_is_never_requested_from_itself(tmp_path):
    """Pass two must not ask for pass three, or the popup would loop."""
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "options": {"restoreScrollTo": 0},
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"A description long enough to be read."}'},
            {"tag": "ytd-comments", "children": [{"tag": "div", "text": "Comments loading"}]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is False


def test_a_page_that_is_not_youtube_never_asks_for_the_reveal(tmp_path):
    result = extract({
        "url": "https://www.reddit.com/r/x/comments/a/b/", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "shreddit-post", "children": [
                {"tag": "div", "attrs": {"slot": "text-body"}, "text": "Post body here."}]},
            {"tag": "shreddit-comment", "children": [
                {"tag": "div", "attrs": {"slot": "comment"}, "text": "A reply."}]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is False


ORCHESTRATION = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const extractSrc = readFileSync(`${dir}/extract.js`, "utf8");

const page = { scrollY: 0, revealed: false, scrolledTo: null };
page.world = {
  location: { href: "https://www.youtube.com/watch?v=abc", hostname: "www.youtube.com" },
  document: {
    title: "A video",
    body: { innerText: "watch page" },
    documentElement: { scrollHeight: 4000 },
    querySelectorAll: (sel) => {
      if (sel.includes("ytd-comment-thread-renderer")) {
        return page.revealed
          ? [{ innerText: "A top comment.", getBoundingClientRect: () => ({ width: 9, height: 9 }) }]
          : [];
      }
      // A laid-out container, the way a real watch page has one. A page that only carries the
      // 0x0 stub is a different test (see the browse-page test below).
      if (sel.includes("ytd-comments") || sel.includes("#comments")) {
        return [{ getBoundingClientRect: () => ({ width: 800, height: 500 }) }];
      }
      if (sel.includes("script")) {
        return [{ textContent: '{"shortDescription":"The description of the video."}' }];
      }
      return [];
    },
    querySelector: () => null,
  },
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
  scrollTo: (x, y) => { page.scrolledTo = y; page.scrollY = y; },
};

const calls = [];
const sent = [];
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 7, url: "https://www.youtube.com/@channel/featured" }] },
  scripting: {
    executeScript: async ({ files, func, args }) => {
      calls.push(files ? "inject"
        : String(func).includes("scrollIntoView") ? "reveal"
        : String(func).includes("ytd-comment-thread-renderer") ? "poll"
        : "extract");
      if (files) {
        globalThis.location = page.world.location;
        globalThis.document = page.world.document;
        globalThis.window = page.world;
        page.world.dictatorExtract = new Function(`${extractSrc}; return dictatorExtract;`)();
        return [{ result: null }];
      }
      if (String(func).includes("scrollIntoView")) {
        page.revealed = true;
        page.scrollY = 3000;
        return [{ result: null }];
      }
      if (String(func).includes("ytd-comment-thread-renderer")) {
        return [{ result: page.revealed }];
      }
      const opts = { preferSelection: args[0] };
      if (args.length > 1) opts.restoreScrollTo = args[1];
      return [{ result: page.world.dictatorExtract(opts) }];
    },
  },
};
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 1 }) };
};
// A dynamic import needs a file:// URL on Windows: an absolute "D:\..." path is refused by the
// default ESM loader. Found in CI on windows-latest.
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, calls, text: sent[0] ? sent[0].text : "", leftAt: page.scrolledTo }));
"""


def test_the_popup_reveals_the_comments_then_reads_again_and_restores_the_scroll(tmp_path):
    """The shipped client.js, executed against a stubbed chrome API, not read.

    A second pass that forgot to hand the saved position back would leave the reader's tab
    scrolled down the page; a first pass that ignored revealComments would send the description
    alone. Both are caught here by running the real file.
    """
    script = tmp_path / "orchestration.mjs"
    script.write_text(ORCHESTRATION, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    out = json.loads(run.stdout)
    assert out["calls"] == ["inject", "extract", "reveal", "poll", "extract"], out["calls"]
    assert out["result"]["ok"] is True
    assert out["result"]["message"] == "Sent the video to Listen."
    assert "The description of the video." in out["text"]
    assert "Comments:" in out["text"] and "A top comment." in out["text"]
    assert out["leftAt"] == 0, "the tab must be put back where the reader had it"


def test_the_reveal_targets_the_visible_comments_not_the_hidden_panel_copy(tmp_path):
    """At a narrow window the document's first ytd-comments is a hidden panel copy at the top.

    Measured live 2026-10-20 at 720x588: scrolling to `ytd-comments` scrolled nothing (its chain is
    inside #panels, 0 px tall), so the comments never mounted. #below's copy is the real one.
    """
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"id": "panels"}, "children": [
                {"tag": "ytd-comments", "hidden": True, "text": ""}]},
            {"tag": "div", "attrs": {"id": "below"}, "children": [
                {"tag": "ytd-comments", "children": [{"tag": "div", "text": "Comments loading"}]}]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is True

    client = (EXT / "client.js").read_text(encoding="utf-8")
    assert "#below #comments" in client, "the reveal must aim at the visible comments container"
    # and a container that refuses to scroll still gets the window moved as a fallback
    assert "documentElement.scrollHeight" in client


def test_a_mobile_youtube_page_reads_its_own_comment_markup(tmp_path):
    """Opera GX is served m.youtube.com, whose tags are not the desktop ones.

    Measured live 2026-10-20 on m.youtube.com: comments are ytm-comment-renderer with the text in
    .YtmCommentRendererContent, and they only exist after the reader opens the comments sheet. The
    extractor reads them when they are there and never opens the sheet itself.
    """
    result = extract({
        "url": "https://m.youtube.com/watch?v=abc", "host": "m.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"The description of the video."}'},
            {"tag": "ytm-comment-renderer", "children": [
                {"tag": "button", "attrs": {"class": "YtmCommentRendererContent"},
                 "text": "A mobile comment about the video that long enough to read."}]},
        ]},
    }, tmp_path)
    assert result["how"] == "video"
    assert "The description of the video." in result["text"]
    assert "Comments:" in result["text"]
    assert "A mobile comment about the video" in result["text"]


def test_a_mobile_youtube_page_with_the_sheet_closed_reads_only_the_description(tmp_path):
    """With the comments sheet closed there are no comment nodes, and nothing is invented."""
    result = extract({
        "url": "https://m.youtube.com/watch?v=abc", "host": "m.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"The description of the video."}'},
        ]},
    }, tmp_path)
    assert result["how"] == "video"
    assert "Comments:" not in result["text"]

def test_a_mobile_youtube_page_asks_for_the_comments_sheet_to_be_tapped(tmp_path):
    """Regression, measured live 2026-10-24 on m.youtube.com: the page has no comments section at
    all, so scrolling mounted nothing (0 renderers after scrolling to the bottom and waiting 6 s).
    The teaser has to be tapped, and the extractor has to ask for that instead of quietly sending
    the description alone — 2,362 characters against 3,862 with the conversation.
    """
    result = extract({
        "url": "https://m.youtube.com/watch?v=abc", "host": "m.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"The description of the video."}'},
            {"tag": "yt-video-metadata-carousel-view-model", "attrs": {"role": "button"},
             "text": "Comments2.4M"},
        ]},
    }, tmp_path)
    assert result["revealComments"] is True
    assert result["revealMode"] == "tap", result.get("revealMode")
    # the position still travels, so the reader's tab is left where they had it
    assert result["scrollY"] == 0


def test_the_recommended_videos_heading_is_not_mistaken_for_the_teaser(tmp_path):
    """The mobile page's own carousel heading reads "Comments" too; only the teaser's tag counts.

    The teaser is identified by tag so a page whose only "Comments" text is the recommended list is
    not reported as a page that can be tapped, which would send the popup looking for nothing.
    """
    result = extract({
        "url": "https://m.youtube.com/watch?v=abc", "host": "m.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"The description of the video."}'},
            {"tag": "h2", "attrs": {"class": "ytCarouselTitleViewModelTitle"}, "text": "Comments"},
        ]},
    }, tmp_path)
    assert result["revealComments"] is False
    assert result["revealMode"] == ""


def test_the_desktop_page_still_asks_for_a_scroll_not_a_tap(tmp_path):
    """Two different reveals, and the page that already has a comments section keeps the scroll."""
    result = extract({
        "url": "https://www.youtube.com/watch?v=abc", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"A description long enough to be read."}'},
            {"tag": "ytd-comments", "children": [{"tag": "div", "text": "Comments loading"}]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is True
    assert result["revealMode"] == "scroll", result.get("revealMode")


def test_a_youtube_page_that_is_not_a_watch_page_never_asks_for_a_reveal(tmp_path):
    """Regression, measured live 2026-10-24 on the home page, search results and history.

    Every non-watch YouTube page carries a 0x0 ytd-comments stub inside #below. Counting matches
    meant the popup scrolled the tab (4517 px on the home page), waited, and sent a second read
    that returned different text — 2417 characters on the pass the reader saw against 4485 after
    the scroll. A laid-out container is the watch-page signal; a stub is not.
    """
    result = extract({
        "url": "https://www.youtube.com/feed/history", "host": "www.youtube.com",
        "body": {"children": [
            {"tag": "ytd-comments", "hidden": True, "text": ""},
            {"tag": "main", "children": [para("A list of videos you watched, longest first. " * 20)]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is False, "a browse page must not be scrolled for comments"
    assert result["revealMode"] == ""
    assert result["scrollY"] is None


def test_a_mobile_page_that_already_has_comments_asks_for_nothing(tmp_path):
    """The sheet is already open (the reader opened it); reading it must not re-open or close it."""
    result = extract({
        "url": "https://m.youtube.com/watch?v=abc", "host": "m.youtube.com",
        "body": {"children": [
            {"tag": "script", "text": '{"shortDescription":"The description of the video."}'},
            {"tag": "yt-video-metadata-carousel-view-model", "attrs": {"role": "button"},
             "text": "Comments2.4M"},
            {"tag": "ytm-comment-renderer", "children": [
                {"tag": "button", "attrs": {"class": "YtmCommentRendererContent"},
                 "text": "A mobile comment the reader had already opened."}]},
        ]},
    }, tmp_path)
    assert result["revealComments"] is False
    assert "A mobile comment the reader had already opened." in result["text"]


GMAIL_SEND = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const extractSrc = readFileSync(`${dir}/extract.js`, "utf8");

// The measured message view, 2026-10-24: one div.a3s, subject in h2.hP, sender in .gD, and the
// folder chrome inside div[role="main"] alongside them. The real extract.js runs against it.
function el(spec) {
  const node = {
    tag: spec.tag || "div", attrs: spec.attrs || {}, text: spec.text || "",
    children: (spec.children || []).map(el),
    innerText: spec.text || "",
    getBoundingClientRect: () => ({ width: 500, height: 200 }),
  };
  if (node.children.length) node.innerText = node.children.map((c) => c.innerText).join("\n");
  return node;
}
const tree = el({"children": [
  {"tag": "div", "text": "Compose Labels Inbox 30,463 Starred Snoozed Sent Drafts"},
  {"attrs": {"role": "main"}, "children": [
    {"text": "Print all\nIn new window"},
    {"tag": "h2", "attrs": {"class": "hP"}, "text": "RAG vs Agentic RAG"},
    {"text": "Inbox Search for all messages with label Inbox"},
    {"tag": "span", "attrs": {"class": "gD"}, "text": "ByteByteGo"},
    {"attrs": {"class": "a3s aiL msg-1"}, "text": "The whole point of the message."},
  ]},
]});
const all = [];
(function walk(n) { for (const c of n.children) { all.push(c); walk(c); } })(tree);
function matches(node, sel) {
  return sel.split(",").map((s) => s.trim()).some((s) => {
    const body = s.replace(/([#.\[])/g, " $1").trim();
    const bits = s.match(/([a-z0-9]+)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/gi) || [];
    let ok = true; let sawTag = false;
    for (const bit of bits) {
      if (bit.startsWith(".")) { if (!String(node.attrs.class || "").split(/\s+/).includes(bit.slice(1))) ok = false; }
      else if (bit.startsWith("#")) { if (node.attrs.id !== bit.slice(1)) ok = false; }
      else if (bit.startsWith("[")) { const m = bit.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
        if (!m || node.attrs[m[1]] === undefined) ok = false; else if (m[2] && node.attrs[m[1]] !== m[2]) ok = false; }
      else if (!sawTag && bit.toLowerCase() === node.tag.toLowerCase()) { sawTag = true; }
      else if (sawTag === false && /^[a-z0-9]+$/i.test(bit) && bit.toLowerCase() !== node.tag.toLowerCase()) ok = false;
    }
    return ok;
  });
}
globalThis.location = { href: "https://mail.google.com/mail/u/0/#inbox/FMfc", hostname: "mail.google.com" };
globalThis.document = {
  title: "RAG vs Agentic RAG - someone@gmail.com - Gmail",
  body: tree, activeElement: null,
  querySelectorAll: (sel) => all.filter((n) => matches(n, sel)),
};
globalThis.window = {
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
};
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 21, url: "https://mail.google.com/mail/u/0/#inbox/FMfc" }] },
  scripting: {
    executeScript: async ({ files }) => {
      if (files) {
        globalThis.dictatorExtract = new Function(`${extractSrc}; return dictatorExtract;`)();
        return [{ result: null }];
      }
      return [{ result: globalThis.dictatorExtract({}) }];
    },
  },
};
const sent = [];
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 40 }) };
};
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, text: sent[0] ? sent[0].text : "" }));
"""


def test_the_popup_calls_a_gmail_message_an_email_not_the_post(tmp_path):
    """The one place "how" reaches the user is the popup's own wording.

    A mail read that reported "Sent the post to Listen." would be describing an email as a social
    post. This runs the shipped client.js and extract.js against the measured message view.
    """
    script = tmp_path / "gmail.mjs"
    script.write_text(GMAIL_SEND, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    out = json.loads(run.stdout)
    assert out["result"]["ok"] is True, out["result"]
    assert out["result"]["message"] == "Sent the email to Listen.", out["result"]["message"]
    assert "The whole point of the message." in out["text"]
    assert "Print all" not in out["text"]


MOBILE_TAP = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const extractSrc = readFileSync(`${dir}/extract.js`, "utf8");

// The measured mobile page, 2026-10-24: no comments until the teaser is tapped, 20 renderers after
// it, and a close button on the sheet that puts the page back. Every dispatch is recorded so the
// test can prove a pointer sequence was sent rather than a bare click.
const page = { open: false, closed: false, events: [], scrollY: 0 };
function renderer(n) {
  return { innerText: `Mobile comment number ${n} about the video.`,
           getBoundingClientRect: () => ({ width: 300, height: 40, left: 10, top: 200 }) };
}
function makeDocument() {
  const teaser = {
    innerText: "Comments2.4M", tagName: "YT-VIDEO-METADATA-CAROUSEL-VIEW-MODEL",
    getBoundingClientRect: () => ({ width: 358, height: 92, left: 8, top: 300 }),
    getAttribute: () => "button",
    dispatchEvent(ev) { page.events.push(ev.type); page.open = true; return true; },
  };
  const close = {
    innerText: "", tagName: "BUTTON",
    getAttribute: (name) => (name === "aria-label" ? "Close" : null),
    getBoundingClientRect: () => ({ width: 48, height: 48, left: 330, top: 20 }),
    dispatchEvent(ev) { page.events.push(`close:${ev.type}`); page.closed = true; return true; },
  };
  return {
    title: "A mobile video",
    body: { innerText: "watch page" },
    documentElement: { scrollHeight: 4000 },
    querySelectorAll: (sel) => {
      if (sel.includes("ytm-comment-renderer")) {
        return page.open ? [1, 2, 3].map(renderer) : [];
      }
      if (sel.includes("yt-video-metadata-carousel-view-model")) return page.open ? [] : [teaser];
      if (sel === "button, [role='button']") return [close];
      if (sel.includes("script")) {
        return [{ textContent: '{"shortDescription":"The description of the video."}' }];
      }
      return [];
    },
    querySelector: (sel) => (sel.includes("yt-video-metadata-carousel-view-model") ? teaser : null),
    activeElement: null,
  };
}
const calls = [];
const sent = [];
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 21, url: "https://m.youtube.com/@channel/featured" }] },
  scripting: {
    executeScript: async ({ files, func, args }) => {
      const body = String(func);
      calls.push(files ? "inject"
        : body.includes("yt-video-metadata-carousel-view-model") ? "reveal"
        : body.includes("aria-label") ? "close"
        : body.includes("ytm-comment-renderer") ? "poll"
        : "extract");
      if (files) {
        globalThis.location = { href: "https://m.youtube.com/@channel/featured", hostname: "m.youtube.com" };
        globalThis.document = makeDocument();
        globalThis.window = {
          getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
          getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
          scrollY: page.scrollY, scrollTo: () => {},
        };
        globalThis.dictatorExtract = new Function(`${extractSrc}; return dictatorExtract;`)();
        return [{ result: null }];
      }
      if (body.includes("yt-video-metadata-carousel-view-model")) {
        globalThis.document.querySelector("yt-video-metadata-carousel-view-model").dispatchEvent({ type: "pointerdown" });
        return [{ result: null }];
      }
      if (body.includes("aria-label")) {
        globalThis.document.querySelectorAll("button, [role='button']")[0].dispatchEvent({ type: "click" });
        return [{ result: true }];
      }
      if (body.includes("ytm-comment-renderer")) {
        return [{ result: page.open }];
      }
      const opts = { preferSelection: args[0] };
      if (args.length > 1) opts.restoreScrollTo = args[1];
      return [{ result: globalThis.dictatorExtract(opts) }];
    },
  },
};
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 1 }) };
};
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, calls, events: page.events, closed: page.closed,
                             open: page.open, text: sent[0] ? sent[0].text : "" }));
"""


def test_the_popup_taps_the_mobile_teaser_then_closes_the_sheet_it_opened(tmp_path):
    """The shipped client.js on the mobile page: tap, wait, read, and put the page back.

    Measured live 2026-10-24: a bare MouseEvent("click") is ignored by the teaser's handler, so the
    tap is a pointerdown/pointerup/click sequence. The sheet the popup opened must be closed again,
    or the reader is left looking at comments they never asked to open.
    """
    script = tmp_path / "mobile_tap.mjs"
    script.write_text(MOBILE_TAP, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    out = json.loads(run.stdout)
    assert out["result"]["ok"] is True, out["result"]
    assert out["calls"] == ["inject", "extract", "reveal", "poll", "extract", "close"], out["calls"]
    assert out["events"] == ["pointerdown", "close:click"], out["events"]
    assert out["closed"] is True, "the popup must close the sheet it opened"
    assert "The description of the video." in out["text"]
    assert "Mobile comment number 1" in out["text"]
    assert "Comments:" in out["text"]


def test_the_desktop_path_never_sends_a_tap_or_a_close(tmp_path):
    """A page with a comments section is scrolled, not tapped, and nothing is closed on it."""
    source = (EXT / "client.js").read_text(encoding="utf-8")
    assert 'page.revealMode === "tap"' in source, "the caller must branch on the page's own answer"
    assert "if (tapped)" in source, "closing the sheet must be conditional on the tap path"


# ----- round 27: the popup's own send path, against a real bridge ------------
# These run the shipped client.js in Node, over real HTTP, against the app's real bridge code on an
# ephemeral port. Only the port number is remapped in the harness (the extension ships a fixed
# 127.0.0.1:8765); everything else — the JSON body, the pairing header, the byte and character caps
# and their status codes — is the production path.
LIMITS = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const realPort = process.argv[3];
const fixture = JSON.parse(readFileSync(process.argv[4], "utf8"));

globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": fixture.token }), set: async () => {} } },
  tabs: { query: async () => [{ id: 3, url: "https://example.com/long" }] },
  scripting: {
    executeScript: async ({ files, func, args }) => {
      if (files) {
        globalThis.location = { href: "https://example.com/long", hostname: "example.com" };
        // A real article of LONG_TEXT characters, so the trim to the cap is exercised rather than a
        // page that reads empty.
        const article = {
          innerText: "word ".repeat(fixture.long),
          getBoundingClientRect: () => ({ width: 800, height: 4000 }),
        };
        const body = { innerText: article.innerText, textContent: article.innerText };
        globalThis.document = {
          title: "A long page", body, activeElement: null,
          querySelectorAll: (sel) => {
            if (sel.includes("script")) return [];
            if (sel === "article") return [article];
            return [];
          },
        };
        globalThis.window = {
          getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
          getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
        };
        globalThis.dictatorExtract = new Function(`${readFileSync(`${dir}/extract.js`, "utf8")}; return dictatorExtract;`)();
        return [{ result: null }];
      }
      return [{ result: globalThis.dictatorExtract({ preferSelection: args ? args[0] : true }) }];
    },
  },
};
// The one stub: the extension ships a fixed port. Everything else below is the real socket.
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) =>
  realFetch(String(url).replace("127.0.0.1:8765", `127.0.0.1:${realPort}`), init);

const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const out = {};
// 1. exactly the documented cap, through send() — the path "Send this page" uses
out.atCap = await client.send({ kind: "text", text: "a".repeat(fixture.cap), url: "https://example.com/long" });
// 2. one character more, which the bridge must refuse with its 400
out.overCap = await client.send({ kind: "text", text: "a".repeat(fixture.cap + 1), url: "https://example.com/long" });
// 3. the same cap in emoji, which JSON escapes to about 720 KB: the worst case the byte cap is
//    sized for, and the one a 200 KB cap refused before round 26
out.emojiCap = await client.send({ kind: "text", text: "\u{1F600}".repeat(fixture.cap) });
// 4. a page far over the cap: extract.js trims to the cap, so the send itself succeeds
out.sendPage = await client.sendPage("page");
console.log(JSON.stringify(out));
"""


def _bridge_for_limits():
    """A real bridge on its own port, with the payloads it received."""
    import socket

    from listen_app import bridge as py_bridge

    received: list[dict] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = "b" * 48
    live = py_bridge.Bridge(received.append, token=token, port=port)
    assert live.start(), live.error
    return live, received, port, token


def test_the_send_path_accepts_exactly_the_cap_and_the_bridge_refuses_one_more(tmp_path):
    """Round 26 raised the bridge's byte cap so 60,000 characters arrive. This proves the popup's
    own send() path, over a real socket, hits both sides of that limit: 60,000 accepted with a
    character count in the message, 60,001 refused with the bridge's own refusal.
    """
    from listen_app.bridge import MAX_TEXT

    live, received, port, token = _bridge_for_limits()
    try:
        script = tmp_path / "limits.mjs"
        script.write_text(LIMITS, encoding="utf-8")
        fixture = tmp_path / "limits.json"
        fixture.write_text(json.dumps({"token": token, "cap": MAX_TEXT, "long": 30_000}),
                           encoding="utf-8")
        run = subprocess.run([node(), str(script), str(EXT), str(port), str(fixture)],
                             capture_output=True, text=True, encoding="utf-8", timeout=90)
        assert run.returncode == 0, run.stderr[:600]
        out = json.loads(run.stdout)
    finally:
        live.stop()

    at_cap = out["atCap"]
    assert at_cap["ok"] is True, at_cap
    assert "60,000 characters" in at_cap["message"], at_cap["message"]
    assert len(received[0]["text"]) == MAX_TEXT

    over = out["overCap"]
    assert over["ok"] is False, over
    # the refusal the bridge itself wrote, relayed by the popup: not a client-side guess
    assert over["message"] == "text is longer than 60000 characters", over["message"]

    # a page far over the cap is trimmed by extract.js, so it still sends and arrives whole
    assert out["sendPage"]["ok"] is True, out["sendPage"]
    assert "trimmed to 60,000 characters" in out["sendPage"]["message"], out["sendPage"]["message"]

    # The worst case the byte cap exists for: 60,000 emoji is about 720 KB once JSON escapes it,
    # and it must be accepted end to end rather than refused by a 200 KB byte cap.
    emoji = out["emojiCap"]
    assert emoji["ok"] is True, emoji
    assert len(received[1]["text"]) == MAX_TEXT
    assert received[1]["text"][0] == "\U0001F600"

    # Three deliveries, in order: at-cap text, at-cap emoji, trimmed page. The over-cap send was
    # refused at the bridge, so it is not among them, and no delivered text is 60,001 characters.
    assert len(received) == 3, [len(item.get("text") or "") for item in received]
    assert received[0]["text"] == "a" * MAX_TEXT
    assert len(received[2]["text"]) == MAX_TEXT
    assert all(len(item["text"]) != MAX_TEXT + 1 for item in received), "the refused send was delivered"



# ----- round 22: walls and missing pages the phrase list missed --------------
# Each of these was measured live in Chromium on 2026-10-20 and came back as readable page text
# before the fix. They are behaviour tests: the shipped extract.js runs against a fake DOM.


def test_reddits_prove_your_humanity_challenge_is_refused(tmp_path):
    """Regression, live 2026-10-20: reddit.com/r/python/comments/1/ was read as 186 characters.

    The old challenge guard allowed at most one paragraph, and Reddit's page carries two, so the
    phrase check was never reached. The page is refused as a wall, not sent as the article.
    """
    result = extract({
        "url": "https://www.reddit.com/r/python/comments/1/", "host": "www.reddit.com",
        "title": "Reddit - Please wait for verification",
        "body": {"children": [
            {"tag": "h1", "text": "Prove your humanity"},
            {"tag": "p", "text": "We're committed to safety and security. But not for bots. "
                                 "Please prove you're human to continue."},
            {"tag": "p", "text": "Reddit uses a security service to protect itself from "
                                 "online attacks."},
        ]},
    }, tmp_path)
    assert result["how"] == "wall", result["text"][:120]
    assert result["text"] == ""


def test_a_real_reddit_thread_that_mentions_the_challenge_wording_is_still_read(tmp_path):
    """The paragraph rule must not become a false positive: real prose is several full paragraphs."""
    body = [para("A long post about scraping, where the author explains that Reddit now shows a "
                 "page saying Prove your humanity before you can read a thread at all. " * 2)]
    body += [para("A second and third real paragraph of the post, each with its own argument "
                  "and enough words to count as prose on the page. " * 2) for _ in range(3)]
    result = extract({
        "url": "https://www.reddit.com/r/python/comments/x/y", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "div", "attrs": {"data-testid": "post-content"}, "children": body},
        ]},
    }, tmp_path)
    assert result["how"] == "post", result["how"]
    assert "A second and third real paragraph" in result["text"]


def test_googles_unusual_traffic_page_is_refused(tmp_path):
    """Regression, live 2026-10-20: google.com/search?q=... was read as 340 characters of the wall.

    No challenge or not-found phrase matched it, so the traffic check text was sent as page body.
    """
    result = extract({
        "url": "https://www.google.com/search?q=speech+synthesis", "host": "www.google.com",
        "body": {"children": [
            {"tag": "div", "text": "About this page"},
            {"tag": "p", "text": "Our systems have detected unusual traffic from your computer "
                                 "network. This page checks to see if it's really you sending the "
                                 "requests, and not a robot."},
            {"tag": "p", "text": "Why did this happen? This page appears when Google "
                                 "automatically detects requests coming from your computer "
                                 "network which appear to be in violation of the Terms of Service."},
        ]},
    }, tmp_path)
    assert result["how"] == "wall", result["text"][:120]
    assert result["text"] == ""


def test_a_google_docs_file_that_does_not_exist_is_refused(tmp_path):
    """Regression, live 2026-10-20: docs.google.com/document/d/<dead>/edit answered HTTP 404 with
    "Sorry, the file you have requested does not exist." and was read as 116 characters of page."""
    result = extract({
        "url": "https://docs.google.com/document/d/1/edit", "host": "docs.google.com",
        "title": "Google Docs",
        "body": {"children": [
            {"tag": "div", "text": "Sorry, the file you have requested does not exist."},
            {"tag": "p", "text": "Make sure that you have the correct URL and that the file exists."},
        ]},
    }, tmp_path)
    assert result["how"] == "missing", result["text"][:120]
    assert result["text"] == ""


NAVIGATION = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const mode = process.argv[3];   // "once" or "forever"
const extractSrc = readFileSync(`${dir}/extract.js`, "utf8");
const page = { boom: mode === "forever", attempts: 0 };
const MAIL = "Inbox: three messages, one from Sam about the deployment window. ";

// A page with a real <main>, because the extractor only accepts a region with 200+ characters.
class El {
  constructor(tag, text, children = []) {
    this.tag = tag; this.text = text || ""; this.children = children;
    for (const c of children) c.parent = this;
  }
  get innerText() { return this.text || this.children.map((c) => c.innerText).join("\n"); }
  get textContent() { return this.innerText; }
  getBoundingClientRect() { return { width: 100, height: 100 }; }
  querySelectorAll(sel) {
    const tags = sel.split(",").map((s) => s.trim().toLowerCase());
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (tags.includes(c.tag)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
}
function buildPage() {
  const main = new El("main", MAIL.repeat(6));
  const body = new El("body", "", [main]);
  return { body, main };
}
const dom = buildPage();
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 9, url: "https://outlook.live.com/mail/" }] },
  scripting: {
    executeScript: async ({ files, func }) => {
      // The inject step always succeeds; it is the evaluate that dies when the document swaps.
      if (files) {
        globalThis.location = { href: "https://outlook.live.com/mail/", hostname: "outlook.live.com" };
        globalThis.document = {
          title: "Mail",
          body: dom.body,
          activeElement: null,
          querySelectorAll: (s) => dom.body.querySelectorAll(s),
        };
        globalThis.window = {
          getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
          getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
        };
        globalThis.dictatorExtract = new Function(`${extractSrc}; return dictatorExtract;`)();
        return [{ result: null }];
      }
      // The readiness poll client.js makes before its retry. Measured on outlook.live.com/mail/
      // 2026-10-20: readyState was still "interactive" 1.5 s in and "complete" about 5 s in, so
      // the new document is modelled as settling on the third tick (400 ms each).
      if (String(func).includes("readyState")) {
        page.polls = (page.polls || 0) + 1;
        return [{ result: page.boom || page.polls >= 3 ? "complete" : "interactive" }];
      }
      page.attempts += 1;
      // Reading into a document that is still loading is what threw live. So the evaluate fails
      // until the document has settled: a caller that retries on a fixed delay, without waiting
      // for readyState, hits this every time and gets nothing.
      if (page.boom || (page.polls || 0) < 3) {
        throw new Error("Execution context was destroyed.");
      }
      return [{ result: globalThis.dictatorExtract({}) }];
    },
  },
};
const sent = [];
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 30 }) };
};
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, attempts: page.attempts, polls: page.polls || 0,
                              sent: sent.length, text: sent[0] ? sent[0].text : "" }));
"""


def _navigate(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "navigation.mjs"
    script.write_text(NAVIGATION, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT), mode], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    return json.loads(run.stdout)


def test_a_page_that_navigates_mid_read_is_read_on_one_retry(tmp_path):
    """Regression, live 2026-10-20: outlook.live.com/mail/ threw "Execution context was destroyed".

    That is the page replacing its own document, not a page we cannot read, so the client waits for
    the new document to finish loading, then reads once more and sends that.
    """
    out = _navigate(tmp_path, "once")
    assert out["result"]["ok"] is True, out["result"]
    assert out["attempts"] == 2, out["attempts"]
    assert "Inbox: three messages" in out["text"], out["text"][:80]
    assert out["sent"] == 1


def test_a_page_that_keeps_navigating_is_reported_as_a_redirect_not_retried_forever(tmp_path):
    """A stuck redirect is named as one, and the read is not attempted a third time."""
    out = _navigate(tmp_path, "forever")
    assert out["result"]["ok"] is False
    assert "sign-in" in out["result"]["message"]
    assert out["attempts"] == 2, out["attempts"]
    assert out["sent"] == 0



BLANK_PAGE = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const mode = process.argv[3];   // "inbox", "redirect" or "other"
const extractSrc = readFileSync(`${dir}/extract.js`, "utf8");

// Measured live 2026-10-21 on outlook.live.com/mail/: "complete" with an empty body at 750 ms,
// still empty at 2.5 s, the Microsoft 365 product page by 6 s. The page replaces itself with no
// thrown error, which is why the destroyed-context retry never fires on it.
const START = mode === "other" ? "https://example.com/report" : "https://outlook.live.com/mail/";
const AFTER = mode === "redirect"
  ? "https://www.microsoft.com/en-us/microsoft-365/outlook/email-and-calendar-software-microsoft-outlook"
  : START;
const FILL = mode === "redirect"
  ? "Microsoft 365 brings together your favourite apps in one place. ".repeat(8)
  : "Inbox: three messages, one from Sam about the deployment window. ".repeat(8);

const page = { filled: false, polls: 0, extracts: 0, url: START };
function mountDocument() {
  globalThis.document = {
    title: "Mail",
    body: { innerText: page.filled ? FILL : "", textContent: page.filled ? FILL : "" },
    activeElement: null,
    documentElement: { scrollHeight: 900 },
    querySelectorAll: () => [],
  };
}
mountDocument();
globalThis.location = { href: START, hostname: new URL(START).hostname };
globalThis.window = {
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
  scrollY: 0, scrollTo: () => {},
};
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 11, url: START }] },
  scripting: {
    executeScript: async ({ files, func }) => {
      if (files) {
        globalThis.dictatorExtract = new Function(`${extractSrc}; return dictatorExtract;`)();
        mountDocument();
        return [{ result: null }];
      }
      const body = String(func);
      // Complete the whole time it is blank: nothing throws, so a caller that only retries on a
      // thrown error never reaches a second read.
      if (body.includes("readyState")) return [{ result: "complete" }];
      if (body.includes("innerText")) {
        page.polls += 1;
        if (page.polls >= 2) { page.filled = true; page.url = AFTER; mountDocument(); }
        return [{ result: page.filled }];
      }
      page.extracts += 1;
      globalThis.location = { href: page.url, hostname: new URL(page.url).hostname };
      mountDocument();
      return [{ result: globalThis.dictatorExtract({}) }];
    },
  },
};
const sent = [];
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 40 }) };
};
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, extracts: page.extracts, polls: page.polls, sent: sent.length,
                             text: sent[0] ? sent[0].text : "" }));
"""


def _blank_page(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "blank.mjs"
    script.write_text(BLANK_PAGE, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT), mode], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    return json.loads(run.stdout)


def test_a_mailbox_page_that_reports_complete_while_blank_is_read_once_it_has_text(tmp_path):
    """Regression, measured live 2026-10-21: outlook.live.com/mail/ reported readyState
    "complete" with a 0-character body from 750 ms to 2.5 s. Nothing threw, so the
    destroyed-context retry never fired and the reader was told "Found no text on the page".
    The client now watches the body rather than readyState, and reads once more.
    """
    out = _blank_page(tmp_path, "inbox")
    assert out["result"]["ok"] is True, out["result"]
    assert "Inbox: three messages" in out["text"]
    assert out["extracts"] == 2, out["extracts"]
    assert out["sent"] == 1


def test_a_mailbox_that_redirects_to_the_product_page_is_named_not_sent(tmp_path):
    """After the blank window the same page becomes Microsoft 365's sales page. Sending that as
    mail would put marketing copy in a note, so it is reported instead of sent."""
    out = _blank_page(tmp_path, "redirect")
    assert out["result"]["ok"] is False, out["result"]
    assert "redirected" in out["result"]["message"].lower(), out["result"]["message"]
    assert out["sent"] == 0


def test_an_empty_page_on_any_other_host_is_not_read_twice(tmp_path):
    """The re-read is scoped to the hosts measured to go blank, so an ordinary empty page still
    says so immediately: one read, no polling, the same message as before."""
    out = _blank_page(tmp_path, "other")
    assert out["result"]["ok"] is False
    assert out["result"]["message"].startswith("Found no text on the page"), out["result"]
    assert out["extracts"] == 1, out["extracts"]
    assert out["polls"] == 0, out["polls"]


def test_reddits_network_security_block_is_refused_too(tmp_path):
    """The same URL served a different wall to this network on 2026-10-20.

    While the "Prove your humanity" page was being probed, reddit.com/r/python/comments/1/ answered
    143 characters of "You've been blocked by network security." with no paragraphs at all, which
    the old code read as page text. Both walls are refused now.
    """
    result = extract({
        "url": "https://www.reddit.com/r/python/comments/1/", "host": "www.reddit.com",
        "body": {"children": [
            {"tag": "div", "text": "You've been blocked by network security."},
            {"tag": "div", "text": "If you think you've been blocked by mistake, file a ticket "
                                   "below and we'll look into it."},
            {"tag": "a", "text": "File a ticket"},
        ]},
    }, tmp_path)
    assert result["how"] == "wall", result["text"][:120]
    assert result["text"] == ""


# ----- dictation ------------------------------------------------------------
def test_the_dictate_command_is_declared_and_sends_no_audio():
    """Alt+Shift+D asks the app to start/stop its own mic. The extension never records anything,
    which is why it still asks for no microphone permission."""
    commands = manifest()["commands"]
    assert "dictate" in commands, commands
    assert commands["dictate"]["suggested_key"]["default"] == "Alt+Shift+D"
    assert "microphone" not in manifest().get("permissions", [])
    client = (EXT / "client.js").read_text(encoding="utf-8")
    assert "/dictate" in client
    background = (EXT / "background.js").read_text(encoding="utf-8")
    assert "dictate" in background, "the worker must handle the command the manifest declares"


def test_every_declared_command_is_handled():
    """A command in the manifest with no listener is a shortcut that silently does nothing."""
    background = (EXT / "background.js").read_text(encoding="utf-8")
    for command in manifest()["commands"]:
        assert f'"{command}"' in background, f"{command} is declared but never handled"


# ----- Round 28: the blank window, part two, and the dictation hotkey wiring ---
BLANK_SWAP = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const extractSrc = readFileSync(`${dir}/extract.js`, "utf8");
const START = "https://outlook.live.com/mail/";
const FILL = "Inbox: three messages, one from Sam about the deployment window. ".repeat(8);

// Measured live 2026-10-21 on outlook.live.com/mail/: the page finished loading while blank and
// filled in without throwing. The sequence modelled here is the one R24 does not cover: the body
// poll sees text, and the very next read lands on a document that has swapped again (the mailbox
// re-rendering its shell), so the single re-read R24 allows comes back empty as well.
const page = { filled: false, polls: 0, extracts: 0 };
function mountDocument() {
  globalThis.document = {
    title: "Mail",
    body: { innerText: page.filled ? FILL : "", textContent: page.filled ? FILL : "" },
    activeElement: null,
    documentElement: { scrollHeight: 900 },
    querySelectorAll: () => [],
  };
}
mountDocument();
globalThis.location = { href: START, hostname: "outlook.live.com" };
globalThis.window = {
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  getSelection: () => ({ rangeCount: 0, isCollapsed: true, toString: () => "" }),
  scrollY: 0, scrollTo: () => {},
};
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 12, url: START }] },
  scripting: {
    executeScript: async ({ files, func }) => {
      if (files) {
        globalThis.dictatorExtract = new Function(`${extractSrc}; return dictatorExtract;`)();
        mountDocument();
        return [{ result: null }];
      }
      const body = String(func);
      if (body.includes("readyState")) return [{ result: "complete" }];
      if (body.includes("innerText")) {
        page.polls += 1;
        if (page.polls >= 2) { page.filled = true; mountDocument(); }
        return [{ result: page.filled }];
      }
      page.extracts += 1;
      if (page.extracts === 2) { page.filled = false; mountDocument(); }
      return [{ result: globalThis.dictatorExtract({}) }];
    },
  },
};
const sent = [];
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 40 }) };
};
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, extracts: page.extracts, polls: page.polls,
                             sent: sent.length, text: sent[0] ? sent[0].text : "" }));
"""


def _blank_swap(tmp_path: Path) -> dict:
    script = tmp_path / "blank_swap.mjs"
    script.write_text(BLANK_SWAP, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    return json.loads(run.stdout)


def test_a_mailbox_that_goes_blank_again_after_filling_is_still_read(tmp_path):
    """The blank window is not one round trip: the poll can see text and the read still land on a
    document that swapped again. One re-read then reports "Found no text" on a page that had text,
    so the client keeps polling inside the same budget until a read actually returns text.
    """
    out = _blank_swap(tmp_path)
    assert out["result"]["ok"] is True, out["result"]
    assert "Inbox: three messages" in out["text"], out["text"][:80]
    assert out["extracts"] == 3, out["extracts"]
    assert out["sent"] == 1


DICTATE_WIRING = r"""
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const mode = process.argv[3];           // "paired" or "unpaired"
const posts = [];
const badge = { text: null, title: null };
let listener = null;
const token = mode === "unpaired" ? "" : "abc123";

// Only what background.js and client.js actually touch: storage, the action badge, the command
// listener and fetch. No page, because a dictation toggle never reads one.
globalThis.chrome = {
  storage: { local: { get: async () => (token ? { "dictator.token": token } : {}), set: async () => {} } },
  action: {
    setBadgeText: async ({ text }) => { badge.text = text; },
    setBadgeBackgroundColor: async () => {},
    setTitle: async ({ title }) => { badge.title = title; },
  },
  commands: { onCommand: { addListener: (fn) => { listener = fn; } } },
};
globalThis.fetch = async (url, init) => {
  posts.push({ url: String(url), method: init.method, headers: init.headers, body: init.body });
  return { ok: true, status: 200, json: async () => ({ ok: true, recording: true }) };
};

// Importing the worker registers the listener the manifest's shortcut fires.
await import(`${pathToFileURL(`${dir}/background.js`).href}?t=${Date.now()}`);
if (!listener) throw new Error("background.js registered no command listener");
await listener("dictate");
const hotkey = posts.length ? posts[0] : null;

// The popup's Dictate button calls this same exported function; nothing else may POST /dictate.
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}`);
const button = await client.dictate();
const viaButton = posts.length > 1 ? posts[1] : null;

console.log(JSON.stringify({ hotkey, viaButton, button, badge, posts: posts.length }));
"""


def _dictate_wiring(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "dictate_wiring.mjs"
    script.write_text(DICTATE_WIRING, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT), mode], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    return json.loads(run.stdout)


def test_the_dictate_hotkey_sends_the_same_bridge_request_as_the_popup_button(tmp_path):
    """Alt+Shift+D must not be a second implementation of the dictation call.

    Executed, not read: the worker's listener is fired the way the manifest fires it, then the
    function the popup's Dictate button calls is called directly. Both requests have to match on
    URL, method, headers and body, or the shortcut and the button could drift apart.
    """
    out = _dictate_wiring(tmp_path, "paired")
    hotkey, button = out["hotkey"], out["viaButton"]
    assert hotkey and button, out
    assert hotkey["url"].endswith("/dictate"), hotkey["url"]
    assert hotkey == button, f"hotkey {hotkey} vs button {button}"
    assert out["posts"] == 2, out["posts"]
    # The badge is the only feedback the user gets once the popup has closed.
    assert out["badge"]["text"] == "REC", out["badge"]
    assert out["badge"]["title"] == "Listening in Listen. Press again to stop.", out["badge"]


def test_the_dictation_shortcut_reports_an_unpaired_app_instead_of_calling_it(tmp_path):
    """With no pairing code there is nothing to send, so no request is made at all."""
    out = _dictate_wiring(tmp_path, "unpaired")
    assert out["posts"] == 0, out["posts"]
    assert out["hotkey"] is None
    assert out["button"]["ok"] is False
    assert out["badge"]["text"] == "!", out["badge"]
    assert "Not paired" in out["badge"]["title"], out["badge"]


ENGINE_LABEL_WIRING = """
import { pathToFileURL } from "node:url";
const dir = process.argv[2];
const body = process.argv[3] === "with-engine"
  ? { ok: true, recording: false, engine: "cloud" }
  : { ok: true, recording: false };
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "abc123" }), set: async () => {} } },
};
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => body });
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
console.log(JSON.stringify(await client.dictate()));
"""


def _engine_label(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "engine_label.mjs"
    script.write_text(ENGINE_LABEL_WIRING, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT), mode], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    return json.loads(run.stdout)


def test_the_popup_names_the_engine_the_app_actually_used(tmp_path):
    """Local and cloud differ in privacy and in who is paid, so the popup must not stay silent.

    Executed against the shipped client.js with a stubbed bridge reply: the label comes from the
    app's own answer, and nothing is invented when the app does not name one.
    """
    named = _engine_label(tmp_path, "with-engine")
    assert named["ok"] is True
    assert named["engine"] == " (cloud)"
    assert "cloud" in named["message"], named["message"]

    silent = _engine_label(tmp_path, "no-engine")
    assert silent["engine"] == "", "no label is invented when the app sends none"
    assert "(" not in silent["message"], silent["message"]


def test_the_popup_dictate_button_routes_through_the_shared_client_call():
    """The popup must import the one dictation function, not POST to the bridge itself. Its own
    fetch would need its own token handling and could disagree with the hotkey's."""
    popup = (EXT / "popup.js").read_text(encoding="utf-8")
    assert "dictate" in popup.split('from "./client.js"')[0], "dictate must be imported from client.js"
    assert "await dictate()" in popup, popup[:400]
    assert "fetch(" not in popup, "the popup must never talk to the bridge on its own"


YOUTUBE_ROUTE = r"""
const { pathToFileURL } = await import("node:url");
const dir = process.argv[2];
const tabUrl = process.argv[3];
const sent = [];
let scraped = 0;
globalThis.chrome = {
  storage: { local: { get: async () => ({ "dictator.token": "t" }), set: async () => {} } },
  tabs: { query: async () => [{ id: 5, url: tabUrl, title: "A video" }] },
  scripting: {
    executeScript: async ({ files }) => {
      if (files) scraped += 1;
      return [{ result: { text: "description and comments", url: tabUrl, title: "A video", how: "post" } }];
    },
  },
};
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/ingest")) sent.push(JSON.parse(init.body));
  return { ok: true, status: 200, json: async () => ({ ok: true, chars: 1 }) };
};
const client = await import(`${pathToFileURL(`${dir}/client.js`).href}?t=${Date.now()}`);
const result = await client.sendPage("page");
console.log(JSON.stringify({ result, scraped, sent }));
"""


@pytest.mark.parametrize("tab_url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/shorts/abc",
])
def test_a_youtube_video_sends_its_link_so_the_full_transcript_is_fetched(tmp_path, tab_url):
    """Regression (PR #15): a video tab used to send the scraped description and comments as the content.

    The page is no longer scraped for a video. The link is sent, and Listen fetches the transcript from it.
    """
    script = tmp_path / "youtube_route.mjs"
    script.write_text(YOUTUBE_ROUTE, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT), tab_url], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    out = json.loads(run.stdout)
    assert out["scraped"] == 0, "a video page must not be scraped for its description and comments"
    assert out["sent"] == [{"kind": "url", "url": tab_url, "title": "A video"}]
    assert out["result"]["ok"] is True


def test_an_ordinary_page_is_still_read_from_the_page(tmp_path):
    script = tmp_path / "ordinary_route.mjs"
    script.write_text(YOUTUBE_ROUTE, encoding="utf-8")
    run = subprocess.run([node(), str(script), str(EXT), "https://example.com/article"],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr[:600]
    out = json.loads(run.stdout)
    assert out["scraped"] >= 1, "an ordinary page keeps the page-text route"
