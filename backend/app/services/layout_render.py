"""Compose the translated PDF page by page.

Each translated page is the source page with the replaced text removed, plus a
text overlay drawn at the source coordinates. Figures, table rules, formulas,
captions and running heads stay untouched in the source page, so they keep
their vector quality, their position and their own text layer.

Two verification passes keep the result honest:

* the removed text must really be gone from the page's text layer, otherwise
  the affected block falls back to its source wording;
* text outside the replaced blocks must still be there, otherwise the page
  falls back — first to a masked overlay that keeps the translation anchored
  at the source boxes without touching the content stream, then (for pages
  without a usable plan, such as rotated pages) to a freshly typeset reflow
  page, and only lastly to the untouched source page.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings
from app.services import pdf_ops
from app.services.cjk_fonts import CjkFontFamily, require_cjk_font
from app.services.layout_fit import (
    Fragment,
    PagePlan,
    Rect,
    TextMeasurer,
    _normalize,
    current_text_of,
    formula_key,
    fragments_of,
    source_text_of,
)
from app.services.layout_model import PageFrame, SourceChar, lost_regions, measure_pages
from app.services.mineru_layout import (
    Block,
    InlineMath,
    ListBlock,
    Paragraph as IRParagraph,
    Table,
    Title,
)

_POSITION_TOLERANCE = 2.0
# PDFium reports a degenerate box (zero width and height, all glyphs on one
# point) for some glyphs. Their position is unknown, so they can neither be
# required to survive nor counted as leftovers -- treating them as verifiable
# text made a whole page fall back to the source because of one such glyph.
_MIN_CHAR_BOX = 0.5
_CROP_SCALE = 5.0
_MAX_CROP_PIXELS = 1600

_DEBUG_COLORS = {
    "title": (0.85, 0.20, 0.20),
    "paragraph": (0.15, 0.45, 0.85),
    "table": (0.15, 0.60, 0.25),
    "figure": (0.60, 0.30, 0.75),
    "caption": (0.95, 0.60, 0.10),
    "header_footer": (0.45, 0.45, 0.45),
    "lost": (0.90, 0.15, 0.60),
}


class LayoutRenderError(RuntimeError):
    """Raised when too many pages fail to produce a usable document."""


@dataclass
class PageResult:
    index: int
    status: str = "ok"
    reason: str = ""
    blocks: int = 0
    cells: int = 0
    reverted: int = 0


@dataclass
class RenderReport:
    pages: list[PageResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[PageResult]:
        return [page for page in self.pages if page.status not in {"ok", "masked", "reflow"}]


class FormulaCrops:
    """Raster crops of inline formulas, taken from the source page."""

    def __init__(self, source_pdf: Path, cache_dir: Path):
        self.source_pdf = Path(source_pdf)
        self.cache_dir = Path(cache_dir)
        self.paths: dict[str, str] = {}
        self.aspects: dict[str, float] = {}
        self._document = None

    def _page(self, index: int):
        if self._document is None:
            self._document = pdfium.PdfDocument(str(self.source_pdf))
        return self._document[index]

    def crop(self, key: str, page_index: int, bbox: Rect | None) -> str:
        if not key or bbox is None:
            return ""
        if key in self.paths:
            return self.paths[key]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{_safe_key(key)}.png"
        width = max(0.5, bbox[2] - bbox[0])
        height = max(0.5, bbox[3] - bbox[1])
        page = self._page(page_index)
        page_width, page_height = page.get_size()
        scale = min(_CROP_SCALE, _MAX_CROP_PIXELS / max(width, 1.0))
        crop = (
            max(0.0, bbox[0] - 1.0),
            max(0.0, bbox[1] - 1.0),
            max(0.0, page_width - bbox[2] - 1.0),
            max(0.0, page_height - bbox[3] - 1.0),
        )
        try:
            bitmap = page.render(scale=scale, crop=crop)
            try:
                image = bitmap.to_pil()
                image.save(target)
                pixel_width, pixel_height = image.size
            finally:
                bitmap.close()
        except Exception:
            return ""
        self.paths[key] = str(target)
        self.aspects[key] = (pixel_width / pixel_height) if pixel_height else 1.0
        return self.paths[key]

    def close(self) -> None:
        if self._document is not None:
            self._document.close()
            self._document = None


def _safe_key(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", key).strip("_")


def prepare_formula_crops(
    source_pdf: Path, frames: list[PageFrame], blocks: list[Block], cache_dir: Path
) -> FormulaCrops:
    """Rasterise every inline formula before the fit search needs its size."""
    crops = FormulaCrops(source_pdf, cache_dir)
    for block in blocks:
        page_index = getattr(block, "page_index", -1)
        if not (0 <= page_index < len(frames)):
            continue
        for key, bbox in _formula_boxes(block):
            crops.crop(key, page_index, bbox)
    return crops


def _formula_boxes(block: Block) -> list[tuple[str, Rect]]:
    """Every inline formula in a block, paired with its own source box."""
    from app.services.mineru_layout import ListBlock

    page_index = getattr(block, "page_index", -1)
    boxes: list[tuple[str, Rect]] = []
    runs: list = []
    if isinstance(block, IRParagraph):
        runs = list(block.runs)
    elif isinstance(block, ListBlock):
        for item in block.items:
            runs.extend(item)
    for run in runs:
        if isinstance(run, InlineMath) and run.bbox:
            boxes.append((formula_key(page_index, run.bbox), run.bbox))
    return boxes


def _text_present(chars: list[SourceChar], char: str, rect: Rect) -> bool:
    for candidate in chars:
        if candidate.char != char:
            continue
        if (
            abs(candidate.rect[0] - rect[0]) <= _POSITION_TOLERANCE
            and abs(candidate.rect[1] - rect[1]) <= _POSITION_TOLERANCE
        ):
            return True
    return False


def _has_usable_box(char: SourceChar) -> bool:
    return (
        (char.rect[2] - char.rect[0]) >= _MIN_CHAR_BOX
        and (char.rect[3] - char.rect[1]) >= _MIN_CHAR_BOX
    )


def _in_any_rect(char: SourceChar, rects: list[Rect]) -> bool:
    center_x = (char.rect[0] + char.rect[2]) / 2
    center_y = (char.rect[1] + char.rect[3]) / 2
    for rect in rects:
        if rect[0] <= center_x <= rect[2] and rect[1] <= center_y <= rect[3]:
            return True
    return False


def verify_removal(
    before: list[SourceChar], after: list[SourceChar], rects: list[Rect]
) -> list[SourceChar]:
    """Source characters that should be gone but are still on the page."""
    leftovers: list[SourceChar] = []
    for char in before:
        if not char.char.strip() or not _has_usable_box(char):
            continue
        if not _in_any_rect(char, rects):
            continue
        if _text_present(after, char.char, char.rect):
            leftovers.append(char)
    return leftovers


def verify_preserved(
    before: list[SourceChar], after: list[SourceChar], rects: list[Rect]
) -> list[SourceChar]:
    """Source characters outside the replaced blocks that went missing."""
    missing: list[SourceChar] = []
    for char in before:
        if not char.char.strip() or not _has_usable_box(char):
            continue
        if _in_any_rect(char, rects):
            continue
        if not _text_present(after, char.char, char.rect):
            missing.append(char)
    return missing


def _page_text_chars(page_bytes: bytes) -> list[SourceChar]:
    frames = measure_pages(page_bytes)
    return frames[0].chars if frames else []


def _draw_masks(canvas, page_plan: PagePlan) -> None:
    canvas.setFillColorRGB(1, 1, 1)
    for rect in page_plan.mask_rects:
        canvas.rect(
            rect[0],
            rect[1],
            max(0.1, rect[2] - rect[0]),
            max(0.1, rect[3] - rect[1]),
            stroke=0,
            fill=1,
        )


def _draw_blocks(
    canvas,
    measurer: TextMeasurer,
    page_plan: PagePlan,
) -> int:
    drawn = 0
    for plan in page_plan.translated_plans:
        width = max(1.0, plan.target[2] - plan.target[0])
        paragraph = measurer.paragraph(
            plan.fragments, plan.size, plan.leading, bold=plan.bold, align=plan.align
        )
        _, height = paragraph.wrapOn(canvas, width, 100000)
        y = plan.target[3] - height
        if y < plan.target[1] - 1.0:
            y = plan.target[1]
        paragraph.drawOn(canvas, plan.target[0], y)
        drawn += 1
    return drawn


def _draw_cells(canvas, measurer: TextMeasurer, page_plan: PagePlan) -> int:
    drawn = 0
    for plan in page_plan.cells:
        if plan.status != "translated":
            continue
        size = plan.size
        leading = plan.leading or size * 1.2
        fragments = [Fragment(kind="text", text=plan.translated)]
        width = max(1.0, plan.source_rect[2] - plan.source_rect[0])
        paragraph = measurer.paragraph(
            fragments, size, leading, bold=plan.bold, align=plan.align
        )
        _, height = paragraph.wrapOn(canvas, width, 100000)
        top = plan.source_rect[3] - 0.12 * size
        bottom = plan.source_rect[1] + 0.12 * size
        y = min(top - height, bottom) if top - height < bottom else top - height
        y = max(y, bottom)
        paragraph.drawOn(canvas, plan.source_rect[0], y)
        drawn += 1
    return drawn


def _render_overlay(
    measurer: TextMeasurer,
    page_plan: PagePlan,
    *,
    mask: bool,
) -> bytes:
    buffer = io.BytesIO()
    canvas = pdf_canvas.Canvas(
        buffer, pagesize=(page_plan.width, page_plan.height)
    )
    if mask:
        _draw_masks(canvas, page_plan)
    drawn = _draw_blocks(canvas, measurer, page_plan)
    drawn += _draw_cells(canvas, measurer, page_plan)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _page_has_translation(page_blocks: list[Block]) -> bool:
    """Whether the page holds any translated content worth reflowing."""
    for block in page_blocks:
        if isinstance(block, (Title, IRParagraph, ListBlock)):
            if _normalize(current_text_of(block)) != _normalize(source_text_of(block)):
                return True
        elif isinstance(block, Table):
            if any(
                cell.translated.strip()
                and _normalize(cell.translated) != _normalize(cell.text)
                for cell in block.cells
            ):
                return True
    return False


def _try_masked_overlay(writer, source_page, plan, measurer, result) -> bool:
    """Draw the translation anchored at the source boxes over white masks,
    without touching the page's content stream.

    Used when content-stream surgery cannot remove the source text cleanly
    (one text operator carries several blocks): the source glyphs stay in the
    text layer but are painted over, and every block keeps its original
    position, so the page still looks like the source.
    """
    if not plan.translated_plans and not any(
        cell.status == "translated" for cell in plan.cells
    ):
        return False
    try:
        overlay = _render_overlay(measurer, plan, mask=True)
        target = writer.add_page(source_page)
        pdf_ops.merge_overlay_bytes(
            target, overlay, clip=(0, 0, plan.width, plan.height)
        )
    except Exception as exc:
        result.reason = f"{result.reason}; masked-overlay fallback failed: {exc}"
        return False
    result.status = "masked"
    result.blocks = len(plan.translated_plans)
    result.cells = sum(1 for cell in plan.cells if cell.status == "translated")
    return True


def _try_reflow(writer, frame, page_blocks, crops, fonts, result) -> bool:
    """Replace a failed page with a freshly typeset reflow of its content."""
    if not settings.layout_reflow_fallback:
        return False
    if not _page_has_translation(page_blocks):
        return False
    from app.services.reflow_render import reflow_page_pdf

    try:
        payload = reflow_page_pdf(frame, page_blocks, crops=crops, fonts=fonts)
        for page in pypdf.PdfReader(io.BytesIO(payload)).pages:
            writer.add_page(page)
    except Exception as exc:
        result.reason = f"{result.reason}; reflow fallback failed: {exc}"
        return False
    result.status = "reflow"
    return True


def render_document(
    *,
    source_pdf: Path,
    plans: list[PagePlan],
    frames: list[PageFrame],
    blocks: list[Block],
    output_pdf: Path,
    crops: FormulaCrops,
    fonts: CjkFontFamily | None = None,
    debug_pdf: Path | None = None,
) -> RenderReport:
    fonts = fonts or require_cjk_font()
    measurer = TextMeasurer(fonts, crops.paths, crops.aspects)
    report = RenderReport()
    reader = pypdf.PdfReader(str(source_pdf))
    writer = pypdf.PdfWriter()

    blocks_by_page: dict[int, list[Block]] = {}
    for block in blocks:
        blocks_by_page.setdefault(getattr(block, "page_index", -1), []).append(block)

    for index, source_page in enumerate(reader.pages):
        frame = frames[index] if index < len(frames) else None
        plan = plans[index] if index < len(plans) else None
        result = PageResult(index=index)
        if frame is None or plan is None:
            result.status = "original"
            result.reason = "page has no layout plan"
            writer.add_page(source_page)
            report.pages.append(result)
            continue
        if plan.status != "ok":
            result.reason = plan.reason or "page layout unavailable"
            if _try_reflow(
                writer, frame, blocks_by_page.get(index, []), crops, fonts, result
            ):
                report.notes.append(
                    f"page {index + 1}: reflowed ({result.reason})"
                )
            else:
                result.status = "original"
                writer.add_page(source_page)
            report.pages.append(result)
            continue

        rendered = _attempt_page(
            writer,
            reader,
            source_page,
            frame,
            plan,
            measurer,
            blocks_by_page.get(index, []),
            crops=crops,
        )
        result.status = rendered.status
        result.reason = rendered.reason
        result.blocks = rendered.blocks
        result.cells = rendered.cells
        result.reverted = rendered.reverted
        if rendered.status != "ok":
            if _try_masked_overlay(writer, source_page, plan, measurer, result):
                report.notes.append(
                    f"page {index + 1}: masked overlay ({rendered.reason})"
                )
            elif _try_reflow(
                writer, frame, blocks_by_page.get(index, []), crops, fonts, result
            ):
                report.notes.append(
                    f"page {index + 1}: reflowed ({rendered.reason})"
                )
            else:
                writer.add_page(source_page)
        if rendered.reason:
            report.notes.append(f"page {index + 1}: {rendered.reason}")
        report.pages.append(result)

    _enforce_failure_budget(report)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with output_pdf.open("wb") as handle:
        writer.write(handle)
    if debug_pdf is not None:
        _write_debug_pdf(reader, frames, plans, blocks_by_page, debug_pdf)
    return report


def _attempt_page(
    writer,
    reader,
    source_page,
    frame: PageFrame,
    plan: PagePlan,
    measurer: TextMeasurer,
    page_blocks: list[Block],
    *,
    crops: FormulaCrops,
) -> PageResult:
    """Build one translated page, reverting blocks that verification rejects."""
    result = PageResult(index=frame.index)
    mask = not frame.has_text_layer
    for attempt in range(2):
        rects = plan.removal_rects
        if not rects:
            if result.reverted:
                result.status = "original"
                result.reason = (
                    "every replaced block was reverted to keep the source text"
                )
            else:
                # Nothing to replace: the source page is already the result.
                writer.add_page(source_page)
                result.reason = "no translatable content"
            return result
        try:
            page_bytes = _page_bytes(reader, source_page, rects)
        except Exception as exc:  # never lose a page to a stream rewrite bug
            result.status = "original"
            result.reason = f"page rewrite failed: {exc}"
            return result
        base_chars = _page_text_chars(page_bytes)
        if not mask:
            # Removal is decided with a small slack (text operators sit a hair
            # outside their glyph boxes), but only the block's own box has to
            # be empty afterwards: the slack band may belong to the next line.
            leftovers = verify_removal(frame.chars, base_chars, plan.mask_rects)
            missing = verify_preserved(frame.chars, base_chars, rects)
            if leftovers:
                reverted = _revert_offenders(plan, leftovers)
                if reverted:
                    result.reverted += reverted
                    continue
            if missing:
                reverted = _revert_dragged_blocks(plan, missing)
                if reverted:
                    result.reverted += reverted
                    continue
                result.status = "original"
                result.reason = (
                    f"{len(missing)} source characters would have been lost"
                )
                return result
        overlay = _render_overlay(measurer, plan, mask=mask)
        page = pypdf.PdfReader(io.BytesIO(page_bytes)).pages[0]
        pdf_ops.merge_overlay_bytes(page, overlay, clip=(0, 0, plan.width, plan.height))
        writer.add_page(page)
        result.blocks = len(plan.translated_plans)
        result.cells = sum(1 for cell in plan.cells if cell.status == "translated")
        return result
    # Second attempt still had leftovers: the caller decides the fallback.
    result.status = "original"
    result.reason = "source text could not be removed cleanly"
    return result


def _page_bytes(reader, source_page, rects: list[Rect]) -> bytes:
    writer = pypdf.PdfWriter()
    pdf_ops.remove_text_page(writer, reader, source_page, rects)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _revert_offenders(plan: PagePlan, leftovers: list[SourceChar]) -> int:
    """Send blocks whose source text survived back to the original content."""
    reverted = 0
    for plan_block in plan.blocks:
        if plan_block.status != "translated":
            continue
        if any(_in_any_rect(char, [plan_block.source_rect]) for char in leftovers):
            plan_block.status = "original"
            plan_block.reason = "source text survived removal"
            reverted += 1
    for cell in plan.cells:
        if cell.status != "translated":
            continue
        if any(_in_any_rect(char, [cell.source_rect]) for char in leftovers):
            cell.status = "original"
            cell.reason = "source text survived removal"
            reverted += 1
    return reverted


def _revert_dragged_blocks(plan: PagePlan, missing: list[SourceChar]) -> int:
    """Revert blocks whose removal dragged neighbouring source text away.

    A single text-showing operator can carry both a translated block and an
    untouched neighbour (an inline formula that could not be located, a page
    number). Dropping it then loses the neighbour's characters; reverting the
    block that shares the operator keeps the page usable instead of throwing
    away every other translation on it.
    """
    reverted = 0
    for plan_block in plan.blocks:
        if plan_block.status != "translated":
            continue
        if any(_shares_line(plan_block.source_rect, char) for char in missing):
            plan_block.status = "original"
            plan_block.reason = "removal would have dragged neighbouring text away"
            reverted += 1
    for cell in plan.cells:
        if cell.status != "translated":
            continue
        if any(_shares_line(cell.source_rect, char) for char in missing):
            cell.status = "original"
            cell.reason = "removal would have dragged neighbouring text away"
            reverted += 1
    return reverted


def _shares_line(rect: Rect, char: SourceChar) -> bool:
    """Whether a character sits on the same line as a replaced block."""
    center_y = (char.rect[1] + char.rect[3]) / 2
    height = max(1.0, char.rect[3] - char.rect[1])
    tolerance = max(4.0, 1.5 * height)
    if not (rect[1] - tolerance <= center_y <= rect[3] + tolerance):
        return False
    if char.rect[2] < rect[0]:
        return rect[0] - char.rect[2] <= 3.0 * height
    if char.rect[0] > rect[2]:
        return char.rect[0] - rect[2] <= 3.0 * height
    return True


def _enforce_failure_budget(report: RenderReport) -> None:
    total = len(report.pages)
    failed = len(report.failed)
    if total and failed == total:
        raise LayoutRenderError(
            f"Layout rendering failed on all {total} page(s); no output produced"
        )
    if failed > 3 and failed >= 0.2 * total:
        raise LayoutRenderError(
            f"Layout rendering failed on {failed} of {total} pages; no output produced"
        )


def _write_debug_pdf(
    reader,
    frames: list[PageFrame],
    plans: list[PagePlan],
    blocks_by_page: dict[int, list[Block]],
    debug_pdf: Path,
) -> None:
    """Mark the source page with block categories and reused caption regions."""
    writer = pypdf.PdfWriter()
    fonts = require_cjk_font()
    for index, source_page in enumerate(reader.pages):
        frame = frames[index] if index < len(frames) else None
        if frame is None:
            writer.add_page(source_page)
            continue
        plan = plans[index] if index < len(plans) else None
        buffer = io.BytesIO()
        canvas = pdf_canvas.Canvas(buffer, pagesize=(frame.width, frame.height))
        canvas.setFont(fonts.regular, 6)
        for block in blocks_by_page.get(index, []):
            if not block.bbox:
                continue
            kind = _debug_kind(block)
            _debug_box(canvas, block.bbox, kind)
        if plan is not None:
            for region in lost_regions(frame):
                _debug_box(canvas, region, "lost")
        canvas.showPage()
        canvas.save()
        overlay = pypdf.PdfReader(io.BytesIO(buffer.getvalue())).pages[0]
        page = pypdf.PdfWriter()
        page.add_page(source_page)
        page.pages[0].merge_page(overlay)
        writer.add_page(page.pages[0])
    debug_pdf.parent.mkdir(parents=True, exist_ok=True)
    with debug_pdf.open("wb") as handle:
        writer.write(handle)


def _debug_kind(block: Block) -> str:
    from app.services.mineru_layout import DisplayMath, Image, Table

    if isinstance(block, Title):
        return "title"
    if isinstance(block, Table):
        return "table"
    if isinstance(block, Image):
        return "figure"
    if isinstance(block, DisplayMath):
        return "formula"
    return "paragraph"


def _debug_box(canvas, rect: Rect, kind: str) -> None:
    color = _DEBUG_COLORS.get(kind, (0.2, 0.2, 0.2))
    canvas.setStrokeColorRGB(*color)
    canvas.setLineWidth(0.5)
    if kind in {"caption", "lost"}:
        canvas.setDash(2, 2)
    else:
        canvas.setDash()
    canvas.rect(
        rect[0], rect[1], max(0.1, rect[2] - rect[0]), max(0.1, rect[3] - rect[1]),
        stroke=1, fill=0,
    )
    if kind != "lost":
        canvas.setFillColorRGB(*color)
        canvas.drawString(rect[0] + 1.0, rect[3] + 1.0, kind)
