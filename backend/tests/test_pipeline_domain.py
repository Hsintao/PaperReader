"""The translation domain reaches the prompts, the record and the glossary."""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings
from app.services import document_pipeline, glossary_service, layout_model
from app.services.app_settings import AppSettings
from app.services.mineru_layout import apply_translations, collect_translatable_strings
from app.services.mineru_service import MinerUResult


def _write_source(path, lines) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=letter)
    canvas.setFont("Helvetica", 11)
    y = 700
    for line in lines:
        canvas.drawString(72, y, line)
        y -= 40
    canvas.showPage()
    canvas.save()


def _structured_result(source) -> MinerUResult:
    pages = layout_model.parse_local_pages(source)
    markdown = "\n\n".join(page.markdown for page in pages if page.markdown)
    blocks = [page.blocks for page in pages if page.blocks]
    return MinerUResult(
        markdown=markdown,
        mode_label="fixture",
        extracted_files=[],
        content_blocks=blocks or None,
        boxes_normalized=False,
    )


def test_pipeline_uses_the_configured_domain_and_records_terms(isolated_storage, monkeypatch):
    source = settings.upload_dir / "domain.pdf"
    _write_source(source, ["A paragraph long enough to translate."])
    record = document_pipeline.create_document_record(source, "pdf")
    calls: list[dict] = []

    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", lambda *a, **k: _structured_result(source)
    )
    monkeypatch.setattr(document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: "")
    monkeypatch.setattr(
        document_pipeline,
        "extract_document_terms",
        lambda *a, **k: [("attention", "注意力")],
    )

    def fake_translate_ir(ir, **kwargs):
        calls.append(kwargs)
        apply_translations(
            ir, [f"译-{index}" for index, _ in enumerate(collect_translatable_strings(ir))]
        )
        return []

    monkeypatch.setattr(document_pipeline, "translate_ir", fake_translate_ir)

    provider = AppSettings(
        api_key="k",
        base_url="https://llm.example/v1",
        model="m",
        pdf_parser="local",
        translation_domain="medical",
    )
    result = document_pipeline.process_document(record, provider_settings=provider)

    assert result.status == "done", result.logs
    assert result.metadata["translation_domain"] == "medical"
    assert any("Translation domain: 医学" in line for line in result.logs)

    assert calls
    assert calls[0]["domain"] == "medical"
    assert calls[0]["checkpoint_namespace"] == "ir:medical"
    assert "本文论文标题为" in calls[0]["translation_context"]
    assert "attention = 注意力" in calls[0]["translation_context"]

    pending = glossary_service.pending_path("medical")
    assert pending.is_file()
    assert "注意力" in pending.read_text(encoding="utf-8")


def test_pipeline_defaults_to_the_general_domain(isolated_storage, monkeypatch):
    source = settings.upload_dir / "default-domain.pdf"
    _write_source(source, ["Another paragraph long enough to translate."])
    record = document_pipeline.create_document_record(source, "pdf")
    calls: list[dict] = []

    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", lambda *a, **k: _structured_result(source)
    )
    monkeypatch.setattr(document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: "")
    monkeypatch.setattr(document_pipeline, "extract_document_terms", lambda *a, **k: [])

    def fake_translate_ir(ir, **kwargs):
        calls.append(kwargs)
        apply_translations(
            ir, [f"译-{index}" for index, _ in enumerate(collect_translatable_strings(ir))]
        )
        return []

    monkeypatch.setattr(document_pipeline, "translate_ir", fake_translate_ir)

    result = document_pipeline.process_document(record)

    assert result.status == "done", result.logs
    assert result.metadata["translation_domain"] == "general"
    assert calls[0]["checkpoint_namespace"] == "ir:general"
    assert "强制术语表" not in calls[0]["translation_context"]
    assert not glossary_service.pending_path("general").is_file()
