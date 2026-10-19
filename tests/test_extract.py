from pathlib import Path

import pytest

from listen_app.extract import (
    ExtractError,
    UnsupportedInput,
    extract_file,
    extract_html,
    validate_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_txt_fixture_reads_plain_text():
    assert extract_file(FIXTURES / "sample.txt") == "Plain text fixture. It has two sentences."


def test_md_fixture_reads_markdown_as_text():
    text = extract_file(FIXTURES / "sample.md")
    assert text.startswith("# Markdown fixture")
    assert "A paragraph for the markdown reader." in text


def test_pdf_fixture_extracts_text():
    assert extract_file(FIXTURES / "sample.pdf") == "Plain text fixture. It has two sentences."


def test_docx_fixture_extracts_paragraphs():
    assert extract_file(FIXTURES / "sample.docx") == (
        "Word fixture paragraph one.\nWord fixture paragraph two."
    )


def test_unsupported_suffix_is_refused(tmp_path):
    f = tmp_path / "book.epub"
    f.write_bytes(b"not read")
    with pytest.raises(UnsupportedInput, match=r"\.epub is not supported"):
        extract_file(f)


def test_empty_text_file_is_an_error(tmp_path):
    f = tmp_path / "blank.txt"
    f.write_text("   \n", encoding="utf-8")
    with pytest.raises(ExtractError, match="no readable text"):
        extract_file(f)


def test_html_main_text_drops_nav_and_footer():
    # Offline: the link path is extract_html on fetched HTML, so no network call here.
    html = (
        "<html><head><title>T</title></head><body>"
        "<nav>menu</nav>"
        "<article><h1>Head</h1>"
        "<p>Main paragraph one has enough words to count as real content for the extractor here.</p>"
        "<p>Second paragraph follows with more real content to extract properly from the page body.</p>"
        "</article><footer>foot</footer></body></html>"
    )
    text = extract_html(html, "https://example.com/a")
    assert "Main paragraph one" in text
    assert "Second paragraph" in text
    assert "menu" not in text
    assert "foot" not in text


def test_validate_url_accepts_http_and_https():
    assert validate_url("  https://example.com/a  ") == "https://example.com/a"
    assert validate_url("http://example.com") == "http://example.com"


@pytest.mark.parametrize("bad", ["", "example.com", "ftp://example.com", "https://", "file:///etc/passwd"])
def test_validate_url_rejects_non_web_links(bad):
    with pytest.raises(ValueError):
        validate_url(bad)
