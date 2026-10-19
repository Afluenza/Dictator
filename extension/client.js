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

/* Extract the page in the active tab and send it. "auto" prefers a selection, "page" ignores it. */
export async function sendPage(mode = "auto") {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id || !/^https?:/i.test(tab.url || "")) {
    return { ok: false, message: "This tab is not a web page, so there is nothing to read." };
  }
  let results;
  try {
    // extract.js is injected first and declares dictatorExtract; the second call reads it back.
    // Two injections share one isolated world, so the extraction stays a readable file instead of
    // being copied inline here.
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["extract.js"] });
    results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (preferSelection) => globalThis.dictatorExtract({ preferSelection }),
      args: [mode === "auto"],
    });
  } catch (error) {
    return { ok: false, message: `Cannot read this page: ${error.message}` };
  }
  const page = results && results[0] && results[0].result;
  if (!page || !page.text || page.text.trim().length < 2) {
    return { ok: false, message: "Found no text on the page. Select the part you want, then send again." };
  }
  const trimmed = page.truncated ? " (trimmed to 60,000 characters)" : "";
  const sent = await send({ kind: "text", text: page.text, url: page.url, title: page.title });
  return sent.ok ? { ok: true, message: `Sent ${page.how} to Listen${trimmed}.` } : sent;
}

/* Send only the link, so the app reads it through its own anonymous route. */
export async function sendLink() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/i.test(tab.url || "")) {
    return { ok: false, message: "This tab is not a web page, so there is no link to send." };
  }
  return send({ kind: "url", url: tab.url, title: tab.title || "" });
}
