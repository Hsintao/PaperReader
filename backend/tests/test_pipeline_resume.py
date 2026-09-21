import json

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings
from app.models import store
from app.services import document_pipeline, layout_model
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
    """A real extraction: geometry comes from the fixture's own text layer."""
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


def _translate_ok(ir, **kwargs):
    apply_translations(
        ir,
        [f"译-{index}" for index, _ in enumerate(collect_translatable_strings(ir))],
    )
    return [], []


def test_translation_retry_reuses_parse_checkpoint_and_finishes(
    isolated_storage, monkeypatch
):
    source = settings.upload_dir / "resume.pdf"
    _write_source(source, ["First paragraph of the paper.", "Second paragraph here."])
    record = document_pipeline.create_document_record(source, "pdf")
    parse_calls: list[int] = []

    def extract_once(*args, **kwargs):
        parse_calls.append(1)
        return _structured_result(source)

    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", extract_once
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(
        document_pipeline,
        "translate_ir",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("Translation incomplete: chunk 2 failed")
        ),
    )
    first = document_pipeline.process_document(record)
    assert first.status == "failed"
    assert first.failure is not None
    assert first.failure.stage == "translate"
    assert first.failure.chunk == 2

    def extractor_must_not_run(*args, **kwargs):
        raise AssertionError("retry must load the completed parse checkpoint")

    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", extractor_must_not_run
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)

    second = document_pipeline.process_document(first, resume_from="translate")
    assert second.status == "done", second.logs
    assert second.failure is None
    assert parse_calls == [1]
    assert second.translated_pdf_url is not None
    assert any(
        artifact.kind == "translated_pdf" for artifact in second.artifacts
    )
    assert any(artifact.kind == "layout_plan" for artifact in second.artifacts)


def test_clean_retry_reuses_extraction_checkpoint(isolated_storage, monkeypatch):
    source = settings.upload_dir / "clean-resume.pdf"
    _write_source(source, ["A paragraph long enough to translate."])
    record = document_pipeline.create_document_record(source, "pdf")
    parse_calls: list[int] = []

    def extract_once(*args, **kwargs):
        parse_calls.append(1)
        return _structured_result(source)

    real_clean = document_pipeline._clean_nougat_text_with_metadata
    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", extract_once
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(
        document_pipeline,
        "_clean_nougat_text_with_metadata",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("clean stage failed")),
    )

    first = document_pipeline.process_document(record)
    assert first.status == "failed"
    assert first.failure is not None
    assert first.failure.stage == "clean"

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not parse again")),
    )
    monkeypatch.setattr(
        document_pipeline, "_clean_nougat_text_with_metadata", real_clean
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)

    second = document_pipeline.process_document(first, resume_from="clean")

    assert second.status == "done", second.logs
    assert second.failure is None
    assert parse_calls == [1]
    assert second.translated_pdf_url is not None


def test_reprocess_after_completion_reuses_the_extraction_checkpoint(
    isolated_storage, monkeypatch
):
    source = settings.upload_dir / "reprocess.pdf"
    _write_source(source, ["First paragraph of the paper.", "Second paragraph here."])
    record = document_pipeline.create_document_record(source, "pdf")
    parse_calls: list[int] = []

    def extract_once(*args, **kwargs):
        parse_calls.append(1)
        return _structured_result(source)

    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", extract_once
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)

    first = document_pipeline.process_document(record)
    assert first.status == "done", first.logs
    assert parse_calls == [1]

    resume_from = document_pipeline.cached_resume_stage(first)
    assert resume_from == "clean"

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("reprocess must reuse the completed parse checkpoint")
        ),
    )

    second = document_pipeline.process_document(first, resume_from=resume_from)
    assert second.status == "done", second.logs
    assert parse_calls == [1]
    assert second.translated_pdf_url is not None


def test_render_retry_reuses_extraction_checkpoint(isolated_storage, monkeypatch):
    source = settings.upload_dir / "render-resume.pdf"
    _write_source(source, ["Only paragraph in this document."])
    record = document_pipeline.create_document_record(source, "pdf")

    def extract_once(*args, **kwargs):
        return _structured_result(source)

    monkeypatch.setattr(
        document_pipeline, "extract_structured_from_pdf_local", extract_once
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)
    real_render = document_pipeline.render_document
    monkeypatch.setattr(
        document_pipeline,
        "render_document",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("layout failed")),
    )

    first = document_pipeline.process_document(record)
    assert first.status == "failed"
    assert first.failure is not None
    assert first.failure.stage == "render"

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not parse again")),
    )
    monkeypatch.setattr(document_pipeline, "render_document", real_render)

    second = document_pipeline.process_document(first, resume_from="render")
    assert second.status == "done", second.logs
    assert second.translated_pdf_url is not None


@pytest.mark.parametrize("payload", [None, [], "text", 1])
def test_extraction_checkpoint_ignores_non_object_json(
    isolated_storage, tmp_path, payload
):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    checkpoint = tmp_path / "extraction-checkpoint.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    assert document_pipeline._load_extraction_checkpoint(checkpoint, source) is None


def test_extraction_checkpoint_roundtrip_keeps_layout_geometry(
    isolated_storage, tmp_path
):
    source = tmp_path / "source.pdf"
    _write_source(source, ["Paragraph for the checkpoint."])
    result = _structured_result(source)
    checkpoint = tmp_path / "extraction-checkpoint.json"

    document_pipeline._save_extraction_checkpoint(checkpoint, source, result)
    restored = document_pipeline._load_extraction_checkpoint(checkpoint, source)

    assert restored is not None
    assert restored.content_blocks == result.content_blocks
    assert restored.boxes_normalized is False


def test_document_stages_include_render(isolated_storage):
    from app.services.stage_tracker import init_stages

    source = settings.upload_dir / "stages.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord("stages-doc", "pdf", source)
    init_stages(record)
    keys = [stage.key for stage in record.stages]
    assert keys[-1] == "render"
    assert "latex_build" not in keys


def test_failed_translation_block_is_recorded_as_a_layout_issue(
    isolated_storage, tmp_path, monkeypatch
):
    from app.models import store

    source = settings.upload_dir / "issue-doc.pdf"
    # Four pages, so a single original page stays inside the fallback budget.
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    for index in range(4):
        canvas.setFont("Helvetica", 11)
        canvas.drawString(72, 700, f"Paragraph number {index} of the issue fixture.")
        canvas.showPage()
    canvas.save()
    record = store.DocumentRecord("issue-doc", "pdf", source)

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *args, **kwargs: _structured_result(source),
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )

    def failing_translate_ir(ir, **kwargs):
        # What translate_ir does when a piece fails: the block keeps its own
        # wording, and the failure is reported as a structured issue.
        texts = collect_translatable_strings(ir)
        apply_translations(
            ir, [texts[0]] + [f"译-{index}" for index in range(1, len(texts))]
        )
        return [], [
            {
                "kind": "block_original",
                "logical_index": 0,
                "source": texts[0],
                "reason": "1 of 1 piece(s) could not be translated after repeated retries",
            }
        ]

    monkeypatch.setattr(document_pipeline, "translate_ir", failing_translate_ir)

    result = document_pipeline.process_document(record)

    assert result.status == "done", result.logs
    issues = result.metadata["layout_issues"]
    assert issues == [
        {
            "kind": "block_original",
            "page": 1,
            "block_kind": "text_block",
            "message": (
                "翻译失败，保留原文：1 of 1 piece(s) could not be translated "
                "after repeated retries"
            ),
        }
    ]


def test_reprocessing_clears_previous_layout_issues(isolated_storage, monkeypatch):
    from app.models import store

    source = settings.upload_dir / "issue-reset.pdf"
    _write_source(source, ["A paragraph for the reprocess check."])
    record = store.DocumentRecord("issue-reset", "pdf", source)
    record.metadata["layout_issues"] = [
        {"kind": "page_original", "page": 2, "block_kind": "page", "message": "old"}
    ]

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *args, **kwargs: _structured_result(source),
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)

    result = document_pipeline.process_document(record)

    assert result.status == "done", result.logs
    assert all(issue.get("message") != "old" for issue in result.metadata["layout_issues"])


def test_layout_plan_v2_records_typography_and_fallbacks(isolated_storage, monkeypatch):
    import json

    from app.models import store
    from app.services.layout_fit import TypographyProfile

    source = settings.upload_dir / "plan-v2.pdf"
    _write_source(source, ["A paragraph for the layout plan."])
    record = store.DocumentRecord("plan-v2", "pdf", source)

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *args, **kwargs: _structured_result(source),
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)

    result = document_pipeline.process_document(record)
    assert result.status == "done", result.logs

    plan_path = settings.output_dir / "plan-v2" / "layout-plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert payload["version"] == "layout-plan-v2"
    assert payload["source"]["document_id"] == "plan-v2"
    assert payload["source"]["page_count"] == 1
    assert payload["typography"]["body_single"] == TypographyProfile.BODY_SINGLE.size
    assert payload["typography"]["title_bonus"] == 1.0
    assert payload["typography"]["title_min"] == 7.0
    assert payload["typography"]["title_leading"] == pytest.approx(1.3)
    assert payload["typography"]["min_body"] == 6.0
    page = payload["pages"][0]
    assert page["status"] in {"ok", "masked", "original"}
    assert page["columns"]["kind"] in {"single", "double", "mixed"}
    assert "flow_chains" in page
    assert all("formula_fallback" in block for block in page["blocks"])
    assert "captions" in page and "cells" in page


def test_planning_fallbacks_become_structured_issues(isolated_storage, monkeypatch):
    from app.models import store

    source = settings.upload_dir / "issues.pdf"
    # Two pages, so one page falling back stays inside the failure budget.
    canvas = pdf_canvas.Canvas(str(source), pagesize=letter)
    for index in range(2):
        canvas.setFont("Helvetica", 11)
        canvas.drawString(72, 700, f"Paragraph {index} whose plan falls back.")
        canvas.showPage()
    canvas.save()
    record = store.DocumentRecord("issues", "pdf", source)

    monkeypatch.setattr(
        document_pipeline,
        "extract_structured_from_pdf_local",
        lambda *args, **kwargs: _structured_result(source),
    )
    monkeypatch.setattr(
        document_pipeline, "extract_text_from_pdf_text_layer", lambda *a, **k: ""
    )
    monkeypatch.setattr(document_pipeline, "translate_ir", _translate_ok)

    original_plan = document_pipeline.plan_document

    def failing_plan(frames, blocks, *, measurer):
        plans = original_plan(frames, blocks, measurer=measurer)
        plans[0].status = "original"
        plans[0].reason = (
            "body text does not fit inside its column at the minimum font size of 6pt"
        )
        return plans

    monkeypatch.setattr(document_pipeline, "plan_document", failing_plan)

    result = document_pipeline.process_document(record)

    assert result.status == "done", result.logs
    issues = result.metadata["layout_issues"]
    kinds = {issue["kind"] for issue in issues}
    assert "page_original" in kinds
    assert all(issue["page"] == 1 for issue in issues)
    assert all(issue["message"] for issue in issues)
    # The plan is written before the translated PDF is published.
    plan_path = settings.output_dir / "issues" / "layout-plan.json"
    assert plan_path.is_file()
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert payload["pages"][0]["status"] == "original"
