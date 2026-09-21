"""The SoMark parse branch: cloud extraction first, local fallback on failure."""

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings
from app.services import document_pipeline, layout_model
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


def _structured_result(source, mode_label: str) -> MinerUResult:
    pages = layout_model.parse_local_pages(source)
    markdown = "\n\n".join(page.markdown for page in pages if page.markdown)
    blocks = [page.blocks for page in pages if page.blocks]
    return MinerUResult(
        markdown=markdown,
        mode_label=mode_label,
        extracted_files=[],
        content_blocks=blocks or None,
        boxes_normalized=False,
    )


def _prepare(monkeypatch, source) -> None:
    def fake_translate_ir(ir, **kwargs):
        apply_translations(
            ir, [f"译-{index}" for index, _ in enumerate(collect_translatable_strings(ir))]
        )
        return [], []

    monkeypatch.setattr(document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: "")
    monkeypatch.setattr(document_pipeline, "extract_document_terms", lambda *a, **k: [])
    monkeypatch.setattr(document_pipeline, "translate_ir", fake_translate_ir)


def _provider() -> AppSettings:
    return AppSettings(
        api_key="k",
        base_url="https://llm.example/v1",
        model="m",
        pdf_parser="somark",
        somark_api_key="sk-somark",
        somark_base_url="https://somark.example/api/v1",
    )


def test_somark_parser_extracts_into_its_own_directory(isolated_storage, monkeypatch):
    source = settings.upload_dir / "somark.pdf"
    _write_source(source, ["A paragraph long enough to translate."])
    record = document_pipeline.create_document_record(source, "pdf")
    _prepare(monkeypatch, source)
    calls: list[tuple] = []

    def fake_somark(pdf_path, output_dir, **kwargs):
        calls.append((pdf_path, output_dir, kwargs.get("config")))
        return _structured_result(source, "somark")

    monkeypatch.setattr(document_pipeline, "extract_structured_from_pdf_somark", fake_somark)
    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("local parser must not run")),
    )

    result = document_pipeline.process_document(record, provider_settings=_provider())

    assert result.status == "done", result.logs
    assert "Submitting PDF to SoMark" in result.logs
    assert len(calls) == 1
    pdf_path, output_dir, config = calls[0]
    assert Path(pdf_path) == source
    assert Path(output_dir).name == "somark"
    assert config.api_key == "sk-somark"
    assert config.base_url == "https://somark.example/api/v1"

    checkpoint = settings.output_dir / record.document_id / "extraction-checkpoint.json"
    assert checkpoint.is_file()


def test_somark_failure_falls_back_to_the_local_parser(isolated_storage, monkeypatch):
    source = settings.upload_dir / "somark-fallback.pdf"
    _write_source(source, ["A paragraph long enough to translate."])
    record = document_pipeline.create_document_record(source, "pdf")
    _prepare(monkeypatch, source)
    local_dirs: list[Path] = []

    def failing_somark(*args, **kwargs):
        raise RuntimeError("somark is unreachable")

    def fake_local(pdf_path, output_dir, **kwargs):
        local_dirs.append(Path(output_dir))
        return _structured_result(source, "local:text-layer")

    monkeypatch.setattr(document_pipeline, "extract_structured_from_pdf_somark", failing_somark)
    monkeypatch.setattr(document_pipeline, "extract_structured_from_pdf_local", fake_local)

    result = document_pipeline.process_document(record, provider_settings=_provider())

    assert result.status == "done", result.logs
    assert any("SoMark unavailable (somark is unreachable)" in line for line in result.logs)
    assert [path.name for path in local_dirs] == ["local"]
