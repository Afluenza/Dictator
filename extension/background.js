/* The three keyboard shortcuts in the manifest. That is the whole job of this worker: it wakes on
 * a command, sends, and goes back to sleep. It holds no listeners on page content, reads no
 * traffic, and schedules nothing.
 *
 * Alt+Shift+S sends the selection; Alt+Shift+P sends the whole page; Alt+Shift+D toggles
 * dictation. The dictation toggle carries no audio: it asks Listen to start or stop its own
 * microphone on 127.0.0.1, so this extension keeps asking for no mic permission at all.
 * The popup reports the same results when it is open, so there is one code path, not two. */

import { dictate, sendPage } from "./client.js";

chrome.commands.onCommand.addListener(async (command) => {
  if (command === "send-selection") {
    await sendPage("auto");
  } else if (command === "send-page") {
    await sendPage("page");
  } else if (command === "dictate") {
    // The result is shown where the user is looking: a badge on the toolbar icon, cleared when
    // the second press stops the recording. Nothing is spoken or shown over the page itself.
    const result = await dictate();
    await chrome.action.setBadgeText({
      text: !result.ok ? "!" : result.recording ? "REC" : "",
    });
    await chrome.action.setBadgeBackgroundColor({ color: result.ok ? "#C4574F" : "#A94740" });
    await chrome.action.setTitle({ title: result.message });
  }
});
