def test_gui_module_imports_extractors_it_uses():
    # The window imports the file extractors it uses. A broken import would fail here, not at launch.
    from listen_app import gui

    assert callable(gui.extract_file)
    assert callable(gui.validate_url)
    assert gui.ExtractError is not None
    assert gui.UnsupportedInput is not None


def test_a_failed_load_clears_the_box_so_a_summary_cannot_mislabel_old_text():
    """Wiring rule: the source URL is committed only with text that arrived.

    Before the fix, a failed link load left the previous text in the box AND set the pending
    source to the URL that failed, so Summarize would have labelled old text with the wrong link.
    """
    import inspect

    from listen_app import gui

    source = inspect.getsource(gui.ListenApp)
    assert '"load_failed"' in source, "the worker must report a failed load as its own event"
    assert 'self._events.put(("text", job(), source_url))' in source, "the URL travels with the text"
    assert '_loaded_source = ""' in source, "a failed load must forget the source"
    # And it must be the field Summarize reads, not the one the worker sets up front.
    assert "_loaded_source" in inspect.getsource(gui.ListenApp._on_summarize)
