/* The popup: pair once, then send. Every message shown here comes from a real reply or a real
 * failure — nothing reports success it did not get. */

import { dictate, getToken, probe, sendLink, sendPage, setToken } from "./client.js";

const ui = {
  dot: document.getElementById("dot"),
  headline: document.getElementById("headline"),
  pair: document.getElementById("pair-title").closest("section"),
  code: document.getElementById("code"),
  save: document.getElementById("save"),
  sendText: document.getElementById("send-text"),
  sendLink: document.getElementById("send-link"),
  dictate: document.getElementById("dictate"),
  status: document.getElementById("status"),
};

function say(message, kind = "") {
  ui.status.textContent = message;
  ui.status.className = kind ? `muted ${kind}` : "muted";
}

function setConnected(connected) {
  ui.dot.classList.toggle("off", !connected);
  ui.headline.textContent = connected ? "Listen is ready" : "Send to Listen";
}

async function refresh() {
  const token = await getToken();
  if (!token) {
    ui.pair.classList.remove("hidden");
    setConnected(false);
    say("Pair once to get started.");
    return;
  }
  ui.pair.classList.add("hidden");
  const state = await probe();
  setConnected(state.ok);
  say(state.ok ? "" : "Listen is not open. Start the app, then send again.");
}

async function send(handler) {
  for (const button of [ui.sendText, ui.sendLink, ui.dictate]) button.disabled = true;
  say("Sending...");
  try {
    const result = await handler();
    say(result.message, result.ok ? "ok" : "bad");
  } catch (error) {
    say(`Failed: ${error.message}`, "bad");
  } finally {
    for (const button of [ui.sendText, ui.sendLink, ui.dictate]) button.disabled = false;
  }
}

ui.save.addEventListener("click", async () => {
  const token = await setToken(ui.code.value);
  if (!token) {
    say("Paste the code from Listen first.", "bad");
    return;
  }
  ui.code.value = "";
  await refresh();
});

ui.code.addEventListener("keydown", (event) => {
  if (event.key === "Enter") ui.save.click();
});

ui.sendText.addEventListener("click", () => send(() => sendPage("page")));
ui.sendLink.addEventListener("click", () => send(sendLink));
ui.dictate.addEventListener("click", async () => {
  // Same call as the hotkey. The popup closes on the second press, so the badge is the state.
  const result = await dictate();
  say(result.message, result.ok ? "ok" : "bad");
  ui.dictate.textContent = result.ok && result.recording ? "Stop dictating (Alt+Shift+D)"
                                                         : "Dictate (Alt+Shift+D)";
});

refresh();
