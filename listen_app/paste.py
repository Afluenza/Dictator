"""Put dictated text into whatever field the user has focused.

Three routes, one per OS, and no third-party module: the clipboard plus the OS's own
keystroke sender. That is the smallest thing that works everywhere:

- macOS:   pbcopy, then AppleScript "keystroke v using command down" (needs Accessibility
           permission for the process that runs this app).
- Windows: clip.exe, then PowerShell [System.Windows.Forms.SendKeys]::SendWait("^v").
- Linux:   wl-copy + wtype (Wayland), else xclip/xsel + xdotool (X11).

Nothing here runs a shell string: every call is a list, so dictated text can never become a
command. The previous clipboard contents are put back afterwards, because silently replacing
what someone copied is not a side effect dictation gets to have.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import Sequence

CLIPBOARD_RESTORE_DELAY_S = 0.6  # time for the target app to read the paste before it is put back


class PasteError(Exception):
    """The text could not be placed at the cursor. Says which piece was missing."""


def _run(argv: Sequence[str], runner=subprocess.run, input_text: str | None = None) -> str:
    """Run one command with no shell. Returns stdout; raises PasteError with the real reason."""
    try:
        done = runner(argv, input=input_text, capture_output=True, text=True, timeout=10)
    except FileNotFoundError as exc:
        raise PasteError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise PasteError(f"{argv[0]} did not answer in time") from exc
    if getattr(done, "returncode", 0) != 0:
        detail = (getattr(done, "stderr", "") or "").strip().splitlines()
        raise PasteError(f"{argv[0]} failed: {detail[0] if detail else 'no detail'}")
    return getattr(done, "stdout", "") or ""


def _which(name: str) -> str | None:
    return shutil.which(name)


# ----- per-OS plans ---------------------------------------------------------
def macos_plan() -> list[tuple[str, ...]]:
    return [
        ("pbpaste",),
        ("pbcopy",),
        ("osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'),
    ]


def windows_plan() -> list[tuple[str, ...]]:
    return [
        ("powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"),
        ("clip.exe",),
        ("powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('^v')"),
    ]


def linux_plan(which=_which) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(clipboard_cmds, paste_cmds). Wayland first; X11 only if the Wayland tools are absent."""
    if which("wl-copy") and which("wtype"):
        get, put = ["wl-paste"], ["wl-copy"]
        paste = ["wtype", "-"]
    elif which("xclip") and which("xdotool"):
        get, put = ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]
        paste = ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
    elif which("xsel") and which("xdotool"):
        get, put = ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]
        paste = ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
    else:
        raise PasteError("no clipboard tool found (need wl-clipboard+wtype, or xclip/xsel + xdotool)")
    return [get, put], [paste]


def paste_text(text: str, platform: str | None = None, runner=subprocess.run, sleeper=time.sleep,
               which=_which) -> None:
    """Copy `text`, send the paste keystroke, then restore the old clipboard.

    An empty string is a no-op: dictation that heard nothing must not paste nothing over a draft.
    """
    if not text:
        return
    os_name = platform or sys.platform

    if os_name == "darwin":
        get_cmd, put_cmd, key_cmd = macos_plan()
        restore = _run(get_cmd, runner)
    elif os_name.startswith("win"):
        get_cmd, put_cmd, key_cmd = windows_plan()
        restore = _run(get_cmd, runner)
    else:
        (get_cmd, put_cmd), (key_cmd,) = linux_plan(which)
        restore = _run(get_cmd, runner)

    _run(put_cmd, runner, input_text=text)
    _run(key_cmd, runner)
    # The key has been sent; the target app reads the clipboard on its own next tick.
    sleeper(CLIPBOARD_RESTORE_DELAY_S)
    try:
        _run(put_cmd, runner, input_text=restore)
    except PasteError:
        pass  # the paste already happened; a failed restore is not worth failing the dictation
