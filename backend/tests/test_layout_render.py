"""Layout-renderer coverage: measurement, fit, composition and degradation."""

import io

import pypdf
import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import layout_fit, layout_model, layout_render
from app.services.cjk_fonts import find_cjk_font, require_cjk_font
from app.services.layout_fit import BlockPlan, Fragment, PagePlan
from app.services.mineru_layout import (
    Paragraph,
    Table,
    TableCell,
    TextRun,
    Title,
    blocks_to_ir,
    collect_translatable_strings,
    apply_translations,
)

pytestmark = pytest.mark.skipif(
    find_cjk_font() is None, reason="requires an installed CJK font"
)


def _source(path, lines, *, caption=None, footer=None, title=None) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    if footer:
        canvas.setFont("Helvetica", 8)
        canvas.drawString(72, 40, footer)
    y = 700
    if title:
        canvas.setFont("Helvetica-Bold", 16)
        canvas.drawString(72, y, title)
        y -= 40
    canvas.setFont("Helvetica", 10)
    for line in lines:
        canvas.drawString(72, y, line)
        y -= 12
    if caption:
        canvas.setFont("Helvetica", 8)
        canvas.drawString(72, 460, caption)
    canvas.showPage()
    canvas.save()


def _plans(source, translations=None, *, blocks_override=None, frames=None):
    frames = frames or layout_model.measure_pages(source)
    if blocks_override is not None:
        blocks = blocks_override
    else:
        pages = layout_model.parse_local_pages(source)
        blocks = blocks_to_ir(
            [page.blocks for page in pages],
            [(page.width, page.height) for page in pages],
            normalized_boxes=False,
        )
    segments = collect_translatable_strings(blocks)
    apply_translations(
        blocks,
        translations
        if translations is not None
        else [f"【译文】第{index + 1}段。" for index in range(len(segments))],
    )
    crops = layout_render.prepare_formula_crops(
        source, frames, blocks, source.parent / "crops"
    )
    measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths)
    plans = layout_fit.plan_document(frames, blocks, measurer=measurer)
    return blocks, frames, plans, crops, measurer


def _page_text(path, index=0) -> str:
    document = pdfium.PdfDocument(str(path))
    try:
        textpage = document[index].get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            textpage.close()
    finally:
        document.close()


def test_rendered_page_keeps_size_and_replaces_only_translated_text(tmp_path):
    source = tmp_path / "source.pdf"
    _source(
        source,
        ["The first body paragraph explains the method.", "It continues on a second line."],
        caption="Figure 1. A caption that stays in English.",
        footer="Page 3",
        title="A Study of Layout Engines",
    )
    blocks, frames, plans, crops, _measurer = _plans(source)
    try:
        output = tmp_path / "translated.pdf"
        report = layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=blocks,
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    assert [page.status for page in report.pages] == ["ok"]
    reader = pypdf.PdfReader(str(output))
    assert len(reader.pages) == len(pypdf.PdfReader(str(source)).pages)
    assert reader.pages[0].mediabox.width == pytest.approx(612.0)
    assert reader.pages[0].mediabox.height == pytest.approx(792.0)

    text = _page_text(output)
    assert "The first body paragraph" not in text
    assert "A Study of Layout Engines" not in text
    assert "【译文】" in text
    # Captions and running feet are reused verbatim, never translated.
    assert "Figure 1. A caption that stays in English." in text
    assert "Page 3" in text


def test_translation_uses_whitespace_below_before_shrinking(tmp_path):
    source = tmp_path / "roomy.pdf"
    _source(source, ["Short source line."], caption="Figure 1. Caption.", footer="1")
    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[TextRun(text="Short source line.")],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 704.0),
        source_text="Short source line.",
    )
    block.runs[0].text = (
        "这是一段明显比原文更长的译文，用来检验排版是否优先占用下方的留白，"
        "而不是立刻缩小字号。留白足够时应当在默认字号附近排版。"
    )
    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    # Roomy pages may grow slightly past the default size, within the ceiling.
    assert plan.size >= plan.baseline_size
    assert plan.size <= layout_fit.SIZE_CEIL_RATIO * plan.baseline_size + 0.05
    assert plan.target[1] < plan.source_rect[1]  # the box grew downwards


def test_block_shrinks_in_place_when_space_is_bounded(tmp_path):
    source = tmp_path / "tight.pdf"
    _source(source, ["Line one of a bounded paragraph that must fit."], footer="1")
    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[TextRun(text="Line one of a bounded paragraph that must fit.")],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 704.0),
        source_text="Line one of a bounded paragraph that must fit.",
    )
    block.runs[0].text = (
        "这段译文很长，既放不下也无处可去，因此只能在自身区域内缩小字号才放得下。"
    )
    # A neighbouring block right below blocks the whitespace below this one.
    neighbour = Paragraph(
        runs=[TextRun(text="Neighbour block below.")],
        page_index=0,
        bbox=(72.0, 600.0, 400.0, 680.0),
        source_text="Neighbour block below.",
    )
    neighbour.runs[0].text = "下方相邻块。"
    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [block, neighbour], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    assert plan.size < plan.baseline_size
    assert plan.size >= layout_fit.MIN_SIZE_RATIO * plan.baseline_size
    assert plan.target[1] <= 680.5


def test_missing_inline_formula_geometry_drops_only_the_formula(tmp_path):
    source = tmp_path / "formula.pdf"
    _source(source, ["Inline math paragraph."], footer="1")
    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[TextRun(text="Inline math "), __import__("app.services.mineru_layout", fromlist=["InlineMath"]).InlineMath(latex="x^2")],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 704.0),
        source_text="Inline math x^2",
    )
    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    # The formula has no geometry to lift it out of the page, so it is left out
    # and the surrounding prose is rebuilt from the translation.
    assert plan.status == "translated"
    assert all(fragment.kind != "formula" for fragment in plan.fragments)
    assert any(fragment.kind == "text" for fragment in plan.fragments)


def test_inline_formula_is_cropped_from_the_source_page(tmp_path):
    source = tmp_path / "inline.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 700, "Before")
    canvas.drawString(110, 700, "x2")
    canvas.drawString(130, 700, "after")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    math_chars = [char for char in frames[0].chars if char.rect[0] >= 105 and char.rect[0] <= 128]
    bbox = (
        min(char.rect[0] for char in math_chars),
        min(char.rect[1] for char in math_chars),
        max(char.rect[2] for char in math_chars),
        max(char.rect[3] for char in math_chars),
    )
    from app.services.mineru_layout import InlineMath

    block = Paragraph(
        runs=[TextRun(text="Before "), InlineMath(latex="x^2", bbox=bbox), TextRun(text=" after")],
        page_index=0,
        bbox=(72.0, 694.0, 400.0, 712.0),
        source_text="Before x2 after",
    )
    block.runs[0].text = "之前"
    block.runs[2].text = "之后"

    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "crops")
    try:
        fragment = layout_fit.fragments_of(block)[1]
        assert fragment.kind == "formula"
        assert fragment.image_key
        assert crops.paths.get(fragment.image_key), "the formula must be cropped"
        assert crops.aspects.get(fragment.image_key, 0) > 0
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()
    assert plans[0].blocks[0].status == "translated"


def test_untranslated_blocks_stay_in_the_source_language(tmp_path):
    source = tmp_path / "references.pdf"
    _source(source, ["[1] A reference entry that stays English."], footer="1")
    frames = layout_model.measure_pages(source)
    pages = layout_model.parse_local_pages(source)
    blocks = blocks_to_ir(
        [page.blocks for page in pages],
        [(page.width, page.height) for page in pages],
        normalized_boxes=False,
    )
    # Translation returns the source text unchanged, as the bibliography mask does.
    apply_translations(blocks, collect_translatable_strings(blocks))
    crops = layout_render.prepare_formula_crops(source, frames, blocks, tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, blocks, measurer=measurer)
    finally:
        crops.close()

    assert plans[0].blocks == []
    assert plans[0].status == "original"


def test_table_cells_are_replaced_and_rules_survive(tmp_path):
    source = tmp_path / "table.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.line(72, 500, 400, 500)
    canvas.line(72, 460, 400, 460)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(80, 470, "Method")
    canvas.drawString(220, 470, "Score")
    canvas.drawString(80, 440, "Ours")
    canvas.drawString(220, 440, "0.91")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    table = Table(
        rel_path="",
        caption="Table 1. Results.",
        page_index=0,
        bbox=(72.0, 430.0, 400.0, 505.0),
        cells=[
            TableCell(text="Method", bbox=(80.0, 466.0, 130.0, 478.0), translated="方法"),
            TableCell(text="Score", bbox=(220.0, 466.0, 260.0, 478.0), translated="分数"),
            TableCell(text="Ours", bbox=(80.0, 436.0, 110.0, 448.0), translated="本文"),
            TableCell(text="0.91", bbox=(220.0, 436.0, 250.0, 448.0), translated="0.91"),
        ],
    )
    crops = layout_render.prepare_formula_crops(source, frames, [table], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [table], measurer=measurer)
        output = tmp_path / "table-out.pdf"
        layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=[table],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    translated = [cell for cell in plans[0].cells if cell.status == "translated"]
    assert translated
    text = _page_text(tmp_path / "table-out.pdf")
    assert "方法" in text and "分数" in text
    assert "Method" not in text and "Ours" not in text
    # Untranslatable cell content keeps its original glyphs.
    assert "0.91" in text

    bitmap = pdfium.PdfDocument(str(tmp_path / "table-out.pdf"))[0].render(scale=1.0)
    image = bitmap.to_pil().convert("RGB")
    # The table rules are still drawn (a dark pixel on the top rule).
    dark = [
        image.getpixel((x, y))
        for y in range(round(792 - 501), round(792 - 499))
        for x in range(80, 120)
        if image.getpixel((x, y))[0] < 128
    ]
    assert dark
    bitmap.close()


def test_all_failed_pages_raise_instead_of_publishing_a_half_product(tmp_path):
    source = tmp_path / "broken.pdf"
    _source(source, ["Body paragraph."], footer="1")
    frames = layout_model.measure_pages(source)
    plan = PagePlan(index=0, width=frames[0].width, height=frames[0].height)
    plan.status = "original"
    plan.reason = "layout failure"
    crops = layout_render.prepare_formula_crops(source, frames, [], tmp_path / "c")
    try:
        with pytest.raises(layout_render.LayoutRenderError):
            layout_render.render_document(
                source_pdf=source,
                plans=[plan],
                frames=frames,
                blocks=[],
                output_pdf=tmp_path / "never.pdf",
                crops=crops,
            )
    finally:
        crops.close()
    assert not (tmp_path / "never.pdf").exists()


def test_debug_pdf_marks_block_categories(tmp_path):
    source = tmp_path / "debug.pdf"
    _source(source, ["Body paragraph for debug."], caption="Figure 1. Caption.", footer="1")
    blocks, frames, plans, crops, _measurer = _plans(source)
    try:
        output = tmp_path / "translated.pdf"
        debug = tmp_path / "layout-debug.pdf"
        layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=blocks,
            output_pdf=output,
            crops=crops,
            debug_pdf=debug,
        )
    finally:
        crops.close()

    assert debug.is_file()
    text = _page_text(debug)
    assert "paragraph" in text
    assert "title" in text


def test_dropped_region_becomes_a_continuation_target(tmp_path):
    source = tmp_path / "dropped.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 700, "Claimed block text.")
    # Text the parser never claimed: this paragraph's tail, dropped at the
    # bottom of the column.
    canvas.drawString(72, 660, "Unclaimed tail text that the parser lost entirely.")
    canvas.showPage()
    canvas.save()

    frames = layout_model.measure_pages(source)
    block = Paragraph(
        runs=[TextRun(text="Claimed block text. Unclaimed tail text that the parser lost entirely.")],
        page_index=0,
        bbox=(72.0, 690.0, 300.0, 706.0),
        source_text="Claimed block text. Unclaimed tail text that the parser lost entirely.",
    )
    block.runs[0].text = (
        "这是被声明块内的正文，后面还有一段被解析器丢掉、需要续排到下一个区域的文字。"
        "这段译文刻意写得比原稿更长，只有在丢失区域里续排才放得下。"
    )
    crops = layout_render.prepare_formula_crops(source, frames, [block], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [block], measurer=measurer)
    finally:
        crops.close()

    plan = plans[0].blocks[0]
    assert plan.status == "translated"
    assert plan.continuation is not None
    assert plan.target[1] <= plan.continuation[3] + 0.5
    # The dropped tail's text is removed together with the block.
    assert plan.source_rect[1] <= plan.continuation[1] + 0.5


def test_over_removal_reverts_one_block_and_keeps_the_other_pages(tmp_path):
    """One over-broad text run costs its own block, not the whole document."""
    source = tmp_path / "shared-object.pdf"
    _write_pdf_with_shared_object_page(source, extra_pages=4)

    frames = layout_model.measure_pages(source)
    # Page 1 draws its whole line in one operator while the parsed box covers
    # only its first part, so removing the operator would eat the rest of the
    # line. The remaining pages are ordinary per-line text objects.
    plans = [_plan_with_translated_block(frames[0], width=140.0)]
    plans += [_plan_with_translated_block(frame) for frame in frames[1:]]
    crops = layout_render.prepare_formula_crops(source, frames, [], tmp_path / "c")
    try:
        output = tmp_path / "out.pdf"
        report = layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=[],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    assert report.pages[0].status == "original"
    assert report.pages[0].reverted == 1
    assert "reverted" in report.pages[0].reason
    assert all(page.status == "ok" for page in report.pages[1:])

    document = pdfium.PdfDocument(str(output))
    try:
        first = document[0].get_textpage()
        try:
            text = first.get_text_range()
        finally:
            first.close()
        # The reverted page keeps its source wording untouched.
        assert "First line of a shared text object" in text
        assert "Second line of the same object" in text
        # The remaining pages are translated normally.
        second = document[1].get_textpage()
        try:
            text = second.get_text_range()
        finally:
            second.close()
        assert "被替换的一行文字。" in text
        assert "Page 2 first line of the paragraph." not in text
    finally:
        document.close()


def _plan_with_translated_block(frame, width: float = 320.0) -> PagePlan:
    plan = PagePlan(index=frame.index, width=frame.width, height=frame.height)
    plan.blocks.append(
        BlockPlan(
            kind="paragraph",
            page_index=frame.index,
            source_rect=(72.0, 692.0, width, 706.0),
            target=(72.0, 692.0, width, 706.0),
            fragments=[Fragment(kind="text", text="被替换的一行文字。")],
            size=10.0,
            baseline_size=10.0,
            leading=15.0,
            source_text="First line of a shared text object",
        )
    )
    return plan


def _write_pdf_with_shared_object_page(path, extra_pages: int = 4) -> None:
    """Page 1 draws two lines in one text object; later pages draw one each."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    buffer = io.BytesIO()
    canvas = pdf_canvas.Canvas(buffer, pagesize=letter)
    for index in range(extra_pages + 1):
        canvas.setFont("Helvetica", 10)
        canvas.drawString(72, 700, f"Page {index + 1} first line of the paragraph.")
        canvas.drawString(72, 688, "Second line that must survive removal.")
        canvas.showPage()
    canvas.save()

    reader = PdfReader(io.BytesIO(buffer.getvalue()))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 10 Tf 72 700 Td (First line of a shared text object) Tj"
        b" 0 -12 Td (Second line of the same object) Tj ET\n"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    for index in range(1, extra_pages + 1):
        writer.add_page(reader.pages[index])
    with path.open("wb") as handle:
        writer.write(handle)
