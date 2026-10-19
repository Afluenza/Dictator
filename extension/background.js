/* The two keyboard shortcuts in the manifest. That is the whole job of this worker: it wakes on
 * a command, sends, and goes back to sleep. It holds no listeners on page content, reads no
 * traffic, and schedules nothing.
 *
 * A shortcut pressing Alt+Shift+S sends the selection; Alt+Shift+P sends the whole page. The
 * popup reports the same results when it is open, so there is one code path, not two. */

import { sendPage } from "./client.js";

chrome.commands.onCommand.addListener(async (command) => {
  if (command === "send-selection") {
    await sendPage("auto");
  } else if (command === "send-page") {
    await sendPage("page");
  }
});
