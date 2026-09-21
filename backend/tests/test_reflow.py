"""One page in, one page out: a failed page reverts instead of adding sheets."""

import pypdf
import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import layout_model, layout_render
from app.services.layout_fit import BlockPlan, Fragment, PagePlan
from app.services.mineru_layout import Paragraph


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
    from app.services.mineru_layout import Paragraph, TextRun

    return Paragraph(
        runs=[TextRun(text=text)],
        page_index=0,
        bbox=(72.0, 690.0, 400.0, 706.0),
        source_text=source,
    )


def test_failed_page_writes_exactly_one_original_page(tmp_path):
    block = _translated_block("这是排版失败后本应重排的译文。", "Body paragraph.")
    output, report = _render(tmp_path, [block])

    assert report.pages[0].status == "original"
    assert report.pages[0].reason == "rotated page"
    # The page keeps its own source text and no extra sheet is produced.
    assert "Body paragraph." in _page_text(output, 0)
    assert "这是排版失败后本应重排的译文。" not in _page_text(output, 0)
    assert len(pypdf.PdfReader(str(output)).pages) == 2


def test_output_page_count_always_matches_the_source(tmp_path):
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

    assert len(report.pages) == 2
    assert len(pypdf.PdfReader(str(output)).pages) == 2


def test_page_fallback_counts_towards_the_failure_budget(tmp_path):
    block = _translated_block("译文。", "Body paragraph.")
    _output, report = _render(tmp_path, [block])
    assert [page.index for page in report.failed] == [0]


def test_all_pages_original_fails_the_document(tmp_path):
    source = tmp_path / "source.pdf"
    _source(source, ["Body paragraph."])
    frames = layout_model.measure_pages(source)
    plans = [_failed_plan(frames[0]), _failed_plan(frames[1])]
    crops = layout_render.prepare_formula_crops(source, frames, [], tmp_path / "c")
    try:
        with pytest.raises(layout_render.LayoutRenderError):
            layout_render.render_document(
                source_pdf=source,
                plans=plans,
                frames=frames,
                blocks=[],
                output_pdf=tmp_path / "out.pdf",
                crops=crops,
            )
    finally:
        crops.close()


@pytest.mark.parametrize(
    "total, failed, expected",
    [
        (10, 3, True),
        (20, 4, False),
        (21, 4, True),
        (2, 2, False),
    ],
)
def test_failure_budget_thresholds(total, failed, expected):
    report = layout_render.RenderReport(
        pages=[
            layout_render.PageResult(
                index=index, status="original" if index < failed else "ok"
            )
            for index in range(total)
        ]
    )
    if expected:
        layout_render._enforce_failure_budget(report)
    else:
        with pytest.raises(layout_render.LayoutRenderError):
            layout_render._enforce_failure_budget(report)


def test_no_page_status_can_add_pages(tmp_path):
    """Only ``ok``, ``masked`` and ``original`` are valid page outcomes."""
    block = _translated_block("译文。", "Body paragraph.")
    output, report = _render(tmp_path, [block])
    statuses = {page.status for page in report.pages}
    assert statuses <= {"ok", "masked", "original"}
    assert len(report.pages) == len(pypdf.PdfReader(str(output)).pages)


def test_masked_overlay_keeps_bbox_anchor_when_removal_is_unclean(tmp_path, monkeypatch):
    """When content-stream surgery fails, the translation is drawn anchored at
    the source boxes over white masks, and the page count is unchanged."""
    source = tmp_path / "source.pdf"
    _source(source, ["First line of a shared object"])
    frames = layout_model.measure_pages(source)

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
    # The page count matches the source: no reflow spill.
    assert len(pypdf.PdfReader(str(output)).pages) == 2

    # The masked page renders the translation over the masked source line.
    bitmap = pdfium.PdfDocument(str(output))[0].render(scale=1.0)
    image = bitmap.to_pil().convert("L")
    assert image.getextrema()[0] < 128  # glyphs were actually drawn
    bitmap.close()


def test_a_page_with_nothing_to_translate_is_not_a_fallback(tmp_path):
    """A bibliography page keeps the source page and is not a failure."""
    from app.services.layout_fit import NOTHING_TO_TRANSLATE

    source = tmp_path / "source.pdf"
    _source(source, ["Body paragraph."])
    frames = layout_model.measure_pages(source)
    empty = PagePlan(index=1, width=frames[1].width, height=frames[1].height)
    empty.status = "original"
    empty.reason = NOTHING_TO_TRANSLATE
    crops = layout_render.prepare_formula_crops(source, frames, [], tmp_path / "c")
    try:
        output = tmp_path / "out.pdf"
        report = layout_render.render_document(
            source_pdf=source,
            plans=[_ok_plan(frames[0]), empty],
            frames=frames,
            blocks=[],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    assert [page.status for page in report.pages] == ["ok", "source"]
    assert report.failed == []
    assert report.source_only and report.source_only[0].index == 1
    assert len(pypdf.PdfReader(str(output)).pages) == 2


def test_many_bibliography_pages_still_publish(tmp_path):
    """Source-only pages never exhaust the fallback budget."""
    from app.services.layout_fit import NOTHING_TO_TRANSLATE

    source = tmp_path / "source.pdf"
    _source(source, ["Body paragraph."])
    frames = layout_model.measure_pages(source)
    plans = [_ok_plan(frames[0]), _failed_plan(frames[1])]
    # Six extra source-only pages, as a long bibliography produces.
    for index in range(2, 8):
        page = PagePlan(index=index, width=612.0, height=792.0)
        page.status = "original"
        page.reason = NOTHING_TO_TRANSLATE
        plans.append(page)
        frames.append(layout_model.PageFrame(index=index, width=612.0, height=792.0))

    writer = pypdf.PdfWriter()
    for _ in range(len(frames)):
        writer.add_blank_page(width=612, height=792)
    padded = tmp_path / "padded.pdf"
    with padded.open("wb") as handle:
        writer.write(handle)

    crops = layout_render.FormulaCrops(padded, tmp_path / "c")
    try:
        report = layout_render.render_document(
            source_pdf=padded,
            plans=plans,
            frames=frames,
            blocks=[],
            output_pdf=tmp_path / "out.pdf",
            crops=crops,
        )
    finally:
        crops.close()

    assert len(report.pages) == len(frames)
    assert [page.status for page in report.pages].count("source") == 6
    assert len(report.failed) == 1
