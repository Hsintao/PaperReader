"""Fixed Chinese typography template and page column model."""

import pytest

from app.services import layout_fit
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_fit import (
    TypographyProfile,
    block_style,
    body_profile,
    title_profile,
)
from app.services.layout_model import PageFrame, SourceChar, detect_page_columns
from app.services.mineru_layout import Paragraph, TextRun, Title


def _frame(width=612.0, height=792.0, index=0) -> PageFrame:
    return PageFrame(index=index, width=width, height=height)


def _line(frame: PageFrame, text: str, x0: float, y: float, width: float = 4.0):
    for offset, char in enumerate(text):
        frame.chars.append(
            SourceChar(
                char=char,
                rect=(x0 + offset * width, y, x0 + (offset + 1) * width, y + 9.0),
                size=10.0,
            )
        )


def _body(x0: float, y0: float, x1: float, y1: float) -> Paragraph:
    return Paragraph(
        runs=[TextRun("Body text")],
        page_index=0,
        bbox=(x0, y0, x1, y1),
        source_text="Body text",
    )


def test_single_column_page_uses_the_single_column_body_template():
    frame = _frame()
    for row in range(12):
        _line(frame, "x" * 90, 72, 700 - row * 14)
    blocks = [_body(72, 520, 540, 700), _body(72, 300, 540, 500)]

    columns = detect_page_columns(frame, blocks)
    assert columns.kind == "single"
    assert len(columns.columns) == 1

    profile = body_profile(columns)
    assert profile.size == pytest.approx(10.5)
    assert profile.leading_ratio == pytest.approx(1.5)
    assert profile.serif is True


def test_double_column_page_uses_the_double_column_body_template():
    frame = _frame()
    for row in range(20):
        _line(frame, "x" * 40, 72, 700 - row * 14)
        _line(frame, "y" * 40, 320, 700 - row * 14)
    blocks = [
        _body(72, 500, 300, 700),
        _body(72, 280, 300, 490),
        _body(320, 500, 545, 700),
        _body(320, 280, 545, 490),
    ]

    columns = detect_page_columns(frame, blocks)
    assert columns.kind == "double"
    assert len(columns.columns) == 2
    left, right = sorted(columns.columns)
    assert left[0] == pytest.approx(72, abs=6)
    assert right[0] == pytest.approx(320, abs=6)

    profile = body_profile(columns)
    assert profile.size == pytest.approx(9.0)
    assert profile.leading_ratio == pytest.approx(1.5)


def test_full_width_title_does_not_create_a_third_column():
    frame = _frame()
    for row in range(20):
        _line(frame, "x" * 40, 72, 700 - row * 14)
        _line(frame, "y" * 40, 320, 700 - row * 14)
    blocks = [
        Title(level=1, text="A full width paper title", page_index=0, bbox=(72, 730, 545, 760)),
        _body(72, 500, 300, 700),
        _body(320, 500, 545, 700),
    ]

    columns = detect_page_columns(frame, blocks)
    assert columns.kind == "double"
    assert len(columns.columns) == 2


def test_title_levels_captions_and_footnotes_use_their_profile_sizes():
    frame = _frame()
    assert title_profile(1).size == pytest.approx(16.0)
    assert title_profile(2).size == pytest.approx(13.0)
    assert title_profile(3).size == pytest.approx(11.5)
    assert title_profile(9).size == pytest.approx(11.5)
    assert title_profile(1).serif is False
    assert title_profile(1).bold is True
    assert title_profile(3).bold is False

    assert TypographyProfile.CAPTION.size == pytest.approx(8.0)
    assert TypographyProfile.CAPTION.leading_ratio == pytest.approx(1.3)
    assert TypographyProfile.TABLE_CELL.size == pytest.approx(8.0)
    assert TypographyProfile.FOOTNOTE.size == pytest.approx(7.5)
    assert TypographyProfile.AFFILIATION.size == pytest.approx(8.5)

    columns = detect_page_columns(frame, [])
    single = body_profile(columns)

    size, bold, leading_ratio, align, serif = block_style(
        frame, (72, 700, 540, 720), role="body", columns=columns, level=1
    )
    assert size == pytest.approx(single.size)
    assert bold is False
    assert leading_ratio == pytest.approx(single.leading_ratio)
    assert serif is True

    size, bold, _, _, serif = block_style(
        frame, (72, 730, 540, 760), role="body", columns=columns, level=1, is_title=True
    )
    assert size == pytest.approx(16.0)
    assert bold is True
    assert serif is False


def test_leading_comes_from_the_template_not_the_source_line_pitch():
    """A source page set solid must not force the translation to the same pitch."""
    frame = _frame()
    # 8pt baselines on 10.5pt type: a ratio of 0.76, far below the template.
    for row in range(30):
        _line(frame, "x" * 90, 72, 700 - row * 8)
    columns = detect_page_columns(frame, [_body(72, 400, 540, 700)])
    _size, _bold, leading_ratio, _align, _serif = block_style(
        frame, (72, 400, 540, 700), role="body", columns=columns
    )
    assert leading_ratio == pytest.approx(1.5)


def test_short_translation_is_not_enlarged_to_fill_its_box():
    fonts = require_cjk_font()
    measurer = layout_fit.TextMeasurer(fonts)
    fragments = [layout_fit.Fragment(kind="text", text="短译文")]
    plan = layout_fit.BlockPlan(
        kind="paragraph",
        page_index=0,
        source_rect=(72, 600, 540, 700),
        target=(72, 400, 540, 700),
        fragments=fragments,
        baseline_size=10.5,
        size=10.5,
        leading=10.5 * 1.5,
        leading_ratio=1.5,
    )
    fitted = layout_fit.fit_size(measurer, plan, plan.target, leading_ratio=1.5)
    assert fitted == pytest.approx(10.5)
