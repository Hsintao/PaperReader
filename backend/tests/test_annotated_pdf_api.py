"""On-demand annotated-source PDF for documents parsed before the feature."""

from pathlib import Path

import pypdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings
from app.models.store import ArtifactEntry, DocumentRecord, save_document
from app.services.cjk_fonts import require_cjk_font
from app.services.document_pipeline import _save_extraction_checkpoint
from app.services.mineru_service import MinerUResult


def _source(path: Path) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 700, "Annotation Route Paper")
    canvas.showPage()
    canvas.save()


def _content_blocks() -> list:
    return [
        [
            {
                "type": "title",
                "bbox": [72, 92, 400, 122],
                "content": {
                    "title_content": [
                        {"type": "text", "content": "Annotation Route Paper"}
                    ],
                    "level": 1,
                },
            }
        ]
    ]


def _record(document_id: str, source: Path) -> DocumentRecord:
    return save_document(
        DocumentRecord(
            document_id=document_id,
            source_type="pdf",
            source_path=source,
            source_filename=f"{document_id}.pdf",
            status="done",
        )
    )


def _write_checkpoint(document_id: str, source) -> Path:
    output_dir = settings.output_dir / document_id
    output_dir.mkdir(parents=True, exist_ok=True)
    _save_extraction_checkpoint(
        output_dir / "extraction-checkpoint.json",
        source,
        MinerUResult(
            markdown="# Annotation Route Paper",
            mode_label="test",
            content_blocks=_content_blocks(),
            boxes_normalized=False,
        ),
    )
    return output_dir


def test_annotated_pdf_endpoint_builds_from_the_extraction_checkpoint(
    client, isolated_storage
):
    document_id = "doc-annot-pdf"
    source = settings.upload_dir / f"{document_id}.pdf"
    _source(source)
    _record(document_id, source)
    output_dir = _write_checkpoint(document_id, source)

    assert client.get(f"/api/document/{document_id}").json()["annotated_pdf_url"] is None

    response = client.post(f"/api/document/{document_id}/annotated-pdf")
    assert response.status_code == 200, response.text
    url = response.json()["annotated_pdf_url"]
    assert url.endswith("_原文标注.pdf")

    status = client.get(f"/api/document/{document_id}").json()
    assert status["annotated_pdf_url"] == url
    assert any(item["kind"] == "annotated_pdf" for item in status["artifacts"])

    annotated = output_dir / f"{document_id}_原文标注.pdf"
    assert annotated.is_file()
    assert len(pypdf.PdfReader(str(annotated)).pages) == 1


def test_annotated_pdf_endpoint_reports_missing_parse_cache(client, isolated_storage):
    document_id = "doc-annot-missing"
    source = settings.upload_dir / f"{document_id}.pdf"
    _source(source)
    _record(document_id, source)

    response = client.post(f"/api/document/{document_id}/annotated-pdf")
    assert response.status_code == 409


def test_annotation_from_an_older_revision_is_rebuilt_not_served(
    client, isolated_storage
):
    document_id = "doc-annot-stale"
    source = settings.upload_dir / f"{document_id}.pdf"
    _source(source)
    record = _record(document_id, source)
    output_dir = _write_checkpoint(document_id, source)
    stale = output_dir / f"{document_id}_原文标注.pdf"
    stale.write_bytes(b"%PDF-1.4\n% annotated by an earlier revision\n")
    record.artifacts.append(
        ArtifactEntry(
            name=stale.name,
            kind="annotated_pdf",
            path=str(stale),
            url=f"/data/outputs/{document_id}/{stale.name}",
        )
    )
    save_document(record)

    # An artifact from an older annotation style is not offered to the reader.
    assert client.get(f"/api/document/{document_id}").json()["annotated_pdf_url"] is None

    response = client.post(f"/api/document/{document_id}/annotated-pdf")
    assert response.status_code == 200, response.text

    status = client.get(f"/api/document/{document_id}").json()
    assert status["annotated_pdf_url"] == response.json()["annotated_pdf_url"]
    assert len(pypdf.PdfReader(str(stale)).pages) == 1


def test_document_status_exposes_structured_layout_issues(client, isolated_storage):
    document_id = "doc-layout-issues"
    source = settings.upload_dir / f"{document_id}.pdf"
    _source(source)
    record = _record(document_id, source)

    # Documents produced before V2 report an empty list rather than failing.
    assert client.get(f"/api/document/{document_id}").json()["layout_issues"] == []

    record.metadata["layout_issues"] = [
        {
            "kind": "page_original",
            "page": 4,
            "block_kind": "page",
            "message": "translation does not fit at the minimum font size",
        },
        {
            "kind": "cell_original",
            "page": 2,
            "block_kind": "table_cell",
            "message": "cell translation does not fit",
        },
    ]
    save_document(record)

    issues = client.get(f"/api/document/{document_id}").json()["layout_issues"]
    assert [issue["kind"] for issue in issues] == ["page_original", "cell_original"]
    assert issues[0]["page"] == 4
    assert issues[0]["block_kind"] == "page"
    assert issues[1]["message"] == "cell translation does not fit"


def test_document_status_reports_every_issue_kind(client, isolated_storage):
    document_id = "doc-layout-issue-kinds"
    source = settings.upload_dir / f"{document_id}.pdf"
    _source(source)
    record = _record(document_id, source)
    record.metadata["layout_issues"] = [
        {
            "kind": "block_original",
            "page": 1,
            "block_kind": "paragraph",
            "message": "1 of 2 piece(s) could not be translated after repeated retries",
        },
        {
            "kind": "cell_original",
            "page": 2,
            "block_kind": "table_cell",
            "message": "cell translation does not fit",
        },
        {
            "kind": "caption_original",
            "page": 3,
            "block_kind": "figure_caption",
            "message": "caption translation does not fit at the minimum font size",
        },
        {
            "kind": "page_original",
            "page": 4,
            "block_kind": "page",
            "message": "body text does not fit inside its column at the minimum font size of 6pt",
        },
    ]
    save_document(record)

    issues = client.get(f"/api/document/{document_id}").json()["layout_issues"]
    assert [issue["page"] for issue in issues] == [1, 2, 3, 4]
    assert {issue["kind"] for issue in issues} == {
        "block_original",
        "cell_original",
        "caption_original",
        "page_original",
    }
    for issue in issues:
        assert set(issue) == {"kind", "page", "block_kind", "message"}
        assert isinstance(issue["page"], int) and issue["page"] >= 1
