"""Same-column text chains: the page is solved as a whole, not block by block."""

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import layout_fit, layout_model
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_fit import (
    Fragment,
    PagePlan,
    build_flow_chains,
    solve_page_layout,
)
from app.services.mineru_layout import DisplayMath, Paragraph, TextRun, Title


def _frame(width=612.0, height=792.0, index=0):
    return layout_model.PageFrame(index=index, width=width, height=height)


def _body(plan_index: int, box, text: str, *, size: float = 10.5):
    return layout_fit.BlockPlan(
        kind="paragraph",
        page_index=0,
        source_rect=box,
        target=box,
        fragments=[Fragment(kind="text", text=text)],
        size=size,
        baseline_size=size,
        leading=size * 1.5,
        leading_ratio=1.5,
        source_text=text,
    )


def _page_plan(frame, blocks) -> PagePlan:
    return PagePlan(
        index=frame.index,
        width=frame.width,
        height=frame.height,
        blocks=blocks,
        columns=layout_model.PageColumns(
            kind="single", columns=[frame.rect]
        ),
    )


def test_two_paragraphs_borrow_the_gap_between_them():
    frame = _frame()
    # The first paragraph's own box holds four lines but the translation needs
    # more; the gap before the next paragraph is what it borrows.
    first = _body(
        0,
        (72.0, 540.0, 540.0, 620.0),
        "第一段的中文译文比较长，需要借用下面段落之前的空白来排下整段文字。" * 8,
    )
    second = _body(1, (72.0, 400.0, 540.0, 420.0), "第二段。")
    plan = _page_plan(frame, [first, second])

    chains = build_flow_chains(frame, plan, plan.columns)
    assert len(chains) == 1
    assert [item.block_plan_index for item in chains[0].items] == [0, 1]

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    # The first paragraph keeps its own top, grows down into the gap and stops
    # above the second paragraph, which keeps its anchor.
    assert first.target[3] == first.source_rect[3]
    assert first.target[1] < first.source_rect[1]
    assert first.target[1] >= second.source_rect[3] - 1.0
    assert second.target[3] == second.source_rect[3]
    assert second.target[1] == pytest.approx(second.source_rect[1], abs=6.0)


def test_caption_pushes_the_following_body_down():
    frame = _frame()
    body = _body(0, (72.0, 300.0, 540.0, 320.0), "图注下面的正文段落。")
    plan = _page_plan(frame, [body])
    plan.captions.append(
        layout_fit.CaptionPlan(
            owner_kind="figure",
            source_rect=(72.0, 330.0, 300.0, 350.0),
            target=(72.0, 300.0, 300.0, 350.0),
            source_text="Figure 1.",
            translated="图 1。一段比较长的图注，需要占用更多垂直空间。" * 2,
            size=8.0,
            baseline_size=8.0,
            leading=8.0 * 1.3,
        )
    )
    chains = build_flow_chains(frame, plan, plan.columns)
    assert any(
        item.block_plan_index == -1 for chain in chains for item in chain.items
    )

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)
    assert body.target[3] <= plan.captions[0].target[1] + 1.0


def test_an_intervening_display_formula_stops_movement():
    frame = _frame()
    first = _body(
        0,
        (72.0, 600.0, 540.0, 620.0),
        "需要很多空间的第一段中文译文。" * 6,
    )
    formula = (72.0, 500.0, 300.0, 520.0)
    second = _body(1, (72.0, 400.0, 540.0, 420.0), "公式下面的第二段。")
    plan = _page_plan(frame, [first, second])
    plan.obstacles = [formula]

    chains = build_flow_chains(frame, plan, plan.columns)
    # The equation ends the chain: the second paragraph starts a new one.
    assert len(chains) == 2
    assert [item.block_plan_index for item in chains[0].items] == [0]
    assert [item.block_plan_index for item in chains[1].items] == [1]


def test_two_columns_move_independently():
    frame = _frame()
    left = _body(0, (72.0, 600.0, 290.0, 620.0), "左栏文字。" * 20)
    right = _body(1, (320.0, 600.0, 540.0, 620.0), "右栏文字。")
    plan = _page_plan(frame, [left, right])
    plan.columns = layout_model.PageColumns(
        kind="double", columns=[(72.0, 0.0, 290.0, 792.0), (320.0, 0.0, 540.0, 792.0)]
    )

    chains = build_flow_chains(frame, plan, plan.columns)
    assert len(chains) == 2
    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    # The right column's short paragraph keeps its own anchor.
    assert right.target[3] == right.source_rect[3]
    assert right.target[1] == pytest.approx(right.source_rect[1], abs=6.0)
    # The left column's long paragraph grows into its own whitespace only.
    assert left.target[0] == left.source_rect[0]
    assert left.target[2] == left.source_rect[2]


def test_a_full_width_heading_feeds_both_columns():
    frame = _frame()
    heading = layout_fit.BlockPlan(
        kind="title",
        page_index=0,
        source_rect=(72.0, 700.0, 540.0, 720.0),
        target=(72.0, 700.0, 540.0, 720.0),
        fragments=[Fragment(kind="text", text="1 引言")],
        size=11.5,
        baseline_size=11.5,
        leading=11.5 * 1.3,
        leading_ratio=1.3,
        bold=False,
        serif=True,
    )
    left = _body(1, (72.0, 600.0, 290.0, 620.0), "左栏。")
    right = _body(2, (320.0, 600.0, 540.0, 620.0), "右栏。")
    plan = _page_plan(frame, [heading, left, right])
    plan.columns = layout_model.PageColumns(
        kind="double", columns=[(72.0, 0.0, 290.0, 792.0), (320.0, 0.0, 540.0, 792.0)]
    )

    chains = build_flow_chains(frame, plan, plan.columns)
    # The full-width heading owns its own chain; each column gets one.
    assert len(chains) == 3
    assert [item.block_plan_index for item in chains[0].items] == [0]


def test_one_body_size_is_solved_for_the_whole_page():
    frame = _frame()
    dense = _body(
        0,
        (72.0, 600.0, 540.0, 620.0),
        "密集段落的中文译文，需要整页统一缩小才能放下。" * 12,
    )
    sparse = _body(1, (72.0, 200.0, 540.0, 220.0), "短段落。")
    plan = _page_plan(frame, [dense, sparse])

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    assert plan.body_size > 0
    assert dense.size == plan.body_size
    assert sparse.size == plan.body_size
    assert dense.size >= 6.0


def test_a_page_that_cannot_fit_at_six_points_falls_back_whole():
    frame = _frame()
    huge = _body(0, (72.0, 20.0, 540.0, 30.0), "无法放下的超长译文。" * 400)
    plan = _page_plan(frame, [huge])

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    assert plan.status == "original"
    assert "6" in plan.reason or "minimum" in plan.reason


def test_a_long_first_paragraph_does_not_push_the_rest_off_the_page():
    """A paragraph too long for its column reverts the page instead of
    printing its ink over the following blocks."""
    frame = _frame()
    blocks = [
        _body(0, (72.0, 600.0, 300.0, 730.0), "第一段译文。" * 80),
        _body(1, (72.0, 470.0, 300.0, 590.0), "第二段译文。"),
        _body(2, (72.0, 340.0, 300.0, 460.0), "第三段译文。"),
        _body(3, (72.0, 200.0, 300.0, 330.0), "第四段译文。"),
    ]
    plan = _page_plan(frame, blocks)
    measurer = layout_fit.TextMeasurer(require_cjk_font())
    fits = solve_page_layout(plan, measurer)

    assert not fits
    assert plan.status == "original"
    for block in blocks:
        assert block.target == block.source_rect


def test_a_translation_that_would_overlap_ink_shrinks_instead():
    """A paragraph that outgrows its gap at the template size must shrink the
    page, not print over the next block."""
    frame = _frame()
    first = _body(
        0,
        (72.0, 640.0, 300.0, 730.0),
        "第一段的中文译文比原文长出不少，需要借掉下方的空白。" * 6,
    )
    second = _body(1, (72.0, 560.0, 300.0, 630.0), "第二段译文。")
    plan = _page_plan(frame, blocks=[first, second])
    measurer = layout_fit.TextMeasurer(require_cjk_font())

    assert not solve_page_layout(plan, measurer, body_size=10.5, commit=False)
    assert solve_page_layout(plan, measurer, body_size=9.0)

    assert plan.status == "ok"
    assert first.target[3] == first.source_rect[3]
    assert first.target[1] > second.target[3]
    assert second.target[3] == second.source_rect[3]


def test_side_by_side_blocks_in_one_chain_do_not_count_as_overlap():
    """A row of figure labels shares a column lane while sitting side by
    side; only x-overlapping ink constrains the solve."""
    frame = _frame()
    blocks = [
        _body(0, (262.5, 660.5, 348.8, 670.0), "中等尺度增强"),
        _body(1, (167.7, 660.5, 247.9, 669.2), "粗尺度增强"),
        _body(2, (72.0, 400.0, 290.0, 420.0), "正文段落。"),
    ]
    plan = _page_plan(frame, blocks)
    plan.columns = layout_model.PageColumns(
        kind="double",
        columns=[(52.0, 0.0, 296.2, 792.0), (316.4, 0.0, 560.0, 792.0)],
    )
    measurer = layout_fit.TextMeasurer(require_cjk_font())

    chains = build_flow_chains(frame, plan, plan.columns)
    labels = [item for chain in chains for item in chain.items[:2]]
    assert len(chains) >= 1 and len(labels) >= 2

    assert solve_page_layout(plan, measurer)
    assert plan.status == "ok"
    for block in blocks:
        assert block.target[3] == block.source_rect[3]


def test_a_paragraph_may_grow_into_the_gap_without_dragging_the_column():
    frame = _frame()
    first = _body(0, (72.0, 600.0, 300.0, 730.0), "第一段译文。" * 30)
    second = _body(1, (72.0, 470.0, 300.0, 590.0), "第二段。")
    plan = _page_plan(frame, [first, second])
    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    assert plan.status == "ok"
    # The first paragraph hangs below its box; the second stays anchored.
    assert first.target[1] < first.source_rect[1]
    assert second.target[3] == second.source_rect[3]


def test_an_original_block_does_not_break_the_page_solve():
    """A block kept in the source language is not part of the movable flow."""
    frame = _frame()
    kept = _body(0, (72.0, 600.0, 540.0, 620.0), "原文保留。")
    kept.status = "original"
    moved = _body(1, (72.0, 400.0, 540.0, 420.0), "需要排版的译文。")
    plan = _page_plan(frame, [kept, moved])

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    assert plan.status == "ok"
    assert moved.target[3] == moved.source_rect[3]


def test_a_page_that_reverts_keeps_every_block_at_its_source_box():
    frame = _frame()
    huge = _body(0, (72.0, 20.0, 540.0, 30.0), "无法放下的超长译文。" * 400)
    plan = _page_plan(frame, [huge])

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    solve_page_layout(plan, measurer)

    assert plan.status == "original"
    assert huge.size == huge.baseline_size
    assert huge.target == huge.source_rect
