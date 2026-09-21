"""Deleting a document also removes the artifacts derived from it."""

from pathlib import Path

from app.core.config import settings
from app.models.store import DocumentRecord, save_document


def _document(document_id: str, source: Path) -> DocumentRecord:
    return save_document(
        DocumentRecord(
            document_id=document_id,
            source_type="pdf",
            source_path=source,
            source_filename=f"{document_id}.pdf",
            status="done",
        )
    )


def _artifacts(document_id: str) -> Path:
    """The per-document output directory with everything the pipeline wrote."""
    output_dir = settings.output_dir / document_id
    (output_dir / "mineru" / "images").mkdir(parents=True, exist_ok=True)
    (output_dir / "figure-previews").mkdir(parents=True, exist_ok=True)
    (output_dir / "formula-crops").mkdir(parents=True, exist_ok=True)
    (output_dir / "original.pdf").write_bytes(b"%PDF-1.4\n")
    (output_dir / f"{document_id}_Chinese_ver.pdf").write_bytes(b"%PDF-1.4\n")
    (output_dir / f"{document_id}_原文标注.pdf").write_bytes(b"%PDF-1.4\n")
    (output_dir / "layout-plan.json").write_text("{}", encoding="utf-8")
    (output_dir / "alignment.json").write_text("[]", encoding="utf-8")
    (output_dir / "extraction-checkpoint.json").write_text("{}", encoding="utf-8")
    (output_dir / "translation-checkpoint.json").write_text("{}", encoding="utf-8")
    (output_dir / "mineru" / "images" / "page_001_img_00.png").write_bytes(b"png")
    return output_dir


def _source(document_id: str) -> Path:
    source = settings.upload_dir / f"{document_id}_paper.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    return source


def test_delete_removes_the_document_artifacts(client, isolated_storage):
    document_id = "doc-purge"
    source = _source(document_id)
    _document(document_id, source)
    output_dir = _artifacts(document_id)
    assert output_dir.is_dir() and source.is_file()

    response = client.delete(f"/api/document/{document_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["document_id"] == document_id
    # The response names what was removed.
    assert any("output dir" in line for line in body["removed"])
    assert any("source" in line for line in body["removed"])

    # The derived artifacts and the uploaded source are gone.
    assert not output_dir.exists()
    assert not source.exists()
    # And the document no longer appears in the library.
    assert client.get(f"/api/document/{document_id}").status_code == 404
    assert all(
        item["document_id"] != document_id
        for item in client.get("/api/documents").json()
    )


def test_delete_leaves_other_documents_untouched(client, isolated_storage):
    kept_id = "doc-kept"
    kept_source = _source(kept_id)
    _document(kept_id, kept_source)
    kept_dir = _artifacts(kept_id)

    doomed_id = "doc-doomed"
    _document(doomed_id, _source(doomed_id))
    _artifacts(doomed_id)

    assert client.delete(f"/api/document/{doomed_id}").status_code == 200

    assert kept_dir.is_dir()
    assert (kept_dir / "original.pdf").is_file()
    assert kept_source.is_file()
    assert client.get(f"/api/document/{kept_id}").status_code == 200


def test_delete_is_idempotent_for_artifacts(client, isolated_storage):
    document_id = "doc-twice"
    _document(document_id, _source(document_id))
    output_dir = _artifacts(document_id)

    assert client.delete(f"/api/document/{document_id}").status_code == 200
    assert not output_dir.exists()
    # A second delete has nothing to remove and still reports the document gone.
    assert client.delete(f"/api/document/{document_id}").status_code == 404


def test_delete_tolerates_a_missing_output_directory(client, isolated_storage):
    document_id = "doc-no-artifacts"
    source = _source(document_id)
    _document(document_id, source)
    assert not (settings.output_dir / document_id).exists()

    assert client.delete(f"/api/document/{document_id}").status_code == 200
    assert not source.exists()
