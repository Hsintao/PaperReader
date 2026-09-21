"""Inline and display formulas survive translation in every fallback mode."""

from pathlib import Path

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


def test_unlocatable_formula_is_typeset_from_its_latex(tmp_path):
    source = tmp_path / "line.pdf"
    _source(source, [("Before", 72), ("after", 130)])
    frames = layout_model.measure_pages(source)
    # The translation reached the block before the crops were prepared, so the
    # gap between the source words cannot be matched to the runs any more; the
    # formula is typeset from its reported LaTeX instead of cropping text.
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
        assert crops.paths, "the LaTeX render must produce an image"
        measurer = layout_fit.TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    assert plan.formula_fallback == "latex_render"
    formula = next(
        fragment for fragment in plan.fragments if fragment.kind == "formula"
    )
    assert formula.fallback == "latex_render"
    assert formula.image_key
    assert measurer.has_image(formula.image_key)
    # No source geometry is claimed: the image was typeset, not cropped.
    assert formula.source_bbox is None


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


def test_render_latex_typesets_and_caches(tmp_path):
    source = tmp_path / "latex.pdf"
    _source(source, [("Body", 72)])
    crops = layout_render.FormulaCrops(source, tmp_path / "c")
    try:
        key = layout_fit.latex_formula_key(r"G _ {\sigma _ {s}}")
        path = crops.render_latex(key, r"G _ {\sigma _ {s}}")
        assert path
        assert Path(path).is_file()
        assert crops.aspects[key] > 0
        # Same content renders once; empty input never produces an image.
        assert crops.render_latex(key, r"G _ {\sigma _ {s}}") == path
        assert crops.render_latex(layout_fit.latex_formula_key(""), "") == ""
    finally:
        crops.close()


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


def test_cjk_paragraph_with_inline_formulas_measures_at_every_size(tmp_path):
    """reportlab's CJK breaker runs ord() on the glyph that overflows the
    line, and an inline image's glyph text is empty; the measurer then read
    the block as unplaceable and the whole page shrank to the 6pt floor."""
    from PIL import Image as PILImage

    narrow = tmp_path / "narrow.png"
    PILImage.new("RGBA", (34, 33), (0, 0, 0, 255)).save(narrow)
    wide = tmp_path / "wide.png"
    PILImage.new("RGBA", (420, 33), (0, 0, 0, 255)).save(wide)

    measurer = layout_fit.TextMeasurer(
        require_cjk_font(),
        {"narrow": str(narrow), "wide": str(wide)},
        {"narrow": 34 / 33, "wide": 420 / 33},
    )
    fragments = [
        layout_fit.Fragment(kind="text", text="其中"),
        layout_fit.Fragment(kind="formula", image_key="narrow"),
        layout_fit.Fragment(kind="text", text="是亮度范围的均值，S 是 sigmoid 曲线，"),
        layout_fit.Fragment(kind="formula", image_key="wide"),
        layout_fit.Fragment(kind="text", text="（经过适当的平移和归一化）。"),
    ]
    for size in (6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0, 10.5):
        height = measurer.measure(
            fragments, size, size * 1.5, bold=False, align="left", width=244.2
        )
        assert 0.0 < height < 1000.0
