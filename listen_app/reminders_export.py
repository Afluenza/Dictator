"""Export note lines to the macOS Reminders app, via AppleScript (osascript).

The app's notes are plain markup (see notes_format). Each non-empty line becomes one
reminder. Headings and bullets are turned into plain titles, and inline tags are
stripped.

Writes are restricted to the two dedicated test lists, "Listen Test" and
"Listen Test 2". Every other name, including the user's real lists, is refused
before the platform check and before any AppleScript runs, so the app can never
write to a real list by accident.

Everything runs on the local machine. Nothing goes over the network.
"""

from __future__ import annotations

import subprocess
import sys

from listen_app import notes_format as nf

TEST_LIST = "Listen Test"
TEST_LIST_2 = "Listen Test 2"
TEST_LISTS = (TEST_LIST, TEST_LIST_2)
MAX_TITLE_CHARS = 500


class RemindersError(RuntimeError):
    pass


def require_test_list(list_name: str) -> str:
    """Return the name if it is a dedicated test list, otherwise refuse.

    Called first by every write path, so a real list (Homework, Chores, Groceries,
    Money, Coding, Gaming, Family, Music or the default Reminders list) can never be
    written to, even when a caller asks for it by name.
    """
    if list_name not in TEST_LISTS:
        raise RemindersError(
            f"writes are limited to the test lists {TEST_LISTS}; refusing {list_name!r}"
        )
    return list_name


def reminder_titles(markup: str) -> list[str]:
    """One title per non-empty line of the note, with markup removed."""
    plain, _styles = nf.parse(markup)
    titles: list[str] = []
    for line in plain.split("\n"):
        title = line.strip()
        for prefix in ("### ", "## ", "# ", "- "):
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                break
        if title:
            titles.append(title[:MAX_TITLE_CHARS])
    return titles


def _applescript_string(text: str) -> str:
    """Quote text for AppleScript: escape backslashes and double quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_script(list_name: str, titles: list[str]) -> str:
    lines = [
        'tell application "Reminders"',
        f"  if not (exists list {_applescript_string(list_name)}) then",
        f"    make new list with properties {{name:{_applescript_string(list_name)}}}",
        "  end if",
        f"  tell list {_applescript_string(list_name)}",
    ]
    for title in titles:
        lines.append(f"    make new reminder with properties {{name:{_applescript_string(title)}}}")
    lines += ["  end tell", "end tell"]
    return "\n".join(lines)


def export_to_reminders(markup: str, list_name: str = TEST_LIST) -> int:
    """Create one reminder per line in the test list `list_name`. Returns the number created.

    Raises RemindersError for any non-test list, on a non-Mac, or if osascript fails.
    The list is created if missing.
    """
    require_test_list(list_name)  # the safety check runs before anything else
    if sys.platform != "darwin":
        raise RemindersError("Reminders export needs macOS")
    titles = reminder_titles(markup)
    if not titles:
        return 0
    result = subprocess.run(
        ["osascript", "-e", _build_script(list_name, titles)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RemindersError(result.stderr.strip() or "osascript failed")
    return len(titles)


def count_in_list(list_name: str) -> int:
    """How many reminders the named list holds now. Read only; used to read back an export.

    A list that does not exist yet counts as 0, so the read-back never fails first.
    """
    if not list_name or not list_name.strip():
        raise RemindersError("a Reminders list name is required")
    script = (
        'tell application "Reminders"\n'
        f"  if exists list {_applescript_string(list_name)} then\n"
        f"    return count of reminders of list {_applescript_string(list_name)}\n"
        "  end if\n"
        "  return 0\n"
        "end tell"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RemindersError(result.stderr.strip() or "osascript failed")
    return int(result.stdout.strip())


def clear_list(list_name: str) -> None:
    """Delete every reminder in the named test list. Every other name is refused.

    Idempotent: a test list that does not exist yet counts as empty, so the export
    run can clear a list before it has ever been created.
    """
    require_test_list(list_name)
    script = (
        'tell application "Reminders"\n'
        # The existence test is inside the script: one osascript call, and no error
        # when the list is missing.
        f"  if exists list {_applescript_string(list_name)} then\n"
        f"    delete every reminder of list {_applescript_string(list_name)}\n"
        "  end if\n"
        "end tell"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RemindersError(result.stderr.strip() or "osascript failed")
