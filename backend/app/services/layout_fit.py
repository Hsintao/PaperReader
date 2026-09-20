"""Per-block typography and fit search for the translated page.

A translated block keeps the source block's box, but its typography comes from
the fixed Chinese template rather than from the source page: body copy is Song
at 10.5pt in one column and 9pt in two, headings are Hei, and captions, table
cells and footnotes have their own sizes and leading. Only alignment and
paragraph indentation are still read from the source. When a translation does
not fit, the page gives ground in the order the layout design prescribes: use
the whitespace below the block, move later text down, shrink the whole page's
body size down to 6pt, and only then fall back to the original page.
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
from app.services.layout_model import (
    PageColumns,
    PageFrame,
    SourceChar,
    detect_page_columns,
)
from app.services.mineru_layout import (
    Block,
    Image,
    InlineMath,
    ListBlock,
    Paragraph as IRParagraph,
    Table,
    TextRun,
    Title,
)

Rect = tuple[float, float, float, float]

DEFAULT_BODY_SIZE = 10.5      # single-column body baseline
DOUBLE_COLUMN_BODY_SIZE = 9.0
MIN_BODY_SIZE = 6.0           # design floor: the page falls back below this
TITLE_SIZES = {1: 16.0, 2: 13.0}
SMALL_TITLE_SIZE = 11.5       # title level 3 and deeper
MIN_SIZE_RATIO = 0.85         # a block never shrinks below 85% of its default
SIZE_CEIL_RATIO = 1.1         # the unified page size grows at most this much
ABS_MIN_SIZE = 6.0            # hard bounds: never too small, never too large
ABS_MAX_SIZE = 22.0
TITLE_KEEP_RATIO = 1.25       # headings bigger than this keep their own level
MIN_LEADING_RATIO = 1.25      # design floor for line spacing
MAX_LEADING_RATIO = 1.8
CELL_MIN_RATIO = 0.75         # table cell text shrinks at most to this ratio
REMOVAL_SLACK = 2.0           # text origins sit a hair outside their glyph box
FIT_EPSILON = 0.6             # points of slack when testing whether text fits
FIT_ITERATIONS = 12


@dataclass(frozen=True)
class TypographyProfile:
    """One row of the fixed Chinese template."""

    size: float
    leading_ratio: float
    serif: bool = True
    bold: bool = False


# Named rows of the template (docs/translation-layout.md section 3).
TypographyProfile.TITLE_1 = TypographyProfile(16.0, 1.2, serif=False, bold=True)
TypographyProfile.TITLE_2 = TypographyProfile(13.0, 1.25, serif=False, bold=True)
TypographyProfile.TITLE_3 = TypographyProfile(11.5, 1.3, serif=False, bold=False)
TypographyProfile.BODY_SINGLE = TypographyProfile(DEFAULT_BODY_SIZE, 1.5)
TypographyProfile.BODY_DOUBLE = TypographyProfile(DOUBLE_COLUMN_BODY_SIZE, 1.5)
TypographyProfile.AFFILIATION = TypographyProfile(8.5, 1.35)
TypographyProfile.CAPTION = TypographyProfile(8.0, 1.3)
TypographyProfile.TABLE_CELL = TypographyProfile(8.0, 1.25)
TypographyProfile.FOOTNOTE = TypographyProfile(7.5, 1.3)


def title_profile(level: int) -> TypographyProfile:
    if level <= 1:
        return TypographyProfile.TITLE_1
    if level == 2:
        return TypographyProfile.TITLE_2
    return TypographyProfile.TITLE_3


def body_profile(columns: PageColumns) -> TypographyProfile:
    if columns.kind in {"double", "mixed"}:
        return TypographyProfile.BODY_DOUBLE
    return TypographyProfile.BODY_SINGLE


def role_profile(role: str, columns: PageColumns) -> TypographyProfile:
    """The template row for a block's semantic role."""
    if role == "affiliation":
        return TypographyProfile.AFFILIATION
    if role == "footnote":
        return TypographyProfile.FOOTNOTE
    if role == "keywords":
        return TypographyProfile.AFFILIATION
    return body_profile(columns)


@dataclass
class Fragment:
    """A piece of a translated block: text, an inline formula, or a break."""

    kind: str = "text"
    text: str = ""
    image_key: str = ""
    aspect: float = 1.0
    # Where the formula was cropped from, kept so a missing geometry can be
    # recovered and so the diagnostics can name the fallback that was used.
    source_bbox: Rect | None = None
    source_line_bbox: Rect | None = None
    baseline_ratio: float = 0.0
    fallback: str = ""


# How an inline formula's geometry was obtained (docs/translation-layout.md
# section 10). `exact_crop` uses the parser's box, `recovered_bbox` and
# `line_crop` bound it from the page, and `original_block` keeps the whole
# block in the source language.
FormulaFallback = str
FORMULA_FALLBACKS = ("exact_crop", "recovered_bbox", "line_crop", "original_block")


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
    serif: bool = True
    formula_fallback: str = ""
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
    serif: bool = True
    cell: object | None = None


@dataclass
class CaptionPlan:
    """A figure or table caption, drawn as movable text beside its artwork."""

    owner_kind: str
    source_rect: Rect
    target: Rect
    source_text: str
    translated: str
    size: float = 0.0
    baseline_size: float = 0.0
    leading: float = 0.0
    leading_ratio: float = 1.3
    align: str = "left"
    status: str = "translated"
    reason: str = ""
    owner: Block | None = None

    @property
    def fragments(self) -> list["Fragment"]:
        return [Fragment(kind="text", text=self.translated)]


@dataclass
class PagePlan:
    index: int
    width: float
    height: float
    blocks: list[BlockPlan] = field(default_factory=list)
    cells: list[CellPlan] = field(default_factory=list)
    captions: list[CaptionPlan] = field(default_factory=list)
    status: str = "ok"
    reason: str = ""
    leading: float = 0.0
    body_size: float = 0.0
    had_text_layer: bool = True
    columns: PageColumns | None = None

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
        rects.extend(
            caption.source_rect
            for caption in self.captions
            if caption.status == "translated"
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


def style_profile(
    role: str, columns: PageColumns, *, is_title: bool, level: int = 1
) -> TypographyProfile:
    """The fixed template row for a block, before any page-level shrinking."""
    if is_title:
        return title_profile(level)
    return role_profile(role, columns)


def block_style(
    frame: PageFrame,
    rect: Rect,
    *,
    is_title: bool = False,
    level: int = 1,
    role: str = "body",
    columns: PageColumns | None = None,
) -> tuple[float, bool, float, str, bool]:
    """(font size, bold, leading ratio, alignment, serif) for a block.

    Size, weight and leading come from the fixed template; only alignment is
    still measured from the source layout.
    """
    profile = style_profile(
        role, columns or PageColumns(), is_title=is_title, level=level
    )
    size = min(max(profile.size, ABS_MIN_SIZE), ABS_MAX_SIZE)
    return (
        size,
        profile.bold,
        profile.leading_ratio,
        _measure_alignment(frame, rect, is_title, level),
        profile.serif,
    )


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

    def __init__(
        self,
        fonts: CjkFontSet,
        image_paths=None,
        image_aspects=None,
        crop_formula_fn=None,
    ):
        self.fonts = fonts
        self.image_paths = image_paths if image_paths is not None else {}
        self.image_aspects = image_aspects if image_aspects is not None else {}
        self.crop_formula_fn = crop_formula_fn
        self._canvas = pdf_canvas.Canvas(io.BytesIO(), pagesize=(1, 1))

    def has_image(self, key: str) -> bool:
        return bool(key) and key in self.image_paths

    def crop_formula(self, key: str, page_index: int, bbox: Rect) -> str:
        """Crop a formula the parser gave no geometry for, on demand."""
        if self.crop_formula_fn is None:
            return ""
        path = self.crop_formula_fn(key, page_index, bbox)
        if path:
            self.image_paths[key] = path
        return path

    def style(
        self, size: float, leading: float, *, bold: bool, align: str, serif: bool = True
    ) -> ParagraphStyle:
        return ParagraphStyle(
            name="block",
            fontName=self.fonts.serif(bold) if serif else self.fonts.sans(bold),
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
        serif: bool = True,
    ) -> Paragraph:
        return Paragraph(
            self.markup(fragments, size) or " ",
            self.style(size, leading, bold=bold, align=align, serif=serif),
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
        serif: bool = True,
    ) -> float:
        if not fragments:
            return 0.0
        paragraph = self.paragraph(
            fragments, size, leading, bold=bold, align=align, serif=serif
        )
        try:
            _, height = paragraph.wrapOn(self._canvas, max(1.0, width), 100000)
        except Exception:
            return 100000.0
        return float(height)


def formula_key(page_index: int, bbox: Rect | None) -> str:
    if bbox is None:
        return ""
    return f"{page_index}:{bbox[0]:.1f}:{bbox[1]:.1f}:{bbox[2]:.1f}:{bbox[3]:.1f}"


def _formula_fragment(page_index: int, run: InlineMath) -> Fragment:
    if not run.bbox:
        fallback = ""
    else:
        fallback = getattr(run, "fallback", "") or "exact_crop"
    return Fragment(
        kind="formula",
        image_key=formula_key(page_index, run.bbox),
        source_bbox=run.bbox,
        fallback=fallback,
    )


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
                fragments.append(_formula_fragment(page_index, run))
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
                    fragments.append(_formula_fragment(page_index, run))
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


def _same_line(left: Rect, right: Rect) -> bool:
    """Whether two boxes sit on the same text line."""
    height = max(
        1.0, min(left[3] - left[1], right[3] - right[1])
    )
    return abs(((left[1] + left[3]) / 2) - ((right[1] + right[3]) / 2)) <= 0.6 * height


def _contains_foreign_text(frame: PageFrame, rect: Rect, slack: int = 0) -> bool:
    """Whether a candidate box swallows text other than the formula itself."""
    inside = [char for char in chars_in_rect(frame, rect) if char.char.strip()]
    return len(inside) > slack


def recover_inline_formula_box(
    frame: PageFrame, block: Block, run_index: int
) -> Rect | None:
    """Bound a geometry-less inline formula from the text around it.

    The parser sometimes reports inline math without a box. The formula sits
    between two text spans on one line, so the gap between the preceding
    character and the following one bounds it. A recovered box is accepted only
    when it lies inside the paragraph, sits on a single line and holds no other
    text.
    """
    runs = list(getattr(block, "runs", []) or [])
    if not (0 <= run_index < len(runs)) or not isinstance(runs[run_index], InlineMath):
        return None
    paragraph = getattr(block, "bbox", None)
    if not paragraph:
        return None

    previous = next(
        (
            run
            for run in reversed(runs[:run_index])
            if isinstance(run, TextRun) and run.text.strip()
        ),
        None,
    )
    following = next(
        (
            run
            for run in runs[run_index + 1 :]
            if isinstance(run, TextRun) and run.text.strip()
        ),
        None,
    )
    if previous is None or following is None:
        return None

    before_chars = _chars_for_run(frame, previous, paragraph)
    after_chars = _chars_for_run(frame, following, paragraph)
    if not before_chars or not after_chars:
        return None
    if not _same_line(before_chars[-1].rect, after_chars[0].rect):
        return None
    left = max(char.rect[2] for char in before_chars)
    right = min(char.rect[0] for char in after_chars)
    if right - left <= 0.5:
        return None
    top = max(char.rect[3] for char in before_chars + after_chars)
    bottom = min(char.rect[1] for char in before_chars + after_chars)
    if top - bottom <= 0.5:
        return None
    candidate = (left, bottom, right, top)
    if not _rect_within(paragraph, candidate, slack=1.0):
        return None
    # The gap must hold the formula and nothing else: the formula's own glyphs
    # are not in the page's text layer, so any character sitting in the gap
    # belongs to other prose and the box is not this formula's.
    known = {char.rect for char in before_chars + after_chars}
    inside = [
        char
        for char in chars_in_rect(frame, candidate)
        if char.char.strip() and char.rect not in known
    ]
    if inside:
        return None
    return candidate


def _in_reading_order(chars: list[SourceChar]) -> list[SourceChar]:
    """Characters ordered top-to-bottom, left-to-right.

    Glyph boxes start at different heights (a lowercase `o` and a capital `B`
    do not share a top edge), so lines are clustered by vertical centre first;
    sorting on the raw box top would interleave the words of one line.
    """
    if not chars:
        return []
    ordered = sorted(chars, key=lambda char: -((char.rect[1] + char.rect[3]) / 2))
    lines: list[list[SourceChar]] = []
    for char in ordered:
        centre = (char.rect[1] + char.rect[3]) / 2
        height = max(1.0, char.rect[3] - char.rect[1])
        if lines:
            last_centre = sum(
                (item.rect[1] + item.rect[3]) / 2 for item in lines[-1]
            ) / len(lines[-1])
            if abs(centre - last_centre) <= 0.6 * height:
                lines[-1].append(char)
                continue
        lines.append([char])
    result: list[SourceChar] = []
    for line in lines:
        result.extend(sorted(line, key=lambda char: char.rect[0]))
    return result


def _chars_for_run(
    frame: PageFrame, run: TextRun, paragraph: Rect
) -> list[SourceChar]:
    """The source characters the run's own words were drawn with.

    The run text locates its own characters inside the paragraph: the words are
    found in the page's own text layer in reading order, so the box that comes
    back belongs to this run and not to a lookalike elsewhere on the page.
    """
    if not any(char.strip() for char in run.text):
        return []
    candidates = [char for char in chars_in_rect(frame, paragraph) if char.char.strip()]
    candidates = _in_reading_order(candidates)
    needle = _normalize(run.text)
    if not needle:
        return []
    # Normalization drops spaces and punctuation, so each source character is
    # mapped to the slice of the normalized string it contributed.
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for char in candidates:
        piece = _normalize(char.char)
        spans.append((cursor, cursor + len(piece)))
        pieces.append(piece)
        cursor += len(piece)
    haystack = "".join(pieces)
    start = haystack.find(needle)
    if start < 0:
        return []
    end = start + len(needle)
    return [
        char
        for char, (begin, finish) in zip(candidates, spans)
        if finish > begin and finish > start and begin < end
    ]


def _rect_within(outer: Rect, inner: Rect, slack: float = 1.0) -> bool:
    return (
        inner[0] >= outer[0] - slack
        and inner[1] >= outer[1] - slack
        and inner[2] <= outer[2] + slack
        and inner[3] <= outer[3] + slack
    )


def source_line_box(frame: PageFrame, block: Block, bbox: Rect | None) -> Rect | None:
    """The smallest source line that contains a formula's known position."""
    paragraph = getattr(block, "bbox", None)
    if not paragraph:
        return None
    chars = [char for char in chars_in_rect(frame, paragraph) if char.char.strip()]
    if not chars:
        return None
    if bbox is not None:
        anchor = bbox
    else:
        anchor = None
    if anchor is not None:
        on_line = [char for char in chars if _same_line(char.rect, anchor)]
    else:
        on_line = chars
    if not on_line:
        return None
    return (
        min(char.rect[0] for char in on_line),
        min(char.rect[1] for char in on_line),
        max(char.rect[2] for char in on_line),
        max(char.rect[3] for char in on_line),
    )


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
            serif=plan.serif,
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


def _recovered_block_rect(frame: PageFrame, block: Block) -> Rect | None:
    """A block box rebuilt from the text the parser reported without geometry."""
    runs = list(getattr(block, "runs", []) or [])
    boxes: list[Rect] = []
    for index, run in enumerate(runs):
        if isinstance(run, InlineMath) and run.bbox:
            boxes.append(run.bbox)
            continue
        if isinstance(run, InlineMath):
            recovered = recover_inline_formula_box(frame, block, index)
            if recovered:
                boxes.append(recovered)
            continue
        if isinstance(run, TextRun) and run.text.strip():
            chars = _chars_for_run(frame, run, frame.rect)
            if chars:
                boxes.append(
                    (
                        min(char.rect[0] for char in chars),
                        min(char.rect[1] for char in chars),
                        max(char.rect[2] for char in chars),
                        max(char.rect[3] for char in chars),
                    )
                )
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def resolve_formula_fragments(
    frame: PageFrame,
    block: Block,
    plan: BlockPlan,
    measurer: TextMeasurer,
    indices: list[int],
) -> str | None:
    """Give every geometry-less inline formula a croppable box.

    Tried in order: the box recovered from the surrounding text spans, then the
    source line that holds the formula. Returns the fallback name that worked,
    or None when the formula cannot be located at all.
    """
    runs = list(getattr(block, "runs", []) or [])
    resolved = "recovered_bbox"
    for index in indices:
        fragment = plan.fragments[index]
        run_index = _run_index_for_fragment(plan.fragments, index)
        run = runs[run_index] if 0 <= run_index < len(runs) else None
        bbox = recover_inline_formula_box(frame, block, run_index)
        mode = "recovered_bbox"
        if bbox is None:
            bbox = source_line_box(
                frame, block, fragment.source_bbox or getattr(run, "bbox", None)
            )
            mode = "line_crop"
        if bbox is None:
            return None
        key = formula_key(frame.index, bbox)
        if not key:
            return None
        path = measurer.crop_formula(key, frame.index, bbox)
        if not path:
            return None
        fragment.image_key = key
        fragment.source_bbox = bbox
        fragment.fallback = mode
        fragment.baseline_ratio = _baseline_ratio(frame, bbox)
        if mode == "line_crop":
            resolved = "line_crop"
    return resolved


def _run_index_for_fragment(fragments: list[Fragment], fragment_index: int) -> int:
    """Which InlineMath run a fragment index corresponds to."""
    seen = 0
    for index, fragment in enumerate(fragments):
        if fragment.kind != "formula":
            continue
        if index == fragment_index:
            return seen
        seen += 1
    return -1


def _baseline_ratio(frame: PageFrame, bbox: Rect) -> float:
    """Where the formula's baseline sits inside its own box (0 at the bottom)."""
    chars = [char for char in chars_in_rect(frame, bbox) if char.char.strip()]
    if not chars:
        return 0.25
    height = max(0.5, bbox[3] - bbox[1])
    baseline = min(char.rect[1] for char in chars)
    return max(0.0, min(1.0, (baseline - bbox[1]) / height))


def plan_block(
    frame: PageFrame,
    block: Block,
    *,
    measurer: TextMeasurer,
    occupied: list[Rect],
    lost: list[Rect],
    columns: PageColumns | None = None,
) -> BlockPlan:
    source_rect = block.bbox or _recovered_block_rect(frame, block)
    if source_rect is None:
        # No geometry at all, so the block cannot be replaced safely.
        return BlockPlan(
            kind="paragraph",
            page_index=frame.index,
            source_rect=frame.rect,
            target=frame.rect,
            fragments=fragments_of(block),
            status="original",
            reason="block has no source geometry",
            formula_fallback="original_block",
            block=block,
        )
    is_title = isinstance(block, Title)
    size, bold, leading_ratio, align, serif = block_style(
        frame,
        source_rect,
        is_title=is_title,
        level=getattr(block, "level", 1),
        role=getattr(block, "role", "body"),
        columns=columns,
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
        serif=serif,
        block=block,
    )
    plan.formula_fallback = next(
        (
            fragment.fallback
            for fragment in plan.fragments
            if fragment.kind == "formula" and fragment.fallback
        ),
        "",
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
    unresolved = [
        index
        for index, fragment in enumerate(plan.fragments)
        if fragment.kind == "formula" and not measurer.has_image(fragment.image_key)
    ]
    if unresolved:
        fallback = resolve_formula_fragments(
            frame, block, plan, measurer, unresolved
        )
        if fallback is None:
            # A formula that cannot be located at all must not be erased by
            # masking its paragraph, so the whole block keeps its source.
            plan.status = "original"
            plan.reason = "inline formula could not be located"
            plan.formula_fallback = "original_block"
            return plan
        plan.formula_fallback = fallback

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


# A caption may grow into the whitespace below its own box, but never past the
# artwork or the next block it would otherwise cover.
CAPTION_MAX_GROWTH = 60.0


def _caption_target(
    frame: PageFrame, source_rect: Rect, occupied: list[Rect]
) -> Rect:
    """The caption's own box, extended only into the whitespace below it.

    The top stays at the caption's own top edge, so a caption drawn under an
    image can never grow up over the artwork.
    """
    top = source_rect[3]
    for region in occupied:
        if region == source_rect:
            continue
        if not overlaps_horizontally(source_rect, region):
            continue
        if region[1] >= source_rect[3] - 1.0:
            top = min(top, region[1])
    floor = max(frame.origin[1], source_rect[1] - CAPTION_MAX_GROWTH)
    for region in occupied:
        if region == source_rect:
            continue
        if not overlaps_horizontally(source_rect, region):
            continue
        if region[3] <= source_rect[1] + 1.0:
            floor = max(floor, region[3])
    return (source_rect[0], floor, source_rect[2], top)


def plan_caption(
    frame: PageFrame,
    block: Block,
    *,
    measurer: TextMeasurer,
    occupied: list[Rect],
) -> CaptionPlan | None:
    """Plan a translated caption beside its immutable owner.

    The caption starts at its own source box and may grow into the whitespace
    below it; it never moves the artwork, and it drops to the design's 6pt
    floor before giving up. A caption that cannot be located or cannot fit
    keeps its source wording in place.
    """
    if not isinstance(block, (Image, Table)):
        return None
    translated = (getattr(block, "translated_caption", "") or "").strip()
    if not translated:
        return None
    source_rect = getattr(block, "caption_bbox", None)
    if not source_rect:
        return None
    owner_kind = "figure" if isinstance(block, Image) else "table"
    profile = TypographyProfile.CAPTION
    owner_box = getattr(block, "bbox", None)
    obstacles = [region for region in occupied if region != source_rect]
    target = _caption_target(frame, source_rect, obstacles)
    fragments = [Fragment(kind="text", text=translated)]
    align = _measure_alignment(frame, source_rect, False, 1)
    width = max(1.0, target[2] - target[0])

    plan = CaptionPlan(
        owner_kind=owner_kind,
        source_rect=source_rect,
        target=target,
        source_text=getattr(block, "caption", "") or "",
        translated=translated,
        size=profile.size,
        baseline_size=profile.size,
        leading=profile.size * profile.leading_ratio,
        leading_ratio=profile.leading_ratio,
        align=align,
        owner=block,
    )

    def fits(size: float) -> bool:
        return (
            measurer.measure(
                fragments,
                size,
                size * profile.leading_ratio,
                bold=False,
                align=align,
                width=width,
                serif=True,
            )
            <= (target[3] - target[1]) + FIT_EPSILON
        )

    if fits(profile.size):
        plan.size = profile.size
        plan.leading = profile.size * profile.leading_ratio
        return plan
    low, high = ABS_MIN_SIZE, profile.size
    fitted = 0.0
    for _ in range(FIT_ITERATIONS):
        middle = (low + high) / 2
        if fits(middle):
            fitted = middle
            low = middle
        else:
            high = middle
        if high - low < 0.05:
            break
    if fitted <= 0.0:
        plan.status = "original"
        plan.reason = "caption translation does not fit at the minimum font size"
        plan.size = profile.size
        plan.leading = profile.size * profile.leading_ratio
        return plan
    plan.size = fitted
    plan.leading = fitted * profile.leading_ratio
    return plan


def plan_table_cells(
    frame: PageFrame, table: Table, *, measurer: TextMeasurer
) -> list[CellPlan]:
    plans: list[CellPlan] = []
    for cell in table.cells:
        if not cell.bbox or not cell.text.strip() or not cell.translated.strip():
            continue
        size = min(max(TypographyProfile.TABLE_CELL.size, ABS_MIN_SIZE), ABS_MAX_SIZE)
        bold = False
        leading_ratio = TypographyProfile.TABLE_CELL.leading_ratio
        _unused, _unused_bold, _unused_ratio, align, _serif = block_style(
            frame, cell.bbox, is_title=False, role="body"
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
                    serif=True,
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
            serif=True,
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
                serif=block.serif,
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
            serif=block.serif,
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
            serif=block.serif,
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

    columns = detect_page_columns(frame, blocks)
    plan.columns = columns

    # A block with no geometry still gets a plan so the page can report why its
    # content stayed in the source language.
    translatable = [
        block
        for block in blocks
        if isinstance(block, (Title, IRParagraph, ListBlock))
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
                columns=columns,
            )
        )
    for block in blocks:
        if isinstance(block, Table):
            plan.cells.extend(plan_table_cells(frame, block, measurer=measurer))
        caption = plan_caption(
            frame, block, measurer=measurer, occupied=occupied
        )
        if caption is not None:
            plan.captions.append(caption)

    _drop_duplicate_blocks(plan)
    if plan.translated_plans:
        unify_page(plan, measurer)
    if not plan.translated_plans and not any(
        cell.status == "translated" for cell in plan.cells
    ) and not any(caption.status == "translated" for caption in plan.captions):
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
