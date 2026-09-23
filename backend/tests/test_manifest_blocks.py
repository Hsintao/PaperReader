"""Manifest blocks: artwork lists, table captions and logical objects.

The worker converts BabelDOC's debug layout into the manifest. These tests feed
it a hand-written layout that has the shapes a real paper produces — a table
whose caption sits above its cells, an isolated formula that is also a layout
region, and a layout region that spans paragraphs on two pages — and check what
the reader receives.
"""

from __future__ import annotations

import json
from pathlib import Path

from workers.pdfmathtranslate.manifest import build_manifest


def _box(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"x": x0, "y": y0, "x2": x1, "y2": y1}


def _paragraph(
    debug_id: str,
    label: str,
    bbox: dict,
    text: str,
    layout_id: int | None,
) -> dict:
    return {
        "debug_id": debug_id,
        "layout_label": label,
        "box": bbox,
        "unicode": text,
        "layout_id": layout_id,
    }


def _write_debug(debug_dir: Path, pages: list[dict], translated: list[list[dict]]) -> Path:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "paragraph_finder.json").write_text(
        json.dumps({"page": pages}, ensure_ascii=False), encoding="utf-8"
    )
    (debug_dir / "il_translated.json").write_text(
        json.dumps(
            {"page": [{"pdf_paragraph": items} for items in translated]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return debug_dir


def _manifest(debug_dir: Path) -> dict:
    return build_manifest(debug_dir, source_pdf=Path("/papers/paper.pdf"))


def _blocks(manifest: dict, page_index: int) -> list[dict]:
    return manifest["pages"][page_index]["blocks"]


def _table_page() -> dict:
    """One page holding a captioned table, a figure and an isolated formula."""
    return {
        "mediabox": {"box": _box(0, 0, 612, 792)},
        "page_layout": [
            {"id": 1, "class_name": "isolate_formula", "box": _box(150, 320, 450, 380), "conf": 0.9},
            {"id": 2, "class_name": "table", "box": _box(60, 400, 550, 600), "conf": 0.9},
            {"id": 3, "class_name": "figure", "box": _box(60, 60, 550, 300), "conf": 0.9},
        ],
        "pdf_paragraph": [
            _paragraph("eq", "isolate_formula", _box(150, 320, 450, 380), "E = mc^2", 1),
            _paragraph("h1", "table_cell", _box(70, 560, 180, 585), "Method", 2),
            _paragraph("h2", "table_cell", _box(200, 560, 320, 585), "Training data", 2),
            _paragraph("r1", "table_cell", _box(70, 520, 180, 545), "Ours", 2),
            _paragraph(
                "cap", "table_caption", _box(60, 610, 400, 640), "Table 1: Method comparison", 2
            ),
            _paragraph("fig", "figure_caption", _box(60, 40, 400, 58), "Figure 1: Overview", 3),
        ],
    }


def test_a_table_keeps_its_caption_while_its_cells_hold_the_body(tmp_path):
    debug = _write_debug(
        tmp_path / "debug",
        [_table_page()],
        [[
            {"debug_id": "eq", "unicode": "E = mc^2"},
            {"debug_id": "cap", "unicode": "表 1：方法比较"},
            {"debug_id": "fig", "unicode": "图 1：概览"},
        ]],
    )

    manifest = _manifest(debug)

    assert [table["caption"] for table in manifest["tables"]] == ["Table 1: Method comparison"]
    assert [table["translated_caption"] for table in manifest["tables"]] == ["表 1：方法比较"]
    table_block = next(
        block for block in _blocks(manifest, 0) if block["kind"] == "table"
    )
    assert table_block["source_text"] == "Table 1: Method comparison"
    assert table_block["translated_text"] == "表 1：方法比较"
    assert [cell["text"] for row in table_block["table"]["rows"] for cell in row] == [
        "Method",
        "Training data",
        "Ours",
    ]
    assert [cell["bbox"] for row in table_block["table"]["rows"] for cell in row][0] == [
        70.0,
        560.0,
        180.0,
        585.0,
    ]
    # Rows run top to bottom, like the page they were read from.
    assert table_block["table_html"].index("Method") < table_block["table_html"].index("Ours")
    assert [item["source_text"] for item in table_block["captions"]] == [
        "Table 1: Method comparison"
    ]


def test_formulas_stay_in_the_body_and_only_real_artwork_is_listed(tmp_path):
    debug = _write_debug(
        tmp_path / "debug",
        [_table_page()],
        [[
            {"debug_id": "eq", "unicode": "E = mc^2"},
            {"debug_id": "cap", "unicode": "表 1：方法比较"},
            {"debug_id": "fig", "unicode": "图 1：概览"},
        ]],
    )

    manifest = _manifest(debug)

    assert [figure["label"] for figure in manifest["figures"]] == ["figure"]
    assert [table["label"] for table in manifest["tables"]] == ["table"]
    kinds = [block["kind"] for block in _blocks(manifest, 0)]
    # The isolated formula is a body block exactly once: the layout region that
    # also covers it must not add a second, empty one.
    assert kinds.count("formula") == 1
    assert [block["source_text"] for block in _blocks(manifest, 0) if block["kind"] == "formula"] == [
        "E = mc^2"
    ]


def test_a_layout_region_groups_the_fragments_it_produced(tmp_path):
    page = {
        "mediabox": {"box": _box(0, 0, 612, 792)},
        "page_layout": [],
        "pdf_paragraph": [
            _paragraph("a", "plain text", _box(60, 700, 550, 760), "First part.", 7),
            _paragraph("b", "plain text", _box(60, 660, 550, 690), "Second part.", 7),
            _paragraph("c", "plain text", _box(60, 600, 550, 640), "Other region.", 8),
        ],
    }
    debug = _write_debug(
        tmp_path / "debug",
        [page],
        [[
            {"debug_id": "a", "unicode": "第一段。"},
            {"debug_id": "b", "unicode": "第二段。"},
            {"debug_id": "c", "unicode": "另一个区域。"},
        ]],
    )

    manifest = _manifest(debug)
    blocks = _blocks(manifest, 0)

    assert [block["fragment_id"] for block in blocks] == ["p0-b0", "p0-b1", "p0-b2"]
    assert [block["logical_id"] for block in blocks] == ["p0-l7", "p0-l7", "p0-l8"]
    assert blocks[0]["fragments"] == ["p0-b0", "p0-b1"]
    assert blocks[1]["fragments"] == ["p0-b0", "p0-b1"]
    assert blocks[2]["fragments"] == ["p0-b2"]
    assert manifest["logical_objects"] == [
        {"id": "p0-l7", "fragments": ["p0-b0", "p0-b1"]},
        {"id": "p0-l8", "fragments": ["p0-b2"]},
    ]


def test_the_same_layout_number_on_two_pages_is_two_objects(tmp_path):
    """BabelDOC numbers its layout regions from one on every page."""
    page = {
        "mediabox": {"box": _box(0, 0, 612, 792)},
        "page_layout": [],
        "pdf_paragraph": [
            _paragraph("x", "plain text", _box(60, 700, 550, 760), "On page one.", 3),
        ],
    }
    second = {
        "mediabox": {"box": _box(0, 0, 612, 792)},
        "page_layout": [],
        "pdf_paragraph": [
            _paragraph("y", "plain text", _box(60, 700, 550, 760), "On page two.", 3),
        ],
    }
    debug = _write_debug(
        tmp_path / "debug",
        [page, second],
        [
            [{"debug_id": "x", "unicode": "第一页。"}],
            [{"debug_id": "y", "unicode": "第二页。"}],
        ],
    )

    manifest = _manifest(debug)

    assert [block["logical_id"] for block in _blocks(manifest, 0)] == ["p0-l3"]
    assert [block["logical_id"] for block in _blocks(manifest, 1)] == ["p1-l3"]
    assert [block["fragments"] for block in _blocks(manifest, 0)] == [["p0-b0"]]
    assert [block["fragments"] for block in _blocks(manifest, 1)] == [["p1-b0"]]
    assert manifest["logical_objects"] == [
        {"id": "p0-l3", "fragments": ["p0-b0"]},
        {"id": "p1-l3", "fragments": ["p1-b0"]},
    ]


def test_a_block_without_an_upstream_region_is_its_own_fragment(tmp_path):
    page = {
        "mediabox": {"box": _box(0, 0, 612, 792)},
        "page_layout": [],
        "pdf_paragraph": [
            _paragraph("x", "plain text", _box(60, 700, 550, 760), "Loose text.", None),
        ],
    }
    debug = _write_debug(
        tmp_path / "debug",
        [page],
        [[{"debug_id": "x", "unicode": "散落文本。"}]],
    )

    manifest = _manifest(debug)
    block = _blocks(manifest, 0)[0]

    assert block["logical_id"] == "p0-b0"
    assert block["fragments"] == ["p0-b0"]
    assert manifest["logical_objects"] == []
