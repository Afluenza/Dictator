/* Read one page, post or comment out of the live DOM.
 *
 * Injected into the tab the user is looking at, only when they press the button, only through
 * chrome.scripting.executeScript with activeTab. It reads; it never writes to the page, never
 * listens to keystrokes, and never runs in the background.
 *
 * Order of preference, because a social post is not a whole page:
 *   1. the text the user selected, if there is one — a selected comment is what they meant;
 *   2. the article or main region, if the page declares one (news, blogs, Wikipedia);
 *   3. the site's own post container, for the hosts that wrap a post in something specific;
 *   4. the densest block of paragraph text on the page.
 * Site-specific selectors come before the generic block scan so a Reddit comment thread does not
 * lose the comment to the sidebar. Nothing here fetches: no XHR, no fetch(), no iframe reads.
 */
/* Injected as a classic script by chrome.scripting, so this declaration lands on the isolated
 * world's global object and the popup reads it back as globalThis.dictatorExtract. No export. */
function dictatorExtract(options) {
  // A non-simple parameter list cannot carry a "use strict" directive, so the default is set here.
  const preferSelection = !options || options.preferSelection !== false;

  // A hard ceiling, matching listen_app.bridge.MAX_TEXT. The bridge refuses more, so trimming
  // here turns a refusal into a page the reader actually gets.
  const LIMIT = 60000;

  const SOCIAL = [
    // X / Twitter
    ["x.com", '[data-testid="tweetText"]'],
    ["twitter.com", '[data-testid="tweetText"]'],
    // Reddit: the post body first, then the comment the reader opened
    ["reddit.com", '[data-testid="post-content"] , .Post header + div, [slot="text-body"]'],
    // LinkedIn, Mastodon, Bluesky, Threads
    ["linkedin.com", ".feed-shared-update-v2__description, .attributed-text-segment-list__content"],
    ["mastodon.", ".status__content, .entry__content"],
    ["bsky.app", '[data-testid="postText"]'],
    ["threads.net", 'div[dir="auto"] span'],
    // Q&A and forums
    ["stackoverflow.com", ".js-post-body"],
    ["stackexchange.com", ".js-post-body"],
    ["news.ycombinator.com", ".commtext, .toptext"],
    ["v2ex.com", ".topic_content, .reply_content"],
    // Wikipedia: the article lives in #mw-content-text; the language count and the
    // Article/Talk/Read/Edit tabs sit in the surrounding <main>, so reading <main> sends them too
    ["wikipedia.org", "#mw-content-text"],
    // Video and media pages
    ["youtube.com", "#description-inline-expander, ytd-text-inline-expander, #description"],
    ["bilibili.com", ".desc-info-text, .basic-desc-info"],
  ];

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const box = el.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) return false;
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
  }

  function tidy(text) {
    return (text || "")
      .replace(/\r\n?/g, "\n")
      .replace(/[ \t\u00a0]+/g, " ")
      .replace(/ *\n */g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  // The selection, if it is a real one. Reading it from the DOM element keeps it working after
  // the popup steals focus, which is why selectionText on its own is not enough.
  function fromSelection() {
    const selected = window.getSelection ? window.getSelection() : null;
    if (selected && selected.rangeCount && !selected.isCollapsed) {
      const text = tidy(selected.toString());
      if (text.length >= 2) return text;
    }
    const active = document.activeElement;
    if (active && typeof active.value === "string" && active.selectionEnd > active.selectionStart) {
      const text = tidy(active.value.slice(active.selectionStart, active.selectionEnd));
      if (text.length >= 2) return text;
    }
    return "";
  }

  function fromSelector(selector) {
    let best = "";
    for (const el of document.querySelectorAll(selector)) {
      if (!visible(el)) continue;
      const text = tidy(el.innerText);
      if (text.length > best.length) best = text;
    }
    return best;
  }

  function fromMain() {
    for (const selector of ["article", "main", '[role="main"]', "#content", "#main"]) {
      const text = fromSelector(selector);
      if (text.length >= 200) return text;
    }
    return "";
  }

  // The generic fallback: the container with the most real prose in it. Picking the single
  // longest paragraph was wrong on a comment thread, where one long comment beat the post, and
  // on an index page, where the longest paragraph is a teaser. Scoring containers by their
  // paragraph text, with a penalty for the share of it that is link text, is the small version
  // of what a readability pass does — and it needs no dependency.
  function fromBlocks() {
    const candidates = new Set();
    for (const p of document.querySelectorAll("p, li, blockquote")) {
      if (!visible(p)) continue;
      let node = p.parentElement;
      for (let depth = 0; node && depth < 3; depth += 1, node = node.parentElement) candidates.add(node);
    }
    let best = null;
    let bestScore = 0;
    for (const el of candidates) {
      // The document root and body are wrappers, never the content. Scoring them lets a page's
      // whole link directory tie the prose block, so the wrapper is skipped and the block wins.
      if (!visible(el) || el === document.body || el === document.documentElement) continue;
      const paragraphs = el.querySelectorAll("p, li, blockquote");
      let prose = 0;
      let linked = 0;
      for (const p of paragraphs) {
        if (!visible(p)) continue;
        const own = (p.textContent || "").trim().length;
        prose += own;
        for (const a of p.querySelectorAll("a")) linked += (a.textContent || "").trim().length;
      }
      if (prose < 200) continue;
      const score = prose * (1 - Math.min(0.9, linked / Math.max(1, prose)));
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    if (best) {
      const text = tidy(best.innerText);
      if (text.length >= 200) return text;
    }
    const body = tidy(document.body ? document.body.innerText : "");
    return body.length >= 40 ? body : "";
  }

  // Web components keep their text in an open shadow root, which innerText does not cross, so a
  // page built from them reads as empty. This runs only when every other path found nothing, so
  // ordinary pages are untouched. Closed shadow roots are not reachable and are not read.
  function fromOpenShadowRoots() {
    const parts = [];
    const walk = (node) => {
      for (const el of node.querySelectorAll("*")) {
        if (el.shadowRoot) {
          const text = tidy(el.shadowRoot.textContent || "");
          if (text.length >= 20) parts.push(text);
          walk(el.shadowRoot);
        }
      }
    };
    walk(document);
    return parts.join("\n\n");
  }

  function hostMatches(host, pattern) {
    return host === pattern || host.endsWith("." + pattern) || host.includes(pattern);
  }

  // A bot-check or challenge page carries its own text ("Performing security verification"),
  // which the generic paths would read as the article. Such a page is refused as "wall", so the
  // reader says it found no article instead of sending the challenge to Listen. A selection the
  // user made is never refused: the user chose that text.
  const CHALLENGE = [
    /performing security verification/i,
    /verify(ing)? you are (not )?(a )?human/i,
    /checking your browser before accessing/i,
    /just a moment\.\.\./i,
    /enable javascript and cookies to continue/i,
  ];

  // A challenge page is short: the phrase plus a few lines of boilerplate. An article that merely
  // mentions the phrase is long, so only a short page can be refused. The length ceiling is what
  // keeps real prose safe.
  const CHALLENGE_MAX_CHARS = 800;

  // Three facts must all hold: the page is short, it carries the challenge phrase, and it has at
  // most one paragraph. A challenge is one boilerplate block. An article has several paragraphs
  // of prose, so even a short article that quotes the phrase is never refused.
  function isChallengePage() {
    const all = tidy(document.body ? document.body.innerText : "");
    if (all.length > CHALLENGE_MAX_CHARS) return false;
    if (!CHALLENGE.some((pattern) => pattern.test(all))) return false;
    const paragraphs = Array.from(document.querySelectorAll("p")).filter((p) => (p.textContent || "").trim().length > 0);
    return paragraphs.length <= 1;
  }

  // A Wikipedia page for a title that does not exist carries #noarticletext. A real article never
  // does, so this is a clean, structural test rather than a phrase match.
  function isMissingPage() {
    return document.querySelectorAll("#noarticletext").length > 0;
  }

  const host = location.hostname.toLowerCase();
  let text = preferSelection ? fromSelection() : "";
  let how = text ? "selection" : "";
  if (!text && isChallengePage()) {
    return { text: "", url: location.href, title: (document.title || "").slice(0, 300), how: "wall", truncated: false };
  }
  if (!text && isMissingPage()) {
    return { text: "", url: location.href, title: (document.title || "").slice(0, 300), how: "missing", truncated: false };
  }
  if (!text) {
    for (const [pattern, selector] of SOCIAL) {
      if (!hostMatches(host, pattern)) continue;
      const site = fromSelector(selector);
      if (site.length >= 20) {
        text = site;
        how = "post";
        break;
      }
    }
  }
  if (!text) {
    text = fromMain();
    how = text ? "main" : "";
  }
  if (!text) {
    text = fromBlocks();
    how = text ? "page" : "";
  }
  if (!text) {
    text = fromOpenShadowRoots();
    how = text ? "shadow" : "";
  }

  const truncated = text.length > LIMIT;
  if (truncated) text = text.slice(0, LIMIT);

  return {
    text,
    // empty when nothing was read, so the caller says so instead of sending a blank page
    url: location.href,
    title: (document.title || "").slice(0, 300),
    how,
    truncated,
  };
}
