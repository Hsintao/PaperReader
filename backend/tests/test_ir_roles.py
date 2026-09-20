"""IR semantic roles and caption slots for the fixed Chinese template."""

from app.services.layout_model import pages_from_middle
from app.services.mineru_layout import (
    Image,
    ListBlock,
    Paragraph,
    Table,
    TextRun,
    Title,
    blocks_to_ir,
    collect_translatable_strings,
    translatable_mask,
)


def _front_matter_pages() -> list:
    return [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Abstract"}], "level": 2},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Abstract body."}]},
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Index Terms"}], "level": 2},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Keyword list."}]},
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "1 Introduction"}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Introduction body."}]},
        },
    ]]


def test_abstract_and_keywords_headings_tag_following_paragraphs():
    ir = blocks_to_ir(_front_matter_pages())
    assert [block.role for block in ir] == [
        "body",          # paper title
        "abstract",      # Abstract heading
        "abstract",      # abstract body
        "keywords",      # Index Terms heading
        "keywords",      # keyword list
        "body",          # 1 Introduction
        "body",          # introduction body
    ]


def test_references_heading_and_entries_get_their_own_roles():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Body paragraph."}]},
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "References"}], "level": 1},
        },
        {
            "type": "list",
            "content": {
                "list_type": "reference_list",
                "list_items": [{
                    "item_type": "text",
                    "item_content": [{"type": "text", "content": "[1] An entry."}],
                }],
            },
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Appendix A"}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Appendix body."}]},
        },
    ]]
    ir = blocks_to_ir(pages)
    roles = [block.role for block in ir]
    assert roles[2] == "reference_heading"
    assert roles[3] == "reference_entry"
    assert roles[4] == "appendix"
    assert roles[5] == "appendix"
    # The reference heading translates; its entries do not.
    assert translatable_mask(ir) == [True, True, True, False, True, True]


def test_affiliation_paragraph_after_authors_is_its_own_role():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Jane Doe"}]},
        },
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [{
                    "type": "text",
                    "content": "Department of Computer Science, Example University",
                }]
            },
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "1 Introduction"}], "level": 1},
        },
    ]]
    ir = blocks_to_ir(pages)
    roles = [block.role for block in ir]
    assert roles[1] == "author"
    assert roles[2] == "affiliation"
    # Author names stay in the source language; affiliations are translated.
    assert translatable_mask(ir) == [True, False, True, True]


def test_caption_source_text_and_bbox_survive_content_list_parsing(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as pdf_canvas

    from app.services.layout_model import measure_pages

    source = tmp_path / "captions.pdf"
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 700, "Paper")
    canvas.setFont("Helvetica", 8)
    canvas.drawString(72, 400, "Figure 1. A caption that sits under the artwork.")
    canvas.drawString(72, 300, "Table 1. Results for every method considered.")
    canvas.drawString(72, 288, "Note: all values are means over five runs.")
    canvas.showPage()
    canvas.save()

    pages = [[
        {
            "type": "title",
            "bbox": [72, 92, 400, 122],
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "image",
            "bbox": [118, 590, 653, 980],
            "content": {
                "image_source": {"path": "images/f1.jpg"},
                "image_caption": [{"type": "text", "content": "Figure 1. A caption that sits under the artwork."}],
            },
        },
        {
            "type": "table",
            "bbox": [118, 405, 653, 490],
            "content": {
                "image_source": {"path": "images/t1.jpg"},
                "table_caption": [{"type": "text", "content": "Table 1. Results for every method considered."}],
                "table_footnote": [{"type": "text", "content": "Note: all values are means over five runs."}],
            },
        },
    ]]
    frames = measure_pages(source)
    assert frames[0].has_text_layer
    ir = blocks_to_ir(pages, [(frames[0].width, frames[0].height)], frames=frames)
    figure = ir[1]
    table = ir[2]
    assert isinstance(figure, Image)
    assert isinstance(table, Table)
    assert figure.caption == "Figure 1. A caption that sits under the artwork."
    assert figure.caption_bbox is not None
    assert table.caption == (
        "Table 1. Results for every method considered. "
        "Note: all values are means over five runs."
    )
    assert table.caption_bbox is not None
    # The caption's own box is recovered from the page, not copied from the
    # artwork box.
    assert figure.caption_bbox != figure.bbox
    assert table.caption_bbox != table.bbox
    assert 0 <= figure.caption_bbox[1] < figure.caption_bbox[3] <= frames[0].height
    assert 0 <= table.caption_bbox[1] < table.caption_bbox[3] <= frames[0].height
    # The translation slot exists but is empty until the caption is translated.
    assert figure.translated_caption == ""
    assert table.translated_caption == ""


def test_caption_source_text_and_bbox_survive_middle_json_parsing():
    middle = {
        "pdf_info": [
            {
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    {
                        "type": "title",
                        "bbox": [72, 100, 540, 130],
                        "lines": [{
                            "bbox": [72, 100, 540, 130],
                            "spans": [{"bbox": [72, 100, 540, 130], "type": "text", "content": "Paper"}],
                        }],
                    },
                    {
                        "type": "image",
                        "bbox": [72, 300, 400, 500],
                        "blocks": [{
                            "type": "image_caption",
                            "bbox": [72, 280, 400, 300],
                            "lines": [{
                                "bbox": [72, 280, 400, 300],
                                "spans": [{"bbox": [72, 280, 400, 300], "type": "text", "content": "Figure 1. A caption."}],
                            }],
                        }],
                    },
                ],
            }
        ]
    }
    blocks, _frames = pages_from_middle(middle)
    figure = blocks[1]
    assert isinstance(figure, Image)
    assert figure.caption == "Figure 1. A caption."
    assert figure.caption_bbox == (72.0, 492.0, 400.0, 512.0)
    assert figure.translated_caption == ""


def test_roles_default_to_body_for_hand_built_ir():
    title = Title(level=1, text="Paper")
    paragraph = Paragraph(runs=[TextRun("Body.")])
    listing = ListBlock(list_type="", items=[[TextRun("Item")]])
    image = Image(rel_path="f.jpg", caption="Figure 1.")
    table = Table(caption="Table 1.")
    assert title.role == "body"
    assert paragraph.role == "body"
    assert listing.role == "body"
    assert image.translated_caption == ""
    assert table.translated_caption == ""
    assert collect_translatable_strings([title, paragraph, listing]) == [
        "Paper",
        "Body.",
        "Item",
    ]
