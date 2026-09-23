"""The reader's outline and figure gallery, built from the worker's manifest."""

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.core.config import settings
from app.models.store import DocumentRecord
from app.services.document_manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    load_document_manifest,
    load_manifest,
)
from app.services.document_structure import build_document_structure


def _manifest_payload(*, pages: list[dict], figures: list[dict] | None = None,
                      tables: list[dict] | None = None,
                      glossary: list[dict] | None = None) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": {"worker": "pdfmathtranslate"},
        "source_pdf": "paper.pdf",
        "source_sha256": "0" * 64,
        "mode_label": "PDFMathTranslate-next 2.9.0 · mono",
        "page_count": len(pages),
        "unit": "point",
        "boxes_normalized": False,
        "pages": pages,
        "figures": figures or [],
        "tables": tables or [],
        "references": [],
        "logical_objects": [],
        "glossary": glossary or [],
    }


def _write_manifest(document_id: str, payload: dict) -> Path:
    extraction = settings.output_dir / document_id / "extraction"
    extraction.mkdir(parents=True, exist_ok=True)
    path = extraction / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _record(document_id: str, **kwargs) -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        source_type="pdf",
        source_path=Path("/nonexistent") / f"{document_id}.pdf",
        source_filename=f"{document_id}.pdf",
        status="done",
        **kwargs,
    )


def _blank_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


_OUTLINE_PAGES = [
    {
        "index": 0,
        "width": 612.0,
        "height": 792.0,
        "blocks": [
            {"kind": "title", "level": 1, "bbox": [50.0, 700.0, 500.0, 720.0],
             "source_text": "1 Introduction", "translated_text": "1 引言"},
            {"kind": "paragraph", "bbox": [50.0, 640.0, 500.0, 690.0],
             "source_text": "Body text", "translated_text": "正文"},
            {"kind": "title", "level": 2, "bbox": [50.0, 600.0, 500.0, 620.0],
             "source_text": "1.1 Motivation", "translated_text": "1.1 动机"},
            {"kind": "title", "level": 2, "bbox": [50.0, 560.0, 500.0, 580.0],
             "source_text": "1.2 Scope", "translated_text": "1.2 范围"},
        ],
    },
    {
        "index": 1,
        "width": 612.0,
        "height": 792.0,
        "blocks": [
            {"kind": "title", "level": 1, "bbox": [50.0, 700.0, 500.0, 720.0],
             "source_text": "2 Method", "translated_text": "2 方法"},
        ],
    },
]


def test_outline_nests_the_manifest_titles(isolated_storage):
    _write_manifest("doc-outline", _manifest_payload(pages=_OUTLINE_PAGES))

    structure = build_document_structure(_record("doc-outline"))

    outline = structure["outline"]
    assert [item["title"] for item in outline] == ["1 引言", "2 方法"]
    assert [item["page_index"] for item in outline] == [0, 1]
    assert [item["title"] for item in outline[0]["items"]] == ["1.1 动机", "1.2 范围"]
    assert outline[1]["items"] == []


def test_gallery_lists_the_manifest_figures_and_tables_in_reading_order(isolated_storage):
    _write_manifest(
        "doc-gallery",
        _manifest_payload(
            pages=_OUTLINE_PAGES,
            figures=[{
                "page_index": 0,
                "bbox": [50.0, 300.0, 400.0, 500.0],
                "caption": "Figure 1. Architecture",
                "translated_caption": "图 1 架构",
                "logical_id": "p0-b5",
            }],
            tables=[{
                "page_index": 1,
                "bbox": [50.0, 400.0, 500.0, 560.0],
                "caption": "Table 1. Results",
                "translated_caption": "表 1 结果",
                "logical_id": "p1-b2",
            }],
        ),
    )

    structure = build_document_structure(_record("doc-gallery"))

    assert [item["kind"] for item in structure["figures"]] == ["figure", "table"]
    assert [item["caption"] for item in structure["figures"]] == [
        "Figure 1. Architecture",
        "Table 1. Results",
    ]
    assert [item["page"] for item in structure["figures"]] == [1, 2]


def test_gallery_crops_each_entry_from_the_source_page(isolated_storage):
    document_id = "doc-crops"
    _write_manifest(
        document_id,
        _manifest_payload(
            pages=_OUTLINE_PAGES,
            figures=[{
                "page_index": 0,
                "bbox": [50.0, 300.0, 400.0, 500.0],
                "caption": "Figure 1. Architecture",
                "translated_caption": "图 1 架构",
            }],
        ),
    )
    _blank_pdf(settings.output_dir / document_id / "original.pdf")
    record = _record(
        document_id,
        original_pdf_url=f"/data/outputs/{document_id}/original.pdf",
    )

    structure = build_document_structure(record)

    figure = structure["figures"][0]
    assert figure["url"].startswith(f"/data/outputs/{document_id}/figure-previews/")
    assert (settings.data_dir / figure["url"].removeprefix("/data/")).is_file()


def test_a_document_without_a_manifest_has_no_structure(isolated_storage):
    assert build_document_structure(_record("doc-missing")) == {"outline": [], "figures": []}


def test_a_manifest_without_pages_has_no_structure(isolated_storage):
    _write_manifest("doc-empty", _manifest_payload(pages=[]))

    assert build_document_structure(_record("doc-empty")) == {"outline": [], "figures": []}


def test_a_manifest_is_readable_by_the_reader(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(_manifest_payload(
            pages=_OUTLINE_PAGES,
            glossary=[{"source": "attention", "target": "注意力"}],
        )),
        encoding="utf-8",
    )

    manifest = load_document_manifest(path)

    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.page_count == 2
    assert manifest.glossary == [{"source": "attention", "target": "注意力"}]
    assert manifest.markdown("source").startswith("# 1 Introduction")


def test_a_missing_manifest_is_rejected(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "absent.json")


def test_a_manifest_that_is_not_json_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json at all", encoding="utf-8")

    with pytest.raises(ManifestError):
        load_manifest(path)


def test_a_manifest_that_is_not_an_object_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([{"page": 1}]), encoding="utf-8")

    with pytest.raises(ManifestError):
        load_manifest(path)


def test_a_manifest_from_an_unknown_schema_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    payload = _manifest_payload(pages=_OUTLINE_PAGES)
    payload["schema_version"] = "paperreader-manifest-v0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestError):
        load_manifest(path)


def test_a_manifest_without_pages_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    payload = _manifest_payload(pages=_OUTLINE_PAGES)
    payload.pop("pages")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestError):
        load_manifest(path)
