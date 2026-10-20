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
      if (sel.includes("ytd-comments") || sel.includes("#comments")) return [{}];
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
  tabs: { query: async () => [{ id: 7, url: "https://www.youtube.com/watch?v=abc" }] },
  scripting: {
    executeScript: async ({ files, func, args }) => {
      calls.push(files ? "inject" : String(func).includes("scrollIntoView") ? "reveal" : "extract");
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
    assert out["calls"] == ["inject", "extract", "reveal", "extract"], out["calls"]
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
