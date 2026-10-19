"""Pure-logic tests for the Reminders export. The AppleScript path is tested live, not here."""

import pytest

from listen_app.reminders_export import (
    TEST_LIST,
    TEST_LIST_2,
    TEST_LISTS,
    RemindersError,
    _build_script,
    clear_list,
    export_to_reminders,
    reminder_titles,
    require_test_list,
)

# The lists actually on this Mac that must never be written to.
REAL_LIST_NAMES = ("Homework", "Chores", "Groceries", "Money", "Coding", "Gaming", "Family", "Music")


def test_headings_and_bullets_become_plain_titles():
    markup = "# Groceries\n- milk\n## Later\n- [b]eggs[/b]\n\n   \nbread"
    assert reminder_titles(markup) == ["Groceries", "milk", "Later", "eggs", "bread"]


def test_empty_note_gives_no_titles():
    assert reminder_titles("") == []
    assert reminder_titles("\n  \n") == []


def test_quotes_and_backslashes_are_escaped_for_applescript():
    script = _build_script("Listen Test", ['say "hi"', "back\\slash"])
    assert 'name:"say \\"hi\\""' in script
    assert 'name:"back\\\\slash"' in script


def test_both_test_lists_are_accepted():
    assert TEST_LISTS == ("Listen Test", "Listen Test 2")
    assert require_test_list(TEST_LIST) == TEST_LIST
    assert require_test_list(TEST_LIST_2) == TEST_LIST_2


@pytest.mark.parametrize("name", REAL_LIST_NAMES + ("Reminders", "Listen Test 3", "", "   "))
def test_real_lists_and_unknown_names_are_refused(name):
    with pytest.raises(RemindersError):
        require_test_list(name)


@pytest.mark.parametrize("name", REAL_LIST_NAMES + ("Reminders", "listen test"))
def test_write_paths_refuse_a_real_list(name):
    """export_to_reminders and clear_list must refuse a real list before any write."""
    with pytest.raises(RemindersError):
        export_to_reminders("- milk\n- eggs", name)
    with pytest.raises(RemindersError):
        clear_list(name)
