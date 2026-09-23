"""The side-by-side merge of the original and translated PDFs."""

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NullObject
from reportlab.pdfgen import canvas

from app.models.store import ArtifactEntry, save_document
from app.services.document_pipeline import create_document_record
from app.services.pdf_ops import build_side_by_side_pdf


def _write_labeled_pdf(path: Path, pages: int, size: tuple[float, float], label: str) -> Path:
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=size)
    for _ in range(pages):
        sheet.drawString(72, size[1] - 72, label)
        sheet.showPage()
    sheet.save()
    path.write_bytes(buffer.getvalue())
    return path


def test_merge_pairs_pages_and_keeps_both_text_layers(tmp_path):
    original = _write_labeled_pdf(tmp_path / "original.pdf", 2, (612, 792), "source text")
    translated = _write_labeled_pdf(tmp_path / "translated.pdf", 2, (612, 792), "translated text")
    output = tmp_path / "merged.pdf"

    assert build_side_by_side_pdf(original, translated, output) == 2

    merged = PdfReader(str(output))
    assert len(merged.pages) == 2
    box = merged.pages[0].mediabox
    assert float(box.width) == pytest.approx(612 * 2 + 12)
    assert float(box.height) == pytest.approx(792)
    text = merged.pages[0].extract_text()
    assert "source text" in text
    assert "translated text" in text


def test_merge_aligns_mismatched_page_sizes(tmp_path):
    original = _write_labeled_pdf(tmp_path / "original.pdf", 1, (612, 792), "source")
    translated = _write_labeled_pdf(tmp_path / "translated.pdf", 1, (500, 700), "target")
    output = tmp_path / "merged.pdf"

    build_side_by_side_pdf(original, translated, output)

    box = PdfReader(str(output)).pages[0].mediabox
    assert float(box.width) == pytest.approx(612 + 12 + 500)
    assert float(box.height) == pytest.approx(792)


def test_merge_pads_the_shorter_side(tmp_path):
    original = _write_labeled_pdf(tmp_path / "original.pdf", 3, (612, 792), "source")
    translated = _write_labeled_pdf(tmp_path / "translated.pdf", 2, (612, 792), "target")
    output = tmp_path / "merged.pdf"

    assert build_side_by_side_pdf(original, translated, output) == 3

    merged = PdfReader(str(output))
    assert len(merged.pages) == 3
    assert float(merged.pages[2].mediabox.width) == pytest.approx(612 * 2 + 12)


def test_merge_tolerates_null_annots(tmp_path):
    """BabelDOC writes ``/Annots null``, which used to crash the merge."""
    original = _write_labeled_pdf(tmp_path / "original.pdf", 1, (612, 792), "source")
    translated = _write_labeled_pdf(tmp_path / "translated.pdf", 1, (612, 792), "target")
    page = PdfReader(str(translated)).pages[0]
    page[NameObject("/Annots")] = NullObject()
    writer = PdfWriter()
    writer.add_page(page)
    with translated.open("wb") as handle:
        writer.write(handle)
    output = tmp_path / "merged.pdf"

    assert build_side_by_side_pdf(original, translated, output) == 1


def test_merged_pdf_endpoint_rebuilds_legacy_documents(client, tmp_path):
    source = _write_labeled_pdf(tmp_path / "paper.pdf", 1, (612, 792), "source text")
    record = create_document_record(source)
    translated = _write_labeled_pdf(tmp_path / "translated.pdf", 1, (612, 792), "target text")
    record.artifacts.append(
        ArtifactEntry(
            name="paper_Chinese_ver.pdf",
            kind="translated_pdf",
            path=str(translated),
            url="/data/outputs/paper_Chinese_ver.pdf",
        )
    )
    save_document(record)

    response = client.post(f"/api/document/{record.document_id}/merged-pdf")

    assert response.status_code == 200
    url = response.json()["merged_pdf_url"]
    assert url.endswith("_双语对照.pdf")
    status = client.get(f"/api/document/{record.document_id}").json()
    assert status["merged_pdf_url"] == url


def test_merged_pdf_endpoint_needs_a_translation(client, tmp_path):
    source = _write_labeled_pdf(tmp_path / "paper.pdf", 1, (612, 792), "source text")
    record = create_document_record(source)

    response = client.post(f"/api/document/{record.document_id}/merged-pdf")

    assert response.status_code == 409
