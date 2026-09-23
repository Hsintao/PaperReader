"""`_clean_extracted_text` turns manifest Markdown into reader text.

The translator's Markdown is the document: page markers it leaves behind for
pages it could not read are dropped, and blank-line runs collapse to a single
paragraph break.
"""

from app.services.document_pipeline import _clean_extracted_text


def test_clean_extracted_text_leaves_clean_markdown_alone() -> None:
    assert _clean_extracted_text("Heading\n\nBody") == "Heading\n\nBody"


def test_clean_extracted_text_drops_missing_page_markers() -> None:
    text = "[MISSING_PAGE_EMPTY:1]\n\n[MISSING_PAGE_EMPTY:2]\n\n## Section\n\nBody"

    cleaned = _clean_extracted_text(text)

    assert "MISSING_PAGE" not in cleaned
    assert cleaned == "## Section\n\nBody"


def test_clean_extracted_text_collapses_blank_line_runs() -> None:
    assert _clean_extracted_text("A\n\n\n\n\nB") == "A\n\nB"


def test_clean_extracted_text_keeps_a_marker_inside_a_line() -> None:
    text = "See [MISSING_PAGE_EMPTY:1] for the appendix"

    assert _clean_extracted_text(text) == text


def test_clean_extracted_text_tolerates_empty_input() -> None:
    assert _clean_extracted_text("") == ""