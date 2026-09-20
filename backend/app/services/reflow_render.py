"""Reflow fallback: rebuild a page that layout rendering could not salvage.

The in-place renderer falls back to the untouched source page when the
translated blocks cannot be placed safely, leaving the whole page in English.
Reflow takes the other half of the trade: it drops the original geometry and
typesets the page's translated content fresh. Nothing is lost, overlapping or
shrunk below readability — the cost is that the page no longer looks like the
source layout. Pages may grow onto follow-up pages when the content is longer
than one page.
"""

from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    Image as RLImage,
    Paragraph as RLParagraph,
    SimpleDocTemplate,
    Spacer,
    Table as RLTable,
    TableStyle,
)

from app.services.cjk_fonts import CjkFontSet
from app.services.layout_fit import (
    DEFAULT_BODY_SIZE,
    SMALL_TITLE_SIZE,
    TITLE_SIZES,
    Rect,
    _inline_markup,
    formula_key,
)
from app.services.layout_model import PageFrame
from app.services.mineru_layout import (
    Author,
    Block,
    DisplayMath,
    Image,
    InlineMath,
    ListBlock,
    Paragraph as IRParagraph,
    Table,
    TextRun,
    Title,
)

MARGIN_RATIO = 0.08
CELL_SIZE = 9.0
BODY_LEADING = 1.5


def _styles(fonts: CjkFontSet) -> dict[str, ParagraphStyle]:
    return {
        "body": ParagraphStyle(
            "body", fontName=fonts.name(False), fontSize=DEFAULT_BODY_SIZE,
            leading=DEFAULT_BODY_SIZE * BODY_LEADING, wordWrap="CJK",
            spaceBefore=0, spaceAfter=6, firstLineIndent=0,
        ),
        "title": ParagraphStyle(
            "title", fontName=fonts.name(True), fontSize=TITLE_SIZES[1],
            leading=TITLE_SIZES[1] * 1.3, wordWrap="CJK",
            spaceBefore=10, spaceAfter=8, firstLineIndent=0,
        ),
        "caption": ParagraphStyle(
            "caption", fontName=fonts.name(False), fontSize=CELL_SIZE,
            leading=CELL_SIZE * 1.4, wordWrap="CJK", alignment=1,
            spaceBefore=0, spaceAfter=6, firstLineIndent=0,
        ),
        "cell": ParagraphStyle(
            "cell", fontName=fonts.name(False), fontSize=CELL_SIZE,
            leading=CELL_SIZE * 1.3, wordWrap="CJK",
            spaceBefore=0, spaceAfter=0, firstLineIndent=0,
        ),
    }


def _runs_markup(runs, page_index: int, crops, size: float) -> str:
    parts: list[str] = []
    for run in runs:
        if isinstance(run, TextRun):
            parts.append(_inline_markup(run.text))
        elif isinstance(run, InlineMath):
            path = crops.paths.get(formula_key(page_index, run.bbox))
            if path:
                height = max(4.0, 0.92 * size)
                width = max(2.0, height * crops.aspects.get(formula_key(page_index, run.bbox), 1.0))
                parts.append(
                    f'<img src="{path}" width="{width:.2f}" height="{height:.2f}" valign="-2"/>'
                )
            elif run.latex:
                parts.append(_inline_markup(f"${run.latex}$"))
    return "".join(parts)


def _region_image(crops, page_index: int, bbox: Rect | None, content_width: float):
    """A raster crop of a figure or display formula, sized to the column."""
    if bbox is None:
        return None
    key = "region:" + formula_key(page_index, bbox)
    path = crops.crop(key, page_index, bbox)
    if not path:
        return None
    aspect = crops.aspects.get(key, 1.0)
    width = min(content_width, max(24.0, bbox[2] - bbox[0]))
    return RLImage(path, width=width, height=width / max(aspect, 0.05))


def _table_flowable(table: Table, content_width: float, style: ParagraphStyle):
    """Rebuild the cell grid proportionally to the source column widths."""
    cells = [cell for cell in table.cells if cell.bbox]
    if not cells:
        return None
    columns = sorted({round(cell.bbox[0]) for cell in cells})
    rows = sorted({round(cell.bbox[1]) for cell in cells})

    def column_of(x: float) -> int:
        return min(range(len(columns)), key=lambda i: abs(columns[i] - x))

    def row_of(y: float) -> int:
        return min(range(len(rows)), key=lambda i: abs(rows[i] - y))

    grid: list[list[str]] = [[""] * len(columns) for _ in rows]
    for cell in cells:
        text = cell.translated.strip() or cell.text
        grid[row_of(cell.bbox[1])][column_of(cell.bbox[0])] = text
    data = [
        [RLParagraph(_inline_markup(text) or " ", style) for text in row] for row in grid
    ]
    widths = [content_width / len(columns)] * len(columns)
    flowable = RLTable(data, colWidths=widths)
    flowable.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return flowable


def reflow_page_pdf(
    frame: PageFrame,
    page_blocks: list[Block],
    *,
    crops,
    fonts: CjkFontSet,
) -> bytes:
    """Typeset one source page's blocks fresh; may spill onto extra pages."""
    styles = _styles(fonts)
    margin = MARGIN_RATIO * frame.width
    content_width = frame.width - 2 * margin
    story: list = []
    for block in page_blocks:
        page_index = getattr(block, "page_index", frame.index)
        if isinstance(block, Title):
            level_style = styles["title"]
            if block.level > 1:
                size = TITLE_SIZES.get(block.level, SMALL_TITLE_SIZE)
                level_style = ParagraphStyle(
                    f"title{block.level}", parent=styles["title"], fontSize=size,
                    leading=size * 1.3,
                )
            if block.text.strip():
                story.append(RLParagraph(_inline_markup(block.text), level_style))
        elif isinstance(block, Author):
            if block.text.strip():
                story.append(RLParagraph(_inline_markup(block.text), styles["caption"]))
        elif isinstance(block, IRParagraph):
            markup = _runs_markup(block.runs, page_index, crops, DEFAULT_BODY_SIZE)
            if markup.strip():
                story.append(RLParagraph(markup, styles["body"]))
        elif isinstance(block, ListBlock):
            ordered = "ordered" in block.list_type or "number" in block.list_type
            for index, item in enumerate(block.items):
                markup = _runs_markup(item, page_index, crops, DEFAULT_BODY_SIZE)
                if not markup.strip():
                    continue
                marker = f"{index + 1}. " if ordered else "\u2022 "
                story.append(RLParagraph(_inline_markup(marker) + markup, styles["body"]))
        elif isinstance(block, DisplayMath):
            image = _region_image(crops, page_index, block.bbox, content_width)
            if image is not None:
                story.append(image)
            elif block.latex.strip():
                story.append(RLParagraph(_inline_markup(block.latex), styles["caption"]))
        elif isinstance(block, Image):
            image = _region_image(crops, page_index, block.bbox, content_width)
            if image is not None:
                story.append(image)
            if block.caption.strip():
                story.append(RLParagraph(_inline_markup(block.caption), styles["caption"]))
        elif isinstance(block, Table):
            if block.caption.strip():
                story.append(RLParagraph(_inline_markup(block.caption), styles["caption"]))
            flowable = _table_flowable(block, content_width, styles["cell"])
            if flowable is not None:
                story.append(flowable)
                story.append(Spacer(1, 6))
    if not story:
        story.append(RLParagraph(" ", styles["body"]))

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=(frame.width, frame.height),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )
    document.build(story)
    return buffer.getvalue()
