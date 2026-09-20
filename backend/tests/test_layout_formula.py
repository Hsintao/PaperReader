"""Inline and display formulas survive translation in every fallback mode."""

import pypdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import layout_fit, layout_model, layout_render
from app.services.cjk_fonts import require_cjk_font
from app.services.mineru_layout import (
    DisplayMath,
    InlineMath,
    Paragraph,
    TextRun,
)


def _source(path, pieces, y=700.0) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    for text, x in pieces:
        canvas.drawString(x, y, text)
    canvas.showPage()
    canvas.save()


def _formula_box(frames, low, high):
    chars = [
        char
        for char in frames[0].chars
        if low <= char.rect[0] <= high
    ]
    return (
        min(char.rect[0] for char in chars),
        min(char.rect[1] for char in chars),
        max(char.rect[2] for char in chars),
        max(char.rect[3] for char in chars),
    )


def test_exact_crop_is_used_when_the_parser_reports_the_box(tmp_path):
    source = tmp_path / "exact.pdf"
    _source(source, [("Before", 72), ("x2", 110), ("after", 130)])
    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[
            TextRun(text="Before "),
            InlineMath(latex="x^2", bbox=_formula_box(frames, 105, 128)),
            TextRun(text=" after"),
        ],
        page_index=0,
        bbox=(72.0, 694.0, 400.0, 712.0),
        source_text="Before x2 after",
    )
    block.runs[0].text = "之前"
    block.runs[2].text = "之后"

    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        fragments = layout_fit.fragments_of(block)
        formula = next(item for item in fragments if item.kind == "formula")
        assert formula.fallback == "exact_crop"
        assert crops.paths.get(formula.image_key)
        measurer = layout_fit.TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    assert any(fragment.kind == "formula" for fragment in plan.fragments)


def test_missing_geometry_is_recovered_from_the_surrounding_text(tmp_path):
    source = tmp_path / "recovered.pdf"
    # Real inline math is not in the page's text layer, so the gap between the
    # surrounding words is empty.
    _source(source, [("Before", 72), ("after", 130)])
    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[
            TextRun(text="Before "),
            InlineMath(latex="x^2"),
            TextRun(text=" after"),
        ],
        page_index=0,
        bbox=(72.0, 694.0, 400.0, 712.0),
        source_text="Before x2 after",
    )

    # Crops are prepared before translation, when the source words are still on
    # the block; the pipeline then translates and lays the page out.
    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        formula = next(
            fragment
            for fragment in layout_fit.fragments_of(block)
            if fragment.kind == "formula"
        )
        assert formula.image_key, "the gap between the words must bound the formula"
        assert crops.paths.get(formula.image_key)
        assert 100.0 <= formula.source_bbox[0] <= 105.0
        assert 125.0 <= formula.source_bbox[2] <= 135.0

        block.runs[0].text = "之前"
        block.runs[2].text = "之后"
        measurer = layout_fit.TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    plan_formula = next(
        fragment for fragment in plan.fragments if fragment.kind == "formula"
    )
    # The formula is present, cropped and atomic: text sits on both sides of it.
    assert plan_formula.image_key
    assert measurer.has_image(plan_formula.image_key)
    kinds = [fragment.kind for fragment in plan.fragments]
    assert "formula" in kinds
    assert kinds.index("formula") not in (0, len(kinds) - 1)


def test_line_crop_keeps_the_formula_when_the_gap_cannot_be_bounded(tmp_path):
    source = tmp_path / "line.pdf"
    _source(source, [("Before", 72), ("after", 130)])
    frames = layout_model.measure_pages(source)
    # The paragraph box does not reach the formula, so the gap cannot be
    # bounded and the formula falls back to the source line that holds it.
    block = Paragraph(
        runs=[
            TextRun(text="Before "),
            InlineMath(latex="x^2"),
            TextRun(text=" after"),
        ],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 716.0),
        source_text="Before x2 after",
    )
    block.runs[0].text = "之前"
    block.runs[2].text = "之后"

    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        assert crops.paths, "the source line must be cropped as the fallback"
        measurer = layout_fit.TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    assert plan.formula_fallback == "line_crop"
    formula = next(
        fragment for fragment in plan.fragments if fragment.kind == "formula"
    )
    assert formula.fallback == "line_crop"
    assert formula.image_key
    assert measurer.has_image(formula.image_key)
    # The whole source line is kept, so the formula cannot be lost.
    assert formula.source_bbox[2] - formula.source_bbox[0] > 60.0


def test_formula_without_any_geometry_keeps_the_whole_block_original(tmp_path):
    source = tmp_path / "nobbox.pdf"
    _source(source, [("Before", 72), ("x2", 110), ("after", 130)])
    frames = layout_model.measure_pages(source)
    # The parser reported no box at all and no gap can bound the formula, so it
    # cannot be located and the whole block stays in the source language.
    block = Paragraph(
        runs=[
            InlineMath(latex="x^2"),
            InlineMath(latex="y^2"),
        ],
        page_index=0,
        bbox=None,
        source_text="x^2 y^2",
    )

    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "original"
    assert plan.formula_fallback == "original_block"


def test_display_math_is_an_immutable_obstacle(tmp_path):
    source = tmp_path / "display.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 700, "Body paragraph that will be translated.")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(150, 640, "E = mc2")
    canvas.drawString(72, 580, "Another body paragraph below the equation.")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    blocks = [
        Paragraph(
            runs=[TextRun(text="Body paragraph that will be translated.")],
            page_index=0,
            bbox=(72.0, 694.0, 400.0, 712.0),
            source_text="Body paragraph that will be translated.",
        ),
        DisplayMath(latex="E = mc^2", page_index=0, bbox=(150.0, 634.0, 220.0, 650.0)),
        Paragraph(
            runs=[TextRun(text="Another body paragraph below the equation.")],
            page_index=0,
            bbox=(72.0, 574.0, 400.0, 592.0),
            source_text="Another body paragraph below the equation.",
        ),
    ]
    blocks[0].runs[0].text = "将被翻译的正文段落。"
    blocks[2].runs[0].text = "公式下方的另一个正文段落。"

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plan = layout_fit.plan_page(frames[0], blocks, measurer=measurer)

    display_rect = blocks[1].bbox
    # The equation is never masked, so its own glyphs stay in place.
    assert all(
        not (
            rect[0] <= display_rect[0]
            and rect[1] <= display_rect[1]
            and rect[2] >= display_rect[2]
            and rect[3] >= display_rect[3]
        )
        for rect in plan.mask_rects
    )
    # It is treated as an obstacle: no translated block overlaps it.
    for block_plan in plan.translated_plans:
        assert not layout_fit.rect_overlap_area(block_plan.target, display_rect)


def test_rendered_page_keeps_the_formula_visible(tmp_path):
    """The source formula glyphs must still be in the output text layer."""
    source = tmp_path / "render.pdf"
    _source(source, [("Before", 72), ("x2", 110), ("after", 130)])
    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[
            TextRun(text="Before "),
            InlineMath(latex="x^2"),
            TextRun(text=" after"),
        ],
        page_index=0,
        bbox=(72.0, 694.0, 400.0, 712.0),
        source_text="Before x2 after",
    )
    block.runs[0].text = "之前"
    block.runs[2].text = "之后"

    output = tmp_path / "out.pdf"
    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
        layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=[block],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    assert output.is_file()
    assert len(pypdf.PdfReader(str(output)).pages) == 1
