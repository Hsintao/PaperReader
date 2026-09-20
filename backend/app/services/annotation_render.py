"""Draw the parser's detected regions back onto the source PDF.

Every IR block carries the page and the box the parser reported for it. This
module paints those boxes on the original pages — one colour per block
category, with the category name at the box's top-left corner, the way an
object-detection annotation looks — so a parse can be inspected against the
page it came from.

Only a vector overlay is added: the source page keeps its text layer, links
and artwork, so the annotated file still supports search and selection.
"""

from __future__ import annotations

import io
from difflib import SequenceMatcher
from pathlib import Path

import pypdf
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import pdf_ops
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_model import (
    PageFrame,
    _join_chars,
    _line_box,
    _normalized_positions,
    document_lines,
)
from app.services.mineru_layout import (
    Author,
    Block,
    DisplayMath,
    Image,
    ListBlock,
    Table,
    Title,
)

Rect = tuple[float, float, float, float]

# Bumped whenever the drawn categories change, so a document annotated by an
# older revision is rebuilt instead of served with an outdated overlay.
ANNOTATION_REVISION = 4

# Category label and RGB colour, shared by every box of that category.
CATEGORY_STYLES: dict[str, tuple[str, tuple[float, float, float]]] = {
    "title": ("标题", (0.85, 0.20, 0.20)),
    "author": ("作者", (0.60, 0.25, 0.65)),
    "paragraph": ("正文", (0.13, 0.42, 0.85)),
    "list": ("列表", (0.05, 0.55, 0.60)),
    "formula": ("公式", (0.90, 0.55, 0.05)),
    "figure": ("图片", (0.20, 0.60, 0.30)),
    "table": ("表格", (0.45, 0.30, 0.80)),
}

# Captions share one colour; the label says which artwork they belong to.
CAPTION_COLOR = (0.75, 0.45, 0.10)
CAPTION_LABELS = {"figure": "图注", "table": "表注"}

_LABEL_SIZE = 6.5
_LABEL_PAD = 2.0
_STROKE_WIDTH = 1.0

# The VLM parser reports a figure's caption as text without a box of its own,
# so the caption's position is recovered from the page's text layer.
_CAPTION_MIN_CHARS = 5
_CAPTION_MIN_RATIO = 0.8
_CAPTION_MAX_LINES = 3


def block_category(block: Block) -> str:
    """Map an IR block to its annotation category."""
    if isinstance(block, Title):
        return "title"
    if isinstance(block, Author):
        return "author"
    if isinstance(block, ListBlock):
        return "list"
    if isinstance(block, DisplayMath):
        return "formula"
    if isinstance(block, Image):
        return "figure"
    if isinstance(block, Table):
        return "table"
    return "paragraph"


def _draw_region(
    canvas,
    font: str,
    rect: Rect,
    label: str,
    color: tuple[float, float, float],
    page_height: float,
) -> None:
    x0, y0, x1, y1 = rect
    canvas.setStrokeColorRGB(*color)
    canvas.setLineWidth(_STROKE_WIDTH)
    canvas.setDash()
    canvas.rect(
        x0, y0, max(0.1, x1 - x0), max(0.1, y1 - y0), stroke=1, fill=0
    )

    box_width = canvas.stringWidth(label, font, _LABEL_SIZE) + 2 * _LABEL_PAD
    box_height = _LABEL_SIZE + 2 * _LABEL_PAD
    # Sit the chip on the box's top-left corner; a box touching the page top
    # keeps its chip just inside instead of running off the page.
    label_y = y1 + 0.5
    if label_y + box_height > page_height:
        label_y = y1 - box_height - 0.5
    label_y = max(0.0, min(label_y, page_height - box_height))
    canvas.setFillColorRGB(*color)
    canvas.rect(x0, label_y, box_width, box_height, stroke=0, fill=1)
    canvas.setFillColorRGB(1.0, 1.0, 1.0)
    canvas.drawString(x0 + _LABEL_PAD, label_y + _LABEL_PAD, label)


def _union_rects(rects: list[Rect]) -> Rect:
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )


def _rect_inside(outer: Rect, inner: Rect, slack: float = 1.0) -> bool:
    return (
        inner[0] >= outer[0] - slack
        and inner[1] >= outer[1] - slack
        and inner[2] <= outer[2] + slack
        and inner[3] <= outer[3] + slack
    )


def _vertical_gap(anchor: Rect, box: Rect) -> float:
    if box[1] >= anchor[3]:
        return box[1] - anchor[3]
    if box[3] <= anchor[1]:
        return anchor[1] - box[3]
    return 0.0


def _locate_caption(caption: str, lines: list[list], anchor: Rect | None) -> Rect | None:
    """Find where a caption's text is drawn, using the page's own text layer.

    The declared caption is aligned against every line window; windows that
    match well are then narrowed to the one sitting by `anchor`, because two
    figures on a page can repeat the same panel labels verbatim.
    """
    needle, _positions = _normalized_positions(caption)
    if len(needle) < _CAPTION_MIN_CHARS:
        return None
    candidates: list[tuple[float, Rect]] = []
    for start in range(len(lines)):
        for count in range(1, _CAPTION_MAX_LINES + 1):
            window = lines[start : start + count]
            if len(window) < count:
                break
            text = "".join(
                _normalized_positions(_join_chars(line))[0] for line in window
            )
            if len(text) < _CAPTION_MIN_CHARS:
                continue
            ratio = SequenceMatcher(None, needle, text, autojunk=False).ratio()
            if ratio >= _CAPTION_MIN_RATIO:
                candidates.append(
                    (ratio, _union_rects([_line_box(line) for line in window]))
                )
    if not candidates:
        return None
    if anchor is None:
        return max(candidates, key=lambda item: item[0])[1]
    beside = [
        candidate
        for candidate in candidates
        if candidate[1][2] > anchor[0] and candidate[1][0] < anchor[2]
    ]
    if beside:
        return min(beside, key=lambda item: _vertical_gap(anchor, item[1]))[1]
    return max(candidates, key=lambda item: item[0])[1]


def _caption_rect(block: Block, frame: PageFrame, lines: list[list]) -> Rect | None:
    """Recover a figure or table caption's box.

    `middle.json` parses carry the caption's geometry directly; the structured
    content list reports it as text only, so it is looked up on the page.
    """
    caption = (getattr(block, "caption", "") or "").strip()
    if not caption:
        return None
    declared = getattr(block, "caption_bbox", None)
    if declared is not None:
        return declared
    if not frame.has_text_layer or not lines:
        return None
    return _locate_caption(caption, lines, getattr(block, "bbox", None))


def render_annotated_pdf(
    *,
    source_pdf: Path,
    frames: list[PageFrame],
    blocks: list[Block],
    output_pdf: Path,
) -> int:
    """Write `source_pdf` with every parsed region boxed and labelled.

    Returns how many regions were drawn.
    """
    fonts = require_cjk_font()
    reader = pypdf.PdfReader(str(source_pdf))
    writer = pypdf.PdfWriter()

    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        page_index = getattr(block, "page_index", -1)
        bbox = getattr(block, "bbox", None)
        if bbox is None or not (0 <= page_index < len(reader.pages)):
            continue
        by_page.setdefault(page_index, []).append(block)

    drawn = 0
    for index, source_page in enumerate(reader.pages):
        page_blocks = by_page.get(index, [])
        if not page_blocks:
            writer.add_page(source_page)
            continue
        frame = frames[index] if index < len(frames) else None
        width = float(frame.width) if frame is not None else float(source_page.mediabox.width)
        height = float(frame.height) if frame is not None else float(source_page.mediabox.height)
        buffer = io.BytesIO()
        canvas = pdf_canvas.Canvas(buffer, pagesize=(width, height))
        canvas.setFont(fonts.regular, _LABEL_SIZE)
        for block in page_blocks:
            label, color = CATEGORY_STYLES[block_category(block)]
            _draw_region(canvas, fonts.regular, block.bbox, label, color, height)
            drawn += 1
        if frame is not None:
            page_lines: list[list] | None = None
            for block in page_blocks:
                if isinstance(block, Image):
                    kind = "figure"
                elif isinstance(block, Table):
                    kind = "table"
                else:
                    continue
                if page_lines is None:
                    page_lines = [line for _page, line in document_lines([frame])]
                rect = _caption_rect(block, frame, page_lines)
                if rect is None:
                    continue
                if block.bbox and _rect_inside(block.bbox, rect):
                    continue
                _draw_region(
                    canvas, fonts.regular, rect, CAPTION_LABELS[kind], CAPTION_COLOR, height
                )
                drawn += 1
        canvas.showPage()
        canvas.save()
        target = writer.add_page(source_page)
        pdf_ops.merge_overlay_bytes(
            target, buffer.getvalue(), clip=(0.0, 0.0, width, height)
        )

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with output_pdf.open("wb") as handle:
        writer.write(handle)
    return drawn
