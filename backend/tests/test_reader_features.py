from pathlib import Path

import json

from app.models.store import DocumentRecord, save_document


def _done_document(document_id: str, extracted: str, translated: str) -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        source_type="pdf",
        source_path=Path("/nonexistent") / f"{document_id}.pdf",
        source_filename=f"{document_id}.pdf",
        status="done",
        extracted_text=extracted,
        translated_text=translated,
    )
    return save_document(record)


def test_annotation_crud(client):
    record = _done_document("doc-annot", "Original text", "译文")

    response = client.post(
        "/api/document/doc-annot/annotations",
        json={"page": 3, "quote": "Original text", "color": "green", "note": "记得回看", "position_ratio": 0.5},
    )
    assert response.status_code == 201, response.text
    annotation = response.json()
    assert annotation["color"] == "green"
    assert annotation["note"] == "记得回看"

    listed = client.get("/api/document/doc-annot/annotations")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    deleted = client.delete(f"/api/document/doc-annot/annotations/{annotation['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/document/doc-annot/annotations").json() == []

    assert record.document_id


def test_empty_quote_rejected(client):
    _done_document("doc-empty", "text", "译文")
    response = client.post(
        "/api/document/doc-empty/annotations",
        json={"page": 1, "quote": "   ", "color": "yellow"},
    )
    assert response.status_code == 400


def test_reading_progress_roundtrip(client):
    _done_document("doc-progress", "text", "译文")

    saved = client.patch("/api/document/doc-progress/progress", json={"page": 7, "ratio": 0.42})
    assert saved.status_code == 200

    status = client.get("/api/document/doc-progress").json()
    assert status["last_read_page"] == 7
    assert abs(status["last_read_ratio"] - 0.42) < 1e-6

    # Out-of-range values are clamped, not rejected.
    client.patch("/api/document/doc-progress/progress", json={"page": -3, "ratio": 5})
    status = client.get("/api/document/doc-progress").json()
    assert status["last_read_page"] == 0
    assert status["last_read_ratio"] == 1.0


def test_notes_export_includes_annotation_and_counterpart(client):
    from app.core.config import settings
    from app.services.alignment_service import save_exact_alignment

    record = _done_document(
        "doc-notes",
        "The Navier-Stokes equations describe fluid motion. Here is a long enough sentence for alignment.",
        "translated: The Navier-Stokes equations describe fluid motion. Here is a long enough sentence for alignment.",
    )
    save_exact_alignment(
        record,
        ["The Navier-Stokes equations describe fluid motion."],
        ["纳维-斯托克斯方程描述流体运动。"],
    )
    assert (settings.output_dir / record.document_id / "alignment.json").is_file()

    empty = client.get("/api/document/doc-notes/notes.md")
    assert empty.status_code == 404

    created = client.post(
        "/api/document/doc-notes/annotations",
        json={"page": 1, "quote": "The Navier-Stokes equations describe fluid motion.", "color": "blue", "note": "核心方程", "position_ratio": 0.0},
    )
    assert created.status_code == 201, created.text

    exported = client.get("/api/document/doc-notes/notes.md")
    assert exported.status_code == 200
    body = exported.text
    assert "The Navier-Stokes equations" in body
    assert "核心方程" in body
    assert "纳维-斯托克斯方程" in body


def _structure_manifest() -> dict:
    return {
        "schema_version": "paperreader-manifest-v1",
        "generator": {"worker": "pdfmathtranslate"},
        "source_pdf": "doc-struct.pdf",
        "source_sha256": "0" * 64,
        "mode_label": "PDFMathTranslate-next 2.9.0 · mono",
        "page_count": 2,
        "unit": "point",
        "boxes_normalized": False,
        "pages": [
            {
                "index": 0,
                "width": 612.0,
                "height": 792.0,
                "blocks": [
                    {"kind": "title", "level": 1, "bbox": [50.0, 700.0, 500.0, 720.0],
                     "source_text": "1 Introduction", "translated_text": "1 引言"},
                    {"kind": "paragraph", "bbox": [50.0, 640.0, 500.0, 690.0],
                     "source_text": "Body text", "translated_text": "正文"},
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
        ],
        "figures": [{
            "page_index": 0,
            "bbox": [50.0, 300.0, 400.0, 500.0],
            "caption": "Figure 1: Overview",
            "translated_caption": "图 1 总览",
        }],
        "tables": [{
            "page_index": 1,
            "bbox": [50.0, 400.0, 500.0, 560.0],
            "caption": "Table 1: Results",
            "translated_caption": "表 1 结果",
        }],
        "references": [],
        "logical_objects": [],
        "glossary": [],
    }


def test_structure_endpoint_returns_outline_and_figures(client):
    from app.core.config import settings
    from pypdf import PdfWriter

    record = _done_document("doc-struct", "text", "译文")
    output_dir = settings.output_dir / record.document_id
    extraction = output_dir / "extraction"
    extraction.mkdir(parents=True, exist_ok=True)
    (extraction / "manifest.json").write_text(
        json.dumps(_structure_manifest(), ensure_ascii=False), encoding="utf-8"
    )
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    with (output_dir / "original.pdf").open("wb") as handle:
        writer.write(handle)
    record.original_pdf_url = f"/data/outputs/{record.document_id}/original.pdf"
    save_document(record)

    response = client.get("/api/document/doc-struct/structure")
    assert response.status_code == 200, response.text
    structure = response.json()
    assert [item["title"] for item in structure["outline"]] == ["1 引言", "2 方法"]
    kinds = [figure["kind"] for figure in structure["figures"]]
    assert kinds == ["figure", "table"]
    assert [figure["page"] for figure in structure["figures"]] == [1, 2]
    assert structure["figures"][0]["caption"] == "Figure 1: Overview"
    assert structure["figures"][0]["url"].startswith(
        f"/data/outputs/{record.document_id}/figure-previews/"
    )


def test_library_search_finds_text_and_snippet(client):
    _done_document(
        "doc-search-a",
        "first document text about vorticity confinement methods",
        "第一篇文档的译文内容",
    )
    _done_document(
        "doc-search-b",
        "second document unrelated",
        "译文提到 vorticity confinement 也出现",
    )

    response = client.get("/api/search", params={"q": "vorticity confinement"})
    assert response.status_code == 200, response.text
    hits = response.json()
    assert {hit["document_id"] for hit in hits} == {"doc-search-a", "doc-search-b"}
    by_side = {hit["side"]: hit for hit in hits}
    assert "vorticity" in by_side["original"]["snippet"]
    assert by_side["translated"]["snippet"]

    empty = client.get("/api/search", params={"q": "zzzznotfound"})
    assert empty.json() == []


def test_bibtex_export_from_metadata(client):
    record = _done_document("doc-bib", "text", "译文")
    from app.models.store import set_document_metadata

    set_document_metadata(
        record.document_id,
        {
            "title": "Attention Is All You Need",
            "authors": "Ashish Vaswani, Noam Shazeer",
            "year": "2017",
            "venue": "NeurIPS",
        },
    )
    response = client.get("/api/document/doc-bib/bibtex")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["bibtex"].startswith("@article{vaswani2017attention,")
    assert "Attention Is All You Need" in payload["bibtex"]
    assert payload["filename"].endswith(".bib")


def test_bibtex_without_metadata_returns_404(client):
    _done_document("doc-nobib", "text", "译文")
    assert client.get("/api/document/doc-nobib/bibtex").status_code == 404
