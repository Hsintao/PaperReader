"""Source-page geometry for the layout renderer.

Everything the renderer needs to pin a translation to the source page lives
here:

* per-character measurements from the PDF text layer (size, weight, box), used
  to derive each block's font size, weight, alignment and line spacing;
* page frames (size, rotation) and the set of source regions already accounted
  for by the parser, so text the parser lost can be told apart from text it
  deliberately kept;
* Block IR materialisation from MinerU's `middle.json`, from
  `content_list_v2.json`, or from a page's own text layer (local parsing).

All rectangles are page points with the origin at the media box lower-left
corner, matching PDF user space; the layout renderer removes text operators in
the same space.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from app.services.mineru_layout import (
    SPAN_DISPLAY_MATH,
    SPAN_IMAGE,
    SPAN_INLINE_MATH,
    SPAN_TABLE,
    SPAN_TEXT,
    Block,
    DisplayMath,
    Image,
    InlineMath,
    Paragraph,
    Span,
    Table,
    TableCell,
    TextRun,
    Title,
    _parse_bbox,
    _to_page_rect,
)

Rect = tuple[float, float, float, float]

_BOLD_PATTERN = re.compile(r"bold|black|heavy|semibold", re.IGNORECASE)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# Below this many characters a page is treated as image-backed (scanned or
# OCR-only), so replaced blocks are masked instead of having text removed.
TEXT_LAYER_MIN_CHARS = 24


@dataclass
class SourceChar:
    char: str
    rect: Rect
    size: float = 0.0
    bold: bool = False


@dataclass
class PageFrame:
    index: int
    width: float
    height: float
    origin: tuple[float, float] = (0.0, 0.0)
    rotation: int = 0
    chars: list[SourceChar] = field(default_factory=list)
    known_regions: list[Rect] = field(default_factory=list)

    @property
    def has_text_layer(self) -> bool:
        return len(self.chars) >= TEXT_LAYER_MIN_CHARS

    @property
    def rect(self) -> Rect:
        x, y = self.origin
        return (x, y, x + self.width, y + self.height)


def _font_is_bold(textpage, index: int, buffer=None) -> bool:
    import ctypes

    import pypdfium2.raw as pdfium_c

    buffer = buffer if buffer is not None else ctypes.create_string_buffer(256)
    flags = ctypes.c_int(0)
    try:
        pdfium_c.FPDFText_GetFontInfo(
            textpage.raw, index, buffer, 256, ctypes.byref(flags)
        )
    except Exception:
        return False
    name = buffer.value.decode("utf-8", "replace")
    return bool(_BOLD_PATTERN.search(name))


def measure_pages(source: str | Path | bytes) -> list[PageFrame]:
    """Measure every page's text layer, returning one frame per page."""
    import ctypes

    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    frames: list[PageFrame] = []
    document = pdfium.PdfDocument(source if isinstance(source, (bytes, bytearray)) else str(source))
    try:
        for index, page in enumerate(document):
            width, height = page.get_size()
            try:
                rotation = int(page.get_rotation())
            except Exception:
                rotation = 0
            chars: list[SourceChar] = []
            textpage = page.get_textpage()
            font_buffer = ctypes.create_string_buffer(256)
            try:
                count = textpage.count_chars()
                for position in range(count):
                    char = textpage.get_text_range(position, 1)
                    if not char or char in "\r\n\ufffe":
                        continue
                    box = textpage.get_charbox(position)
                    # PDFium reports a zero-size box for glyphs it cannot
                    # place. They carry no position, and keeping them corrupts
                    # line clustering and every geometry decision downstream.
                    # Whitespace is exempt: it often has no box either, and
                    # dropping it would glue words together.
                    if (
                        char.strip()
                        and (box[2] - box[0]) < 0.5
                        and (box[3] - box[1]) < 0.5
                    ):
                        continue
                    try:
                        size = float(pdfium_c.FPDFText_GetFontSize(textpage.raw, position))
                    except Exception:
                        size = 0.0
                    chars.append(
                        SourceChar(
                            char=char,
                            rect=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                            size=size,
                            bold=_font_is_bold(textpage, position, font_buffer),
                        )
                    )
            finally:
                textpage.close()
                page.close()
            frames.append(PageFrame(index=index, width=width, height=height, rotation=rotation, chars=chars))
    finally:
        document.close()
    return frames


# ---------------------------------------------------------------------------
# MinerU middle.json
# ---------------------------------------------------------------------------

_MIDDLE_SKIP_KEYS = {
    "img_path",
    "image_path",
    "html",
    "score",
    "index",
    "angle",
    "math_type",
}


def _middle_box(value, height: float) -> Rect | None:
    rect = _parse_bbox(value)
    if rect is None or not height:
        return None
    x0, y0, x1, y1 = rect
    return (x0, height - y1, x1, height - y0)


def _span_text(span: dict) -> str:
    content = span.get("content")
    return content if isinstance(content, str) else ""


def _spans_from_lines(lines: Iterable, height: float) -> list[tuple[Span, bool]]:
    """Flatten MinerU lines into (span, starts_new_line) pairs."""
    out: list[tuple[Span, bool]] = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        first = True
        for raw in line.get("spans") or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("type")
            box = _middle_box(raw.get("bbox"), height)
            if kind == SPAN_INLINE_MATH or kind == SPAN_DISPLAY_MATH:
                out.append(
                    (Span(kind=kind, bbox=box, latex=_span_text(raw).strip()), first)
                )
            elif kind in {SPAN_TEXT, None, "title", "paragraph"}:
                out.append(
                    (Span(kind=SPAN_TEXT, bbox=box, text=_span_text(raw)), first)
                )
            elif kind in {SPAN_IMAGE, SPAN_TABLE}:
                out.append(
                    (Span(kind=kind, bbox=box, text=_span_text(raw)), first)
                )
            first = False
    return out


def _merge_text(buffer: str, fragment: str, new_line: bool) -> str:
    if not buffer:
        return fragment
    if not fragment:
        return buffer
    if new_line and not buffer.endswith((" ", "-")) and not fragment.startswith(" "):
        return f"{buffer} {fragment}"
    return buffer + fragment


def _runs_from_spans(spans: Sequence[tuple[Span, bool]]) -> list:
    runs: list = []
    buffer = ""
    for span, starts_line in spans:
        if span.kind == SPAN_TEXT:
            buffer = _merge_text(buffer, span.text, starts_line)
        else:
            if buffer:
                runs.append(TextRun(text=buffer))
                buffer = ""
            runs.append(InlineMath(latex=span.latex, bbox=span.bbox))
    if buffer:
        runs.append(TextRun(text=buffer))
    return runs


def _blocks_of(block: dict) -> list[dict]:
    nested = block.get("blocks")
    return [item for item in nested or [] if isinstance(item, dict)]


def _caption_from(blocks: Sequence[dict], height: float) -> tuple[str, Rect | None]:
    parts: list[str] = []
    box: Rect | None = None
    for nested in blocks:
        if nested.get("type") not in {"image_caption", "table_caption", "table_footnote"}:
            continue
        for span, _ in _spans_from_lines(nested.get("lines"), height):
            if span.text.strip():
                parts.append(span.text.strip())
        candidate = _middle_box(nested.get("bbox"), height)
        if candidate:
            box = candidate if box is None else _union(box, candidate)
    return " ".join(parts), box


def _union(left: Rect, right: Rect) -> Rect:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def _table_cells_from_middle(block: dict, height: float) -> list[TableCell]:
    """Group a table's text spans into cells by row and column gaps."""
    body_spans: list[Span] = []
    for nested in _blocks_of(block):
        if nested.get("type") != "table_body":
            continue
        for span, _ in _spans_from_lines(nested.get("lines"), height):
            if span.kind == SPAN_TEXT and span.text.strip() and span.bbox:
                body_spans.append(span)
    if not body_spans:
        body_spans = [
            span
            for span, _ in _spans_from_lines(block.get("lines"), height)
            if span.kind == SPAN_TEXT and span.text.strip() and span.bbox
        ]
    if not body_spans:
        return []

    rows: list[list[Span]] = []
    for span in sorted(body_spans, key=lambda item: (-(item.bbox[1] + item.bbox[3]) / 2, item.bbox[0])):
        center = (span.bbox[1] + span.bbox[3]) / 2
        height_span = max(1.0, span.bbox[3] - span.bbox[1])
        if rows:
            last = rows[-1]
            last_center = sum((item.bbox[1] + item.bbox[3]) / 2 for item in last) / len(last)
            if abs(center - last_center) <= 0.6 * height_span:
                last.append(span)
                continue
        rows.append([span])

    cells: list[TableCell] = []
    for row in rows:
        row.sort(key=lambda item: item.bbox[0])
        current: list[Span] = []
        for span in row:
            if current:
                previous = current[-1]
                gap = span.bbox[0] - previous.bbox[2]
                average_glyph = (span.bbox[2] - span.bbox[0]) / max(1, len(span.text.strip()))
                tolerance = max(6.0, 1.5 * average_glyph)
                if gap > tolerance:
                    cells.append(_cell_from_spans(current))
                    current = []
            current.append(span)
        if current:
            cells.append(_cell_from_spans(current))
    return cells


def _cell_from_spans(spans: Sequence[Span]) -> TableCell:
    text = ""
    for span in spans:
        text = _merge_text(text, span.text, bool(text))
    box = spans[0].bbox
    for span in spans[1:]:
        box = _union(box, span.bbox)
    return TableCell(text=text.strip(), bbox=box, spans=list(spans))


def pages_from_middle(payload) -> tuple[list[Block], list[PageFrame]]:
    """Materialise IR blocks and page frames from MinerU's `middle.json`."""
    blocks: list[Block] = []
    frames: list[PageFrame] = []
    pages = payload.get("pdf_info") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return blocks, frames

    first_title_seen = False
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        size = page.get("page_size") or []
        try:
            width, height = float(size[0]), float(size[1])
        except (IndexError, TypeError, ValueError):
            width = height = 0.0
        frame = PageFrame(index=page_index, width=width, height=height)

        for block in page.get("para_blocks") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            box = _middle_box(block.get("bbox"), height)
            if box:
                frame.known_regions.append(box)

            if kind == "text":
                spans = _spans_from_lines(block.get("lines"), height)
                runs = _runs_from_spans(spans)
                if runs:
                    blocks.append(
                        Paragraph(
                            runs=runs,
                            page_index=page_index,
                            bbox=box,
                            spans=[span for span, _ in spans],
                            source_text="".join(
                                run.text for run in runs if isinstance(run, TextRun)
                            ),
                        )
                    )

            elif kind == "title":
                spans = _spans_from_lines(block.get("lines"), height)
                text = "".join(span.text for span, _ in spans if span.kind == SPAN_TEXT).strip()
                if not text:
                    continue
                level = block.get("level")
                try:
                    level_int = max(1, int(level))
                except (TypeError, ValueError):
                    level_int = 1 if not first_title_seen else 2
                first_title_seen = True
                blocks.append(
                    Title(
                        level=level_int,
                        text=text,
                        page_index=page_index,
                        bbox=box,
                        source_text=text,
                    )
                )

            elif kind == "table":
                caption, caption_box = _caption_from(_blocks_of(block), height)
                rel_path = ""
                for nested in _blocks_of(block):
                    for span, _ in _spans_from_lines(nested.get("lines"), height):
                        if span.kind == SPAN_IMAGE and span.text:
                            rel_path = rel_path or span.text
                html = block.get("html") if isinstance(block.get("html"), str) else ""
                table = Table(
                    rel_path=rel_path,
                    html=html or "",
                    caption=caption,
                    page_index=page_index,
                    bbox=box,
                    cells=_table_cells_from_middle(block, height),
                )
                if caption_box:
                    frame.known_regions.append(caption_box)
                blocks.append(table)

            elif kind in {"image", "chart"}:
                rel_path = ""
                for nested in _blocks_of(block):
                    for span, _ in _spans_from_lines(nested.get("lines"), height):
                        if span.kind in {SPAN_IMAGE, SPAN_TABLE} and span.text:
                            rel_path = rel_path or span.text
                caption, caption_box = _caption_from(_blocks_of(block), height)
                if caption_box:
                    frame.known_regions.append(caption_box)
                blocks.append(
                    Image(
                        rel_path=rel_path,
                        caption=caption,
                        page_index=page_index,
                        bbox=box,
                    )
                )

            elif kind == SPAN_DISPLAY_MATH:
                spans = _spans_from_lines(block.get("lines"), height)
                latex = next(
                    (span.latex for span, _ in spans if span.latex), ""
                )
                if latex:
                    blocks.append(
                        DisplayMath(latex=latex, page_index=page_index, bbox=box)
                    )

        for block in page.get("discarded_blocks") or []:
            if not isinstance(block, dict):
                continue
            box = _middle_box(block.get("bbox"), height)
            if box:
                frame.known_regions.append(box)
        for key in ("images", "tables", "interline_equations"):
            for entry in page.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                box = _middle_box(entry.get("bbox"), height)
                if box:
                    frame.known_regions.append(box)

        frames.append(frame)

    return blocks, frames


def compact_middle(payload) -> dict | None:
    """Keep only the geometry the renderer needs from a `middle.json` payload."""
    pages = payload.get("pdf_info") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return None

    def clean(node):
        if isinstance(node, dict):
            return {
                key: clean(value)
                for key, value in node.items()
                if key not in _MIDDLE_SKIP_KEYS
            }
        if isinstance(node, list):
            return [clean(item) for item in node]
        return node

    return {"pdf_info": [clean(page) for page in pages]}


# ---------------------------------------------------------------------------
# Local (text layer) block building
# ---------------------------------------------------------------------------

_HEADING_NUMBERED_PATTERN = re.compile(r"^\d+(?:\.\d+)*\s+[A-Z0-9]")
_HEADING_KEYWORDS = {
    "abstract", "introduction", "related work", "related works", "background",
    "method", "methods", "methodology", "approach", "experiment", "experiments",
    "experimental setup", "results", "evaluation", "discussion", "conclusion",
    "conclusions", "future work", "references", "acknowledgments",
    "acknowledgements", "appendix", "limitations", "overview",
}


_CAPTION_PATTERN = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|图|表)\s*\d+\s*[.:：．]", re.IGNORECASE
)


def is_caption_line(line: str) -> bool:
    """Figure/table captions are reused verbatim, never translated."""
    return bool(_CAPTION_PATTERN.match(line.strip()))


def is_heading_line(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 90:
        return False
    if _HEADING_NUMBERED_PATTERN.match(text):
        return True
    if text.lower() in _HEADING_KEYWORDS:
        return True
    return text.isupper() and len(text) > 3


_CLOSING_PUNCTUATION = ".,;:!?%)]}>\u201d\u2019\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u300b\u3011"
_OPENING_PUNCTUATION = "([{<\u201c\u2018\uff08\u300a\u3010"


def _join_chars(chars: Sequence[SourceChar]) -> str:
    parts: list[str] = []
    previous: SourceChar | None = None
    for char in chars:
        if previous is not None and char.char not in _CLOSING_PUNCTUATION:
            if previous.char not in _OPENING_PUNCTUATION:
                gap = char.rect[0] - previous.rect[2]
                reference = max(1.0, char.size or previous.size or 1.0)
                if gap > WORD_GAP_RATIO * reference and not parts[-1].endswith(" "):
                    parts.append(" ")
        parts.append(char.char)
        previous = char
    joined = re.sub(r"\s+", " ", "".join(parts)).strip()
    joined = re.sub(r"\s+([,.;:!?%)\]}\u3002\uff0c\uff1b\uff1a\uff01\uff1f])", r"\1", joined)
    return joined


WORD_GAP_RATIO = 0.25


def _cluster_lines(chars: Sequence[SourceChar]) -> list[list[SourceChar]]:
    """Group characters into visual lines by baseline, then by horizontal runs.

    PDFium does not emit whitespace glyphs, so lines are recovered from
    geometry. Characters are first bucketed by baseline (glyph boxes of one
    line differ by a few tenths of a point, and descenders sit lower), then
    split into runs wherever the horizontal gap is too large to be a word
    space. Without the second step the two columns of a paper share baselines
    and merge into one page-wide "line".
    """
    buckets: list[list[SourceChar]] = []
    baselines: list[float] = []
    for char in sorted(
        chars, key=lambda item: (-round(item.rect[1] / 2.0), item.rect[0])
    ):
        size = char.size or (char.rect[3] - char.rect[1]) or 1.0
        for index in range(len(buckets) - 1, max(-1, len(buckets) - 5), -1):
            if abs(char.rect[1] - baselines[index]) <= 0.45 * max(buckets[index][-1].size or 1.0, size):
                buckets[index].append(char)
                break
        else:
            buckets.append([char])
            baselines.append(char.rect[1])

    lines: list[list[SourceChar]] = []
    for bucket in buckets:
        bucket.sort(key=lambda item: item.rect[0])
        current: list[SourceChar] = []
        for char in bucket:
            if current:
                previous = current[-1]
                size = max(previous.size or 1.0, char.size or 1.0)
                if char.rect[0] - previous.rect[2] > max(4.0, 1.0 * size):
                    lines.append(current)
                    current = []
            current.append(char)
        if current:
            lines.append(current)
    return sorted(lines, key=lambda line: -_line_box(line)[3])


def _line_box(chars: Sequence[SourceChar]) -> Rect:
    return (
        min(char.rect[0] for char in chars),
        min(char.rect[1] for char in chars),
        max(char.rect[2] for char in chars),
        max(char.rect[3] for char in chars),
    )


def _columns(lines: Sequence[Sequence[SourceChar]], width: float) -> list[list[Sequence[SourceChar]]]:
    """Split lines into columns when the page is visibly two-column."""
    starts = sorted((_line_box(line)[0] for line in lines), reverse=False)
    if len(lines) < 8:
        return [list(lines)]
    split = None
    for index in range(1, len(starts)):
        gap = starts[index] - starts[index - 1]
        if gap > 0.18 * width and starts[index] > 0.4 * width:
            candidate = (starts[index] + starts[index - 1]) / 2
            if split is None or gap > split[0]:
                split = (gap, candidate)
    if split is None:
        return [list(lines)]
    threshold = split[1]
    left = [line for line in lines if _line_box(line)[0] < threshold]
    right = [line for line in lines if _line_box(line)[0] >= threshold]
    if len(left) < 3 or len(right) < 3:
        return [list(lines)]
    return [left, right]


@dataclass
class LocalPage:
    index: int
    width: float
    height: float
    blocks: list[dict] = field(default_factory=list)
    markdown: str = ""


def parse_local_pages(pdf_path: str | Path) -> list[LocalPage]:
    """Build MinerU-shaped content blocks with geometry from a text layer."""
    frames = measure_pages(pdf_path)
    pages: list[LocalPage] = []
    for frame in frames:
        page = LocalPage(index=frame.index, width=frame.width, height=frame.height)
        if frame.has_text_layer:
            lines = _cluster_lines(frame.chars)
            lines = [line for line in lines if not _is_running_head(frame, line)]
            for line_group in _columns(lines, frame.width):
                page.blocks.extend(_blocks_from_lines(line_group, frame))
            _promote_document_title(page, frame)
            page.markdown = "\n\n".join(
                rendered
                for rendered in (_block_markdown(block) for block in page.blocks)
                if rendered
            )
        pages.append(page)
    return pages


def _is_running_head(frame: PageFrame, line: Sequence[SourceChar]) -> bool:
    """Short text in the top/bottom margin is a running head or page number.

    Those stay in the source page and are never translated, which is also why
    they are kept out of the block list and the reading markdown.
    """
    if not line or not frame.height:
        return False
    text = _join_chars(line)
    if not text or len(text) > 120:
        return False
    box = _line_box(line)
    top = frame.height - box[3]
    bottom = box[1]
    return top <= 0.05 * frame.height or bottom <= 0.06 * frame.height


def _block_font_size(frame: PageFrame, box: Rect) -> float:
    sizes = [
        char.size
        for char in frame.chars
        if char.size and _rect_overlaps_text(char, box)
    ]
    if not sizes:
        return 0.0
    return sum(sizes) / len(sizes)


def _promote_document_title(page: LocalPage, frame: PageFrame) -> None:
    """Promote page 1's largest-font top-of-page line to the paper title.

    A running head sits in the margin; the title is the biggest line in the
    upper half of the first page, so size decides instead of position alone.
    """
    if page.index != 0 or not page.blocks:
        return
    best_index = -1
    best_size = 0.0
    for index, block in enumerate(page.blocks):
        if block.get("type") != "paragraph":
            continue
        box = _from_top_down(block["bbox"], page.height)
        if box[1] < 0.45 * frame.height:
            continue
        size = _block_font_size(frame, box)
        text = _join_chars(
            [char for char in frame.chars if _rect_overlaps_text(char, box)]
        )
        if len(text) > 160:
            continue
        if size > best_size:
            best_size, best_index = size, index
    if best_index < 0 or best_size <= 0:
        return
    block = page.blocks[best_index]
    content = block.get("content") or {}
    text = " ".join(
        item.get("content", "") for item in content.get("paragraph_content") or []
    ).strip()
    if not text:
        return
    page.blocks[best_index] = _title_block(
        text, _from_top_down(block["bbox"], page.height), page.height, 1
    )
    for index, other in enumerate(page.blocks):
        if index == best_index or other.get("type") != "title":
            continue
        other["content"]["level"] = 2


def _from_top_down(box, height: float) -> Rect:
    x0, y0, x1, y1 = (float(value) for value in box)
    return (x0, height - y1, x1, height - y0)


def _blocks_from_lines(lines: Sequence[Sequence[SourceChar]], frame: PageFrame) -> list[dict]:
    ordered = sorted(lines, key=lambda line: -(_line_box(line)[1] + _line_box(line)[3]) / 2)
    blocks: list[dict] = []
    current: list[Sequence[SourceChar]] = []
    pitch = _line_pitch(ordered)

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = " ".join(_join_chars(line) for line in current).strip()
        box = _union(_line_box(current[0]), _line_box(current[-1]))
        if text:
            blocks.append(_paragraph_block(text, box, frame.height))
        current = []

    for line in ordered:
        text = _join_chars(line)
        if not text:
            continue
        if is_caption_line(text):
            flush()
            blocks.append(_caption_block(text, _line_box(line), frame.height))
            continue
        if is_heading_line(text):
            flush()
            level = 1 if frame.index == 0 and not blocks else 2
            blocks.append(_title_block(text, _line_box(line), frame.height, level))
            continue
        if current:
            previous_box = _line_box(current[-1])
            box = _line_box(line)
            gap = previous_box[1] - box[3]
            if gap > 0.5 * pitch:
                flush()
        current.append(line)
    flush()
    return blocks


def _line_pitch(lines: Sequence[Sequence[SourceChar]]) -> float:
    """Median baseline-to-baseline distance between consecutive lines."""
    distances = []
    for previous, following in zip(lines, lines[1:]):
        distance = _line_box(previous)[1] - _line_box(following)[1]
        if distance > 0:
            distances.append(distance)
    if not distances:
        return 4.0
    distances.sort()
    return distances[len(distances) // 2]


def _to_top_down(box: Rect, height: float) -> list[float]:
    x0, y0, x1, y1 = box
    return [x0, height - y1, x1, height - y0]


def _paragraph_block(text: str, box: Rect, height: float) -> dict:
    return {
        "type": "paragraph",
        "bbox": _to_top_down(box, height),
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
    }


def _title_block(text: str, box: Rect, height: float, level: int) -> dict:
    return {
        "type": "title",
        "bbox": _to_top_down(box, height),
        "content": {"title_content": [{"type": "text", "content": text}], "level": level},
    }


def _caption_block(text: str, box: Rect, height: float) -> dict:
    """A caption: kept as source text, never queued for translation."""
    return {
        "type": "caption",
        "bbox": _to_top_down(box, height),
        "content": {"caption_content": [{"type": "text", "content": text}]},
    }


def _block_markdown(block: dict) -> str:
    kind = block.get("type")
    content = block.get("content") or {}
    if kind == "caption":
        return " ".join(
            item.get("content", "") for item in content.get("caption_content") or []
        ).strip()
    if kind == "title":
        level = int(content.get("level") or 1)
        text = " ".join(
            item.get("content", "") for item in content.get("title_content") or []
        ).strip()
        return f"{'#' * max(1, min(level, 6))} {text}".strip()
    text = " ".join(
        item.get("content", "") for item in content.get("paragraph_content") or []
    ).strip()
    return text


# ---------------------------------------------------------------------------
# Coverage analysis
# ---------------------------------------------------------------------------


def _rect_overlaps_text(char: SourceChar, region: Rect) -> bool:
    x0, y0, x1, y1 = char.rect
    rx0, ry0, rx1, ry1 = region
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2
    return rx0 <= center_x <= rx1 and ry0 <= center_y <= ry1


def lost_regions(frame: PageFrame, extra_regions: Sequence[Rect] = ()) -> list[Rect]:
    """Text-layer line boxes that no parsed block claims.

    These are the parser's dropped content: the layout renderer may continue a
    paragraph into them, and never treats them as free whitespace.
    """
    regions = list(frame.known_regions) + list(extra_regions)
    uncovered = [
        char
        for char in frame.chars
        if char.char.strip() and not any(_rect_overlaps_text(char, region) for region in regions)
    ]
    if not uncovered:
        return []
    boxes: list[Rect] = []
    for line in _cluster_lines(uncovered):
        if not line:
            continue
        box = _line_box(line)
        if boxes:
            previous = boxes[-1]
            if box[1] <= previous[3] + 2 and not (box[2] < previous[0] - 20 or box[0] > previous[2] + 20):
                boxes[-1] = _union(previous, box)
                continue
        boxes.append(box)
    return boxes

def attach_known_regions(
    frames: list[PageFrame],
    content_blocks: Iterable,
    *,
    normalized_boxes: bool = True,
) -> None:
    """Fill each frame's accounted-for regions from MinerU content blocks."""
    for page_index, entry in enumerate(content_blocks):
        if not isinstance(entry, list):
            page_index = -1
            blocks = [entry]
        else:
            blocks = entry
        if not (0 <= page_index < len(frames)):
            continue
        frame = frames[page_index]
        size = (frame.width, frame.height)
        for block in blocks:
            if not isinstance(block, dict):
                continue
            rect = _to_page_rect(block.get("bbox"), size, normalized=normalized_boxes)
            if rect:
                frame.known_regions.append(rect)


def merge_known_regions(frames: list[PageFrame], sources: list[PageFrame]) -> None:
    """Copy geometry from parser frames onto measured frames."""
    for frame, source in zip(frames, sources):
        if source.width and source.height:
            if (
                abs(source.width - frame.width) > 0.5
                or abs(source.height - frame.height) > 0.5
            ):
                continue
        frame.known_regions.extend(source.known_regions)


# ---------------------------------------------------------------------------
# Inline-formula geometry from MinerU's VLM `*_model.json`
# ---------------------------------------------------------------------------

_INLINE_DET_TYPES = {"inline_formula", "inline_equation"}


def _det_factors(
    dets: Iterable, width: float, height: float
) -> tuple[float, float]:
    """Per-axis factors that turn a page's detection boxes into points.

    The VLM backend writes 0-1 boxes, the pipeline backend writes 0-1000 ones,
    and some builds already write page points; the values decide which. X and Y
    scale separately because a page is not square.
    """
    max_x = max_y = 0.0
    for det in dets:
        box = det.get("bbox") if isinstance(det, dict) else None
        rect = _parse_bbox(box)
        if rect is None:
            continue
        max_x = max(max_x, abs(rect[0]), abs(rect[2]))
        max_y = max(max_y, abs(rect[1]), abs(rect[3]))
    if max_x <= 0 or max_y <= 0:
        return 0.0, 0.0
    if max_x <= 1.5 and max_y <= 1.5:
        return width, height
    if max_x <= width * 1.05 and max_y <= height * 1.05:
        return 1.0, 1.0
    return width / 1000.0, height / 1000.0


def inline_formula_boxes(
    model_payload, frames: Sequence[PageFrame]
) -> dict[int, list[tuple[Rect, str]]]:
    """Per-page inline-formula boxes (page points, lower-left origin) in order."""
    pages = model_payload
    if isinstance(model_payload, dict):
        pages = model_payload.get("pdf_info") or model_payload.get("pages")
    if not isinstance(pages, list):
        return {}

    located: dict[int, list[tuple[Rect, str]]] = {}
    for page_index, page in enumerate(pages):
        if not (0 <= page_index < len(frames)):
            continue
        dets = page
        if isinstance(page, dict):
            dets = page.get("layout_dets") or page.get("dets") or []
        if not isinstance(dets, list):
            continue
        frame = frames[page_index]
        if not frame.width or not frame.height:
            continue
        scale_x, scale_y = _det_factors(dets, frame.width, frame.height)
        if not scale_x or not scale_y:
            continue
        boxes: list[tuple[Rect, str]] = []
        for det in dets:
            if not isinstance(det, dict) or det.get("type") not in _INLINE_DET_TYPES:
                continue
            rect = _parse_bbox(det.get("bbox"))
            if rect is None:
                continue
            x0, y0, x1, y1 = rect
            x0, x1 = x0 * scale_x, x1 * scale_x
            y0, y1 = y0 * scale_y, y1 * scale_y
            boxes.append(
                (
                    (x0, frame.height - y1, x1, frame.height - y0),
                    str(det.get("latex") or det.get("content") or ""),
                )
            )
        if boxes:
            boxes.sort(key=lambda item: (-item[0][3], item[0][0]))
            located[page_index] = boxes
    return located


def _inline_math_runs(block) -> list:
    from app.services.mineru_layout import InlineMath, ListBlock, Paragraph

    runs: list = []
    if isinstance(block, Paragraph):
        runs.extend(run for run in block.runs if isinstance(run, InlineMath))
    elif isinstance(block, ListBlock):
        for item in block.items:
            runs.extend(run for run in item if isinstance(run, InlineMath))
    return runs


def _normalize_latex(latex: str) -> str:
    import re as _re

    return _re.sub(r"[^0-9a-zA-Z]+", "", latex or "").lower()


def attach_inline_formula_boxes(
    blocks: Iterable, model_payload, frames: Sequence[PageFrame]
) -> list[str]:
    """Give inline formulas the geometry the VLM backend omits from content_list.

    The VLM parser reports prose and formula LaTeX in `content_list_v2.json`
    without any span boxes, so those formulas cannot be lifted out of the
    source page — and a paragraph whose formula has no geometry would have to
    keep its English. `*_model.json` carries the missing boxes, in the same
    reading order as the content list.
    """
    located = inline_formula_boxes(model_payload, frames)
    if not located:
        return []

    by_page: dict[int, list] = {}
    blocks = list(blocks)
    for block in blocks:
        page_index = getattr(block, "page_index", -1)
        for run in _inline_math_runs(block):
            by_page.setdefault(page_index, []).append(run)

    notes: list[str] = []
    for page_index, runs in by_page.items():
        boxes = located.get(page_index) or []
        if not boxes:
            continue
        assigned = 0
        if len(boxes) == len(runs):
            for run, (rect, _latex) in zip(runs, boxes):
                run.bbox = rect
            assigned = len(runs)
        else:
            pool = list(boxes)
            for run in runs:
                key = _normalize_latex(run.latex)
                if not key:
                    continue
                for index, (rect, latex) in enumerate(pool):
                    if _normalize_latex(latex) == key:
                        run.bbox = rect
                        pool.pop(index)
                        assigned += 1
                        break
        if assigned:
            notes.append(
                f"page {page_index + 1}: located {assigned} inline formula(s) from the model geometry"
            )
        missing = len(runs) - assigned
        if missing:
            notes.append(
                f"page {page_index + 1}: {missing} inline formula(s) have no geometry; "
                "their blocks keep the source text"
            )
    return notes


# ---------------------------------------------------------------------------
# Text-layer ownership correction (design section 6)
# ---------------------------------------------------------------------------

_MIN_ALIGN_CHARS = 40
_ALIGN_WINDOW_LINES = 600
_ALIGN_COVERAGE = 0.5
_MIN_PART_CHARS = 25
_MIN_PART_MATCH_CHARS = 25
_TRIM_RATIO = 0.75


@dataclass
class BlockPart:
    """One region of a block's text, as the source page actually draws it."""

    rect: Rect
    text: str
    page_index: int
    start: int = 0
    end: int = 0


def _normalized_positions(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isalnum():
            chars.append(char.lower())
            positions.append(index)
    return "".join(chars), positions


def document_lines(frames: Sequence[PageFrame]) -> list[tuple[int, list]]:
    """Every text line of the document, in reading order, with its page."""
    ordered: list[tuple[int, list]] = []
    for frame in frames:
        lines = _cluster_lines(frame.chars)
        if not lines:
            continue
        for group in _columns(lines, frame.width):
            for line in sorted(group, key=lambda item: -_line_box(item)[3]):
                if _join_chars(line):
                    ordered.append((frame.index, line))
    return ordered


def _split_line_runs(
    matched: list[tuple[int, list]]
) -> list[list[tuple[int, list]]]:
    """Split matched lines into runs: one page, one column, contiguous."""
    runs: list[list[tuple[int, list]]] = []
    for page_index, line in matched:
        box = _line_box(line)
        # Extend the run this line continues. Lines arrive in top-down page
        # order, so a paragraph that continues in the next column has the other
        # column's lines in between; looking back for the run in *this* column
        # keeps such a continuation whole instead of orphaning its first line.
        target = None
        for run in reversed(runs):
            previous_page, previous_line = run[-1]
            previous = _line_box(previous_line)
            narrower = min(box[2] - box[0], previous[2] - previous[0])
            overlap = min(box[2], previous[2]) - max(box[0], previous[0])
            same_column = narrower > 0 and overlap / narrower >= 0.6
            gap = previous[1] - box[3]
            # A region break is a page change, a column change, or a large
            # vertical gap. Ordinary paragraph spacing stays inside one region.
            if (
                page_index == previous_page
                and same_column
                and gap <= max(60.0, 6.0 * (previous[3] - previous[1]))
            ):
                target = run
                break
        if target is None:
            runs.append([(page_index, line)])
        else:
            target.append((page_index, line))
    return runs


def block_parts_from_text_layer(
    declared: str, lines: Sequence[tuple[int, list]]
) -> list[BlockPart] | None:
    """Recover the regions a block's declared text is actually drawn in.

    MinerU's VLM output pairs a paragraph's text with a box that often covers
    only part of it; the rest continues in the next column or page, sometimes
    without any block of its own. The declared text is aligned against the
    document's own text layer as a whole, so differing line breaks do not
    matter, and the regions fall out of where the matched lines sit.
    """
    import difflib

    declared_norm, positions = _normalized_positions(declared)
    if len(declared_norm) < _MIN_ALIGN_CHARS:
        return None

    window = list(lines[:_ALIGN_WINDOW_LINES])
    line_norms: list[str] = []
    line_index_of_char: list[int] = []
    page_norm = ""
    for index, (_page_index, line) in enumerate(window):
        text, _positions = _normalized_positions(_join_chars(line))
        line_norms.append(text)
        page_norm += text
        line_index_of_char.extend([index] * len(text))
    if not page_norm:
        return None

    matcher = difflib.SequenceMatcher(None, declared_norm, page_norm, autojunk=False)
    matched = [0] * len(window)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matched[line_index_of_char[block.b + offset]] += 1

    # Every line the declared text matches strongly enough is part of the
    # paragraph wherever it sits: a paragraph may continue past a figure, a
    # caption run or a whole page, so unmatched lines in between are expected
    # and do not end it.
    kept = [
        index
        for index, count in enumerate(matched)
        if len(line_norms[index]) and count >= max(8, 0.5 * len(line_norms[index]))
    ]
    if not kept:
        return None
    covered = sum(matched[index] for index in kept)
    if covered < _ALIGN_COVERAGE * len(declared_norm):
        return None
    if len(kept) == 1 and matched[kept[0]] < 0.8 * len(line_norms[kept[0]]):
        return None

    # An inline formula splits one visual line into several fragments; they
    # share a baseline, so join them before deciding what a region is.
    visual: list[tuple[int, list]] = []
    visual_matches: list[int] = []
    for index in kept:
        entry = window[index]
        box = _line_box(entry[1])
        if visual:
            previous_page, previous_line = visual[-1]
            previous = _line_box(previous_line)
            height = min(box[3] - box[1], previous[3] - previous[1])
            vertical = min(box[3], previous[3]) - max(box[1], previous[1])
            gap = box[0] - previous[2]
            if (
                entry[0] == previous_page
                and height > 0
                and vertical / height >= 0.6
                and gap <= 3.0 * height
            ):
                merged_line = previous_line + [char for char in entry[1]]
                visual[-1] = (previous_page, sorted(merged_line, key=lambda item: item.rect[0]))
                visual_matches[-1] += matched[index]
                continue
        visual.append(entry)
        visual_matches.append(matched[index])

    parts: list[BlockPart] = []
    part_matches: list[int] = []
    cursor = 0
    consumed = 0
    for run in _split_line_runs(visual):
        run_matches = visual_matches[cursor : cursor + len(run)]
        start_norm = consumed
        end_norm = min(len(declared_norm), consumed + sum(run_matches))
        consumed = end_norm
        cursor += len(run)
        raw_start = positions[min(start_norm, len(positions) - 1)]
        raw_end = positions[max(0, min(end_norm, len(positions)) - 1)] + 1
        # Keep the punctuation that closes the part with it.
        while raw_end < len(declared) and declared[raw_end] in " \t\r\n.,;:)\u201d":
            raw_end += 1
        rect = _line_box(run[0][1])
        for _page, line in run[1:]:
            rect = _union(rect, _line_box(line))
        parts.append(
            BlockPart(
                rect=rect,
                text=declared[raw_start:raw_end].strip(),
                page_index=run[0][0],
                start=raw_start,
                end=raw_end,
            )
        )
        part_matches.append(sum(run_matches))

    # A few characters matched on an unrelated line are not a region: a part has
    # to carry a line's worth of the paragraph to count. The parsed box itself
    # always stays.
    keep = [
        index
        for index, _part in enumerate(parts)
        if index == 0 or part_matches[index] >= _MIN_PART_MATCH_CHARS
    ]
    parts = [parts[index] for index in keep]
    part_matches = [part_matches[index] for index in keep]

    # The regions together carry the whole declared text: each part runs up to
    # where the next one starts, so wording between two matched regions keeps
    # its place instead of disappearing from both. A lone region keeps only the
    # text it matched, which is what tells a cut-short paragraph from a whole
    # one further down the pipeline.
    if len(parts) > 1:
        for index, part in enumerate(parts):
            end = parts[index + 1].start if index + 1 < len(parts) else len(declared)
            part.text = declared[part.start : end].strip()
            part.end = end

    parts = [part for part in parts if part.text]
    if not parts:
        return None
    return parts


def _merge_parts_inside(rect: Rect, parts: list[BlockPart]) -> list[BlockPart]:
    """Fold the regions a block's own box already covers into one.

    A broken line — glyphs of one visual line reported as two — can look like a
    second region even though the parsed box holds both, and splitting there
    would cut a paragraph that the box already describes.
    """
    grouped: list[BlockPart] = []
    for part in parts:
        previous = grouped[-1] if grouped else None
        if (
            previous is not None
            and _rect_within(rect, previous.rect)
            and _rect_within(rect, part.rect)
        ):
            grouped[-1] = BlockPart(
                rect=_union(previous.rect, part.rect),
                text=(previous.text + " " + part.text).strip(),
                page_index=previous.page_index,
                start=previous.start,
                end=part.end,
            )
            continue
        grouped.append(part)
    return grouped


def _rect_within(outer: Rect, inner: Rect, slack: float = 4.0) -> bool:
    return (
        outer[0] - slack <= inner[0]
        and outer[1] - slack <= inner[1]
        and inner[2] <= outer[2] + slack
        and inner[3] <= outer[3] + slack
    )


def _formula_offsets(block, declared: str) -> list[tuple[int, object]]:
    """Each formula run with the offset it occupies in the block's plain text."""
    from app.services.mineru_layout import TextRun

    offsets: list[tuple[int, object]] = []
    cursor = 0
    for run in block.runs:
        if isinstance(run, TextRun):
            cursor += len(run.text)
            continue
        offsets.append((min(cursor, len(declared)), run))
    return offsets


def _runs_for_part(
    declared: str, part: BlockPart, formulas: Sequence[tuple[int, object]]
) -> list:
    """The runs of one region: its slice of the text plus the formulas inside."""
    from app.services.mineru_layout import TextRun

    runs: list = []
    cursor = part.start
    for offset, run in formulas:
        if offset < part.start:
            continue
        if offset >= part.end:
            break
        if offset > cursor:
            runs.append(TextRun(text=declared[cursor:offset]))
        runs.append(run)
        cursor = offset
    if cursor < part.end:
        runs.append(TextRun(text=declared[cursor:part.end]))
    if runs and isinstance(runs[0], TextRun):
        runs[0] = TextRun(text=runs[0].text.lstrip())
    if runs and isinstance(runs[-1], TextRun):
        runs[-1] = TextRun(text=runs[-1].text.rstrip())
    if not any(isinstance(run, TextRun) and run.text.strip() for run in runs):
        # A region that only holds formulas has no wording of its own.
        return [TextRun(text=part.text)]
    return runs


def text_of_block_runs(block) -> str | None:
    """A paragraph's text runs, formulas excluded, or None for other blocks."""
    from app.services.mineru_layout import Paragraph, TextRun

    if not isinstance(block, Paragraph) or not block.runs:
        return None
    return "".join(run.text for run in block.runs if isinstance(run, TextRun))


def _keep_runs_prefix(runs: Sequence, keep_chars: int) -> list:
    """Drop the runs that fall past `keep_chars` of text, formulas included."""
    from app.services.mineru_layout import TextRun

    kept: list = []
    remaining = keep_chars
    for run in runs:
        if remaining <= 0:
            break
        if isinstance(run, TextRun):
            text = run.text[:remaining]
            remaining -= len(text)
            if text:
                kept.append(TextRun(text=text))
            continue
        kept.append(run)
    return kept


def align_blocks_to_text_layer(
    frames: Sequence[PageFrame], blocks: list
) -> tuple[list, list[str]]:
    """Correct block regions with the text layer and split cross-region blocks.

    Returns the continuation groups created for blocks whose text is drawn in
    more than one region, together with log notes.
    """
    from app.services.mineru_layout import (
        ContinuationGroup,
        Paragraph,
        TextRun,
        plain_paragraph_text,
    )

    notes: list[str] = []
    groups: list = []
    corrected = 0
    trimmed = 0
    split_blocks = 0

    lines = document_lines(frames)
    if not lines:
        return groups, notes

    for block in list(blocks):
        if not isinstance(block, Paragraph) or not block.bbox:
            continue
        page_index = getattr(block, "page_index", -1)
        if not (0 <= page_index < len(frames)) or not frames[page_index].has_text_layer:
            continue
        declared = text_of_block_runs(block)
        if declared is None:
            continue
        start = _line_start_index(lines, page_index, block.bbox)
        parts = block_parts_from_text_layer(declared, lines[start:])
        if not parts:
            continue
        parts = _merge_parts_inside(block.bbox, parts)

        if len(parts) == 1:
            part = parts[0]
            if part.page_index != page_index:
                continue
            if _rect_change_is_meaningful(block.bbox, part.rect):
                block.bbox = part.rect
                corrected += 1
            # When the parser glued a second paragraph onto this one, the page
            # shows less text than the block declares: keep what the page
            # really draws, otherwise the block cannot fit and stays English.
            # Small differences (hyphenation, spacing) keep the parsed wording
            # so the translation checkpoint still applies.
            declared_norm = _normalized_positions(declared)[0]
            part_norm = _normalized_positions(part.text)[0]
            if len(part_norm) < _TRIM_RATIO * len(declared_norm):
                block.source_text = part.text
                has_formula = any(not isinstance(run, TextRun) for run in block.runs)
                if has_formula:
                    positions = _normalized_positions(declared)[1]
                    raw_end = positions[min(len(part_norm), len(positions)) - 1] + 1
                    block.runs = _keep_runs_prefix(block.runs, raw_end)
                else:
                    block.runs = [TextRun(text=part.text)]
                trimmed += 1
            continue

        # Several regions: one translation, one part drawn in each region. A
        # formula run is placed by where it sits in the declared text, so a
        # paragraph that carries inline math is split like any other.
        formulas = _formula_offsets(block, declared)
        low, high = parts[0].start, max(parts[-1].end, parts[0].start + 1)
        formulas = [
            (min(max(offset, low), high - 1), run) for offset, run in formulas
        ]
        block.bbox = parts[0].rect
        members = []
        for index, part in enumerate(parts):
            runs = _runs_for_part(declared, part, formulas)
            if index == 0:
                block.runs = runs
                block.source_text = part.text
                member = block
            else:
                member = Paragraph(
                    runs=runs,
                    page_index=part.page_index,
                    bbox=part.rect,
                    source_text=part.text,
                )
                _insert_in_reading_order(blocks, member)
            members.append(member)
        group = ContinuationGroup(
            blocks=members,
            source_text=declared,
            lengths=[max(1, len(part.text.strip())) for part in parts],
        )
        for member in members:
            member.continuation_member = True
        block.continuation_group = group
        groups.append(group)
        split_blocks += 1

    if corrected:
        notes.append(
            f"corrected the region of {corrected} paragraph(s) from the source text layer"
        )
    if trimmed:
        notes.append(
            f"trimmed {trimmed} paragraph(s) to the text the page really draws"
        )
    if split_blocks:
        notes.append(
            f"split {split_blocks} paragraph(s) that the parser stored as one region "
            "but the source draws in several"
        )
    return groups, notes


def _line_start_index(
    lines: Sequence[tuple[int, list]], page_index: int, rect: Rect
) -> int:
    for index, (line_page, line) in enumerate(lines):
        if line_page != page_index:
            continue
        box = _line_box(line)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if rect[0] - 6 <= cx <= rect[2] + 6 and rect[1] - 6 <= cy <= rect[3] + 6:
            return index
    for index, (line_page, _line) in enumerate(lines):
        if line_page >= page_index:
            return index
    return 0


def _rect_change_is_meaningful(before: Rect, after: Rect) -> bool:
    if not before or not after:
        return False
    overlap = max(0.0, min(before[2], after[2]) - max(before[0], after[0])) * max(
        0.0, min(before[3], after[3]) - max(before[1], after[1])
    )
    before_area = max(1e-6, (before[2] - before[0]) * (before[3] - before[1]))
    after_area = max(1e-6, (after[2] - after[0]) * (after[3] - after[1]))
    return overlap / min(before_area, after_area) < 0.95


_CAPTION_LINE_PATTERN = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|\u56fe|\u8868)\s*\d+\s*[:.\uff1a]", re.IGNORECASE
)
_MIN_PROSE_CHARS = 60
_MIN_PROSE_LINES = 2
_MIN_PROSE_LETTER_RATIO = 0.55
_MIN_LABEL_CHARS = 26
_SENTENCE_ENDINGS = (".", "\u3002", "!", "\uff01", "?", "\uff1f", ":", "\uff1a", ";", "\uff1b")
# Punctuation that can only close a sentence; unlike ":" or ";", which also
# end labels such as an axis caption.
_SENTENCE_CLOSING = (".", "\u3002", "!", "\uff01", "?", "\uff1f")


def _prose_text_from_lines(lines: Sequence) -> str:
    parts: list[str] = []
    for line in lines:
        text = _join_chars(line)
        if not text:
            continue
        if parts and parts[-1].endswith("-"):
            parts[-1] = parts[-1][:-1] + text
            continue
        parts.append(text)
    return " ".join(parts).strip()


def _is_label_line(text: str) -> bool:
    """Short text without a sentence ending is a figure label or axis value."""
    stripped = text.strip()
    if len(_normalize_latex(stripped)) >= _MIN_LABEL_CHARS:
        return False
    return not stripped.endswith(_SENTENCE_ENDINGS)


def _is_edge_label(text: str) -> bool:
    """Whether a short line at the edge of a recovered run is a figure label.

    A wrapped sentence fragment such as "ing edges are strong." is prose and
    must stay in the run; dropping it left that line in English on the page.
    Labels are title-like tokens ("input image", "WLS: a photographic look",
    "α = 1.8, λ = 3.2").
    """
    stripped = text.strip()
    normalized = _normalize_latex(stripped)
    if len(normalized) >= _MIN_LABEL_CHARS:
        return False
    if stripped.endswith(_SENTENCE_CLOSING):
        # A short line that closes a sentence is the paragraph's own tail, not
        # a label sitting beside it.
        return False
    if stripped[:1].islower() and len(normalized) >= 16:
        return False
    return True


def _looks_like_prose(lines: Sequence, text: str) -> bool:
    normalized = _normalize_latex(text)
    if len(normalized) < _MIN_PROSE_CHARS and len(lines) < _MIN_PROSE_LINES:
        return False
    first = _join_chars(lines[0]) if lines else ""
    if _CAPTION_LINE_PATTERN.match(first):
        return False
    letters = sum(1 for char in text if char.isalpha())
    return letters / max(1, len(text)) >= _MIN_PROSE_LETTER_RATIO


def synthesize_unclaimed_paragraphs(
    frames: Sequence[PageFrame], blocks: list
) -> list[str]:
    """Give body text the parser dropped its own block, straight from the layer.

    MinerU sometimes cuts a paragraph short: its text stops mid-sentence and
    the rest of the paragraph is never reported, leaving that part of the page
    in English while the translation covers only the first half. Unclaimed
    prose lines are turned into ordinary blocks so they are translated and
    replaced in place. Captions and figure labels are excluded on purpose.
    """
    from app.services.mineru_layout import Paragraph, TextRun

    created: list[Paragraph] = []
    for frame in frames:
        if not frame.has_text_layer:
            continue
        lines = _cluster_lines(frame.chars)
        if not lines:
            continue
        claimed = [
            block.bbox
            for block in blocks
            if getattr(block, "page_index", -1) == frame.index
            and getattr(block, "bbox", None)
        ]
        unclaimed = []
        caption_line = None
        for line in lines:
            box = _line_box(line)
            center_x, center_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if any(
                rect[0] - 4 <= center_x <= rect[2] + 4
                and rect[1] - 4 <= center_y <= rect[3] + 4
                for rect in claimed
            ):
                continue
            text = _join_chars(line)
            # Captions and their wrapped continuation stay in the source
            # language, so they never become blocks of their own.
            if _CAPTION_LINE_PATTERN.match(text):
                # A caption that ends its sentence swallows nothing else; a
                # wrapped one keeps skipping its own continuation lines, which
                # sit directly below it rather than anywhere later on the page.
                caption_line = None if text.strip().endswith(_SENTENCE_ENDINGS) else box
                continue
            if caption_line is not None:
                if _continues_caption(caption_line, box):
                    caption_line = (
                        None if text.strip().endswith(_SENTENCE_ENDINGS) else box
                    )
                    continue
                caption_line = None
            if _is_label_line(text):
                continue
            unclaimed.append(line)

        for run in _split_line_runs([(frame.index, line) for line in unclaimed]):
            run_lines = [line for _page, line in run]
            # A label that sits just above or below the prose (a figure tag, a
            # scale caption) is not part of the paragraph.
            while len(run_lines) > 1 and _is_edge_label(_join_chars(run_lines[0])):
                run_lines.pop(0)
            while len(run_lines) > 1 and _is_edge_label(_join_chars(run_lines[-1])):
                run_lines.pop()
            text = _prose_text_from_lines(run_lines)
            if not text or not _looks_like_prose(run_lines, text):
                continue
            rect = _line_box(run_lines[0])
            for line in run_lines[1:]:
                rect = _union(rect, _line_box(line))
            created.append(
                Paragraph(
                    runs=[TextRun(text=text)],
                    page_index=frame.index,
                    bbox=rect,
                    source_text=text,
                )
            )

    for block in created:
        _insert_in_reading_order(blocks, block)

    if not created:
        return []
    return [
        f"recovered {len(created)} paragraph(s) the parser dropped, from the source text layer"
    ]


def _continues_caption(caption: Rect, box: Rect) -> bool:
    """Whether a line sits directly under a caption as its wrapped remainder."""
    height = max(1.0, caption[3] - caption[1])
    gap = caption[1] - box[3]
    if not -1.6 * height <= gap <= 1.6 * height:
        return False
    overlap = min(caption[2], box[2]) - max(caption[0], box[0])
    return overlap > 0.0


def _insert_in_reading_order(blocks: list, block) -> None:
    """Insert `block` after the blocks of its page that sit above it."""
    page_index = getattr(block, "page_index", -1)
    top = block.bbox[3] if block.bbox else 0.0
    position = len(blocks)
    for index, existing in enumerate(blocks):
        if getattr(existing, "page_index", -1) != page_index:
            continue
        existing_box = getattr(existing, "bbox", None)
        if existing_box and existing_box[3] < top - 1.0:
            position = index
            break
    blocks.insert(position, block)
