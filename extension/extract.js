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
      if (!visible(el) || el === document.body) continue;
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

  function hostMatches(host, pattern) {
    return host === pattern || host.endsWith("." + pattern) || host.includes(pattern);
  }

  const host = location.hostname.toLowerCase();
  let text = preferSelection ? fromSelection() : "";
  let how = text ? "selection" : "";
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
