"""Annotated-source PDF coverage: box categories and source preservation."""

from pathlib import Path

import pypdf
import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings
from app.services import document_pipeline
from app.services.annotation_render import (
    block_category,
    render_annotated_pdf,
)
from app.services.app_settings import AppSettings
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_model import caption_rect, document_lines, measure_pages
from app.services.mineru_layout import (
    Author,
    DisplayMath,
    Image,
    ListBlock,
    Paragraph,
    Table,
    TextRun,
    Title,
    apply_translations,
    collect_translatable_strings,
)
from app.services.mineru_service import MinerUResult


def _source(path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 700, "A Study of Layout Engines")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 660, "Body paragraph text.")
    canvas.showPage()
    canvas.save()


def _blocks() -> list:
    return [
        Title(
            level=1,
            text="A Study of Layout Engines",
            page_index=0,
            bbox=(72.0, 690.0, 400.0, 710.0),
        ),
        Author(text="A. Author", page_index=0, bbox=(72.0, 670.0, 260.0, 684.0)),
        Paragraph(
            runs=[TextRun("Body paragraph text.")],
            page_index=0,
            bbox=(72.0, 650.0, 400.0, 664.0),
        ),
        ListBlock(
            items=[[TextRun("item")]], page_index=0, bbox=(72.0, 620.0, 300.0, 640.0)
        ),
        DisplayMath(latex="x^2", page_index=0, bbox=(200.0, 580.0, 300.0, 600.0)),
        Image(rel_path="images/f1.png", page_index=0, bbox=(72.0, 480.0, 300.0, 560.0)),
        Table(html="<table/>", page_index=0, bbox=(72.0, 380.0, 500.0, 460.0)),
    ]


def _page_text(path) -> str:
    document = pdfium.PdfDocument(str(path))
    try:
        textpage = document[0].get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            textpage.close()
    finally:
        document.close()


def test_block_category_covers_every_ir_type():
    assert [block_category(block) for block in _blocks()] == [
        "title",
        "author",
        "paragraph",
        "list",
        "formula",
        "figure",
        "table",
    ]


def test_render_boxes_every_parsed_region_with_its_category(tmp_path):
    source = tmp_path / "source.pdf"
    _source(source)
    frames = measure_pages(source)
    output = tmp_path / "annotated.pdf"

    drawn = render_annotated_pdf(
        source_pdf=source, frames=frames, blocks=_blocks(), output_pdf=output
    )

    assert drawn == 7
    reader = pypdf.PdfReader(str(output))
    assert len(reader.pages) == len(pypdf.PdfReader(str(source)).pages)
    assert reader.pages[0].mediabox.width == pytest.approx(612.0)

    text = _page_text(output)
    # The source text layer survives; annotation only adds an overlay.
    assert "A Study of Layout Engines" in text
    assert "Body paragraph text." in text
    # One label per category drawn on the page.
    for label in ("标题", "作者", "正文", "列表", "公式", "图片", "表格"):
        assert label in text


def test_pages_without_regions_are_copied_unchanged(tmp_path):
    source = tmp_path / "source.pdf"
    _source(source)
    frames = measure_pages(source)
    output = tmp_path / "annotated.pdf"

    drawn = render_annotated_pdf(
        source_pdf=source, frames=frames, blocks=[], output_pdf=output
    )

    assert drawn == 0
    assert len(pypdf.PdfReader(str(output)).pages) == 1


def _captioned_source(path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 700, "Body text that is not a caption.")
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 500, "Figure 1. A caption on the page.")
    canvas.drawString(72, 300, "Figure 2. A caption on the page.")
    canvas.showPage()
    canvas.save()


def _captioned_blocks() -> list:
    return [
        Image(
            rel_path="images/f1.png",
            caption="Figure 1. A caption on the page.",
            page_index=0,
            bbox=(72.0, 520.0, 300.0, 640.0),
        ),
        Image(
            rel_path="images/f2.png",
            caption="Figure 2. A caption on the page.",
            page_index=0,
            bbox=(72.0, 320.0, 300.0, 440.0),
        ),
    ]


def test_caption_without_geometry_is_located_on_the_page(tmp_path):
    source = tmp_path / "source.pdf"
    _captioned_source(source)
    frames = measure_pages(source)
    output = tmp_path / "annotated.pdf"

    drawn = render_annotated_pdf(
        source_pdf=source,
        frames=frames,
        blocks=_captioned_blocks(),
        output_pdf=output,
    )

    # Two figure regions plus their two captions.
    assert drawn == 4
    assert "图注" in _page_text(output)


def test_repeated_caption_text_binds_to_its_own_figure(tmp_path):
    source = tmp_path / "source.pdf"
    _captioned_source(source)
    frames = measure_pages(source)
    blocks = _captioned_blocks()
    lines = [line for _page, line in document_lines([frames[0]])]

    first = caption_rect(blocks[0], frames[0], lines)
    second = caption_rect(blocks[1], frames[0], lines)

    assert first is not None and second is not None
    # Both captions read almost the same, so each must land by its own figure.
    assert first != second
    assert first[1] > second[1]


def test_caption_outside_the_text_layer_is_skipped(tmp_path):
    source = tmp_path / "source.pdf"
    _captioned_source(source)
    frames = measure_pages(source)
    block = Image(
        rel_path="images/f3.png",
        caption="Figure 3. A caption that is not on this page.",
        page_index=0,
        bbox=(72.0, 120.0, 300.0, 240.0),
    )
    output = tmp_path / "annotated.pdf"

    drawn = render_annotated_pdf(
        source_pdf=source, frames=frames, blocks=[block], output_pdf=output
    )

    assert drawn == 1


def _parser_dropped_source(path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 740, "A Dropped Text Paper")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 700, "A first paragraph the parser reports.")
    canvas.drawString(72, 660, "The parser emitted this region with no text at all,")
    canvas.drawString(72, 640, "so the renderer recovered it from the page itself.")
    canvas.showPage()
    canvas.save()


def test_pipeline_annotates_text_the_parser_dropped(isolated_storage, monkeypatch):
    """A region the parser emitted without text is recovered from the page and
    must get a box, because the renderer translates it."""
    source = settings.upload_dir / "dropped.pdf"
    _parser_dropped_source(source)
    structured = MinerUResult(
        markdown="A first paragraph the parser reports.",
        mode_label="fixture",
        content_blocks=[
            [
                {
                    "type": "title",
                    "bbox": [72, 30, 470, 62],
                    "content": {
                        "title_content": [
                            {"type": "text", "content": "A Dropped Text Paper"}
                        ],
                        "level": 1,
                    },
                },
                {
                    "type": "paragraph",
                    "bbox": [72, 78, 470, 98],
                    "content": {
                        "paragraph_content": [
                            {
                                "type": "text",
                                "content": "A first paragraph the parser reports.",
                            }
                        ]
                    },
                },
                # The parser reports the region but leaves its text empty.
                {
                    "type": "paragraph",
                    "bbox": [72, 118, 470, 160],
                    "content": {"paragraph_content": []},
                },
            ]
        ],
        boxes_normalized=False,
    )

    record = document_pipeline.create_document_record(source, "pdf")
    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", lambda *a, **k: structured
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(document_pipeline, "extract_document_terms", lambda *a, **k: [])

    def fake_translate_ir(ir, **kwargs):
        apply_translations(
            ir, [f"译-{index}" for index, _ in enumerate(collect_translatable_strings(ir))]
        )
        return []

    monkeypatch.setattr(document_pipeline, "translate_ir", fake_translate_ir)

    annotated_blocks: list = []
    real_render = document_pipeline.render_annotated_pdf

    def spy(**kwargs):
        annotated_blocks.extend(kwargs["blocks"])
        return real_render(**kwargs)

    monkeypatch.setattr(document_pipeline, "render_annotated_pdf", spy)

    processed = document_pipeline.process_document(
        record,
        provider_settings=AppSettings(
            api_key="k",
            base_url="https://llm.example/v1",
            model="m",
            pdf_parser="local",
        ),
    )

    assert processed.status == "done", processed.logs
    artifact = next(a for a in processed.artifacts if a.kind == "annotated_pdf")
    assert Path(artifact.path).is_file()

    # The dropped lines sit around y=663 and y=643 in page points (origin
    # lower-left) and must be inside the boxes handed to the renderer.
    def covered(y: float) -> bool:
        return any(
            block.bbox
            and block.bbox[0] <= 200.0 <= block.bbox[2]
            and block.bbox[1] <= y <= block.bbox[3]
            for block in annotated_blocks
        )

    assert covered(663.0) and covered(643.0), [
        getattr(block, "bbox", None) for block in annotated_blocks
    ]
