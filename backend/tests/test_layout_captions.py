"""Figure and table captions are translated and pinned next to their artwork."""

import pypdf
import pypdfium2 as pdfium
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import layout_fit, layout_model, layout_render
from app.services.cjk_fonts import require_cjk_font
from app.services.mineru_layout import (
    Image,
    Paragraph,
    Table,
    TableCell,
    TextRun,
)


def _figure_page(path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 700, "Body paragraph above the figure.")
    # Artwork occupies the middle of the page.
    canvas.setFillColorRGB(0.8, 0.8, 0.8)
    canvas.rect(72, 420, 300, 200, stroke=0, fill=1)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 400, "Figure 1. A caption under the artwork.")
    canvas.drawString(72, 388, "Second caption line with more detail.")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 340, "Body paragraph below the caption.")
    canvas.showPage()
    canvas.save()


def _figure_block(frames) -> Image:
    return Image(
        rel_path="images/fig1.png",
        caption="Figure 1. A caption under the artwork. Second caption line with more detail.",
        page_index=0,
        bbox=(72.0, 420.0, 372.0, 620.0),
    )


def test_figure_caption_becomes_a_caption_plan_next_to_the_artwork(tmp_path):
    source = tmp_path / "figure.pdf"
    _figure_page(source)
    frames = layout_model.measure_pages(source)
    image = _figure_block(frames)
    # The caption's box comes from the page's own text layer.
    assert layout_model.attach_caption_boxes([image], frames) == 1
    assert image.caption_bbox is not None

    image.translated_caption = "图 1。图片下方的图注。图注的第二行包含更多细节。"

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plan = layout_fit.plan_page(frames[0], [image], measurer=measurer)

    captions = [caption for caption in plan.captions if caption.status == "translated"]
    assert len(captions) == 1
    caption = captions[0]
    assert caption.owner_kind == "figure"
    assert caption.translated.startswith("图 1")
    assert caption.size == 8.0
    assert caption.leading_ratio == 1.3
    # The caption starts at its own source box and never covers the artwork.
    assert caption.target[3] <= image.caption_bbox[3] + 1.0
    assert not layout_fit.rect_overlap_area(caption.target, image.bbox)


def test_table_caption_and_notes_are_translated(tmp_path):
    source = tmp_path / "table.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 700, "Table 1. Results for every method.")
    canvas.drawString(72, 688, "Note: all values are means over five runs.")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 660, "Body paragraph under the table notes.")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    table = Table(
        rel_path="images/t1.png",
        caption="Table 1. Results for every method. Note: all values are means over five runs.",
        page_index=0,
        bbox=(72.0, 600.0, 400.0, 680.0),
    )
    layout_model.attach_caption_boxes([table], frames)
    assert table.caption_bbox is not None
    table.translated_caption = "表 1。所有方法的结果。注：所有数值为五次运行的均值。"

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plan = layout_fit.plan_page(frames[0], [table], measurer=measurer)

    captions = [caption for caption in plan.captions if caption.status == "translated"]
    assert len(captions) == 1
    assert captions[0].owner_kind == "table"
    assert captions[0].translated.startswith("表 1")
    assert not layout_fit.rect_overlap_area(captions[0].target, table.bbox)


def test_wrapped_caption_and_subfigure_markers_survive(tmp_path):
    source = tmp_path / "wrapped.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 400, "Figure 4: Complete statistics. (a) Left panel shows the")
    canvas.drawString(72, 388, "baseline. (b) Right panel shows our method.")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 350, "Body paragraph after the wrapped caption.")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    image = Image(
        rel_path="images/fig4.png",
        caption=(
            "Figure 4: Complete statistics. (a) Left panel shows the baseline. "
            "(b) Right panel shows our method."
        ),
        page_index=0,
        bbox=(72.0, 410.0, 400.0, 600.0),
    )
    layout_model.attach_caption_boxes([image], frames)
    assert image.caption_bbox is not None
    image.translated_caption = (
        "图 4：完整统计。(a) 左图展示基线。(b) 右图展示本文方法。"
    )

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plan = layout_fit.plan_page(frames[0], [image], measurer=measurer)
    caption = plan.captions[0]
    assert caption.status == "translated"
    assert "(a)" in caption.translated and "(b)" in caption.translated
    # The source caption's two lines bound the target box.
    assert caption.target[3] - caption.target[1] > 10.0


def test_caption_without_geometry_stays_in_the_source_language(tmp_path):
    source = tmp_path / "unlocated.pdf"
    _figure_page(source)
    frames = layout_model.measure_pages(source)
    image = Image(
        rel_path="images/fig1.png",
        caption="A caption that is not drawn anywhere on this page.",
        page_index=0,
        bbox=(72.0, 420.0, 372.0, 620.0),
    )
    image.translated_caption = "一个没有定位的图注。"

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plan = layout_fit.plan_page(frames[0], [image], measurer=measurer)

    assert plan.captions == []


def test_table_cells_use_the_cell_template_and_revert_only_that_cell(tmp_path):
    source = tmp_path / "cells.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 700, "Body paragraph above the table.")
    canvas.setFont("Helvetica", 8)
    canvas.drawString(80, 600, "Method")
    canvas.drawString(240, 600, "Score")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 560, "Body paragraph below the table.")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    table = Table(
        rel_path="images/t2.png",
        page_index=0,
        bbox=(72.0, 560.0, 400.0, 620.0),
        cells=[
            TableCell(text="Method", bbox=(80.0, 592.0, 160.0, 608.0), translated="方法"),
            TableCell(
                text="Score",
                bbox=(240.0, 592.0, 300.0, 608.0),
                translated="分数" * 60,
            ),
        ],
    )
    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plan = layout_fit.plan_page(frames[0], [table], measurer=measurer)

    assert len(plan.cells) == 2
    good, bad = plan.cells
    assert good.status == "translated"
    assert good.baseline_size == 8.0
    assert good.size == 8.0
    assert bad.status == "original"
    assert "fit" in bad.reason
    # Only the failing cell keeps its English text.
    assert [cell.status for cell in plan.cells] == ["translated", "original"]


def test_rendered_captions_and_cells_are_searchable(tmp_path):
    source = tmp_path / "render.pdf"
    _figure_page(source)
    frames = layout_model.measure_pages(source)
    image = _figure_block(frames)
    layout_model.attach_caption_boxes([image], frames)
    image.translated_caption = "图 1。图片下方的图注。"

    output = tmp_path / "out.pdf"
    crops = layout_render.FormulaCrops(source, tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [image], measurer=measurer)
        layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=[image],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    assert output.is_file()
    document = pdfium.PdfDocument(str(output))
    try:
        text = document[0].get_textpage().get_text_range()
    finally:
        document.close()
    assert "图1。" in text.replace(" ", "").replace("\r", "").replace("\n", "")
    # The caption is real text, not a raster crop.
    assert len(pypdf.PdfReader(str(output)).pages) == 1
