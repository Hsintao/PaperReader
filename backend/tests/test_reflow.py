"""Reflow fallback: failed pages are rebuilt fresh instead of staying English."""

import pypdf
import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import layout_model, layout_render
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_fit import PagePlan
from app.services.mineru_layout import Paragraph, TextRun


def _source(path, lines) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    y = 700
    for line in lines:
        canvas.drawString(72, y, line)
        y -= 14
    canvas.showPage()
    # A second, healthy page so the all-pages-failed budget does not trigger.
    canvas.drawString(72, 700, "Second page first line.")
    canvas.showPage()
    canvas.save()


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


def _failed_plan(frame) -> PagePlan:
    plan = PagePlan(index=frame.index, width=frame.width, height=frame.height)
    plan.status = "original"
    plan.reason = "rotated page"
    return plan


def _ok_plan(frame) -> PagePlan:
    from app.services.layout_fit import BlockPlan, Fragment

    plan = PagePlan(index=frame.index, width=frame.width, height=frame.height)
    plan.blocks.append(
        BlockPlan(
            kind="paragraph",
            page_index=frame.index,
            source_rect=(72.0, 692.0, 320.0, 706.0),
            target=(72.0, 692.0, 320.0, 706.0),
            fragments=[Fragment(kind="text", text="第二页译文。")],
            size=10.5,
            baseline_size=10.5,
            leading=15.0,
            source_text="Second page first line.",
        )
    )
    return plan


def _render(tmp_path, blocks, lines=("Body paragraph.",)):
    source = tmp_path / "source.pdf"
    _source(source, lines)
    frames = layout_model.measure_pages(source)
    plans = [_failed_plan(frames[0]), _ok_plan(frames[1])]
    crops = layout_render.prepare_formula_crops(source, frames, blocks, tmp_path / "c")
    try:
        output = tmp_path / "out.pdf"
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
    return output, report


def _translated_block(text: str, source: str) -> Paragraph:
    return Paragraph(
        runs=[TextRun(text=text)],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 706.0),
        source_text=source,
    )


def test_failed_page_is_reflowed_with_translated_content(tmp_path):
    block = _translated_block("这是排版失败后重排出来的译文。", "Body paragraph.")
    output, report = _render(tmp_path, [block])

    assert report.pages[0].status == "reflow"
    text = _page_text(output)
    assert "这是排版失败后重排出来的译文。" in text
    assert "Body paragraph." not in text


def test_page_without_translation_stays_original(tmp_path):
    block = _translated_block("Body paragraph.", "Body paragraph.")
    output, report = _render(tmp_path, [block])

    assert report.pages[0].status == "original"
    assert "Body paragraph." in _page_text(output)


def test_reflow_can_be_disabled(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "layout_reflow_fallback", False)
    block = _translated_block("译文。", "Body paragraph.")
    output, report = _render(tmp_path, [block])

    assert report.pages[0].status == "original"
    assert "Body paragraph." in _page_text(output)


def test_reflow_overflow_spills_onto_extra_pages(tmp_path):
    long_text = "这是一段用来撑版面的译文，" * 40
    blocks = [
        _translated_block(long_text, "Body paragraph."),
        *(
            _translated_block(f"第{index}段{long_text}", f"Extra {index}.")
            for index in range(6)
        ),
    ]
    output, report = _render(
        tmp_path,
        blocks,
        lines=["Body paragraph."] + [f"Extra {index}." for index in range(6)],
    )

    assert report.pages[0].status == "reflow"
    # One source page's content no longer fits one reflowed page.
    assert len(pypdf.PdfReader(str(output)).pages) > 2
    text = "".join(
        _page_text(output, index=index)
        for index in range(len(pypdf.PdfReader(str(output)).pages))
    )
    assert "第5段" in text


def test_reflowed_pages_do_not_count_as_failures(tmp_path):
    block = _translated_block("译文。", "Body paragraph.")
    _output, report = _render(tmp_path, [block])
    assert report.failed == []


def test_reflow_draws_preserved_subscripts_instead_of_their_tags(tmp_path):
    block = _translated_block(
        "本文提出了一种混合ℓ<sub>1</sub>-ℓ<sub>0</sub>分解模型。", "Body paragraph."
    )
    output, report = _render(tmp_path, [block])

    assert report.pages[0].status == "reflow"
    text = _page_text(output)
    assert "ℓ" in text
    assert "<sub>" not in text and "</sub>" not in text


def test_masked_overlay_keeps_bbox_anchor_when_removal_is_unclean(tmp_path, monkeypatch):
    """When content-stream surgery fails, the translation is drawn anchored at
    the source boxes over white masks, and the page count is unchanged."""
    source = tmp_path / "source.pdf"
    _source(source, ["First line of a shared object"])
    frames = layout_model.measure_pages(source)

    from app.services.layout_fit import BlockPlan, Fragment

    plan = PagePlan(index=0, width=frames[0].width, height=frames[0].height)
    plan.blocks.append(
        BlockPlan(
            kind="paragraph",
            page_index=0,
            source_rect=(72.0, 692.0, 320.0, 706.0),
            target=(72.0, 692.0, 320.0, 706.0),
            fragments=[Fragment(kind="text", text="共享对象第一行的译文。")],
            size=10.5,
            baseline_size=10.5,
            leading=15.0,
            source_text="First line of a shared object",
        )
    )

    def failed_attempt(*args, **kwargs):
        # Verification-triggered failure with the translated plan intact, as
        # happens when source text cannot be removed cleanly after retries.
        result = layout_render.PageResult(index=frames[0].index)
        result.status = "original"
        result.reason = "source text could not be removed cleanly"
        return result

    monkeypatch.setattr(layout_render, "_attempt_page", failed_attempt)

    crops = layout_render.prepare_formula_crops(source, frames, [], tmp_path / "c")
    try:
        output = tmp_path / "out.pdf"
        report = layout_render.render_document(
            source_pdf=source,
            plans=[plan, _ok_plan(frames[1])],
            frames=frames,
            blocks=[],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    assert report.pages[0].status == "masked"
    text = _page_text(output)
    assert "共享对象第一行的译文。" in text
    # No reflow spill: the page count matches the source.
    assert len(pypdf.PdfReader(str(output)).pages) == 2

    # The masked page renders the translation over the masked source line.
    bitmap = pdfium.PdfDocument(str(output))[0].render(scale=1.0)
    image = bitmap.to_pil().convert("L")
    assert image.getextrema()[0] < 128  # glyphs were actually drawn
    bitmap.close()
