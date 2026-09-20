"""Intermediate representation (IR) for MinerU's structured output.

MinerU returns `content_list_v2.json` as a list of pages, each page being a
list of typed blocks (title, paragraph, equation_interline, image, table…).
This module parses that into a flat list of typed IR nodes that downstream
translation and layout rendering can consume independently.

Every block also carries the source geometry it came from (`page_index` and
`bbox`, in page points with the origin at the page's lower-left corner) plus
span-level boxes, so the layout renderer can pin the translation to the
original paragraph's coordinates and lift inline formulas out of the source
page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Union

Rect = tuple[float, float, float, float]

# Semantic role of a text block. The layout template picks font family, size
# and leading from the role; translation decides from it what enters the queue.
TextRole = Literal[
    "body",
    "abstract",
    "keywords",
    "author",
    "affiliation",
    "footnote",
    "code",
    "algorithm",
    "acknowledgement",
    "appendix",
    "reference_heading",
    "reference_entry",
    "running",
    "unknown",
]

# Span kinds shared with MinerU's middle.json.
SPAN_TEXT = "text"
SPAN_INLINE_MATH = "inline_equation"
SPAN_DISPLAY_MATH = "interline_equation"
SPAN_IMAGE = "image"
SPAN_TABLE = "table"


@dataclass
class Span:
    """Smallest source unit: a run of text or a formula, with its own box."""

    kind: str = SPAN_TEXT
    bbox: Rect | None = None
    text: str = ""
    latex: str = ""


@dataclass
class TableCell:
    """One table cell's source text and the box its text occupies."""

    text: str
    bbox: Rect | None = None
    spans: list[Span] = field(default_factory=list)
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


@dataclass
class Author:
    text: str
    page_index: int = -1
    bbox: Rect | None = None
    role: str = "author"


@dataclass
class Paragraph:
    runs: list[Run] = field(default_factory=list)
    page_index: int = -1
    bbox: Rect | None = None
    spans: list[Span] = field(default_factory=list)
    source_text: str = ""
    role: str = "body"


@dataclass
class ListBlock:
    """A MinerU list block whose items retain their inline text/math runs.

    MinerU uses this block type for reference lists as well as ordinary lists.
    Keeping it in the IR prevents entire bibliography pages from being silently
    dropped by the translation/rendering pipeline.
    """

    list_type: str = ""
    items: list[list[Run]] = field(default_factory=list)
    page_index: int = -1
    bbox: Rect | None = None
    item_boxes: list[Rect | None] = field(default_factory=list)
    source_text: str = ""
    role: str = "body"


@dataclass
class DisplayMath:
    latex: str
    page_index: int = -1
    bbox: Rect | None = None
    number: str = ""


@dataclass
class Image:
    rel_path: str
    caption: str = ""
    # MinerU sometimes splits one multi-panel figure into several adjacent
    # blocks (and may label one panel as ``chart`` instead of ``image``).
    # Keep the source geometry so the layout renderer can keep those panels
    # together with the rest of the source artwork.
    page_index: int = -1
    bbox: Rect | None = None
    caption_bbox: Rect | None = None
    translated_caption: str = ""


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


Block = Union[Title, Author, Paragraph, ListBlock, DisplayMath, Image, Table]


def _flatten_pages(content_blocks: Iterable) -> list[dict]:
    """`content_list_v2.json` may be a list of pages (list[list[dict]]) or a
    flat list of blocks. Normalize to a flat list of block dicts."""
    flat: list[dict] = []
    for entry in content_blocks:
        if isinstance(entry, list):
            for block in entry:
                if isinstance(block, dict):
                    flat.append(block)
        elif isinstance(entry, dict):
            flat.append(entry)
    return flat


def _flatten_pages_with_positions(content_blocks: Iterable) -> list[tuple[int, dict]]:
    """Flatten blocks while retaining their source page number.

    A flat MinerU response has no reliable page metadata, so ``-1`` is used
    and panel grouping is disabled for those blocks.
    """
    flat: list[tuple[int, dict]] = []
    for page_index, entry in enumerate(content_blocks):
        if isinstance(entry, list):
            for block in entry:
                if isinstance(block, dict):
                    flat.append((page_index, block))
        elif isinstance(entry, dict):
            flat.append((-1, entry))
    return flat


def _parse_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _has_typed_math(content_blocks: Iterable) -> bool:
    """True when the source marks math explicitly (equation_inline/_interline).

    `content_list_v2.json` types real math as dedicated items but strips the
    backslash from escaped currency in plain text (``\\$10.99`` becomes
    ``$10.99``). In that format every bare ``$`` inside a text item is a
    literal dollar, so pairing them as inline math would swallow prose such
    as ``Big & Tall`` into math mode. Legacy outputs without typed math keep
    the heuristic ``$…$`` splitting.
    """
    stack = [content_blocks]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("type") in {"equation_inline", "equation_interline"}:
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _runs_from_paragraph_content(items: Iterable, split_bare_dollars: bool = True) -> list[Run]:
    runs: list[Run] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        content = item.get("content")
        if kind == "text" and isinstance(content, str):
            # MinerU sometimes returns an entire sentence (including `$…$`
            # inline math) as a single "text" item instead of splitting it into
            # alternating "text"/"equation_inline" items.  Split here so that
            # inline math never reaches the translation layer as plain text.
            for run in _split_text_at_math(content, split_bare_dollars=split_bare_dollars):
                runs.append(run)
        elif kind == "equation_inline" and isinstance(content, str):
            latex = content.strip()
            if latex:
                runs.append(InlineMath(latex=latex))
    return runs



def _split_text_at_math(text: str, split_bare_dollars: bool = True) -> list[Run]:
    """Split a raw text string at math-delimiter boundaries.

    Returns a list of alternating TextRun / InlineMath nodes so that formula
    content is never sent to the translation layer as translatable prose.

    ``split_bare_dollars=False`` keeps explicit ``\\(\\)`` / ``\\[\\]``
    delimiters but treats every bare ``$`` as a literal dollar (currency),
    for sources that already type their math.
    """
    runs: list[Run] = []
    cursor = 0
    prose_start = 0

    def escaped(offset: int) -> bool:
        slashes = 0
        offset -= 1
        while offset >= 0 and text[offset] == "\\":
            slashes += 1
            offset -= 1
        return slashes % 2 == 1

    while cursor < len(text):
        opening = text[cursor]
        closing = ""
        content_start = cursor + 1
        if opening == "$" and split_bare_dollars and not escaped(cursor):
            closing = "$"
        elif text.startswith(r"\[", cursor):
            closing = r"\]"
            content_start = cursor + 2
        elif text.startswith(r"\(", cursor):
            closing = r"\)"
            content_start = cursor + 2
        else:
            cursor += 1
            continue

        if closing == "$":
            end = content_start
            while end < len(text):
                if text[end] == "\n":
                    end = -1
                    break
                if text[end] == "$" and not escaped(end):
                    break
                end += 1
        else:
            end = text.find(closing, content_start)
        if end < 0 or end >= len(text):
            cursor = content_start
            continue

        latex = text[content_start:end].strip()
        if not latex:
            cursor = end + len(closing)
            continue
        if closing == "$":
            prose_words = re.findall(r"[A-Za-z]{2,}", latex)
            if (
                re.search(r"\\[\[(]", latex)
                or (re.match(r"\d", latex) and len(prose_words) >= 2)
            ):
                cursor = content_start
                continue
        elif opening in latex or re.search(r"(?<!\\)\$", latex):
            cursor = content_start
            continue
        prose = text[prose_start:cursor]
        if prose:
            runs.append(TextRun(text=prose))
        runs.append(InlineMath(latex=latex))
        cursor = end + len(closing)
        prose_start = cursor

    tail = text[prose_start:]
    if tail:
        runs.append(TextRun(text=tail))
    return runs



def _title_text(content: dict) -> str:
    parts: list[str] = []
    for item in content.get("title_content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            value = item.get("content")
            if isinstance(value, str):
                parts.append(value)
    return " ".join(part.strip() for part in parts if part.strip()).strip()


_AUTHOR_HINTS = (
    "ieee",
    "student member",
    "senior member",
    "life member",
    "corresponding author",
    "university",
    "institute",
    "school of",
    "department of",
    "college of",
    "@",
    "<sup>",
)


def _paragraph_plain_text(content: dict) -> str:
    """Join a paragraph block's plain-text runs (ignoring inline math)."""
    items = content.get("paragraph_content") if isinstance(content, dict) else None
    if not items:
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "text":
            value = item.get("content")
            if isinstance(value, str):
                parts.append(value)
    return " ".join(part.strip() for part in parts if part.strip()).strip()


def _looks_like_authors(text: str) -> bool:
    """Whether a front-matter paragraph is the paper's byline.

    An affiliation or contact block matches the same vocabulary, so it is
    excluded here: it stays a translatable paragraph instead of being folded
    into the untranslated author node.
    """
    stripped = text.strip()
    if _AFFILIATION_PATTERN.search(stripped):
        return False
    if any(char in stripped for char in ".!?\u3002\uff01\uff1f"):
        return False
    return any(hint in stripped.lower() for hint in _AUTHOR_HINTS)


def _header_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _to_page_rect(
    raw: object, page_size: tuple[float, float] | None, *, normalized: bool
) -> Rect | None:
    """Convert a MinerU box into page points with the origin at lower-left.

    `content_list_v2.json` boxes are normalized to 0-1000 of the page; the
    boxes in `middle.json` are already page points. Both measure y from the top
    of the page, while PDF user space measures it from the bottom.
    """
    rect = _parse_bbox(raw)
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    width, height = page_size if page_size else (0.0, 0.0)
    if not height:
        return None
    if normalized:
        if not width:
            return None
        x0 = x0 / 1000.0 * width
        x1 = x1 / 1000.0 * width
        y0 = y0 / 1000.0 * height
        y1 = y1 / 1000.0 * height
    return (x0, height - y1, x1, height - y0)


def blocks_to_ir(
    content_blocks: Iterable,
    page_sizes: list[tuple[float, float]] | None = None,
    *,
    normalized_boxes: bool = True,
    frames: list | None = None,
) -> list[Block]:
    """Convert MinerU `content_list_v2.json` into a flat list of IR blocks.

    Besides the typed blocks, this also:
      * drops short paragraph text that repeats verbatim across pages (running
        headers/footers that MinerU occasionally emits as paragraphs);
      * turns the author/affiliation paragraph right after the paper title into
        an `Author` node so it is never translated;
      * attaches page geometry (page index and box) when `page_sizes` is given,
        which is what lets the layout renderer pin translations to the source;
      * recovers a figure/table caption's box from the text layer when `frames`
        is given and the parser reported the caption as text only.
    """
    positioned = _flatten_pages_with_positions(content_blocks)
    # Typed-math outputs (content_list_v2) already mark formulas explicitly and
    # strip the backslash from escaped currency, so bare "$" in prose must stay
    # literal there instead of being paired into inline math.
    split_bare_dollars = not _has_typed_math(content_blocks)

    def page_size(page_index: int) -> tuple[float, float] | None:
        if page_sizes and 0 <= page_index < len(page_sizes):
            return page_sizes[page_index]
        return None

    para_counts: dict[str, int] = {}
    for _, block in positioned:
        if block.get("type") == "paragraph":
            text = _paragraph_plain_text(block.get("content") or {})
            if 0 < len(text) <= 80:
                key = _header_key(text)
                para_counts[key] = para_counts.get(key, 0) + 1
    repeated = {key for key, count in para_counts.items() if count >= 2}

    ir: list[Block] = []
    seen_title = False
    author_handled = False

    for page_index, block in positioned:
        kind = block.get("type")
        content = block.get("content") or {}
        bbox = _to_page_rect(
            block.get("bbox"), page_size(page_index), normalized=normalized_boxes
        )

        if kind == "title":
            text = _title_text(content) if isinstance(content, dict) else ""
            if not text:
                continue
            level = content.get("level") if isinstance(content, dict) else None
            try:
                level_int = int(level) if level is not None else 1
            except (TypeError, ValueError):
                level_int = 1
            level_int = max(1, level_int)
            seen_title = True
            ir.append(
                Title(
                    level=level_int,
                    text=text,
                    page_index=page_index,
                    bbox=bbox,
                    source_text=text,
                )
            )

        elif kind == "paragraph":
            raw_text = _paragraph_plain_text(content)
            if raw_text and _header_key(raw_text) in repeated:
                continue
            if not seen_title:
                # Running header / stray text before the paper title.
                continue
            items = content.get("paragraph_content") if isinstance(content, dict) else None
            runs = _runs_from_paragraph_content(items or [], split_bare_dollars=split_bare_dollars)
            if (
                not author_handled
                and raw_text
                and _looks_like_authors(raw_text)
            ):
                ir.append(Author(text=raw_text, page_index=page_index, bbox=bbox))
                author_handled = True
                continue
            if runs:
                ir.append(
                    Paragraph(
                        runs=runs,
                        page_index=page_index,
                        bbox=bbox,
                        source_text="".join(
                            run.text for run in runs if isinstance(run, TextRun)
                        ),
                    )
                )

        elif kind == "list":
            if not isinstance(content, dict):
                continue
            list_type = str(content.get("list_type") or "").strip()
            parsed_items: list[list[Run]] = []
            for item in content.get("list_items") or []:
                if not isinstance(item, dict):
                    continue
                item_content = item.get("item_content") or []
                if isinstance(item_content, str):
                    item_content = [{"type": "text", "content": item_content}]
                runs = _runs_from_paragraph_content(item_content, split_bare_dollars=split_bare_dollars)
                if runs:
                    parsed_items.append(runs)
            if parsed_items:
                ir.append(
                    ListBlock(
                        list_type=list_type,
                        items=parsed_items,
                        page_index=page_index,
                        bbox=bbox,
                        source_text=" ".join(
                            "".join(
                                run.text
                                for run in item
                                if isinstance(run, TextRun)
                            )
                            for item in parsed_items
                        ),
                    )
                )

        elif kind == "equation_interline":
            latex = ""
            if isinstance(content, dict):
                latex = (content.get("math_content") or "").strip()
            if latex:
                ir.append(
                    DisplayMath(latex=latex, page_index=page_index, bbox=bbox)
                )

        elif kind in {"image", "chart"}:
            rel_path = ""
            caption = ""
            if isinstance(content, dict):
                source = content.get("image_source") or {}
                if isinstance(source, dict):
                    rel_path = (source.get("path") or "").strip()
                # MinerU uses a distinct caption field for chart blocks even
                # though both image and chart sources are raster assets.
                cap_items = (
                    content.get("image_caption")
                    or content.get("chart_caption")
                    or content.get("caption")
                    or []
                )
                if isinstance(cap_items, list):
                    caption = " ".join(
                        item.get("content", "")
                        for item in cap_items
                        if isinstance(item, dict) and item.get("type") == "text"
                    ).strip()
                elif isinstance(cap_items, str):
                    caption = cap_items.strip()
                caption = _join_caption(caption, content.get("image_footnote"))
            if rel_path:
                ir.append(
                    Image(
                        rel_path=rel_path,
                        caption=caption,
                        page_index=page_index,
                        bbox=bbox,
                    )
                )

        elif kind == "table":
            rel_path = ""
            html = ""
            caption = ""
            if isinstance(content, dict):
                source = content.get("image_source") or {}
                if isinstance(source, dict):
                    rel_path = (source.get("path") or "").strip()
                html = (content.get("html") or content.get("table_body") or "").strip()
                cap_items = content.get("table_caption") or content.get("caption") or []
                if isinstance(cap_items, list):
                    caption = " ".join(
                        item.get("content", "")
                        for item in cap_items
                        if isinstance(item, dict) and item.get("type") == "text"
                    ).strip()
                elif isinstance(cap_items, str):
                    caption = cap_items.strip()
                caption = _join_caption(caption, content.get("table_footnote"))
            if rel_path or html:
                ir.append(
                    Table(
                        rel_path=rel_path,
                        html=html,
                        caption=caption,
                        page_index=page_index,
                        bbox=bbox,
                    )
                )

        # Other block kinds (page_footer, header, etc.) are intentionally skipped.

    if frames:
        attach_caption_boxes(ir, frames)
    classify_roles(ir)
    return ir


def _join_caption(caption: str, footnotes: object) -> str:
    """Append a figure/table's own footnote text to its caption."""
    if isinstance(footnotes, str):
        note = footnotes.strip()
    elif isinstance(footnotes, list):
        note = " ".join(
            item.get("content", "")
            for item in footnotes
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
    else:
        note = ""
    if not note:
        return caption
    return f"{caption} {note}".strip() if caption else note


def attach_caption_boxes(ir: list[Block], frames: list) -> int:
    """Recover caption geometry for blocks whose parser output has none.

    The structured content list reports a caption as text only, so its box is
    looked up on the page's own text layer. A caption that cannot be located
    keeps no box and is later left in the source language in place.
    """
    from app.services.layout_model import caption_rect

    lines_by_page: dict[int, list] = {}
    for frame in frames:
        if frame.has_text_layer:
            from app.services.layout_model import _cluster_lines

            lines_by_page[frame.index] = _cluster_lines(frame.chars)
    recovered = 0
    for block in ir:
        if not isinstance(block, (Image, Table)) or block.caption_bbox is not None:
            continue
        page_index = getattr(block, "page_index", -1)
        if not (0 <= page_index < len(frames)):
            continue
        lines = lines_by_page.get(page_index)
        if not lines:
            continue
        rect = caption_rect(block, frames[page_index], lines)
        if rect is None:
            continue
        block.caption_bbox = rect
        recovered += 1
    return recovered


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
_APPENDIX_HEADING_PATTERN = re.compile(
    r"^\s*(?:appendix|appendices)\b", re.IGNORECASE
)
_AFFILIATION_PATTERN = re.compile(
    r"\b(?:university|universit[ée]|institute|institution|school\s+of|department|"
    r"faculty|college|academy|laborator(?:y|ies)|center\s+for|centre\s+for|"
    r"corresponding\s+author|@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
    re.IGNORECASE,
)
_REFERENCE_ENTRY_PATTERN = re.compile(r"^\s*\[\d{1,4}\]")

# Parser-provided block types that already name the role.
_PARSER_ROLES = {
    "footnote": "footnote",
    "page_footnote": "footnote",
    "code": "code",
    "algorithm": "algorithm",
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

# Roles whose wording stays in the source language: author names, bibliography
# entries and running heads are printed as they appear in the source.
UNTRANSLATABLE_ROLES = {"author", "reference_entry", "running"}


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
    """A short front-matter line that reads as a byline, not a sentence.

    Author names arrive as their own block before any section heading. The
    check stays conservative: a byline is short, holds no sentence-ending
    punctuation, and names no affiliation, which is what tells it apart from
    the address block that follows it.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 160:
        return False
    if _AFFILIATION_PATTERN.search(stripped):
        return False
    return not any(char in stripped for char in ".!?\u3002\uff01\uff1f")


def classify_roles(ir: list[Block]) -> None:
    """Tag every block with the semantic role the layout template consumes.

    Only explicit headings and parser-provided block types decide a role;
    anything uncertain stays ``body`` (or ``unknown`` for a paragraph the
    parser gave no text for), so a wrong guess can never move text into a
    different translation or typography rule than plain prose.
    """
    section = ""
    in_references = False
    front_matter = False
    for block in ir:
        parser_role = _PARSER_ROLES.get(getattr(block, "block_type", ""), "")
        if isinstance(block, Title):
            heading = _heading_role(block.text)
            in_references = heading == "reference_heading"
            section = _SECTION_ROLES.get(heading, "")
            front_matter = not heading
            block.role = heading or "body"
            continue
        if isinstance(block, Author):
            block.role = "author"
            continue
        if parser_role:
            block.role = parser_role
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
            if section:
                block.role = section
                continue
            text = block.source_text or ""
            if _AFFILIATION_PATTERN.search(text):
                block.role = "affiliation"
                continue
            if front_matter and _looks_like_author_name(text):
                block.role = "author"
                continue
            block.role = "body" if text.strip() else "unknown"
            continue
        block.role = "body"


def _block_slots(block: Block):
    """Yield `(source_text, target, attribute)` for one block's strings.

    Blocks that belong to a continuation group are translated through their
    group's first block, so they contribute no slots of their own. Every other
    string is yielded, including the ones `translatable_mask` marks as
    source-language: they still need a slot so the mask and the write-back stay
    aligned, they are simply filled with their own source wording.
    """
    if getattr(block, "continuation_member", False) and not getattr(
        block, "continuation_group", None
    ):
        return
    if isinstance(block, Title):
        yield block.text, block, "text"
    elif isinstance(block, Paragraph):
        for run in block.runs:
            if isinstance(run, TextRun):
                yield run.text, run, "text"
    elif isinstance(block, ListBlock):
        for item in block.items:
            for run in item:
                if isinstance(run, TextRun):
                    yield run.text, run, "text"
    elif isinstance(block, (Image, Table)):
        # Figure/table labels and notes translate; `caption` keeps the source
        # wording so the renderer can match and fall back to it.
        if block.caption.strip():
            yield block.caption, block, "translated_caption"
        if isinstance(block, Table):
            for cell in block.cells:
                if cell.text.strip():
                    yield cell.text, cell, "translated"


def _translation_slots(ir: list[Block]):
    """Yield `(source_text, target, attribute)` for every translatable string.

    Running headers never enter the queue: their original wording is reused
    verbatim from the source page. Captions, table notes and table cell text
    do enter it, because the renderer replaces them in place.
    """
    for block in ir:
        yield from _block_slots(block)


def collect_translatable_strings(ir: list[Block]) -> list[str]:
    """Return all human-readable strings in `ir`, in document order."""
    return [source for source, _, _ in _translation_slots(ir)]


_REFERENCE_HEADING_PATTERN = re.compile(r"^\s*(?:references|bibliography)\s*$", re.IGNORECASE)


def translatable_blocks(ir: list[Block]) -> list[bool]:
    """Per-block twin of `translatable_mask`: False marks source-language blocks.

    Bibliography entries stay English so they remain searchable; they also
    burn a large share of the translation budget. The masked region starts
    after a References/Bibliography heading — the heading itself translates,
    because the section title is Chinese in the translated PDF — or at a typed
    reference list, which MinerU emits even without the heading, and ends at
    the next title, so an appendix placed after the bibliography still
    translates.
    """
    allowed: list[bool] = []
    in_references = False
    for block in ir:
        role = getattr(block, "role", "")
        if isinstance(block, Title):
            in_references = (
                _REFERENCE_HEADING_PATTERN.match(block.text.strip()) is not None
            )
        elif isinstance(block, ListBlock) and block.list_type == "reference_list":
            in_references = True
        # The heading itself is translated: the translated PDF prints
        # "参考文献", not "References". Only the entries below it stay English.
        allowed.append(
            role == "reference_heading"
            or (not in_references and role not in UNTRANSLATABLE_ROLES)
        )
    return allowed


def translatable_mask(ir: list[Block]) -> list[bool]:
    """Boolean twin of `collect_translatable_strings`: False marks strings that
    must stay in the source language."""
    mask: list[bool] = []
    for block, allowed in zip(ir, translatable_blocks(ir)):
        mask.extend([allowed] * sum(1 for _ in _block_slots(block)))
    return mask


def apply_translations(ir: list[Block], translations: list[str]) -> None:
    """Write `translations` back into `ir` in the same order produced by
    `collect_translatable_strings`. Lengths must match."""
    expected = sum(1 for _ in _translation_slots(ir))
    if len(translations) != expected:
        raise ValueError(
            f"Translation count mismatch: got {len(translations)}, expected "
            f"{expected}"
        )
    cursor = 0
    for _, target, attribute in _translation_slots(ir):
        setattr(target, attribute, translations[cursor])
        cursor += 1


# ---------------------------------------------------------------------------
# Paragraphs that the parser split across two regions
# ---------------------------------------------------------------------------

_SENTENCE_ENDINGS = (
    ".", "\u3002", "!", "\uff01", "?", "\uff1f", ":", "\uff1a", ";", "\uff1b",
    "\"", "\u201d", ")", "\uff09", "]", "\u3011",
)
_BREAK_CHARACTERS = "\u3002\uff01\uff1f\uff1b\uff0c\u3001,;.!? \n"


@dataclass
class ContinuationGroup:
    """One paragraph that the parser reported as several adjacent blocks."""

    blocks: list[Block]
    source_text: str
    lengths: list[int]


def plain_paragraph_text(block: Block) -> str | None:
    """The block's text when it is a plain paragraph, else None.

    Only plain text paragraphs take part in continuation grouping: a paragraph
    with an inline formula has to keep its own formula geometry.
    """
    if not isinstance(block, Paragraph) or not block.runs:
        return None
    if any(not isinstance(run, TextRun) for run in block.runs):
        return None
    text = "".join(run.text for run in block.runs)
    return text if text.strip() else None


def _continues_text(first: str, second: str) -> bool:
    first = first.strip()
    second = second.strip()
    if len(first) < 20 or len(second) < 20:
        return False
    if first.endswith(_SENTENCE_ENDINGS):
        return False
    if first.endswith(("-", "\u2010")):
        return True
    return second[0].islower()


def _adjacent_regions(first: Block, second: Block, frames) -> bool:
    """Whether two blocks sit next to each other in reading order.

    Two shapes count: the second block directly under the first in the same
    column, and the second block starting the next column after the first ran
    out of room low on the page.
    """
    first_box = getattr(first, "bbox", None)
    second_box = getattr(second, "bbox", None)
    if not first_box or not second_box:
        return False
    narrower = min(first_box[2] - first_box[0], second_box[2] - second_box[0])
    if narrower <= 0:
        return False
    overlap = min(first_box[2], second_box[2]) - max(first_box[0], second_box[0])
    same_column = overlap / narrower >= 0.6

    first_page = getattr(first, "page_index", -1)
    second_page = getattr(second, "page_index", -1)
    if first_page == second_page:
        if not (0 <= first_page < len(frames)):
            return False
        if same_column:
            # Same column: the second block follows directly underneath.
            height = first_box[3] - first_box[1]
            return abs(first_box[1] - second_box[3]) <= max(6.0, 1.5 * height)
        # Column jump: the first part ran out of room low on the page and the
        # text continues at the top of the next (right-hand) column.
        frame = frames[first_page]
        return (
            second_box[0] > first_box[0]
            and second_box[3] > first_box[3]
            and first_box[1] - frame.origin[1] <= 0.35 * frame.height
        )

    if second_page == first_page + 1:
        if not (0 <= first_page < len(frames) and 0 <= second_page < len(frames)):
            return False
        first_frame = frames[first_page]
        second_frame = frames[second_page]
        near_bottom = first_box[1] - first_frame.origin[1] <= 0.18 * first_frame.height
        near_top = (
            second_frame.origin[1] + second_frame.height - second_box[3]
            <= 0.18 * second_frame.height
        )
        if not (near_bottom and near_top):
            return False
        # Same column on the next page, or its next column.
        return same_column or second_box[0] > first_box[0]
    return False


def plan_continuation_groups(ir: list[Block], frames) -> list[ContinuationGroup]:
    """Find paragraphs the parser split over several adjacent regions."""
    allowed = translatable_blocks(ir)
    groups: list[ContinuationGroup] = []
    index = 0
    while index < len(ir):
        chain = [index]
        cursor = index
        while cursor + 1 < len(ir):
            if getattr(ir[cursor + 1], "continuation_member", False):
                break
            if not (allowed[cursor] and allowed[cursor + 1]):
                break
            first_text = plain_paragraph_text(ir[cursor])
            second_text = plain_paragraph_text(ir[cursor + 1])
            if first_text is None or second_text is None:
                break
            if not _continues_text(first_text, second_text):
                break
            if not _adjacent_regions(ir[cursor], ir[cursor + 1], frames):
                break
            chain.append(cursor + 1)
            cursor += 1
        if len(chain) > 1:
            blocks = [ir[position] for position in chain]
            texts = [plain_paragraph_text(block) or "" for block in blocks]
            groups.append(
                ContinuationGroup(
                    blocks=blocks,
                    source_text=" ".join(text.strip() for text in texts),
                    lengths=[max(1, len(text.strip())) for text in texts],
                )
            )
        index = cursor + 1
    return groups


def merge_continuation_groups(groups: list[ContinuationGroup]) -> None:
    """Translate each group as one paragraph, keeping the member blocks."""
    for group in groups:
        for block in group.blocks:
            setattr(block, "continuation_member", True)
            # Remember where this member's inline formulas sit so that writing
            # the distributed translation back keeps them in place.
            setattr(block, "continuation_runs", list(block.runs))
        first = group.blocks[0]
        first.runs = [TextRun(text=group.source_text)]
        first.continuation_group = group


def _runs_with_formulas(template: list, text: str) -> list:
    """`text` with the template's formulas re-inserted where they sat in it."""
    formulas = [run for run in template if not isinstance(run, TextRun)]
    if not formulas:
        return [TextRun(text=text)]
    source_length = max(1, sum(len(run.text) for run in template if isinstance(run, TextRun)))
    offsets: list[int] = []
    cursor = 0
    for run in template:
        if isinstance(run, TextRun):
            cursor += len(run.text)
            continue
        offsets.append(cursor)
    runs: list = []
    start = 0
    for offset, formula in zip(offsets, formulas):
        fraction = min(1.0, offset / source_length)
        cut = max(start, min(len(text), int(round(fraction * len(text)))))
        if cut > start:
            runs.append(TextRun(text=text[start:cut]))
        runs.append(formula)
        start = cut
    if start < len(text):
        runs.append(TextRun(text=text[start:]))
    return runs or [TextRun(text=text)]


def _nearest_break(text: str, start: int, target: int) -> int:
    """Move a split point to the closest punctuation or space."""
    span = max(1, target - start)
    window = max(4, int(0.18 * span))
    best = None
    for offset in range(0, window + 1):
        for candidate in (target - offset, target + offset):
            if candidate <= start or candidate >= len(text):
                continue
            if text[candidate - 1] in _BREAK_CHARACTERS:
                distance = abs(candidate - target)
                if best is None or distance < best[0]:
                    best = (distance, candidate)
    return best[1] if best else target


def split_proportionally(text: str, lengths: list[int]) -> list[str]:
    """Split a translation across regions in proportion to the source parts."""
    if not lengths:
        return []
    if len(lengths) == 1:
        return [text]
    total = sum(lengths)
    if total <= 0 or not text:
        return [text] + [""] * (len(lengths) - 1)
    parts: list[str] = []
    cursor = 0
    cumulative = 0
    for index, length in enumerate(lengths):
        cumulative += length
        if index == len(lengths) - 1:
            parts.append(text[cursor:])
            break
        target = int(round(len(text) * cumulative / total))
        target = max(cursor, min(len(text), target))
        cut = _nearest_break(text, cursor, target)
        parts.append(text[cursor:cut])
        cursor = cut
    return parts


def split_continuation_groups(groups: list[ContinuationGroup]) -> list[str]:
    """Write each group's translation back into its several regions."""
    notes: list[str] = []
    for group in groups:
        first = group.blocks[0]
        translated = "".join(
            run.text for run in first.runs if isinstance(run, TextRun)
        )
        parts = split_proportionally(translated, group.lengths)
        if any(not part.strip() for part in parts):
            # Nothing sensible to distribute: keep the whole translation in the
            # first region and leave the others in the source language.
            for member in group.blocks[1:]:
                member.runs = [
                    TextRun(text=getattr(member, "source_text", "") or "")
                ]
            notes.append(
                "a paragraph spanning several regions could not be split; "
                "its first region keeps the translation"
            )
        else:
            for block, part in zip(group.blocks, parts):
                template = getattr(block, "continuation_runs", None) or []
                block.runs = _runs_with_formulas(template, part.strip())
            notes.append(
                f"paragraph spanning {len(group.blocks)} regions translated once "
                "and distributed back across them"
            )
        for block in group.blocks:
            block.continuation_member = False
            for name in ("continuation_group", "continuation_runs"):
                if hasattr(block, name):
                    delattr(block, name)
    return notes
