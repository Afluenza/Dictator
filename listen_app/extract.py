"""Text extractors for the Listen inputs: txt/md, pdf, docx, and raw page HTML.

Each extractor returns plain text. Nothing here logs in, calls a cloud API, or writes
files, and nothing here touches the network: a pasted link is fetched by
listen_app.social_read, which then hands the HTML to `extract_html`.

Libraries (all permissive, see THIRD_PARTY_NOTICES.md):
- pdfplumber (MIT) for PDF text
- python-docx (MIT) for DOCX paragraphs
- trafilatura (Apache-2.0) for the main article text of a web page
"""

from __future__ import annotations

from pathlib import Path

TEXT_SUFFIXES = (".txt", ".md")
PDF_SUFFIXES = (".pdf",)
DOCX_SUFFIXES = (".docx",)
SUPPORTED_SUFFIXES = TEXT_SUFFIXES + PDF_SUFFIXES + DOCX_SUFFIXES


class UnsupportedInput(Exception):
    """Raised for an input the app cannot read."""


class ExtractError(Exception):
    """Raised when a supported input yields no readable text."""


def validate_url(url: str) -> str:
    """The link the window will read, or a ValueError naming what to paste instead.

    The rule itself lives in listen_app.social_read.is_web_link, because that is the function the
    reader and the bridge gate on. Keeping a second, looser check here let a link through the
    window and then be refused by the reader ("https://example.com/a b" passed this and failed
    is_web_link), so the user saw a load error for a link the window had already accepted.
    """
    from listen_app.social_read import is_web_link

    cleaned = url.strip()
    if not is_web_link(cleaned):
        raise ValueError("paste a full http:// or https:// link")
    return cleaned


def extract_file(path: str | Path) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = p.read_text(encoding="utf-8")
    elif suffix in PDF_SUFFIXES:
        text = _pdf_text(p)
    elif suffix in DOCX_SUFFIXES:
        text = _docx_text(p)
    else:
        raise UnsupportedInput(
            f"{suffix or 'this file type'} is not supported; use .txt, .md, .pdf, or .docx"
        )
    return _require_text(text, p.name)


def _pdf_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n\n".join(page for page in pages if page.strip())


def _docx_text(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def extract_html(html: str, url: str | None = None) -> str:
    """Main article text from raw HTML. No network call."""
    import trafilatura

    text = trafilatura.extract(html, url=url)
    return _require_text(text or "", url or "the page")


def _require_text(text: str, source: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ExtractError(f"no readable text found in {source}")
    return cleaned
