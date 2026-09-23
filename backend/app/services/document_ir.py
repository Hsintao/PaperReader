"""Reader-facing intermediate representation of a parsed paper.

The worker publishes a stable manifest (see :mod:`app.services.document_manifest`);
this module defines the typed nodes PaperReader builds from it and the semantic
roles that drive the annotated PDF and the reader's categorisation.

Every node carries the page it came from and its box in page points in PDF user
space (the origin at the page's bottom-left corner), matching the manifest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

Rect = tuple[float, float, float, float]

# Semantic role of a text block. The annotation overlay picks a colour per
# role, and the reader uses it to tell prose from bibliography and apparatus.
TextRole = str


@dataclass
class TableCell:
    """One table cell's source text and the box its text occupies."""

    text: str
    bbox: Rect | None = None
    translated: str = ""


@dataclass
class TextRun:
    text: str


@dataclass
class InlineMath:
    latex: str
    bbox: Rect | None = None


Run = Union[TextRun, InlineMath]


@dataclass
class Title:
    level: int
    text: str
    page_index: int = -1
    bbox: Rect | None = None
    source_text: str = ""
    role: str = "body"
    block_type: str = ""


@dataclass
class Author:
    text: str
    page_index: int = -1
    bbox: Rect | None = None
    source_text: str = ""
    role: str = "author"
    block_type: str = ""


@dataclass
class Paragraph:
    runs: list[Run] = field(default_factory=list)
    page_index: int = -1
    bbox: Rect | None = None
    source_text: str = ""
    role: str = "body"
    block_type: str = ""


@dataclass
class ListBlock:
    """A list block whose items retain their inline text/math runs.

    Bibliography entries arrive as list items as well as paragraphs, so the IR
    keeps them apart from prose instead of folding them into it.
    """

    list_type: str = ""
    items: list[list[Run]] = field(default_factory=list)
    page_index: int = -1
    bbox: Rect | None = None
    item_boxes: list[Rect | None] = field(default_factory=list)
    source_text: str = ""
    role: str = "body"
    block_type: str = ""


@dataclass
class DisplayMath:
    latex: str
    page_index: int = -1
    bbox: Rect | None = None
    number: str = ""
    source_text: str = ""
    role: str = "body"
    block_type: str = "formula"


@dataclass
class Image:
    """A figure. ``rel_path`` is empty when the worker kept the artwork as-is."""

    rel_path: str = ""
    caption: str = ""
    page_index: int = -1
    bbox: Rect | None = None
    caption_bbox: Rect | None = None
    translated_caption: str = ""
    source_text: str = ""
    role: str = "body"
    block_type: str = "figure"


@dataclass
class Table:
    rel_path: str = ""
    html: str = ""
    caption: str = ""
    page_index: int = -1
    bbox: Rect | None = None
    caption_bbox: Rect | None = None
    cells: list[TableCell] = field(default_factory=list)
    translated_caption: str = ""
    source_text: str = ""
    role: str = "body"
    block_type: str = "table"


Block = Union[Title, Author, Paragraph, ListBlock, DisplayMath, Image, Table]


# ---------------------------------------------------------------------------
# Semantic roles
# ---------------------------------------------------------------------------

_ABSTRACT_HEADING_PATTERN = re.compile(r"^\s*(?:abstract|summary)\s*$", re.IGNORECASE)
_KEYWORDS_HEADING_PATTERN = re.compile(
    r"^\s*(?:index\s+terms|keywords?|key\s+words|ccs\s+concepts)\b", re.IGNORECASE
)
_ACKNOWLEDGEMENT_HEADING_PATTERN = re.compile(
    r"^\s*(?:acknowledge?ments?|acknowledgements?)\s*$", re.IGNORECASE
)
_APPENDIX_HEADING_PATTERN = re.compile(r"^\s*(?:appendix|appendices)\b", re.IGNORECASE)
_REFERENCE_HEADING_PATTERN = re.compile(
    r"^\s*(?:references|bibliography)\s*$", re.IGNORECASE
)
_AFFILIATION_PATTERN = re.compile(
    r"\b(?:university|universit[ée]|institute|institution|school\s+of|department|"
    r"faculty|college|academy|laborator(?:y|ies)|center\s+for|centre\s+for|"
    r"corresponding\s+author|@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
    re.IGNORECASE,
)
_REFERENCE_ENTRY_PATTERN = re.compile(r"^\s*\[\d{1,4}\]")

# Layout labels the worker reports that already name the role.
_LABEL_ROLES = {
    "footnote": "footnote",
    "code": "code",
    "algorithm": "algorithm",
    "reference_heading": "reference_heading",
    "reference": "reference_entry",
    "reference_content": "reference_entry",
    "header": "running",
    "page_header": "running",
    "footer": "running",
    "page_footer": "running",
}

# Section state that survives across blocks until the next structural heading.
_SECTION_ROLES = {
    "abstract": "abstract",
    "keywords": "keywords",
    "appendix": "appendix",
}


def _heading_role(text: str) -> str:
    stripped = text.strip()
    if _REFERENCE_HEADING_PATTERN.match(stripped):
        return "reference_heading"
    if _ABSTRACT_HEADING_PATTERN.match(stripped):
        return "abstract"
    if _KEYWORDS_HEADING_PATTERN.match(stripped):
        return "keywords"
    if _ACKNOWLEDGEMENT_HEADING_PATTERN.match(stripped):
        return "acknowledgement"
    if _APPENDIX_HEADING_PATTERN.match(stripped):
        return "appendix"
    return ""


def _looks_like_author_name(text: str) -> bool:
    """A short byline, not a sentence.

    A byline is short, holds no sentence-ending punctuation, and names no
    affiliation, which is what tells it apart from the address block that
    follows it. It is only considered on the paper's first page and before the
    first section heading: a body paragraph that happens to be short must never
    be mistaken for an author name.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 160:
        return False
    if _AFFILIATION_PATTERN.search(stripped):
        return False
    if any(char in stripped for char in ".!?\u3002\uff01\uff1f"):
        return False
    return True


def classify_roles(ir: list[Block]) -> None:
    """Tag every block with the semantic role the reader consumes.

    Only explicit headings and layout labels decide a role; anything uncertain
    stays ``body`` (or ``unknown`` for a block with no text), so a wrong guess
    can never categorise prose as bibliography or apparatus.
    """
    section = ""
    in_references = False
    front_matter = True
    byline_window = False
    for block in ir:
        label_role = _LABEL_ROLES.get(getattr(block, "block_type", ""), "")
        if isinstance(block, Title):
            heading = _heading_role(block.text)
            in_references = heading == "reference_heading"
            section = _SECTION_ROLES.get(heading, "")
            # The first real section heading ends the front matter; from then on
            # no paragraph can be a byline.
            if heading:
                front_matter = False
            byline_window = (
                not heading and front_matter and getattr(block, "page_index", -1) == 0
            )
            block.role = heading or "body"
            continue
        if isinstance(block, Author):
            block.role = "author"
            continue
        if label_role:
            block.role = label_role
            continue
        if in_references:
            if isinstance(block, ListBlock) and block.list_type == "reference_list":
                block.role = "reference_entry"
                continue
            if isinstance(block, Paragraph) and _REFERENCE_ENTRY_PATTERN.match(
                block.source_text
            ):
                block.role = "reference_entry"
                continue
        if isinstance(block, ListBlock):
            block.role = section or (
                "reference_entry" if block.list_type == "reference_list" else "body"
            )
            continue
        if isinstance(block, Paragraph):
            window = byline_window
            byline_window = False
            if section:
                block.role = section
                continue
            text = block.source_text or ""
            if front_matter and _AFFILIATION_PATTERN.search(text):
                block.role = "affiliation"
                continue
            if front_matter and window and _looks_like_author_name(text):
                block.role = "author"
                continue
            block.role = "body" if text.strip() else "unknown"
            continue
        block.role = "body"
