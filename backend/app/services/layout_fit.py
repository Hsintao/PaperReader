"""Per-block typography and fit search for the translated page.

A translated block keeps the source block's box, but its typography does not
come from the source page: font sizes start from fixed defaults (body text,
title levels, table cells) and weight is not measured at all. Every page ends
up with one unified body size, which may drift slightly up or down as the
page's actual layout demands, bounded so it never becomes too small or too
large. When the translation does not fit, the block gives ground in the order
the layout design prescribes: use the whitespace below the block, shrink this
block's font within the bounds, and continue into a region the parser dropped.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph

from app.services.cjk_fonts import CjkFontSet
from app.services.layout_model import PageFrame, SourceChar
from app.services.mineru_layout import (
    Block,
    InlineMath,
    ListBlock,
    Paragraph as IRParagraph,
    Table,
    TextRun,
    Title,
)

Rect = tuple[float, float, float, float]

DEFAULT_BODY_SIZE = 10.5      # body text never measured from the source page
TITLE_SIZES = {1: 16.0, 2: 13.0}
SMALL_TITLE_SIZE = 11.5       # title level 3 and deeper
MIN_SIZE_RATIO = 0.85         # a block never shrinks below 85% of its default
SIZE_CEIL_RATIO = 1.1         # the unified page size grows at most this much
ABS_MIN_SIZE = 7.0            # hard bounds: never too small, never too large
ABS_MAX_SIZE = 22.0
TITLE_KEEP_RATIO = 1.25       # headings bigger than this keep their own level
MIN_LEADING_RATIO = 1.3
MAX_LEADING_RATIO = 1.8
CELL_MIN_RATIO = 0.75         # table cell text shrinks at most to this ratio
REMOVAL_SLACK = 2.0           # text origins sit a hair outside their glyph box
FIT_EPSILON = 0.6             # points of slack when testing whether text fits
FIT_ITERATIONS = 12


@dataclass
class Fragment:
    """A piece of a translated block: text, an inline formula, or a break."""

    kind: str = "text"
    text: str = ""
    image_key: str = ""
    aspect: float = 1.0


@dataclass
class BlockPlan:
    kind: str
    page_index: int
    source_rect: Rect
    target: Rect
    fragments: list[Fragment] = field(default_factory=list)
    size: float = 0.0
    baseline_size: float = 0.0
    leading: float = 0.0
    leading_ratio: float = 1.5
    bold: bool = False
    align: str = "left"
    status: str = "translated"
    reason: str = ""
    continuation: Rect | None = None
    source_text: str = ""
    block: Block | None = None


@dataclass
class CellPlan:
    page_index: int
    source_rect: Rect
    source_text: str
    translated: str
    size: float = 0.0
    baseline_size: float = 0.0
    leading: float = 0.0
    bold: bool = False
    align: str = "left"
    status: str = "translated"
    reason: str = ""
    cell: object | None = None


@dataclass
class PagePlan:
    index: int
    width: float
    height: float
    blocks: list[BlockPlan] = field(default_factory=list)
    cells: list[CellPlan] = field(default_factory=list)
    status: str = "ok"
    reason: str = ""
    leading: float = 0.0
    body_size: float = 0.0
    had_text_layer: bool = True

    @property
    def mask_rects(self) -> list[Rect]:
        """Exact source boxes of the blocks whose text is replaced."""
        rects = [
            plan.source_rect
            for plan in self.blocks
            if plan.status in {"translated", "drop"}
        ]
        rects.extend(
            plan.source_rect for plan in self.cells if plan.status == "translated"
        )
        return rects

    @property
    def removal_rects(self) -> list[Rect]:
        """Boxes used to decide which text operators to drop.

        A glyph box starts at the glyph outline while the text operator is
        positioned at the pen origin, so the boxes are opened up slightly;
        otherwise a block's own first characters survive removal.
        """
        return [inflate(rect, REMOVAL_SLACK) for rect in self.mask_rects]

    @property
    def translated_plans(self) -> list[BlockPlan]:
        return [plan for plan in self.blocks if plan.status == "translated"]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# The translation contract keeps inline <sub>/<sup>/<br> tags from the source,
# so the renderer turns them back into typography instead of escaping them into
# visible markup. A script marker is short and holds no Chinese; the model
# sometimes drags a tag pair onto a whole clause, and rendering that as a
# subscript would garble the line, so those pairs lose their tags instead.
_INLINE_TAG_RE = re.compile(r"</?(?:sub|sup)\s*>|<br\s*/?>", re.IGNORECASE)
_REPORTLAB_TAG = {"sub": "sub", "sup": "super"}
_SCRIPT_MAX_CHARS = 12
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_script_content(content: str) -> bool:
    stripped = content.strip()
    if not stripped or len(stripped) > _SCRIPT_MAX_CHARS:
        return False
    return _CJK_RE.search(stripped) is None


def _inline_markup(text: str) -> str:
    """Escape reportlab markup, keeping the inline tags the translation keeps.

    ``<sub>``/``<sup>``/``<br>`` become reportlab's own tags; every other
    angle bracket stays escaped. Unbalanced tags are dropped rather than
    escaped, because a stray tag is a model artifact and rendering it as
    literal text is the defect this function exists to remove.
    """
    parts: list[str] = []
    position = 0
    open_index = -1
    open_end = 0
    open_name = ""
    for match in _INLINE_TAG_RE.finditer(text):
        parts.append(_escape(text[position:match.start()]))
        position = match.end()
        lowered = match.group(0).lower()
        if lowered.startswith("<br"):
            parts.append("<br/>")
            continue
        name = "sub" if "sub" in lowered else "sup"
        if lowered.startswith("</"):
            if open_name == name:
                if _is_script_content(text[open_end:match.start()]):
                    parts.append(f"</{_REPORTLAB_TAG[name]}>")
                else:
                    parts[open_index] = ""
                open_name = ""
                open_index = -1
        elif not open_name:
            open_index = len(parts)
            open_end = match.end()
            open_name = name
            parts.append(f"<{_REPORTLAB_TAG[name]}>")
    parts.append(_escape(text[position:]))
    if open_name:
        parts[open_index] = ""
    return "".join(parts)


def _normalize(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text or "").lower()


def rect_overlap_area(left: Rect, right: Rect) -> float:
    width = min(left[2], right[2]) - max(left[0], right[0])
    height = min(left[3], right[3]) - max(left[1], right[1])
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def overlaps_horizontally(left: Rect, right: Rect) -> bool:
    return min(left[2], right[2]) - max(left[0], right[0]) > 0


def inflate(rect: Rect, amount: float) -> Rect:
    return (rect[0] - amount, rect[1] - amount, rect[2] + amount, rect[3] + amount)


def union(left: Rect, right: Rect) -> Rect:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def chars_in_rect(frame: PageFrame, rect: Rect) -> list[SourceChar]:
    out: list[SourceChar] = []
    for char in frame.chars:
        center_x = (char.rect[0] + char.rect[2]) / 2
        center_y = (char.rect[1] + char.rect[3]) / 2
        if rect[0] <= center_x <= rect[2] and rect[1] <= center_y <= rect[3]:
            out.append(char)
    return out


def block_style(
    frame: PageFrame, rect: Rect, *, is_title: bool, level: int = 1
) -> tuple[float, bool, float, str]:
    """Default (font size, bold, leading ratio, alignment) for a block.

    Size and weight come from the layout defaults, not the source page; only
    line spacing and alignment are still measured from the source layout.
    """
    if is_title:
        size = TITLE_SIZES.get(level, SMALL_TITLE_SIZE)
    else:
        size = DEFAULT_BODY_SIZE
    size = min(max(size, ABS_MIN_SIZE), ABS_MAX_SIZE)
    return (
        size,
        is_title,
        _measure_leading(frame, rect, size),
        _measure_alignment(frame, rect, is_title, level),
    )


def _measure_leading(frame: PageFrame, rect: Rect, size: float) -> float:
    chars = chars_in_rect(frame, rect)
    baselines: list[float] = []
    for char in sorted(chars, key=lambda item: -item.rect[1]):
        if not char.char.strip():
            continue
        if not baselines or abs(baselines[-1] - char.rect[1]) > 0.5 * max(size, 1.0):
            baselines.append(char.rect[1])
    if len(baselines) >= 2 and size > 0:
        distances = sorted(
            baselines[index] - baselines[index + 1]
            for index in range(len(baselines) - 1)
        )
        distances = [value for value in distances if value > 0]
        if distances:
            ratio = distances[len(distances) // 2] / size
            return min(max(ratio, MIN_LEADING_RATIO), MAX_LEADING_RATIO)
    return 1.5


def _measure_alignment(
    frame: PageFrame, rect: Rect, is_title: bool, level: int
) -> str:
    """Only the paper title on the first page's upper half is centred."""
    if not is_title or frame.index != 0 or level > 1:
        return "left"
    if rect[1] < 0.5 * frame.height:
        return "left"
    left_margin = rect[0] - frame.origin[0]
    right_margin = frame.origin[0] + frame.width - rect[2]
    if abs(left_margin - right_margin) <= max(8.0, 0.02 * frame.width):
        return "center"
    return "left"


class TextMeasurer:
    """Builds and measures the same paragraph markup the renderer draws."""

    def __init__(self, fonts: CjkFontSet, image_paths=None, image_aspects=None):
        self.fonts = fonts
        self.image_paths = image_paths or {}
        self.image_aspects = image_aspects or {}
        self._canvas = pdf_canvas.Canvas(io.BytesIO(), pagesize=(1, 1))

    def has_image(self, key: str) -> bool:
        return bool(key) and key in self.image_paths

    def style(
        self, size: float, leading: float, *, bold: bool, align: str
    ) -> ParagraphStyle:
        return ParagraphStyle(
            name="block",
            fontName=self.fonts.name(bold),
            fontSize=size,
            leading=leading,
            wordWrap="CJK",
            alignment=TA_CENTER if align == "center" else TA_LEFT,
            spaceBefore=0,
            spaceAfter=0,
            firstLineIndent=0,
        )

    def markup(self, fragments: list[Fragment], size: float) -> str:
        parts: list[str] = []
        for fragment in fragments:
            if fragment.kind == "break":
                parts.append("<br/>")
            elif fragment.kind == "formula":
                path = self.image_paths.get(fragment.image_key)
                if not path:
                    continue
                height = max(4.0, 0.92 * size)
                width = max(2.0, height * self.image_aspects.get(fragment.image_key, 1.0))
                parts.append(
                    f'<img src="{path}" width="{width:.2f}" height="{height:.2f}" '
                    f'valign="-2"/>'
                )
            else:
                parts.append(_inline_markup(fragment.text))
        return "".join(parts)

    def paragraph(
        self,
        fragments: list[Fragment],
        size: float,
        leading: float,
        *,
        bold: bool,
        align: str,
    ) -> Paragraph:
        return Paragraph(
            self.markup(fragments, size) or " ",
            self.style(size, leading, bold=bold, align=align),
        )

    def measure(
        self,
        fragments: list[Fragment],
        size: float,
        leading: float,
        *,
        bold: bool,
        align: str,
        width: float,
    ) -> float:
        if not fragments:
            return 0.0
        paragraph = self.paragraph(fragments, size, leading, bold=bold, align=align)
        try:
            _, height = paragraph.wrapOn(self._canvas, max(1.0, width), 100000)
        except Exception:
            return 100000.0
        return float(height)


def formula_key(page_index: int, bbox: Rect | None) -> str:
    if bbox is None:
        return ""
    return f"{page_index}:{bbox[0]:.1f}:{bbox[1]:.1f}:{bbox[2]:.1f}:{bbox[3]:.1f}"


def fragments_of(block: Block) -> list[Fragment]:
    page_index = getattr(block, "page_index", -1)
    fragments: list[Fragment] = []
    if isinstance(block, Title):
        fragments.append(Fragment(kind="text", text=block.text))
        return fragments
    if isinstance(block, IRParagraph):
        for run in block.runs:
            if isinstance(run, TextRun):
                fragments.append(Fragment(kind="text", text=run.text))
            elif isinstance(run, InlineMath):
                fragments.append(
                    Fragment(kind="formula", image_key=formula_key(page_index, run.bbox))
                )
        return fragments
    if isinstance(block, ListBlock):
        ordered = "ordered" in block.list_type or "number" in block.list_type
        for index, item in enumerate(block.items):
            if fragments:
                fragments.append(Fragment(kind="break"))
            marker = f"{index + 1}. " if ordered else "\u2022 "
            fragments.append(Fragment(kind="text", text=marker))
            for run in item:
                if isinstance(run, TextRun):
                    fragments.append(Fragment(kind="text", text=run.text))
                elif isinstance(run, InlineMath):
                    fragments.append(
                        Fragment(
                            kind="formula", image_key=formula_key(page_index, run.bbox)
                        )
                    )
    return fragments


def source_text_of(block: Block) -> str:
    """The block's original wording, kept alongside the translation."""
    stored = getattr(block, "source_text", "")
    if stored:
        return stored
    return current_text_of(block)


def current_text_of(block: Block) -> str:
    """The text the block holds now (its translation, after translation)."""
    if isinstance(block, Title):
        return block.text
    if isinstance(block, IRParagraph):
        return "".join(run.text for run in block.runs if isinstance(run, TextRun))
    if isinstance(block, ListBlock):
        return " ".join(
            "".join(run.text for run in item if isinstance(run, TextRun))
            for item in block.items
        )
    return ""


def expand_target(
    frame: PageFrame, source_rect: Rect, occupied: list[Rect]
) -> Rect:
    """Use the free space between this block and the region below it."""
    floor = frame.origin[1]
    for region in occupied:
        if region == source_rect:
            continue
        if not overlaps_horizontally(source_rect, region):
            continue
        if region[3] <= source_rect[1] + 1.0:
            floor = max(floor, region[3])
    return (source_rect[0], min(source_rect[1], floor), source_rect[2], source_rect[3])


def next_region_below(source_rect: Rect, occupied: list[Rect]) -> Rect | None:
    candidates = [
        region
        for region in occupied
        if region[3] <= source_rect[1] + 1.0 and overlaps_horizontally(source_rect, region)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[3])


def _overlaps_any(rect: Rect, regions: list[Rect]) -> bool:
    return any(rect_overlap_area(rect, region) > 0 for region in regions)


def region_is_block_tail(frame: PageFrame, rect: Rect, declared: str) -> bool:
    """Whether a parser-dropped region continues this block's own text.

    Without the check a paragraph the parser lost entirely would be removed
    and never translated, so continuation stays off unless the dropped words
    really are this block's tail.
    """
    region = _normalize("".join(char.char for char in chars_in_rect(frame, rect)))
    if len(region) < 8:
        return False
    return _normalize(declared).endswith(region)


def fit_size(
    measurer: TextMeasurer,
    plan: BlockPlan,
    target: Rect,
    *,
    leading_ratio: float,
) -> float:
    """Largest font size (down to the floor) whose text fits `target`."""
    width = max(1.0, target[2] - target[0])
    height = max(1.0, target[3] - target[1])
    low = max(plan.baseline_size * MIN_SIZE_RATIO, ABS_MIN_SIZE)
    high = min(plan.baseline_size, ABS_MAX_SIZE)

    def fits(size: float) -> bool:
        measured = measurer.measure(
            plan.fragments,
            size,
            size * leading_ratio,
            bold=plan.bold,
            align=plan.align,
            width=width,
        )
        return measured <= height + FIT_EPSILON

    if fits(high):
        return high
    best = 0.0
    for _ in range(FIT_ITERATIONS):
        middle = (low + high) / 2
        if fits(middle):
            best = middle
            low = middle
        else:
            high = middle
        if high - low < 0.05:
            break
    return best


def plan_block(
    frame: PageFrame,
    block: Block,
    *,
    measurer: TextMeasurer,
    occupied: list[Rect],
    lost: list[Rect],
) -> BlockPlan:
    source_rect = block.bbox or frame.rect
    is_title = isinstance(block, Title)
    size, bold, leading_ratio, align = block_style(
        frame, source_rect, is_title=is_title, level=getattr(block, "level", 1)
    )
    plan = BlockPlan(
        kind="title" if is_title else "paragraph",
        page_index=frame.index,
        source_rect=source_rect,
        target=source_rect,
        fragments=fragments_of(block),
        baseline_size=size,
        size=size,
        leading=size * leading_ratio,
        leading_ratio=leading_ratio,
        bold=bold,
        align=align,
        source_text=source_text_of(block),
        block=block,
    )
    if not any(
        fragment.kind == "text" and fragment.text.strip() for fragment in plan.fragments
    ):
        # An empty translation (a segment that could not be translated at all)
        # must not punch a blank hole into the page: leave the source text.
        plan.status = "original"
        plan.reason = "no translated text for this block"
        return plan
    if _box_covers_foreign_text(frame, source_rect, plan.source_text):
        plan.status = "original"
        plan.reason = "block box covers text the block does not own"
        return plan
    if any(
        fragment.kind == "formula" and not measurer.has_image(fragment.image_key)
        for fragment in plan.fragments
    ):
        # The parser omits the geometry of some inline formulas, so they cannot
        # be lifted out of the page. Rebuilding the paragraph without them keeps
        # the surrounding prose readable instead of leaving it in English.
        plan.fragments = [
            fragment
            for fragment in plan.fragments
            if not (
                fragment.kind == "formula"
                and not measurer.has_image(fragment.image_key)
            )
        ]
        if not any(
            fragment.kind == "text" and fragment.text.strip()
            for fragment in plan.fragments
        ):
            # The block is only geometry-less formulas: masking it would erase
            # them from the page, so keep the source content in place.
            plan.status = "original"
            plan.reason = "no translated text for this block"
            return plan

    target = expand_target(frame, source_rect, occupied)
    plan.target = target
    fitted = fit_size(measurer, plan, target, leading_ratio=leading_ratio)
    if fitted < size:
        candidate = next_region_below(source_rect, occupied)
        if (
            candidate is not None
            and _overlaps_any(candidate, lost)
            and region_is_block_tail(frame, candidate, plan.source_text)
        ):
            extended = union(source_rect, candidate)
            extended_fit = fit_size(
                measurer, plan, extended, leading_ratio=leading_ratio
            )
            if extended_fit > fitted + 0.05:
                plan.continuation = candidate
                # The region holds this paragraph's own tail, so its source text
                # is removed with the block instead of staying behind.
                plan.source_rect = extended
                target = extended
                fitted = extended_fit
    plan.target = target
    if fitted <= 0.0:
        plan.status = "original"
        plan.reason = "translation does not fit at the minimum font size"
        plan.size = size
        return plan
    plan.size = fitted
    plan.leading = fitted * leading_ratio
    return plan


def _box_covers_foreign_text(
    frame: PageFrame, rect: Rect, declared: str
) -> bool:
    """Reject a block whose box swallows text its own text does not account for."""
    declared_normalized = _normalize(declared)
    if len(declared_normalized) < 8:
        return False
    inside = _normalize("".join(char.char for char in chars_in_rect(frame, rect)))
    return len(inside) > 1.35 * len(declared_normalized) + 8


def plan_table_cells(
    frame: PageFrame, table: Table, *, measurer: TextMeasurer
) -> list[CellPlan]:
    plans: list[CellPlan] = []
    for cell in table.cells:
        if not cell.bbox or not cell.text.strip() or not cell.translated.strip():
            continue
        size, bold, leading_ratio, align = block_style(
            frame, cell.bbox, is_title=False
        )
        if _normalize(cell.text) == _normalize(cell.translated):
            continue
        fragments = [Fragment(kind="text", text=cell.translated)]
        width = max(1.0, cell.bbox[2] - cell.bbox[0])
        height = max(1.0, cell.bbox[3] - cell.bbox[1])

        def fits(candidate: float) -> bool:
            return (
                measurer.measure(
                    fragments,
                    candidate,
                    candidate * leading_ratio,
                    bold=bold,
                    align=align,
                    width=width,
                )
                <= height + FIT_EPSILON
            )

        fitted = 0.0
        if fits(size):
            fitted = size
        else:
            low, high = max(size * CELL_MIN_RATIO, ABS_MIN_SIZE), size
            for _ in range(10):
                middle = (low + high) / 2
                if fits(middle):
                    fitted = middle
                    low = middle
                else:
                    high = middle
        plan = CellPlan(
            page_index=frame.index,
            source_rect=cell.bbox,
            source_text=cell.text,
            translated=cell.translated,
            size=fitted,
            baseline_size=size,
            bold=bold,
            align=align,
            cell=cell,
        )
        if fitted <= 0.0:
            plan.status = "original"
            plan.reason = "cell translation does not fit"
            plan.size = size
        plan.leading = plan.size * leading_ratio
        plans.append(plan)
    return plans


def _max_fitting_leading(
    measurer: TextMeasurer, block: BlockPlan, size: float
) -> float:
    width = max(1.0, block.target[2] - block.target[0])
    height = max(1.0, block.target[3] - block.target[1])

    def fits(ratio: float) -> bool:
        return (
            measurer.measure(
                block.fragments,
                size,
                size * ratio,
                bold=block.bold,
                align=block.align,
                width=width,
            )
            <= height + FIT_EPSILON
        )

    if fits(MAX_LEADING_RATIO):
        return MAX_LEADING_RATIO
    if not fits(MIN_LEADING_RATIO):
        return MIN_LEADING_RATIO
    low, high = MIN_LEADING_RATIO, MAX_LEADING_RATIO
    for _ in range(8):
        middle = (low + high) / 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def _fits_at(measurer: TextMeasurer, block: BlockPlan, size: float) -> bool:
    """Whether the block's text fits its target at `size` (tightest leading)."""
    width = max(1.0, block.target[2] - block.target[0])
    height = max(1.0, block.target[3] - block.target[1])
    return (
        measurer.measure(
            block.fragments,
            size,
            size * MIN_LEADING_RATIO,
            bold=block.bold,
            align=block.align,
            width=width,
        )
        <= height + FIT_EPSILON
    )


def unify_page(plan: PagePlan, measurer: TextMeasurer) -> None:
    """One font size and one line spacing per page (design section 8).

    The shared size starts at the smallest size any block needed, then grows
    back up while every block still fits. Both directions are bounded, so the
    page size only ever drifts slightly from the default.
    """
    translated = plan.translated_plans
    if not translated:
        return
    body_size = min(block.size for block in translated)
    group = [
        block
        for block in translated
        if block.kind != "title"
        or block.baseline_size <= TITLE_KEEP_RATIO * body_size
    ]
    cap = min(
        min(block.baseline_size for block in group) * SIZE_CEIL_RATIO,
        ABS_MAX_SIZE,
    )
    low, high = body_size, cap
    for _ in range(FIT_ITERATIONS):
        middle = (low + high) / 2
        if all(_fits_at(measurer, block, middle) for block in group):
            low = middle
        else:
            high = middle
        if high - low < 0.05:
            break
    plan.body_size = low
    for block in group:
        block.size = plan.body_size

    plan.leading = min(_max_fitting_leading(measurer, block, block.size) for block in translated)
    for block in translated:
        block.leading = block.size * plan.leading

    width_cap: dict[int, float] = {}
    for index, block in enumerate(translated):
        width_cap[index] = max(1.0, block.target[2] - block.target[0])
    for index, block in enumerate(translated):
        height = max(1.0, block.target[3] - block.target[1])
        measured = measurer.measure(
            block.fragments,
            block.size,
            block.leading,
            bold=block.bold,
            align=block.align,
            width=width_cap[index],
        )
        if measured > height + FIT_EPSILON:
            block.status = "original"
            block.reason = "does not fit after page-level unification"


def plan_page(
    frame: PageFrame,
    blocks: list[Block],
    *,
    measurer: TextMeasurer,
    lost: list[Rect] | None = None,
) -> PagePlan:
    plan = PagePlan(
        index=frame.index,
        width=frame.width,
        height=frame.height,
        had_text_layer=frame.has_text_layer,
    )
    if frame.rotation not in (0, 360):
        plan.status = "original"
        plan.reason = "rotated page"
        return plan

    lost_regions = list(lost or [])
    occupied: list[Rect] = list(frame.known_regions)
    occupied.extend(block.bbox for block in blocks if block.bbox)
    occupied.extend(lost_regions)

    translatable = [
        block
        for block in blocks
        if block.bbox and isinstance(block, (Title, IRParagraph, ListBlock))
    ]
    for block in translatable:
        if _normalize(source_text_of(block)) == _normalize(current_text_of(block)):
            # Untranslated on purpose (bibliography, numbers-only) or already
            # identical: leave the source text exactly where it is.
            continue
        plan.blocks.append(
            plan_block(
                frame,
                block,
                measurer=measurer,
                occupied=occupied,
                lost=lost_regions,
            )
        )
    for block in blocks:
        if isinstance(block, Table):
            plan.cells.extend(plan_table_cells(frame, block, measurer=measurer))

    _drop_duplicate_blocks(plan)
    if plan.translated_plans:
        unify_page(plan, measurer)
    if not plan.translated_plans and not any(
        cell.status == "translated" for cell in plan.cells
    ):
        plan.status = "original"
        plan.reason = "nothing to translate on this page"
    return plan


def plan_document(
    frames: list[PageFrame],
    blocks: list[Block],
    *,
    measurer: TextMeasurer,
) -> list[PagePlan]:
    """Plan every page of a document: geometry, style and fit search."""
    from app.services.layout_model import lost_regions

    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        by_page.setdefault(getattr(block, "page_index", -1), []).append(block)
    plans: list[PagePlan] = []
    for frame in frames:
        page_blocks = by_page.get(frame.index, [])
        lost = lost_regions(
            frame, [block.bbox for block in page_blocks if block.bbox]
        )
        plans.append(
            plan_page(frame, page_blocks, measurer=measurer, lost=lost)
        )
    return plans


def _drop_duplicate_blocks(plan: PagePlan) -> None:
    """A block whose text repeats an earlier one is removed without redrawing."""
    for index, block in enumerate(plan.blocks):
        normalized = _normalize(block.source_text)
        if len(normalized) < 24:
            continue
        for earlier in plan.blocks[:index]:
            if earlier.status not in {"translated", "drop"}:
                continue
            if normalized in _normalize(earlier.source_text):
                block.status = "drop"
                block.reason = "duplicates an earlier block"
                break
