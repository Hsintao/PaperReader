"""Bibliography extraction from the manifest.

The layout model does not label every bibliography paragraph, so references are
found from the section heading and each entry from its own marker. A page holds
a whole column of them in one block, and the facing column often holds a figure
that is not part of the bibliography.
"""

from __future__ import annotations

from workers.pdfmathtranslate.manifest import _references_after_heading


def _block(kind: str, text: str, *, translated: str = "") -> dict:
    return {
        "kind": kind,
        "layout_label": "title" if kind == "title" else "plain text",
        "bbox": [50.0, 80.0, 290.0, 700.0],
        "source_text": text,
        "translated_text": translated,
    }


def _page(index: int, blocks: list[dict]) -> dict:
    return {"index": index, "width": 612.0, "height": 792.0, "blocks": blocks}


def test_entries_are_split_at_their_markers_and_paired_with_their_translation():
    pages = [
        _page(8, [
            _block("title", "References", translated="参考文献"),
            _block(
                "paragraph",
                "[1] J. Canny. A computational approach to edge detection. 1986. 2 "
                "[2] R. T. Collins. A system for video surveillance. 2000. 3",
                translated="[1] J. Canny。一种边缘检测的计算方法。 [2] R. T. Collins。一套视频监控系统。",
            ),
        ]),
    ]

    references = _references_after_heading(pages)

    assert [item["text"].split()[0] for item in references] == ["[1]", "[2]"]
    assert references[0]["translated"].startswith("[1] J. Canny")
    assert references[1]["translated"].startswith("[2] R. T. Collins")
    assert all(item["page_index"] == 8 for item in references)


def test_the_heading_opens_the_section_and_a_later_heading_closes_it():
    pages = [
        _page(3, [
            _block("title", "References"),
            _block("paragraph", "[7] Earlier entry."),
            _block("title", "Appendix A"),
            _block("paragraph", "[8] Not a reference."),
        ]),
    ]

    references = _references_after_heading(pages)

    assert [item["text"] for item in references] == ["[7] Earlier entry."]


def test_figure_captions_beside_the_bibliography_are_not_read_as_entries():
    pages = [
        _page(7, [
            _block("title", "References"),
            _block("paragraph", "[1] J. Canny. Edge detection. 1986."),
            _block("figure", "Figure 7. Qualitative results", translated="图 7。定性结果"),
            _block("paragraph", "[2] R. T. Collins. Video surveillance. 2000."),
        ]),
    ]

    references = _references_after_heading(pages)

    assert [item["text"].split()[0] for item in references] == ["[1]", "[2]"]


def test_an_entry_straddling_a_block_boundary_keeps_its_beginning():
    pages = [
        _page(7, [
            _block("title", "References"),
            _block("paragraph", "[1] J. Canny. Edge detection. 1986. [2] R. T. Collins, A. J."),
        ]),
        _page(8, [
            _block(
                "paragraph",
                "Lipton, T. Kanade. Video surveillance. 2000. [3] J. Dai. Deformable convolutions. 2017.",
            ),
        ]),
    ]

    references = _references_after_heading(pages)

    assert [item["text"].split()[0] for item in references] == ["[1]", "[2]", "[3]"]
    assert "Video surveillance" in references[1]["text"]
    assert references[2]["page_index"] == 8


def test_a_document_without_a_bibliography_heading_yields_nothing():
    pages = [
        _page(0, [
            _block("title", "1. Introduction"),
            _block("paragraph", "See [1] for details."),
        ]),
    ]

    assert _references_after_heading(pages) == []
