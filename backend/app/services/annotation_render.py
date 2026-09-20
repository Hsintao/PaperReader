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
from pathlib import Path

import pypdf
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import pdf_ops
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_model import (
    PageFrame,
    caption_rect,
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
ANNOTATION_REVISION = 5

# Category label and RGB colour, shared by every box of that category.
CATEGORY_STYLES: dict[str, tuple[str, tuple[float, float, float]]] = {
    "title": ("标题", (0.85, 0.20, 0.20)),
    "author": ("作者", (0.60, 0.25, 0.65)),
    "affiliation": ("单位", (0.72, 0.35, 0.75)),
    "abstract": ("摘要", (0.10, 0.35, 0.75)),
    "keywords": ("关键词", (0.05, 0.50, 0.72)),
    "paragraph": ("正文", (0.13, 0.42, 0.85)),
    "list": ("列表", (0.05, 0.55, 0.60)),
    "footnote": ("脚注", (0.35, 0.45, 0.55)),
    "code": ("代码", (0.25, 0.25, 0.30)),
    "algorithm": ("算法", (0.40, 0.20, 0.45)),
    "reference_heading": ("参考文献标题", (0.55, 0.15, 0.15)),
    "reference_entry": ("参考文献条目", (0.70, 0.30, 0.30)),
    "formula": ("公式", (0.90, 0.55, 0.05)),
    "figure": ("图片", (0.20, 0.60, 0.30)),
    "table": ("表格", (0.45, 0.30, 0.80)),
    "unknown": ("未分类", (0.45, 0.45, 0.45)),
}

# Roles that get their own box colour; everything else is drawn as 正文.
_ROLE_CATEGORIES = {
    "author": "author",
    "affiliation": "affiliation",
    "abstract": "abstract",
    "keywords": "keywords",
    "footnote": "footnote",
    "code": "code",
    "algorithm": "algorithm",
    "reference_heading": "reference_heading",
    "reference_entry": "reference_entry",
    "unknown": "unknown",
}

# Captions share one colour; the label says which artwork they belong to.
CAPTION_COLOR = (0.75, 0.45, 0.10)
CAPTION_LABELS = {"figure": "图注", "table": "表注"}

_LABEL_SIZE = 6.5
_LABEL_PAD = 2.0
_STROKE_WIDTH = 1.0


def block_category(block: Block) -> str:
    """Map an IR block to its annotation category."""
    if isinstance(block, Title):
        return _ROLE_CATEGORIES.get(getattr(block, "role", ""), "title")
    if isinstance(block, Author):
        return "author"
    if isinstance(block, ListBlock):
        return _ROLE_CATEGORIES.get(getattr(block, "role", ""), "list")
    if isinstance(block, DisplayMath):
        return "formula"
    if isinstance(block, Image):
        return "figure"
    if isinstance(block, Table):
        return "table"
    return _ROLE_CATEGORIES.get(getattr(block, "role", ""), "paragraph")


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
            label, color = CATEGORY_STYLES.get(
                block_category(block), CATEGORY_STYLES["unknown"]
            )
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
                rect = caption_rect(block, frame, page_lines)
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
