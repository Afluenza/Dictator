/* Talking to the app: one port, one code, two payload kinds.
 *
 * Everything here is loopback. A send is a POST to 127.0.0.1 with the pairing code in a header,
 * carrying either the page's text or just its link — never a cookie, never a page request, never
 * a third party. Nothing is queued for later and nothing retried into a loop: if the app is not
 * open, the popup says so, which is the truth.
 */

export const ENDPOINT = "http://127.0.0.1:8765";
export const TOKEN_HEADER = "X-Dictator-Token";
const PAIR_KEY = "dictator.token";

export async function getToken() {
  const stored = await chrome.storage.local.get(PAIR_KEY);
  return stored[PAIR_KEY] || "";
}

export async function setToken(token) {
  // The app shows the code in groups of four; the token itself has no spaces.
  const clean = (token || "").replace(/\s+/g, "").toLowerCase();
  await chrome.storage.local.set({ [PAIR_KEY]: clean });
  return clean;
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/* Does the app answer at all? Used to show Connected / Not connected in the popup. */
export async function probe() {
  const token = await getToken();
  if (!token) return { ok: false, reason: "unpaired" };
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2500);
    const response = await fetch(`${ENDPOINT}/ping`, { signal: controller.signal });
    clearTimeout(timer);
    return response.ok ? { ok: true, reason: "paired" } : { ok: false, reason: "app-closed" };
  } catch {
    return { ok: false, reason: "app-closed" };
  }
}

/* Send one payload. Returns {ok, message} with a message safe to show the user. */
export async function send(payload) {
  const token = await getToken();
  if (!token) return { ok: false, message: "Not paired yet. Press Browser in Listen for the code." };
  let response;
  try {
    response = await fetch(`${ENDPOINT}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json", [TOKEN_HEADER]: token },
      body: JSON.stringify(payload),
    });
  } catch {
    return { ok: false, message: "Listen is not running. Open the app and try again." };
  }
  const body = await readJson(response);
  if (response.ok && body.ok) {
    return { ok: true, message: `Sent to Listen (${(body.chars || 0).toLocaleString()} characters).` };
  }
  if (response.status === 401) {
    return { ok: false, message: "That code does not match. Press Browser in Listen for the current one." };
  }
  return { ok: false, message: body.error || `Listen refused it (${response.status}).` };
}

/* Ask the app to start or stop dictating. No audio crosses this socket: the microphone stays inside
 * Listen, which is why the extension never needs a mic permission and never sees the transcript. */
export async function dictate() {
  const token = await getToken();
  if (!token) return { ok: false, message: "Not paired yet. Press Browser in Listen for the code." };
  let response;
  try {
    response = await fetch(`${ENDPOINT}/dictate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", [TOKEN_HEADER]: token },
      body: "{}",
    });
  } catch {
    return { ok: false, message: "Listen is not running. Open the app and try again." };
  }
  const body = await readJson(response);
  if (response.ok && body.ok) {
    return {
      ok: true,
      recording: Boolean(body.recording),
      message: body.recording ? "Listening in Listen. Press again to stop." : "Stopped; transcribing.",
    };
  }
  if (response.status === 401) {
    return { ok: false, message: "That code does not match. Press Browser in Listen for the current one." };
  }
  return { ok: false, message: body.error || `Listen refused it (${response.status}).` };
}

/* Chrome's wording when the document being scripted is replaced mid-call. A page that navigates
 * itself (a mailbox redirecting to sign-in, a dashboard replacing its shell) hits this; a page we
 * genuinely cannot script does not. Matched on the message because the error arrives as a plain
 * object from the API, not a typed exception. */
function isContextDestroyed(error) {
  const message = String((error && error.message) || error || "");
  return /execution context was destroyed|frame (was|has been) removed|no tab with id/i.test(message);
}

/* Wait until the tab's document has finished loading, polling rather than sleeping a fixed time.
 * Bounded: a page that never settles returns so the read can be attempted (or reported) anyway.
 * Failures here are swallowed because the wait is a courtesy to the page, not the delivery. */
async function waitForReady(tabId, ceilingMs) {
  for (let waited = 0; waited < ceilingMs; waited += 400) {
    await new Promise((resolve) => setTimeout(resolve, 400));
    try {
      const state = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => document.readyState,
      });
      if (state && state[0] && state[0].result === "complete") return;
    } catch {
      // The document is mid-swap; try again on the next tick.
    }
  }
}

/* Hosts measured to finish loading while still blank, so the body is the only honest signal.
 * outlook.live.com/mail/, measured 2026-10-21: readyState "complete" with a 0-character body at
 * 750 ms, still empty at 2.5 s, the Microsoft 365 product page by 6 s. Nothing throws in that
 * window, so the destroyed-context retry above never fires on it. Only measured hosts are listed:
 * an ordinary empty page must still answer at once rather than being read twice. */
const BLANK_PAGE_HOSTS = ["outlook.live.com"];

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }
}

/* Poll until the document carries text. True as soon as it does, false if the ceiling passes. */
async function waitForText(tabId, ceilingMs) {
  for (let waited = 0; waited <= ceilingMs; waited += 400) {
    try {
      const state = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => Boolean(document.body && document.body.innerText.trim()),
      });
      if (state && state[0] && state[0].result) return true;
    } catch {
      // The document is mid-swap; try again on the next tick.
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
}

/* Inject extract.js, then read the value back. Two injections share one isolated world, so the
 * extraction stays a readable file instead of being copied inline here. */
async function injectAndExtract(tabId, preferSelection) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["extract.js"] });
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (prefer) => globalThis.dictatorExtract({ preferSelection: prefer }),
    args: [preferSelection],
  });
  return results;
}

/* The two reveals, at module scope because chrome.scripting serialises these by their source: each
 * one has to stand alone, with no reference to anything in this file. */

/* Desktop: scroll the comments section into view so YouTube mounts it. #below first, because at a
 * narrow window the document's first ytd-comments is the hidden engagement-panel copy, and
 * scrolling to it moves nothing. */
function revealComments(tap) {
  if (tap) {
    // Mobile has no comments section to scroll to; its teaser opens a sheet. Identified by tag, so
    // the recommended-videos carousel heading cannot be hit instead.
    const teaser = document.querySelector(
      'yt-video-metadata-carousel-view-model[role="button"], ytm-comments-entry-point-header-renderer');
    if (!teaser) return;
    const box = teaser.getBoundingClientRect();
    const at = {
      bubbles: true, cancelable: true, view: window,
      clientX: Math.round(box.left + box.width / 2), clientY: Math.round(box.top + box.height / 2),
    };
    // A real pointer sequence: these handlers ignore a bare click().
    for (const type of ["pointerdown", "pointerup", "click"]) {
      teaser.dispatchEvent(new MouseEvent(type, at));
    }
    return;
  }
  const box = document.querySelector("#below #comments, #below ytd-comments, ytd-comments");
  if (box) box.scrollIntoView({ block: "start" });
  if (!window.scrollY) window.scrollTo(0, document.documentElement.scrollHeight);
}

/* Are the comments in the DOM yet? Both layouts, since the caller does not know which it got. */
function commentsAreMounted() {
  return document.querySelectorAll("ytd-comment-thread-renderer").length > 0
    || document.querySelectorAll("ytm-comment-renderer").length > 0;
}

/* Mobile opened a sheet over the page; leave it as the reader had it. Only ever called on the tap
 * path, so a sheet the reader opened themselves is never touched. */
function closeCommentSheet() {
  const buttons = document.querySelectorAll("button, [role='button']");
  for (const button of buttons) {
    const label = (button.getAttribute("aria-label") || "").toLowerCase();
    const box = button.getBoundingClientRect();
    if (label !== "close" || box.width < 20 || box.height < 20) continue;
    const at = {
      bubbles: true, cancelable: true, view: window,
      clientX: Math.round(box.left + box.width / 2), clientY: Math.round(box.top + box.height / 2),
    };
    for (const type of ["pointerdown", "pointerup", "click"]) {
      button.dispatchEvent(new MouseEvent(type, at));
    }
    return true;
  }
  return false;
}

/* Extract the page in the active tab and send it. "auto" prefers a selection, "page" ignores it. */
export async function sendPage(mode = "auto") {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id || !/^https?:/i.test(tab.url || "")) {
    return { ok: false, message: "This tab is not a web page, so there is nothing to read." };
  }
  let results;
  try {
    results = await injectAndExtract(tab.id, mode === "auto");
  } catch (error) {
    // A mailbox or a dashboard navigates itself after load, and an evaluate into a document that
    // is being replaced fails with "Execution context was destroyed". That is the page moving,
    // not a page we cannot read, so it gets one retry once the navigation settles. Any other
    // error is reported exactly as it came.
    if (!isContextDestroyed(error)) {
      return { ok: false, message: `Cannot read this page: ${error.message}` };
    }
    // Measured on outlook.live.com/mail/ 2026-10-20: the redirect to the product page only
    // settled about 5 s in (readyState still "interactive" at 1.5 s), and a fixed 900 ms retry
    // read the outgoing document and found nothing. So wait for the new document to finish
    // loading instead of guessing a delay, with a ceiling so a page that never settles is still
    // reported rather than hanging the popup.
    await waitForReady(tab.id, 6000);
    try {
      results = await injectAndExtract(tab.id, mode === "auto");
    } catch (retry) {
      return {
        ok: false,
        message: isContextDestroyed(retry)
          ? "This page is redirecting, most likely to a sign-in screen. Open it, sign in, then send again."
          : `Cannot read this page: ${retry.message}`,
      };
    }
  }
  let page = results && results[0] && results[0].result;
  // YouTube mounts its comments only once they approach the viewport, so the first pass asks for a
  // second one: reveal the comments section, give it time to load, then read again with the tab
  // restored to where the reader had it.
  if (page && page.revealComments) {
    try {
      const tapped = page.revealMode === "tap";
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: revealComments,
        args: [tapped],
      });
      // Wait for the comments to actually mount rather than guessing a delay: a fixed 2.5 s was
      // measured sufficient in a fresh tab and not sufficient in a normal one, where the video is
      // playing. Polling stops as soon as they appear, and gives up after 7 s.
      for (let waited = 0; waited < 7000; waited += 400) {
        await new Promise((resolve) => setTimeout(resolve, 400));
        const ready = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: commentsAreMounted,
        });
        if (ready && ready[0] && ready[0].result) break;
      }
      results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (preferSelection, restoreScrollTo) =>
          globalThis.dictatorExtract({ preferSelection, restoreScrollTo }),
        args: [mode === "auto", page.scrollY || 0],
      });
      page = (results && results[0] && results[0].result) || page;
      // The mobile tap opened a sheet over the page. The read is done, so put the page back the way
      // the reader had it. Only on the tap path: a sheet the reader opened themselves is left alone.
      if (tapped) {
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: closeCommentSheet,
        });
      }
    } catch {
      // The reveal is best effort: if it fails, the description already read is still sent.
    }
  }
  // A page can finish loading while still blank and fill in afterwards without ever throwing, so
  // the destroyed-context retry above never fires. On the hosts measured to do it, watch the body
  // and read once more the moment it has text. Bounded, and scoped by host so an ordinary empty
  // page keeps answering at once instead of waiting six seconds to say nothing.
  const startedOn = hostOf(tab.url);
  const wentBlank = !page || !page.text || page.text.trim().length < 2;
  if (wentBlank && BLANK_PAGE_HOSTS.some((known) => startedOn === known || startedOn.endsWith("." + known))) {
    if (await waitForText(tab.id, 6000)) {
      try {
        results = await injectAndExtract(tab.id, mode === "auto");
        const later = results && results[0] && results[0].result;
        if (later) {
          page = later;
          // The same "blank then fill" page is usually the site sending you somewhere else — a
          // mailbox that signs you out lands on its own marketing page. Sending that as mail would
          // put sales copy in a note, so a change of host is reported instead.
          if (hostOf(later.url) !== startedOn) {
            return {
              ok: false,
              message: `This page redirected to ${hostOf(later.url)}, so there was nothing to send. Open the page again and send once it has loaded.`,
            };
          }
        }
      } catch {
        // The re-read is best effort: the message below still tells the truth about what was found.
      }
    }
  }
  if (!page || !page.text || page.text.trim().length < 2) {
    // extract.js names why it refused a page instead of sending the site's sign-in or 404 copy as
    // if it were the post. Saying so is the difference between "nothing here" and "log in first".
    const why = {
      login: "This page is a sign-in wall. Log in, then send again.",
      missing: "That page does not exist, so there is nothing to read.",
      wall: "The site served a bot check instead of the page. Reload it, then send again.",
    }[page && page.how];
    return { ok: false, message: why || "Found no text on the page. Select the part you want, then send again." };
  }
  const trimmed = page.truncated ? " (trimmed to 60,000 characters)" : "";
  const sent = await send({ kind: "text", text: page.text, url: page.url, title: page.title });
  const what = {
    selection: "the selection", post: "the post", video: "the video",
    main: "the page", page: "the page", shadow: "the page",
  }[page.how] || "the page";
  return sent.ok ? { ok: true, message: `Sent ${what} to Listen${trimmed}.` } : sent;
}

/* Send only the link, so the app reads it through its own anonymous route. */
export async function sendLink() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/i.test(tab.url || "")) {
    return { ok: false, message: "This tab is not a web page, so there is no link to send." };
  }
  return send({ kind: "url", url: tab.url, title: tab.title || "" });
}
