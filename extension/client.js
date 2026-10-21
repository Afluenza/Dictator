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

/* Chrome's wording when the document being scripted is replaced mid-call. A page that navigates
 * itself (a mailbox redirecting to sign-in, a dashboard replacing its shell) hits this; a page we
 * genuinely cannot script does not. Matched on the message because the error arrives as a plain
 * object from the API, not a typed exception. */
function isContextDestroyed(error) {
  const message = String((error && error.message) || error || "");
  return /execution context was destroyed|frame (was|has been) removed|no tab with id/i.test(message);
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
    await new Promise((resolve) => setTimeout(resolve, 900));
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
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          // #below first: at a narrow window the document's first ytd-comments is the hidden
          // engagement-panel copy, and scrolling to it moves nothing.
          const box = document.querySelector("#below #comments, #below ytd-comments, ytd-comments");
          if (box) box.scrollIntoView({ block: "start" });
          if (!window.scrollY) window.scrollTo(0, document.documentElement.scrollHeight);
        },
      });
      // Wait for the comments to actually mount rather than guessing a delay: a fixed 2.5 s was
      // measured sufficient in a fresh tab and not sufficient in a normal one, where the video is
      // playing. Polling stops as soon as they appear, and gives up after 7 s.
      for (let waited = 0; waited < 7000; waited += 400) {
        await new Promise((resolve) => setTimeout(resolve, 400));
        const ready = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: () => document.querySelectorAll("ytd-comment-thread-renderer").length > 0,
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
    } catch {
      // The reveal is best effort: if it fails, the description already read is still sent.
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
