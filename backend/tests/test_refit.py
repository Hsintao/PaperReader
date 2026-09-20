"""Concise-translation refit: blocks that did not fit get a shorter translation."""

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import document_pipeline, layout_fit, layout_model, layout_render
from app.services.cjk_fonts import require_cjk_font
from app.services.mineru_layout import InlineMath, Paragraph, Table, TableCell, TextRun

_LONG_TRANSLATION = (
    "这段译文非常非常长，既放不下也无处可去，因此只能在自身区域内缩小字号，"
    "但即使缩到下限也还是放不下，于是整块只能保留原文，等待简洁重试来救它。"
    "再补充一些内容让它确实超过盒子的容量下限。"
)


def _tight_source(path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 40, "1")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 700, "A bounded paragraph that must fit in place.")
    canvas.drawString(72, 620, "Neighbour block below it.")
    canvas.showPage()
    canvas.save()


def _plan(tmp_path, blocks):
    source = tmp_path / "source.pdf"
    _tight_source(source)
    frames = layout_model.measure_pages(source)
    crops = layout_render.prepare_formula_crops(source, frames, blocks, tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, blocks, measurer=measurer)
    finally:
        crops.close()
    return frames, plans


def _tight_paragraph(text: str) -> Paragraph:
    block = Paragraph(
        runs=[TextRun(text=text)],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 704.0),
        source_text="A bounded paragraph that must fit in place.",
    )
    return block


def _neighbour() -> Paragraph:
    block = Paragraph(
        runs=[TextRun(text="下方相邻块。")],
        page_index=0,
        bbox=(72.0, 600.0, 400.0, 680.0),
        source_text="Neighbour block below it.",
    )
    return block


def test_refit_replaces_unfitting_block(tmp_path):
    block = _tight_paragraph(_LONG_TRANSLATION)
    frames, plans = _plan(tmp_path, [block, _neighbour()])
    plan = plans[0].blocks[0]
    assert plan.status == "original"
    assert "fit" in plan.reason

    calls: list[str] = []
    retried = document_pipeline.refit_with_concise_translations(
        plans, translate_fn=lambda source: calls.append(source) or "短译文。"
    )

    assert retried == 1
    assert calls == ["A bounded paragraph that must fit in place."]
    assert block.runs[0].text == "短译文。"

    # After re-planning, the concise translation fits inside the box.
    crops = layout_render.prepare_formula_crops(
        tmp_path / "source.pdf", frames, [block], tmp_path / "c2"
    )
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        replanned = layout_fit.plan_document(frames, [block, _neighbour()], measurer=measurer)
    finally:
        crops.close()
    assert replanned[0].blocks[0].status == "translated"


def test_refit_leaves_fitting_blocks_alone(tmp_path):
    block = _tight_paragraph("短译文。")
    _frames, plans = _plan(tmp_path, [block, _neighbour()])
    assert plans[0].blocks[0].status == "translated"

    def forbidden(source):
        raise AssertionError("fitting blocks must not be re-translated")

    assert document_pipeline.refit_with_concise_translations(plans, translate_fn=forbidden) == 0


def test_refit_skips_blocks_with_inline_formulas(tmp_path):
    block = Paragraph(
        runs=[TextRun(text=_LONG_TRANSLATION), InlineMath(latex="x^2", bbox=(80.0, 691.0, 95.0, 703.0))],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 704.0),
        source_text="A bounded paragraph that must fit in place.",
    )
    _frames, plans = _plan(tmp_path, [block, _neighbour()])
    assert plans[0].blocks[0].status == "original"

    def forbidden(source):
        raise AssertionError("blocks with inline formulas must not be compressed")

    assert document_pipeline.refit_with_concise_translations(plans, translate_fn=forbidden) == 0


def test_refit_replaces_unfitting_cell(tmp_path):
    source = tmp_path / "table.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(80, 470, "Method")
    canvas.showPage()
    canvas.save()

    cell = TableCell(
        text="Method",
        bbox=(80.0, 466.0, 118.0, 478.0),
        translated="这是一种非常非常长、无论如何也塞不进单元格的译文",
    )
    table = Table(
        rel_path="",
        caption="Table 1. Results.",
        page_index=0,
        bbox=(72.0, 430.0, 400.0, 505.0),
        cells=[cell],
    )
    frames = layout_model.measure_pages(source)
    crops = layout_render.prepare_formula_crops(source, frames, [table], tmp_path / "c")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, [table], measurer=measurer)
    finally:
        crops.close()
    assert plans[0].cells[0].status == "original"
    assert "fit" in plans[0].cells[0].reason

    retried = document_pipeline.refit_with_concise_translations(
        plans, translate_fn=lambda source_text: "方法"
    )

    assert retried == 1
    assert cell.translated == "方法"


def test_refit_tolerates_translate_failures(tmp_path):
    block = _tight_paragraph(_LONG_TRANSLATION)
    _frames, plans = _plan(tmp_path, [block, _neighbour()])

    def broken(source):
        raise RuntimeError("provider unavailable")

    assert document_pipeline.refit_with_concise_translations(plans, translate_fn=broken) == 0
    assert block.runs[0].text == _LONG_TRANSLATION
