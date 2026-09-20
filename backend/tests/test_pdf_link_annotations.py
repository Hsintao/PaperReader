"""Translated pages keep their links but never their coloured link borders."""

import pypdf
import pytest
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from app.services import layout_render, pdf_ops


def _rect(x0, y0, x1, y1) -> ArrayObject:
    return ArrayObject(
        [
            NumberObject(x0),
            NumberObject(y0),
            NumberObject(x1),
            NumberObject(y1),
        ]
    )


def _link(rect, *, color, action_kind: str, page_ref, width: float = 1.0):
    annotation = DictionaryObject()
    annotation[NameObject("/Type")] = NameObject("/Annot")
    annotation[NameObject("/Subtype")] = NameObject("/Link")
    annotation[NameObject("/Rect")] = rect
    annotation[NameObject("/Border")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(width)]
    )
    annotation[NameObject("/BS")] = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Border"),
            NameObject("/S"): NameObject("/S"),
            NameObject("/W"): NumberObject(width),
        }
    )
    annotation[NameObject("/C")] = ArrayObject(
        [NumberObject(value) for value in color]
    )
    if action_kind == "dest":
        annotation[NameObject("/Dest")] = ArrayObject(
            [page_ref, NameObject("/Fit")]
        )
    else:
        annotation[NameObject("/A")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): pypdf.generic.TextStringObject(
                    "https://example.org/paper"
                ),
            }
        )
    return annotation


def _annotated_pdf(path):
    """A one-page PDF with a red internal link, a green URI link and a highlight."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.pages[0]
    page_ref = page.indirect_reference
    highlight = DictionaryObject()
    highlight[NameObject("/Type")] = NameObject("/Annot")
    highlight[NameObject("/Subtype")] = NameObject("/Highlight")
    highlight[NameObject("/Rect")] = _rect(72, 600, 200, 620)
    highlight[NameObject("/C")] = ArrayObject([NumberObject(1), NumberObject(1), NumberObject(0)])
    page[NameObject("/Annots")] = ArrayObject(
        [
            writer._add_object(
                _link(_rect(72, 700, 200, 712), color=(1, 0, 0), action_kind="dest", page_ref=page_ref)
            ),
            writer._add_object(
                _link(_rect(72, 660, 220, 672), color=(0, 1, 0), action_kind="uri", page_ref=page_ref)
            ),
            writer._add_object(highlight),
        ]
    )
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _two_page_link_pdf(path):
    """A translatable first page followed by a page carrying coloured links."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as pdf_canvas

    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 700, "First page line.")
    canvas.showPage()
    canvas.showPage()
    canvas.save()

    writer = pypdf.PdfWriter(clone_from=str(path))
    page = writer.pages[1]
    page_ref = page.indirect_reference
    highlight = DictionaryObject()
    highlight[NameObject("/Type")] = NameObject("/Annot")
    highlight[NameObject("/Subtype")] = NameObject("/Highlight")
    highlight[NameObject("/Rect")] = _rect(72, 600, 200, 620)
    highlight[NameObject("/C")] = ArrayObject(
        [NumberObject(1), NumberObject(1), NumberObject(0)]
    )
    page[NameObject("/Annots")] = ArrayObject(
        [
            writer._add_object(
                _link(
                    _rect(72, 700, 200, 712),
                    color=(1, 0, 0),
                    action_kind="dest",
                    page_ref=page_ref,
                )
            ),
            writer._add_object(
                _link(
                    _rect(72, 660, 220, 672),
                    color=(0, 1, 0),
                    action_kind="uri",
                    page_ref=page_ref,
                )
            ),
            writer._add_object(highlight),
        ]
    )
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _translated_plan():
    from app.services.layout_fit import BlockPlan, Fragment, PagePlan

    plan = PagePlan(index=0, width=612, height=792)
    plan.blocks.append(
        BlockPlan(
            kind="paragraph",
            page_index=0,
            source_rect=(72.0, 692.0, 320.0, 706.0),
            target=(72.0, 692.0, 320.0, 706.0),
            fragments=[Fragment(kind="text", text="第一页译文。")],
            size=10.5,
            baseline_size=10.5,
            leading=15.0,
            source_text="First page line.",
        )
    )
    return plan


def _failed_plan():
    from app.services.layout_fit import PagePlan

    plan = PagePlan(index=1, width=612, height=792)
    plan.status = "original"
    plan.reason = "rotated page"
    return plan


def _links(page) -> list[DictionaryObject]:
    annots = page.get("/Annots") or []
    out = []
    for reference in annots:
        annotation = reference.get_object()
        if annotation.get("/Subtype") == "/Link":
            out.append(annotation)
    return out


def test_hide_link_borders_keeps_navigation_and_drops_the_border(tmp_path):
    source = _annotated_pdf(tmp_path / "links.pdf")
    reader = pypdf.PdfReader(str(source))
    before = _links(reader.pages[0])
    assert len(before) == 2
    rects = [list(link["/Rect"]) for link in before]
    assert [list(link["/Border"]) for link in before] == [[0, 0, 1.0], [0, 0, 1.0]]

    writer = pypdf.PdfWriter()
    page = writer.add_page(reader.pages[0])
    assert pdf_ops.hide_link_borders(page) == 2

    after = _links(page)
    assert len(after) == 2
    assert [list(link["/Rect"]) for link in after] == rects
    assert "/A" in after[0] or "/Dest" in after[0]
    assert "/A" in after[1] or "/Dest" in after[1]
    for link in after:
        assert list(link["/Border"]) == [0, 0, 0]
        assert float(link["/BS"]["/W"]) == 0.0
        assert "/C" not in link


def test_hide_link_borders_leaves_non_link_annotations_alone(tmp_path):
    source = _annotated_pdf(tmp_path / "links.pdf")
    reader = pypdf.PdfReader(str(source))
    writer = pypdf.PdfWriter()
    page = writer.add_page(reader.pages[0])
    pdf_ops.hide_link_borders(page)

    annots = [reference.get_object() for reference in page["/Annots"]]
    highlight = next(item for item in annots if item.get("/Subtype") == "/Highlight")
    assert list(highlight["/C"]) == [1, 1, 0]
    assert list(highlight["/Rect"]) == [72, 600, 200, 620]


def test_rendered_translated_pages_have_no_visible_link_border(tmp_path):
    """Every output page is normalized, including original fallback pages."""
    source = _two_page_link_pdf(tmp_path / "links.pdf")
    output = tmp_path / "out.pdf"
    from app.services.layout_model import measure_pages

    frames = measure_pages(source)
    crops = layout_render.FormulaCrops(source, tmp_path / "c")
    try:
        report = layout_render.render_document(
            source_pdf=source,
            plans=[_translated_plan(), _failed_plan()],
            frames=frames,
            blocks=[],
            output_pdf=output,
            crops=crops,
        )
    finally:
        crops.close()
    # One translated page and one original fallback page; the fallback page's
    # links are normalized too.
    assert [page.status for page in report.pages] == ["ok", "original"]
    assert any("link annotation" in note for note in report.notes)

    reader = pypdf.PdfReader(str(output))
    assert len(reader.pages) == 2
    links = _links(reader.pages[1])
    assert len(links) == 2
    for link in links:
        assert list(link["/Border"]) == [0, 0, 0]
        assert float(link["/BS"]["/W"]) == 0.0
        assert "/C" not in link
    # The source file itself is untouched.
    original = pypdf.PdfReader(str(source))
    assert _links(original.pages[0]) == []
    assert list(_links(original.pages[1])[0]["/C"]) == [1, 0, 0]
    assert list(_links(original.pages[1])[0]["/Border"]) == [0, 0, 1.0]


def test_link_border_normalization_is_idempotent(tmp_path):
    source = _annotated_pdf(tmp_path / "links.pdf")
    reader = pypdf.PdfReader(str(source))
    writer = pypdf.PdfWriter()
    page = writer.add_page(reader.pages[0])
    assert pdf_ops.hide_link_borders(page) == 2
    assert pdf_ops.hide_link_borders(page) == 2
    for link in _links(page):
        assert list(link["/Border"]) == [0, 0, 0]
        assert float(link["/BS"]["/W"]) == 0.0


def test_page_without_annots_is_untouched(tmp_path):
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    assert pdf_ops.hide_link_borders(page) == 0


def test_link_only_appearance_stream_is_removed(tmp_path):
    source = _annotated_pdf(tmp_path / "links.pdf")
    reader = pypdf.PdfReader(str(source))
    writer = pypdf.PdfWriter()
    page = writer.add_page(reader.pages[0])
    for link in _links(page):
        link[NameObject("/AP")] = DictionaryObject(
            {NameObject("/N"): writer._add_object(_appearance(writer))}
        )
    pdf_ops.hide_link_borders(page)
    for link in _links(page):
        assert "/AP" not in link


def _appearance(writer):
    from pypdf.generic import DecodedStreamObject

    stream = DecodedStreamObject()
    stream.set_data(b"0 0 0 RG 1 w 0 0 100 12 re S")
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(12)]
            ),
        }
    )
    return stream
