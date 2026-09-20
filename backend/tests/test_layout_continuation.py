"""Inline-formula geometry and paragraphs split across several regions."""

import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import document_pipeline, layout_fit, layout_model, layout_render, translate_service
from app.services.cjk_fonts import find_cjk_font, require_cjk_font
from app.services.layout_model import PageFrame, attach_inline_formula_boxes
from app.services.mineru_layout import (
    InlineMath,
    Paragraph,
    TextRun,
    Title,
    collect_translatable_strings,
    merge_continuation_groups,
    plan_continuation_groups,
    split_continuation_groups,
)

pytestmark = pytest.mark.skipif(
    find_cjk_font() is None, reason="requires an installed CJK font"
)

FRAMES = [PageFrame(index=0, width=612.0, height=792.0)]


def _paragraph(text, bbox, page_index=0):
    return Paragraph(
        runs=[TextRun(text=text)],
        page_index=page_index,
        bbox=bbox,
        source_text=text,
    )


# ---------------------------------------------------------------------------
# Inline-formula geometry from the VLM model output
# ---------------------------------------------------------------------------


def test_model_boxes_scale_per_axis():
    """Regression: X and Y were scaled by one factor, pushing boxes off-page."""
    payload = [
        [
            {
                "type": "inline_formula",
                "bbox": [0.556, 0.07, 0.667, 0.084],
                "latex": "L = D_{x}^{T} D x",
            },
            {"type": "text", "bbox": [0.084, 0.07, 0.329, 0.085]},
        ]
    ]
    boxes = layout_model.inline_formula_boxes(payload, FRAMES)

    rect, latex = boxes[0][0]
    assert latex.startswith("L = D")
    assert rect[0] == pytest.approx(0.556 * 612.0)
    assert rect[2] == pytest.approx(0.667 * 612.0)
    # PDF space: y grows upwards, the detection box is measured from the top.
    assert rect[3] == pytest.approx(792.0 - 0.07 * 792.0)
    assert rect[1] == pytest.approx(792.0 - 0.084 * 792.0)
    assert rect[2] < 612.0


def test_model_boxes_accept_page_points_too():
    payload = [[{"type": "inline_formula", "bbox": [300.0, 20.0, 340.0, 32.0], "latex": "a"}]]
    boxes = layout_model.inline_formula_boxes(payload, FRAMES)
    rect, _ = boxes[0][0]
    assert rect == pytest.approx((300.0, 760.0, 340.0, 772.0))


def test_attach_inline_formula_boxes_fills_content_list_gaps():
    block = Paragraph(
        runs=[
            TextRun(text="as "),
            InlineMath(latex="F_{\\lambda}"),
            TextRun(text=" shows"),
        ],
        page_index=0,
        bbox=(60.0, 600.0, 300.0, 620.0),
        source_text="as F_lambda shows",
    )
    payload = [
        [
            {
                "type": "inline_formula",
                "bbox": [0.1, 0.24, 0.2, 0.26],
                "latex": "F _ { \\lambda }",
            }
        ]
    ]

    notes = attach_inline_formula_boxes([block], payload, FRAMES)

    assert block.runs[1].bbox == pytest.approx((61.2, 586.08, 122.4, 601.92))
    assert any("located 1 inline formula" in note for note in notes)


def test_inline_geometry_count_mismatch_matches_by_latex():
    block = Paragraph(
        runs=[
            InlineMath(latex="x^{2}"),
            TextRun(text=" and "),
            InlineMath(latex="y^{2}"),
        ],
        page_index=0,
        bbox=(60.0, 600.0, 300.0, 620.0),
        source_text="x2 and y2",
    )
    payload = [[{"type": "inline_formula", "bbox": [0.1, 0.24, 0.2, 0.26], "latex": "x ^ 2"}]]

    notes = attach_inline_formula_boxes([block], payload, FRAMES)

    assert block.runs[0].bbox is not None
    assert block.runs[2].bbox is None
    assert any("1 inline formula(s) have no geometry" in note for note in notes)


# ---------------------------------------------------------------------------
# Paragraphs the parser split over several regions
# ---------------------------------------------------------------------------


def test_split_paragraph_is_translated_once_and_distributed_back():
    ir = [
        _paragraph(
            "The first half of one sentence that the parser cut in the middle",
            (72.0, 500.0, 300.0, 560.0),
        ),
        _paragraph(
            "and the second half continues in the region right below it.",
            (72.0, 430.0, 300.0, 495.0),
        ),
    ]
    groups = plan_continuation_groups(ir, FRAMES)
    assert len(groups) == 1
    merge_continuation_groups(groups)

    # One paragraph, one translation request.
    assert collect_translatable_strings(ir) == [
        "The first half of one sentence that the parser cut in the middle "
        "and the second half continues in the region right below it."
    ]

    calls: list[str] = []

    def fake_chat(message, system_prompt, **kwargs):
        calls.append(message)
        return "这是整段的中文译文，前半部分留在上方区域，后半部分回到下方区域。"

    original_chat = translate_service.llm_client.chat
    translate_service.llm_client.chat = fake_chat
    try:
        translate_service.translate_ir(ir)
    finally:
        translate_service.llm_client.chat = original_chat

    assert len(calls) == 1
    notes = split_continuation_groups(groups)
    assert notes and "distributed" in notes[0]

    first, second = ir[0].runs[0].text, ir[1].runs[0].text
    assert first and second
    assert first != second
    assert "".join((first + second).split()) == "".join(
        "这是整段的中文译文，前半部分留在上方区域，后半部分回到下方区域。".split()
    )
    # The split follows the source lengths (and snaps to nearby punctuation).
    source_share = len(ir[0].source_text) / (
        len(ir[0].source_text) + len(ir[1].source_text)
    )
    share = len(first) / (len(first) + len(second))
    assert abs(share - source_share) <= 0.16, (share, source_share)


def test_split_paragraph_keeps_both_regions_in_the_plan():
    ir = [
        _paragraph(
            "A sentence whose first part ends in the upper region of the page",
            (72.0, 500.0, 300.0, 560.0),
        ),
        _paragraph(
            "and whose second part starts in the lower region of the page.",
            (72.0, 430.0, 300.0, 495.0),
        ),
    ]
    groups = plan_continuation_groups(ir, FRAMES)
    merge_continuation_groups(groups)
    translated = "这是整段的中文译文，前半部分留在上方区域，后半部分回到下方区域。"
    ir[0].runs[0].text = translated
    split_continuation_groups(groups)

    measurer = layout_fit.TextMeasurer(require_cjk_font())
    plans = layout_fit.plan_document(FRAMES, ir, measurer=measurer)
    page = plans[0]

    assert len(page.translated_plans) == 2
    first, second = page.translated_plans
    # Each half stays inside its own region instead of pooling in one box.
    assert first.target[3] <= 560.5
    assert first.target[1] >= 490.0
    assert second.target[3] <= 500.0
    assert second.source_rect == (72.0, 430.0, 300.0, 495.0)


def test_paragraph_ending_a_sentence_is_not_grouped():
    ir = [
        _paragraph("A complete sentence that ends properly.", (72.0, 500.0, 300.0, 560.0)),
        _paragraph("another paragraph starts here with new content.", (72.0, 430.0, 300.0, 495.0)),
    ]
    assert plan_continuation_groups(ir, FRAMES) == []


def test_continuation_across_pages_is_grouped():
    frames = [
        PageFrame(index=0, width=612.0, height=792.0),
        PageFrame(index=1, width=612.0, height=792.0),
    ]
    ir = [
        _paragraph(
            "A paragraph that runs out of room at the bottom of the first page",
            (72.0, 80.0, 300.0, 140.0),
            page_index=0,
        ),
        _paragraph(
            "and continues at the top of the following page in the same column.",
            (72.0, 700.0, 300.0, 760.0),
            page_index=1,
        ),
    ]
    groups = plan_continuation_groups(ir, frames)
    assert len(groups) == 1
    assert len(groups[0].blocks) == 2


def test_continuation_into_the_next_column_is_grouped():
    ir = [
        _paragraph(
            "A paragraph that runs out of room at the bottom of the left column",
            (50.0, 70.0, 296.0, 130.0),
        ),
        _paragraph(
            "and continues at the top of the right-hand column of the same page.",
            (314.0, 690.0, 560.0, 760.0),
        ),
    ]
    groups = plan_continuation_groups(ir, FRAMES)
    assert len(groups) == 1


def test_column_jump_that_starts_a_new_sentence_is_not_grouped():
    ir = [
        _paragraph(
            "A paragraph that ends the left column with a full sentence.",
            (50.0, 70.0, 296.0, 130.0),
        ),
        _paragraph(
            "another column begins here with an unrelated new paragraph.",
            (314.0, 690.0, 560.0, 760.0),
        ),
    ]
    assert plan_continuation_groups(ir, FRAMES) == []


def test_paragraph_high_on_the_page_does_not_jump_columns():
    ir = [
        _paragraph(
            "A paragraph in the middle of the left column that keeps going",
            (50.0, 400.0, 296.0, 460.0),
        ),
        _paragraph(
            "and something in the right column at the very same height.",
            (314.0, 400.0, 560.0, 460.0),
        ),
    ]
    assert plan_continuation_groups(ir, FRAMES) == []


def test_bibliography_paragraphs_are_never_grouped():
    ir = [
        Title(level=1, text="References", page_index=0, bbox=(72.0, 700.0, 300.0, 720.0)),
        _paragraph(
            "[1] A reference entry that happens to run past the column edge",
            (72.0, 640.0, 300.0, 680.0),
        ),
        _paragraph(
            "and continues on the next line without a full stop at the end.",
            (72.0, 590.0, 300.0, 630.0),
        ),
    ]
    assert plan_continuation_groups(ir, FRAMES) == []


def test_grouped_paragraph_survives_proportional_split_with_tiny_text():
    ir = [
        _paragraph("Short first part of a sentence that continues", (72.0, 500.0, 300.0, 560.0)),
        _paragraph("in the second region of the page without a stop", (72.0, 430.0, 300.0, 495.0)),
    ]
    groups = plan_continuation_groups(ir, FRAMES)
    merge_continuation_groups(groups)
    ir[0].runs[0].text = "短"  # far too short to fill two regions
    notes = split_continuation_groups(groups)
    assert notes and "could not be split" in notes[0]
    assert ir[0].runs[0].text.strip() == "短"
    assert ir[1].runs[0].text.strip() == ir[1].source_text.strip()


SPLIT_FIRST = (
    "A paragraph that the parser split at the column boundary so its first "
    "region ends here without any closing punctuation"
)
SPLIT_SECOND = (
    "and the second region continues the very same sentence down here for the reader."
)
SPLIT_TRANSLATION = (
    "这是被解析器切成两块的整段译文，前半句留在上方区域，"
    "后半句回到下方区域，用来验证只请求一次翻译。"
)


def _split_paragraph_pdf(path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    for y, text in zip(
        (700, 688, 676),
        (
            "A paragraph that the parser split at the",
            "column boundary so its first region ends",
            "here without any closing punctuation",
        ),
    ):
        canvas.drawString(72, y, text)
    for y, text in zip(
        (640, 628),
        (
            "and the second region continues the very same",
            "sentence down here for the reader.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.showPage()
    canvas.save()


def _text_block(text, top, bottom, left=72.0, right=300.0):
    return {
        "type": "text",
        "bbox": [left, top, right, bottom],
        "lines": [
            {
                "bbox": [left, top, right, bottom],
                "spans": [
                    {"bbox": [left, top, right, bottom], "type": "text", "content": text}
                ],
            }
        ],
    }


def test_pipeline_requests_one_translation_for_a_split_paragraph(tmp_path, monkeypatch):
    from app.services.mineru_service import MinerUResult

    source = tmp_path / "split-paragraph.pdf"
    _split_paragraph_pdf(source)
    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    _text_block(SPLIT_FIRST, 89.0, 119.0),
                    _text_block(SPLIT_SECOND, 149.0, 167.0),
                ],
            }
        ]
    }
    result = MinerUResult(
        markdown=f"# Paper\n\n{SPLIT_FIRST} {SPLIT_SECOND}",
        mode_label="fixture",
        layout_payload=payload,
    )

    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)
    groups = plan_continuation_groups(blocks, frames)
    assert len(groups) == 1
    merge_continuation_groups(groups)

    calls: list[str] = []

    def fake_chat(message, system_prompt, **kwargs):
        calls.append(message)
        return SPLIT_TRANSLATION

    monkeypatch.setattr(translate_service.llm_client, "chat", fake_chat)
    translate_service.translate_ir(blocks)

    # The whole paragraph is one request.
    assert len(calls) == 1
    assert "closing punctuation" in calls[0]
    assert "sentence down here" in calls[0]

    split_continuation_groups(groups)
    first, second = (
        blocks[0].runs[0].text,
        blocks[1].runs[0].text,
    )
    assert first.strip() and second.strip()
    assert first != second

    crops = layout_render.prepare_formula_crops(source, frames, blocks, tmp_path / "crops")
    try:
        measurer = layout_fit.TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = layout_fit.plan_document(frames, blocks, measurer=measurer)
        assert len(plans[0].translated_plans) == 2
        output = tmp_path / "translated.pdf"
        layout_render.render_document(
            source_pdf=source,
            plans=plans,
            frames=frames,
            blocks=blocks,
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()

    document = pdfium.PdfDocument(str(output))
    try:
        textpage = document[0].get_textpage()
        try:
            rendered = textpage.get_text_range()
        finally:
            textpage.close()
    finally:
        document.close()

    # Both regions carry their own half of the single translation.
    assert "".join(first.split()) in "".join(rendered.split())
    assert "".join(second.split()) in "".join(rendered.split())
    assert "closing punctuation" not in rendered


# ---------------------------------------------------------------------------
# Text-layer ownership correction (design section 6)
# ---------------------------------------------------------------------------


def test_alignment_splits_a_block_whose_text_continues_elsewhere(tmp_path):
    from app.services.mineru_service import MinerUResult

    source = tmp_path / "misaligned.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    for y, text in zip(
        (700, 688),
        (
            "The first lines of a paragraph that the parser boxed too small",
            "and whose remainder is drawn far below on the same page.",
        ),
    ):
        canvas.drawString(72, y, text)
    for y, text in zip(
        (500, 488, 476),
        (
            "The tail of that very paragraph sits much lower on this page and",
            "no block of its own was reported for these lines at all, so the",
            "translation would otherwise cover only the first half of it.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.showPage()
    canvas.save()

    declared = (
        "The first lines of a paragraph that the parser boxed too small and whose "
        "remainder is drawn far below on the same page. The tail of that very "
        "paragraph sits much lower on this page and no block of its own was reported "
        "for these lines at all, so the translation would otherwise cover only the "
        "first half of it."
    )
    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [_text_block(declared, 89.0, 107.0)],
            }
        ]
    }
    result = MinerUResult(markdown=declared, mode_label="fixture", layout_payload=payload)
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    groups, notes = layout_model.align_blocks_to_text_layer(frames, blocks)

    assert len(groups) == 1
    assert any("split 1 paragraph" in note for note in notes)
    group = groups[0]
    assert len(group.blocks) == 2
    assert group.source_text == declared
    first, second = group.blocks
    # The first region is the parsed box, the second is where the tail is drawn.
    assert first.bbox[1] == pytest.approx(685.0, abs=6.0)
    assert second.bbox[3] == pytest.approx(510.0, abs=6.0)
    assert "first lines of a paragraph" in first.source_text
    assert "translation would otherwise cover only the first half" in second.source_text

    # The parts together carry the whole paragraph, without overlap.
    combined = (first.source_text + " " + second.source_text).split()
    assert combined == declared.split()


def test_alignment_splits_across_a_page_break_with_a_caption_between(tmp_path):
    """A gap between the two halves — a caption, a page — must not end it."""
    from app.services.mineru_service import MinerUResult

    source = tmp_path / "cross-page.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    for y, text in zip(
        (700, 688),
        (
            "The paragraph starts here, in the upper region of the",
            "first page, and its tail is drawn on the next page.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.drawString(72, 600, "Figure 2: a caption that sits between the two halves.")
    canvas.showPage()
    for y, text in zip(
        (700, 688),
        (
            "The tail of the paragraph continues on the second page,",
            "far below the caption of the figure in between them.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.showPage()
    canvas.save()

    declared = (
        "The paragraph starts here, in the upper region of the first page, and its "
        "tail is drawn on the next page. The tail of the paragraph continues on the "
        "second page, far below the caption of the figure in between them."
    )
    payload = {
        "pdf_info": [
            {"page_size": [612.0, 792.0], "para_blocks": [_text_block(declared, 89.0, 107.0)]},
            {"page_size": [612.0, 792.0], "para_blocks": []},
        ]
    }
    result = MinerUResult(markdown=declared, mode_label="fixture", layout_payload=payload)
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    groups, notes = layout_model.align_blocks_to_text_layer(frames, blocks)

    assert any("split 1 paragraph" in note for note in notes)
    assert len(groups) == 1
    first, second = groups[0].blocks
    assert first.page_index == 0
    assert second.page_index == 1
    assert "starts here" in first.source_text
    assert "second page" in second.source_text
    assert (first.source_text + " " + second.source_text).split() == declared.split()


def test_alignment_corrects_a_box_that_misses_its_text(tmp_path):
    from app.services.mineru_service import MinerUResult

    source = tmp_path / "shifted-box.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 700, "A single paragraph line that the parsed box missed entirely.")
    canvas.showPage()
    canvas.save()

    declared = "A single paragraph line that the parsed box missed entirely."
    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [_text_block(declared, 300.0, 320.0)],
            }
        ]
    }
    result = MinerUResult(markdown=declared, mode_label="fixture", layout_payload=payload)
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    _groups, notes = layout_model.align_blocks_to_text_layer(frames, blocks)

    assert any("corrected the region of 1 paragraph" in note for note in notes)
    assert blocks[0].bbox[3] == pytest.approx(703.0, abs=6.0)


def test_recovered_prose_keeps_its_short_wrapped_last_line(tmp_path):
    """A wrapped sentence fragment must not be mistaken for a figure label."""
    from app.services.mineru_service import MinerUResult
    from app.services.mineru_layout import plain_paragraph_text

    source = tmp_path / "wrapped-tail.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 9)
    for y, text in zip(
        (660, 648, 636, 624),
        (
            "A paragraph whose text the parser never reported ends with a",
            "sentence that wraps onto one more short line and must stay",
            "inside the recovered block, because dropping it left that",
            "line in English on the page.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.drawString(72, 612, "ing edges are strong.")
    canvas.showPage()
    canvas.save()

    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    _text_block("Somewhere else entirely.", 68.0, 82.0)
                ],
            }
        ]
    }
    result = MinerUResult(markdown="Somewhere else entirely.", mode_label="fixture",
                          layout_payload=payload)
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    layout_model.synthesize_unclaimed_paragraphs(frames, blocks)

    recovered = [
        plain_paragraph_text(block)
        for block in blocks
        if isinstance(block, Paragraph)
        and "never reported" in (plain_paragraph_text(block) or "")
    ]
    assert len(recovered) == 1
    assert recovered[0].endswith("ing edges are strong.")


def test_unclaimed_prose_is_recovered_but_captions_are_not(tmp_path):
    from app.services.mineru_service import MinerUResult
    from app.services.mineru_layout import plain_paragraph_text

    source = tmp_path / "dropped-prose.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(90, 700, "Figure 1: A caption that must stay in the source language.")
    canvas.drawString(72, 660, "input image")
    for y, text in zip(
        (600, 588, 576, 564),
        (
            "A whole paragraph that the parser never reported, even though the",
            "source page draws it here, which used to leave this part of the",
            "page in English while the translation covered only the paragraph",
            "above it.",
        ),
    ):
        canvas.drawString(72, y, text)
    # one real block so the page is not entirely unparsed
    canvas.drawString(72, 720, "A parsed paragraph that the parser did report.")
    canvas.showPage()
    canvas.save()

    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    _text_block("A parsed paragraph that the parser did report.", 68.0, 82.0)
                ],
            }
        ]
    }
    result = MinerUResult(
        markdown="A parsed paragraph that the parser did report.", mode_label="fixture",
        layout_payload=payload,
    )
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    notes = layout_model.synthesize_unclaimed_paragraphs(frames, blocks)

    recovered = [
        block
        for block in blocks
        if isinstance(block, Paragraph)
        and "never reported" in (plain_paragraph_text(block) or "")
    ]
    assert len(recovered) == 1
    text = plain_paragraph_text(recovered[0])
    assert "A whole paragraph that the parser never reported" in text
    assert "Figure 1" not in text
    assert "input image" not in text
    assert any("recovered 1 paragraph" in note for note in notes)


def test_two_unclaimed_prose_lines_are_recovered(tmp_path):
    """A short tail the parser dropped is still body text, not a label."""
    from app.services.mineru_service import MinerUResult
    from app.services.mineru_layout import plain_paragraph_text

    source = tmp_path / "short-tail.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(72, 720, "A parsed paragraph that the parser did report.")
    for y, text in zip(
        (600, 588),
        (
            "steady state solution is ensured by biasing the solution towards",
            "the input image by introducing a term into the diffusion equation.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.showPage()
    canvas.save()

    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    _text_block("A parsed paragraph that the parser did report.", 68.0, 82.0)
                ],
            }
        ]
    }
    result = MinerUResult(
        markdown="A parsed paragraph that the parser did report.", mode_label="fixture",
        layout_payload=payload,
    )
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    notes = layout_model.synthesize_unclaimed_paragraphs(frames, blocks)

    recovered = [
        plain_paragraph_text(block)
        for block in blocks
        if isinstance(block, Paragraph)
        and "steady state solution" in (plain_paragraph_text(block) or "")
    ]
    assert len(recovered) == 1
    assert recovered[0].endswith("diffusion equation.")
    assert any("recovered 1 paragraph" in note for note in notes)


def test_alignment_split_keeps_each_formula_with_its_region(tmp_path):
    """A formula paragraph split in two keeps its math where it was drawn."""
    from app.services.mineru_service import MinerUResult

    source = tmp_path / "split-formula.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 10)
    for y, text in zip(
        (700, 688),
        (
            "The operator is applied to the input image that the parser",
            "boxed too small for this particular paragraph of the page.",
        ),
    ):
        canvas.drawString(72, y, text)
    for y, text in zip(
        (500, 488),
        (
            "The tail of that same paragraph continues further down here",
            "with the steady state solution of the very same system.",
        ),
    ):
        canvas.drawString(72, y, text)
    canvas.showPage()
    canvas.save()

    declared = (
        "The operator is applied to the input image that the parser boxed too small "
        "for this particular paragraph of the page. The tail of that same paragraph "
        "continues further down here with the steady state solution of the very same "
        "system."
    )
    formula = InlineMath(latex="x^2", bbox=(250.0, 695.0, 268.0, 706.0))
    block = Paragraph(
        runs=[
            TextRun(text="The operator is applied to the input image that the parser "),
            formula,
            TextRun(
                text=(
                    "boxed too small for this particular paragraph of the page. The tail "
                    "of that same paragraph continues further down here with the steady "
                    "state solution of the very same system."
                )
            ),
        ],
        page_index=0,
        bbox=(72.0, 680.0, 300.0, 704.0),
        source_text=declared,
    )
    frames = layout_model.measure_pages(source)

    groups, notes = layout_model.align_blocks_to_text_layer(frames, [block])

    assert any("split 1 paragraph" in note for note in notes)
    assert len(groups) == 1
    first, second = groups[0].blocks
    assert any(run is formula for run in first.runs)
    assert not any(not isinstance(run, TextRun) for run in second.runs)
    combined = first.source_text + " " + second.source_text
    assert combined.split() == declared.split()


def test_continuation_after_a_neighbouring_column_line_stays_one_run(tmp_path):
    """A paragraph continued across columns keeps its first line.

    The page is scanned top-down, so a line of the neighbouring column sits
    between a paragraph's first line and its continuation. That used to split
    the paragraph into an orphan first line -- too short to pass the prose
    test on its own -- and the rest, leaving the first line in English.
    """
    from app.services.mineru_service import MinerUResult
    from app.services.mineru_layout import plain_paragraph_text

    source = tmp_path / "interleaved-continuation.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 9)
    # Right column: the continuation of a paragraph from the previous column.
    for y, text in zip(
        (700, 688, 676),
        (
            "able. The others are implemented by the authors'",
            "source codes, and all the tone mapping methods use",
            "the default parameters as provided in the papers.",
        ),
    ):
        canvas.drawString(330, y, text)
    # Left column: an unclaimed line whose text falls between the first two
    # right-column lines in top-down page order.
    canvas.drawString(72, 694, "shown in Fig. 8(h).")
    # One parsed block so the page is not entirely unparsed.
    canvas.drawString(72, 720, "A parsed paragraph that the parser did report.")
    canvas.showPage()
    canvas.save()

    payload = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    _text_block(
                        "A parsed paragraph that the parser did report.", 68.0, 82.0
                    )
                ],
            }
        ]
    }
    result = MinerUResult(
        markdown="A parsed paragraph that the parser did report.",
        mode_label="fixture",
        layout_payload=payload,
    )
    blocks, frames, _notes = document_pipeline._build_ir_and_frames(result, source)

    layout_model.synthesize_unclaimed_paragraphs(frames, blocks)

    recovered = [
        block
        for block in blocks
        if isinstance(block, Paragraph)
        and "able. The others are implemented" in (plain_paragraph_text(block) or "")
    ]
    assert len(recovered) == 1
    text = plain_paragraph_text(recovered[0])
    assert "default parameters as provided" in text
    assert "shown in Fig" not in text
    # The box reaches the paragraph's first line, whose top is above y=700.
    assert recovered[0].bbox[3] > 700.0
