from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.services.layout_model import (
    compact_middle,
    measure_pages,
    pages_from_middle,
    parse_local_pages,
)
from app.services.mineru_layout import (
    DisplayMath,
    Image,
    InlineMath,
    ListBlock,
    Paragraph,
    Table,
    TableCell,
    TextRun,
    Title,
    apply_translations,
    blocks_to_ir,
    collect_translatable_strings,
    translatable_mask,
)


SAMPLE_PAGES = [
    [
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Math for CS & AI: Homework 7 "}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Sitian Ding "}]},
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Problem 1 "}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "Denote the first term as "},
                    {"type": "equation_inline", "content": "B(x)"},
                    {"type": "text", "content": ". We have "},
                ]
            },
        },
        {
            "type": "equation_interline",
            "content": {
                "math_content": "A(x) = \\sum_{n} a_n x^n",
                "math_type": "latex",
                "image_source": {"path": "images/eq1.jpg"},
            },
        },
        {
            "type": "image",
            "content": {
                "image_source": {"path": "images/fig1.jpg"},
                "image_caption": [{"type": "text", "content": "A figure caption."}],
            },
        },
    ]
]


def test_blocks_to_ir_extracts_titles_paragraphs_math_image():
    ir = blocks_to_ir(SAMPLE_PAGES)
    assert len(ir) == 6

    assert isinstance(ir[0], Title) and ir[0].level == 1 and "Math for CS" in ir[0].text
    assert isinstance(ir[1], Paragraph) and ir[1].runs == [TextRun(text="Sitian Ding ")]
    assert isinstance(ir[2], Title) and ir[2].text.strip() == "Problem 1"

    para = ir[3]
    assert isinstance(para, Paragraph)
    assert isinstance(para.runs[0], TextRun)
    assert isinstance(para.runs[1], InlineMath) and para.runs[1].latex == "B(x)"
    assert isinstance(para.runs[2], TextRun)

    assert isinstance(ir[4], DisplayMath) and ir[4].latex.startswith("A(x)")
    assert isinstance(ir[5], Image) and ir[5].rel_path == "images/fig1.jpg"
    assert ir[5].caption == "A figure caption."


def test_captions_enter_the_queue_and_author_names_do_not():
    ir = blocks_to_ir(SAMPLE_PAGES)
    segments = collect_translatable_strings(ir)
    # Title, author paragraph, Problem 1, the two prose runs around the inline
    # formula, and the figure caption.
    assert segments == [
        "Math for CS & AI: Homework 7",
        "Sitian Ding ",
        "Problem 1",
        "Denote the first term as ",
        ". We have ",
        "A figure caption.",
    ]

    # Author names stay in the source language; everything else translates.
    assert translatable_mask(ir) == [True, False, True, True, True, True]

    apply_translations(ir, [f"译{i}" for i in range(len(segments))])
    assert ir[0].text == "译0"
    assert ir[1].runs[0].text == "译1"
    assert ir[2].text == "译2"
    assert ir[3].runs[0].text == "译3"
    assert ir[3].runs[2].text == "译4"
    assert ir[5].caption == "A figure caption."
    assert ir[5].translated_caption == "译5"


def test_table_cells_and_caption_are_translated():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "table",
            "bbox": [100, 200, 500, 400],
            "content": {
                "image_source": {"path": "images/t1.jpg"},
                "table_caption": [{"type": "text", "content": "Table 1. Results."}],
            },
        },
    ]]
    ir = blocks_to_ir(pages, page_sizes=[(612.0, 792.0)])
    table = ir[1]
    assert isinstance(table, Table)
    assert table.caption == "Table 1. Results."
    table.cells = [
        TableCell(text="Method", bbox=(110, 300, 200, 320)),
        TableCell(text="Score", bbox=(220, 300, 300, 320)),
    ]

    segments = collect_translatable_strings(ir)
    assert segments == ["Paper", "Table 1. Results.", "Method", "Score"]
    apply_translations(ir, ["论文", "表 1。结果。", "方法", "分数"])
    assert [cell.translated for cell in table.cells] == ["方法", "分数"]
    assert table.caption == "Table 1. Results."
    assert table.translated_caption == "表 1。结果。"


def test_normalized_boxes_are_converted_to_page_points():
    pages = [[
        {
            "type": "title",
            "bbox": [0, 0, 500, 50],
            "content": {"title_content": [{"type": "text", "content": "Title"}], "level": 1},
        },
        {
            "type": "paragraph",
            "bbox": [0, 100, 500, 250],
            "content": {"paragraph_content": [{"type": "text", "content": "Text"}]},
        },
    ]]
    ir = blocks_to_ir(pages, page_sizes=[(600.0, 800.0)])
    # y is flipped: the paragraph sits in the upper half of the page.
    assert ir[1].bbox == (0.0, 600.0, 300.0, 720.0)


def test_reference_list_blocks_are_preserved_and_masked():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "list",
            "content": {
                "list_type": "reference_list",
                "list_items": [
                    {
                        "item_type": "text",
                        "item_content": [
                            {"type": "text", "content": "[1] First complete reference."}
                        ],
                    },
                    {
                        "item_type": "text",
                        "item_content": [
                            {"type": "text", "content": "[2] Second reference with "},
                            {"type": "equation_inline", "content": "x^2"},
                            {"type": "text", "content": "."},
                        ],
                    },
                ],
            },
        },
    ]]

    ir = blocks_to_ir(pages)
    assert isinstance(ir[1], ListBlock)
    assert len(ir[1].items) == 2

    segments = collect_translatable_strings(ir)
    assert segments == [
        "Paper",
        "[1] First complete reference.",
        "[2] Second reference with ",
        ".",
    ]
    assert translatable_mask(ir) == [True, False, False, False]

    apply_translations(ir, ["论文", "[1] First complete reference.", "[2] Second reference with ", "."])
    assert ir[0].text == "论文"
    assert ir[1].items[0][0].text == "[1] First complete reference."


def test_escaped_currency_dollars_do_not_turn_prose_into_inline_math():
    pages = [[{
        "type": "list",
        "content": {
            "list_type": "reference_list",
            "list_items": [{
                "item_type": "text",
                "item_content": [{
                    "type": "text",
                    "content": r"Prices are \$10.99 in Big & Tall and \$3.99 to $x^2$.",
                }],
            }],
        },
    }]]

    ir = blocks_to_ir(pages)
    assert isinstance(ir[0], ListBlock)
    assert [type(run) for run in ir[0].items[0]] == [TextRun, InlineMath, TextRun]
    assert ir[0].items[0][1].latex == "x^2"


def test_chart_panel_keeps_its_source_geometry():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "image",
            "bbox": [100, 500, 400, 700],
            "content": {
                "image_source": {"path": "images/figure-4a.jpg"},
                "image_caption": [{"type": "text", "content": "(a) Left panel."}],
            },
        },
        {
            "type": "chart",
            "bbox": [420, 505, 800, 700],
            "content": {
                "image_source": {"path": "images/figure-4b.jpg"},
                "chart_caption": [
                    {"type": "text", "content": "(b) Right panel."},
                    {"type": "text", "content": "Figure 4: Complete statistics."},
                ],
            },
        },
    ]]

    ir = blocks_to_ir(pages, page_sizes=[(1000.0, 1000.0)], normalized_boxes=False)
    assert isinstance(ir[1], Image) and isinstance(ir[2], Image)
    assert ir[2].rel_path == "images/figure-4b.jpg"
    assert ir[1].page_index == ir[2].page_index == 0
    assert ir[1].bbox == (100.0, 300.0, 400.0, 500.0)
    assert ir[2].bbox == (420.0, 300.0, 800.0, 495.0)


def _write_source(path, lines) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 11)
    y = 700
    for line in lines:
        canvas.drawString(72, y, line)
        y -= 24
    canvas.showPage()
    canvas.save()


def test_text_layer_measurement_reports_size_and_weight(tmp_path):
    source = tmp_path / "measured.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 700, "Bold Title")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 660, "Body text")
    canvas.showPage()
    canvas.save()

    frame = measure_pages(source)[0]
    assert frame.has_text_layer is False  # too few characters for a real page
    title_chars = [char for char in frame.chars if char.size == 16.0]
    body_chars = [char for char in frame.chars if char.size == 10.0]
    assert title_chars and all(char.bold for char in title_chars)
    assert body_chars and not any(char.bold for char in body_chars)


def test_local_parser_recovers_blocks_headers_and_captions(tmp_path):
    source = tmp_path / "local.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 760, "Preprint under review")
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(120, 700, "A Long Paper Title for Layout")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 660, "First paragraph sentence that continues here.")
    canvas.drawString(72, 648, "Second line of the same paragraph.")
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(72, 600, "1 Introduction")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(72, 570, "Body of the introduction.")
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 460, "Figure 1. A caption that is reused verbatim.")
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 40, "Page 7")
    canvas.showPage()
    canvas.save()

    page = parse_local_pages(source)[0]
    kinds = [block["type"] for block in page.blocks]
    assert kinds[0] == "title"
    assert "caption" in kinds
    assert "Page 7" not in page.markdown
    assert "Preprint under review" not in page.markdown

    ir = blocks_to_ir([page.blocks], [(page.width, page.height)], normalized_boxes=False)
    captions = [block for block in ir if getattr(block, "text", "") == "Figure 1. A caption that is reused verbatim."]
    assert captions == []


def test_middle_json_becomes_ir_with_geometry():
    middle = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    {
                        "type": "title",
                        "bbox": [72, 100, 540, 130],
                        "lines": [
                            {
                                "bbox": [72, 100, 540, 130],
                                "spans": [
                                    {"bbox": [72, 100, 540, 130], "type": "text", "content": "Paper Title"}
                                ],
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "bbox": [72, 200, 300, 260],
                        "lines": [
                            {
                                "bbox": [72, 200, 300, 230],
                                "spans": [
                                    {"bbox": [72, 200, 200, 230], "type": "text", "content": "Body text "},
                                    {"bbox": [200, 200, 230, 230], "type": "inline_equation", "content": "x^2"},
                                    {"bbox": [230, 200, 300, 230], "type": "text", "content": "continues."},
                                ],
                            }
                        ],
                    },
                    {
                        "type": "table",
                        "bbox": [72, 400, 400, 500],
                        "blocks": [
                            {
                                "type": "table_body",
                                "bbox": [72, 400, 400, 500],
                                "lines": [
                                    {
                                        "bbox": [80, 420, 380, 440],
                                        "spans": [
                                            {"bbox": [80, 420, 200, 440], "type": "text", "content": "Method"},
                                            {"bbox": [250, 420, 380, 440], "type": "text", "content": "Score"},
                                        ],
                                    }
                                ],
                            },
                            {
                                "type": "table_caption",
                                "bbox": [72, 500, 400, 520],
                                "lines": [
                                    {
                                        "bbox": [72, 500, 400, 520],
                                        "spans": [
                                            {"bbox": [72, 500, 400, 520], "type": "text", "content": "Table 1. Results."}
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
                "discarded_blocks": [
                    {"type": "text", "bbox": [72, 30, 200, 50], "lines": []}
                ],
            }
        ]
    }

    blocks, frames = pages_from_middle(middle)
    kinds = [type(block).__name__ for block in blocks]
    assert kinds == ["Title", "Paragraph", "Table"]

    title, paragraph, table = blocks
    assert title.bbox == (72.0, 662.0, 540.0, 692.0)
    assert paragraph.page_index == 0
    assert [type(run).__name__ for run in paragraph.runs] == ["TextRun", "InlineMath", "TextRun"]
    assert paragraph.runs[1].bbox == (200.0, 562.0, 230.0, 592.0)
    assert table.caption == "Table 1. Results."
    assert [(cell.text) for cell in table.cells] == ["Method", "Score"]
    assert table.cells[0].bbox == (80.0, 352.0, 200.0, 372.0)

    frame = frames[0]
    assert frame.width == 612.0 and frame.height == 792.0
    # The discarded block (running head / page number) is accounted for but is
    # not translated.
    assert (72.0, 742.0, 200.0, 762.0) in frame.known_regions
    assert len(frame.known_regions) == 5

    compacted = compact_middle(middle)
    assert compacted is not None
    assert compacted["pdf_info"][0]["para_blocks"][2]["type"] == "table"
