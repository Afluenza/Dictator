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

  // Second half of the comment reveal: the popup passes back the position it saved, so the tab is
  // left exactly where the reader found it. Always run, whatever the extraction decided.
  const restoreScrollTo = options && typeof options.restoreScrollTo === "number"
    ? options.restoreScrollTo : null;
  const finish = (result) => {
    if (restoreScrollTo !== null) {
      try {
        window.scrollTo(0, restoreScrollTo);
      } catch (error) {
        // A page that refuses the scroll is left where it is; the reading is unaffected.
      }
    }
    return result;
  };

  // A hard ceiling, matching listen_app.bridge.MAX_TEXT. The bridge refuses more, so trimming
  // here turns a refusal into a page the reader actually gets.
  const LIMIT = 60000;

  const SOCIAL = [
    // X / Twitter
    ["x.com", '[data-testid="tweetText"]'],
    ["twitter.com", '[data-testid="tweetText"]'],
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
    ["bilibili.com", ".desc-info-text, .basic-desc-info"],
  ];

  // Hosts where the page is a post with a conversation under it. The post body alone loses the
  // replies, and the whole <main> drags in the sidebar and the site chrome, so the two are read
  // separately and joined. Measured live in Chromium 2026-10-20.
  const THREADS = [
    {
      host: "reddit.com",
      // old.reddit is kept as a fallback: the redesign serves <shreddit-post>, and a logged-out
      // or legacy view can still serve the old markup.
      // A link or image post has no [slot="text-body"] — only a title — so the title is the post
      // and the replies below it are still the conversation. Without these selectors the whole
      // thread read came back empty, the generic scan took over, and the note began with the
      // sidebar's community rules with the replies mixed in as page text (measured 2026-10-21).
      post: ['shreddit-post [slot="text-body"]', 'shreddit-post [slot="title"]',
        "shreddit-post h1", '[data-testid="post-content"]', ".usertext-body"],
      comments: ['shreddit-comment [slot="comment"]', ".comment .usertext-body"],
    },
    {
      host: "youtube.com",
      post: ["#description-inline-expander", "ytd-text-inline-expander", "#description"],
      // Mobile YouTube (m.youtube.com, the layout a phone-sized or mobile UA request receives) has
      // its own tags: comments are ytm-comment-renderer and only exist after the reader taps the
      // comments teaser to open its sheet. Measured live 2026-10-20. They are read when they are
      // there; this extractor never opens the sheet itself, because that changes the reader's page.
      comments: ["ytd-comment-thread-renderer #content-text", "ytd-comment-view-model #content-text",
        "ytm-comment-renderer .YtmCommentRendererContent", "ytm-comment-renderer [class*='CommentRendererContent']"],
    },
    {
      // Facebook renders the post and the comments it has loaded inside one role="article", so the
      // container is read whole and no separate comment scan runs: scanning would duplicate the
      // post text under "Comments:". Unverified — a signed-in session is needed to confirm the
      // markup, and the generic paths take over when this selector finds nothing.
      host: "facebook.com",
      post: ['div[role="article"]'],
      comments: [],
    },
    {
      // Instagram post pages are an <article> holding the caption and the loaded comments. Same
      // reasoning as Facebook, and the same caveat: unverified until a signed-in run.
      host: "instagram.com",
      post: ["article"],
      comments: [],
    },
    {
      // Discord is a message list, not a post: every message is a reply. messageContent holds the
      // message text only, so the author line and the timestamp stay out. Unverified until a
      // signed-in run on discord.com/channels.
      host: "discord.com",
      post: ['ol[data-list-id="chat-messages"] [class*="messageContent"]'],
      comments: [],
      each: true,
    },
  ];

  // Hosts that show a sign-in wall to a logged-out reader instead of the post. Their wall page is
  // a short marketing/login page, which the generic paths would send as if it were the article.
  const LOGIN_HOSTS = ["facebook.com", "instagram.com", "discord.com", "x.com", "twitter.com",
    "linkedin.com", "threads.net",
    // A logged-out Gmail tab does not stay on mail.google.com: measured live 2026-10-20 it lands on
    // accounts.google.com/v3/signin/identifier with a 128-character sign-in card ("Sign in to
    // continue to Gmail / Email or phone / Next"), which the generic paths sent as page text. Both
    // hosts are listed because only the final one is what the content script ever sees.
    "accounts.google.com", "mail.google.com"];
  const LOGIN_WALL_MAX_CHARS = 1500;
  // A signed-in page always carries its own content container. Requiring that container to be
  // absent is what keeps a short real post from being refused for merely quoting "sign in".
  const LOGIN_CONTENT = [
    ["facebook.com", 'div[role="article"]'],
    ["instagram.com", "article"],
    ["discord.com", 'ol[data-list-id="chat-messages"]'],
    ["x.com", '[data-testid="tweetText"]'],
    ["twitter.com", '[data-testid="tweetText"]'],
    ["linkedin.com", ".feed-shared-update-v2__description, .attributed-text-segment-list__content"],
    ["threads.net", 'div[dir="auto"] span'],
    // Deliberately no Google entry. On every other host here the signed-in container is known and
    // measured, so it can be required positively; the signed-in Gmail DOM was NOT measured (no
    // signed-in session in this run), so asserting a container I have not seen could refuse a real
    // mailbox. Google is instead protected by the length ceiling: the sign-in card was 128
    // characters live, and a real mailbox, with its folder list and message rows, is far over 1500.
    // A wrong answer here costs one refused press, never a wall sent as the note.
  ];

  // A page that no longer exists still answers HTTP 200 on many sites, and a content script never
  // sees the status, so the wording is the only signal. Measured live 2026-10-20: bbc.com's 404 was
  // extracted as 2158 characters of article, because the page is wordy and has real paragraphs.
  const NOT_FOUND = [
    /error 404\b/i,
    /\b404 not found\b/i,
    /page not found/i,
    /we can't find the page/i,
    /couldn't find that page/i,
    /page you're looking for is no longer here/i,
    /this page (does not|doesn't) exist/i,
    // Google Docs words a dead file id this way and carries no other marker. Measured live
    // 2026-10-20: docs.google.com/document/d/<dead>/edit was read as 116 characters of "Sorry,
    // the file you have requested does not exist." with how "page".
    /the file you have requested does not exist/i,
    /(requested|this) (file|document) (does not|doesn't) exist/i,
  ];
  // The marker has to be near the top, where a site puts its own not-found notice, and the page
  // has to be small. An article ABOUT 404 pages is long, so the length ceiling keeps it readable.
  const NOT_FOUND_HEAD = 500;
  const NOT_FOUND_MAX_CHARS = 6000;

  // Comments are capped twice over: a count ceiling so the top replies are what arrives, and a
  // character ceiling so a long thread cannot crowd the post body out of LIMIT.
  const MAX_COMMENTS = 8;
  const COMMENT_MAX_CHARS = 600;
  const COMMENT_BUDGET = 6000;

  // YouTube ships the full description in the player JSON on the page, while the visible box is
  // collapsed to about 370 characters behind a "more" button. The JSON is read from the page and
  // parsed only for that one field, so nothing is clicked or fetched. The size guard keeps a page
  // with an unexpectedly huge script from being scanned.
  const SHORT_DESCRIPTION = /"shortDescription":"((?:[^"\\]|\\.)*)"/;
  const YT_JSON_MAX = 8000000;

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

  // Each match of a selector in document order, deduplicated by text. A quote-reply on Reddit
  // repeats the comment it answers, and the same reply can be reached by two selectors.
  function fromEach(selectors, max, maxChars) {
    const seen = new Set();
    const parts = [];
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (!visible(el)) continue;
        let text = tidy(el.innerText);
        if (text.length < 2) continue;
        if (text.length > maxChars) text = text.slice(0, maxChars).trim() + "…";
        const key = text.slice(0, 120);
        if (seen.has(key)) continue;
        seen.add(key);
        parts.push(text);
        if (parts.length >= max) return parts;
      }
    }
    return parts;
  }

  // YouTube's watch page carries the whole description in the player response, which the site
  // leaves in a <script>. The visible expander is collapsed to a few hundred characters and the
  // full text sits in a hidden node, so the script is the honest source. The field is decoded as a
  // JSON string so \n and \u escapes come back as characters.
  function fromYouTubeDescription() {
    for (const script of document.querySelectorAll("script")) {
      const body = script.textContent || "";
      if (body.length > YT_JSON_MAX || body.indexOf("shortDescription") < 0) continue;
      const found = SHORT_DESCRIPTION.exec(body);
      if (!found) continue;
      let decoded = "";
      try {
        decoded = JSON.parse('"' + found[1] + '"');
      } catch (error) {
        decoded = found[1];
      }
      const text = tidy(decoded);
      if (text.length >= 20) return text;
    }
    return "";
  }

  // A logged-out Facebook, Instagram, Discord or X page is a sign-in card: short, no post, and it
  // carries its own wording. Sending it would put "Log in to see this" in a note as if it were the
  // post, so it is refused as "login". A page that carries a real post is long, so it is kept.
  function isLoginWall() {
    if (!LOGIN_HOSTS.some((pattern) => hostMatches(host, pattern))) return false;
    const all = tidy(document.body ? document.body.innerText : "");
    if (!all || all.length > LOGIN_WALL_MAX_CHARS) return false;
    // "sign in" is in the pattern because Facebook and Instagram word their card that way rather
    // than "log in"; the container check below is what keeps a real post out of this branch.
    if (!/log ?in|sign ?in|sign ?up|create (a )?new account/i.test(all)) return false;
    return !LOGIN_CONTENT.some(([pattern, selector]) =>
      hostMatches(host, pattern) && document.querySelectorAll(selector).length > 0);
  }

  // A not-found page is refused as "missing", the same verdict a dead link gets, so the reader
  // says there was no page instead of summarising the site's 404 copy.
  function isNotFoundPage() {
    const all = tidy(document.body ? document.body.innerText : "");
    if (!all || all.length > NOT_FOUND_MAX_CHARS) return false;
    return NOT_FOUND.some((pattern) => pattern.test(all.slice(0, NOT_FOUND_HEAD)));
  }

  // The top replies, under a heading, or "" when the site keeps them inside the post container
  // (Facebook, Instagram) or the page has none. Bounded by COMMENT_BUDGET so the post keeps room.
  function commentsBlock(site) {
    if (!site.comments.length) return "";
    const comments = fromEach(site.comments, MAX_COMMENTS, COMMENT_MAX_CHARS);
    const kept = [];
    let used = 0;
    for (const comment of comments) {
      if (used + comment.length > COMMENT_BUDGET) break;
      used += comment.length;
      kept.push(comment);
    }
    return kept.length ? "\n\nComments:\n\n" + kept.join("\n\n") : "";
  }

  // The post body (or, on Discord, the visible messages), then the top replies.
  function fromThread(site) {
    let post = "";
    for (const selector of site.post) {
      if (site.each) {
        const messages = fromEach([selector], MAX_COMMENTS, COMMENT_MAX_CHARS);
        if (messages.length) {
          post = messages.join("\n\n");
          break;
        }
        continue;
      }
      post = fromSelector(selector);
      // A real post can be one short line ("Title only" posts have no body at all), so the floor is
      // minimal here; the caller's 20-character floor still rejects a page with nothing usable.
      if (post.length >= 2) break;
      post = "";
    }
    // Mobile YouTube has no #description-inline-expander: the description sits in the crawler
    // description node, and the page's own player JSON is used for the full text when it is there.
    if (!post && hostMatches(host, "youtube.com")) {
      post = fromSelector("ytm-crawler-description, ytm-expandable-video-description-body-renderer");
    }
    // A post with nothing to read (an image post with no title) still has its conversation, so the
    // replies are sent under their own heading rather than being dropped with the body. Returning
    // "" here let the generic scan take the page instead, which is how a sidebar reached a note.
    const comments = commentsBlock(site);
    if (!post) return comments.trim();
    return post + comments;
  }

  // YouTube only mounts its comments after they scroll near the viewport: measured live 2026-10-20,
  // a watch page that had finished loading still had zero comment renderers until the comments
  // section was scrolled to, and then had 20. So the first pass reports where the tab was, and the
  // caller scrolls, waits, and calls again with restoreScrollTo — the tab ends where the reader
  // left it. Only YouTube needs this; Reddit renders its thread up front.
  //
  // The selector is #below's copy on purpose: at a narrow window the FIRST ytd-comments in the
  // document is the hidden engagement-panel one inside #panels (0 px tall, at the top), so
  // scrolling to it does nothing at all. Measured live 2026-10-20 at 720x588.
  const YT_COMMENTS = "#below #comments, #below ytd-comments, ytd-comments";
  function needsCommentReveal() {
    if (!hostMatches(host, "youtube.com")) return false;
    if (!document.querySelectorAll(YT_COMMENTS).length) return false;
    return document.querySelectorAll("ytd-comment-thread-renderer").length === 0;
  }

  // Mobile YouTube is a different page, not a narrow desktop one: there is no comments section at
  // all, only a "Comments 1K" teaser that opens a sheet. Scrolling does nothing (measured live
  // 2026-10-24: scrolled to the bottom, still 0 comments after 6 s), and the first tap on the teaser
  // mounts 20. So mobile asks for a different reveal, and the caller taps instead of scrolling.
  // The teaser is identified by its own tag, never by the word "Comments", so the recommended-videos
  // carousel heading cannot be tapped by mistake. Measured live on three videos.
  const YT_MOBILE_TEASER = 'yt-video-metadata-carousel-view-model[role="button"], ytm-comments-entry-point-header-renderer';
  function needsMobileCommentTap() {
    if (!hostMatches(host, "youtube.com")) return false;
    // Desktop already has #below's comments container; the teaser is the mobile-only signal.
    if (document.querySelectorAll(YT_COMMENTS).length) return false;
    if (document.querySelectorAll("ytm-comment-renderer").length) return false;
    return document.querySelectorAll(YT_MOBILE_TEASER).length > 0;
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
    // Reddit's logged-out challenge. Measured live 2026-10-20: reddit.com/r/python/comments/1/
    // was read as 186 characters of "Prove your humanity / We're committed to safety and
    // security. But not for bots..." with how "page".
    /prove you(?:'re| are) (?:a )?human/i,
    // Reddit's other wall, served to this network at the same URL on 2026-10-20 while the
    // "Prove your humanity" page was being probed: 143 characters of refusal that the old code
    // read as page text.
    /you've been blocked by network security/i,
    // Google's traffic wall on a search URL.
    /(systems have )?detected unusual traffic/i,
    /this page (checks|appears when google automatically detects)/i,
  ];

  // A challenge page is short: the phrase plus a few lines of boilerplate. An article that merely
  // mentions the phrase is long, so only a short page can be refused. The length ceiling is what
  // keeps real prose safe.
  const CHALLENGE_MAX_CHARS = 800;

  // Three facts must all hold: the page is short, it carries the challenge phrase, and it does
  // not look like real prose. A challenge is one boilerplate block plus a line or two of help
  // text; an article that merely mentions the phrase arrives as several filled paragraphs.
  //
  // The earlier version counted *any* paragraph and required at most one. That is what let two
  // live walls through on 2026-10-20: Reddit's "Prove your humanity" page and Google's "unusual
  // traffic" page each ship two or three <p> elements, so both were sent as page text.
  const CHALLENGE_PROSE_PARAGRAPHS = 3;
  const CHALLENGE_PROSE_MIN_CHARS = 80;
  function isChallengePage() {
    const all = tidy(document.body ? document.body.innerText : "");
    if (all.length > CHALLENGE_MAX_CHARS) return false;
    if (!CHALLENGE.some((pattern) => pattern.test(all))) return false;
    const filled = Array.from(document.querySelectorAll("p"))
      .map((p) => tidy(p.textContent || ""))
      .filter((text) => text.length >= CHALLENGE_PROSE_MIN_CHARS);
    return filled.length < CHALLENGE_PROSE_PARAGRAPHS;
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
    return finish({ text: "", url: location.href, title: (document.title || "").slice(0, 300), how: "wall", truncated: false });
  }
  if (!text && isMissingPage()) {
    return finish({ text: "", url: location.href, title: (document.title || "").slice(0, 300), how: "missing", truncated: false });
  }
  if (!text && isNotFoundPage()) {
    return finish({ text: "", url: location.href, title: (document.title || "").slice(0, 300), how: "missing", truncated: false });
  }
  if (!text && isLoginWall()) {
    return finish({ text: "", url: location.href, title: (document.title || "").slice(0, 300), how: "login", truncated: false });
  }
  if (!text) {
    const site = THREADS.find((entry) => hostMatches(host, entry.host));
    if (site) {
      // A YouTube description comes from the page's own player JSON, because the visible box is
      // collapsed; the collapsed box is only the fallback.
      const described = site.host === "youtube.com" ? fromYouTubeDescription() : "";
      const thread = described
        ? described + commentsBlock(site)
        : fromThread(site);
      if (thread.length >= 20) {
        text = thread;
        how = hostMatches(host, "youtube.com") ? "video" : "post";
      }
    }
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

  // The first pass on a YouTube page whose comments have not mounted yet asks for a second pass
  // rather than sending the description alone.
  // Compare against null, not truthiness: a saved position of 0 is the common case (the reader was
  // at the top), and treating it as "no restore" made this pass ask for another one, forever.
  // Two different reveals for two different pages: desktop scrolls to the comments section, mobile
  // taps the teaser that opens the comments sheet. The caller is told which one, rather than
  // guessing from the URL.
  const available = restoreScrollTo === null;
  const mobileTap = available && needsMobileCommentTap();
  const reveal = available && !mobileTap && needsCommentReveal();
  return finish({
    text,
    // empty when nothing was read, so the caller says so instead of sending a blank page
    url: location.href,
    title: (document.title || "").slice(0, 300),
    how,
    truncated,
    // 0 is the honest answer "the tab has not been scrolled", which the caller cannot tell from
    // a missing field, so the flag travels with it.
    revealComments: reveal || mobileTap,
    // "scroll" for desktop, "tap" for mobile; absent when no second pass is wanted.
    revealMode: reveal ? "scroll" : mobileTap ? "tap" : "",
    scrollY: reveal || mobileTap ? Math.round(window.scrollY || 0) : null,
  });
}
