"""The pipeline around the worker: stages, artifacts and retries.

The worker itself is stubbed: these tests care about what the pipeline does
with the products a run published.
"""

import io
import json
import sys
import types
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.models.store import FailureEntry, save_document
from app.services import document_pipeline
from app.services.document_pipeline import (
    GLOSSARY_KIND,
    MANIFEST_KIND,
    _event_reporter,
    _stage_key,
    create_document_record,
    process_document,
)
from app.services.pdf_translation_worker import WorkerProducts, _read_events
from app.services.stage_tracker import init_stages
from workers.pdfmathtranslate.events import EventWriter


def _write_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


@pytest.fixture(autouse=True)
def offline_metadata_lookup(monkeypatch):
    """The pipeline looks its paper up with a lazy import; nothing here may reach it."""
    stub = types.ModuleType("app.services.paper_metadata")
    stub.fetch_paper_metadata = lambda title: {}
    monkeypatch.setitem(sys.modules, "app.services.paper_metadata", stub)


def _manifest_payload(*, glossary: list[dict] | None = None) -> dict:
    return {
        "schema_version": "paperreader-manifest-v1",
        "generator": {"worker": "pdfmathtranslate"},
        "source_pdf": "paper.pdf",
        "source_sha256": "0" * 64,
        "mode_label": "PDFMathTranslate-next 2.9.0 · mono",
        "page_count": 1,
        "unit": "point",
        "boxes_normalized": False,
        "pages": [
            {
                "index": 0,
                "width": 612.0,
                "height": 792.0,
                "blocks": [
                    {
                        "kind": "title",
                        "level": 1,
                        "layout_label": "title",
                        "bbox": [50.0, 700.0, 500.0, 720.0],
                        "source_text": "Attention Is All You Need",
                        "translated_text": "注意力就是你所需要的一切",
                    },
                    {
                        "kind": "paragraph",
                        "layout_label": "plain text",
                        "bbox": [50.0, 600.0, 500.0, 690.0],
                        "source_text": "The dominant sequence transduction models are based on complex recurrent networks.",
                        "translated_text": "主流的序列转换模型基于复杂的循环网络。",
                    },
                ],
            }
        ],
        "figures": [],
        "tables": [],
        "references": [{"page_index": 0, "text": "[1] Vaswani et al. NeurIPS 2017."}],
        "logical_objects": [],
        "glossary": glossary or [],
    }


def _stub_worker(
    monkeypatch, tmp_path, *, glossary: bool = False, dual: bool = False, on_call=None
) -> list[dict]:
    """Point the pipeline at a run that published files, without a process."""
    output_dir = tmp_path / "worker-output"
    extraction_dir = output_dir / "extraction"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    translated = _write_pdf(output_dir / "translated.pdf")
    dual_pdf = _write_pdf(output_dir / "translated.dual.pdf") if dual else None
    manifest = extraction_dir / "manifest.json"
    manifest.write_text(
        json.dumps(_manifest_payload(glossary=[{"source": "attention", "target": "注意力"}])),
        encoding="utf-8",
    )
    glossary_path = None
    if glossary:
        glossary_path = extraction_dir / "glossary.csv"
        glossary_path.write_text("source,target\nattention,注意力\n", encoding="utf-8")

    calls: list[dict] = []

    def fake_run_worker(**kwargs):
        calls.append(kwargs)
        if on_call is not None:
            on_call(kwargs)
        return WorkerProducts(
            translated_pdf=translated,
            manifest_path=manifest,
            extraction_dir=extraction_dir,
            debug_dir=extraction_dir / "debug",
            mode_label="PDFMathTranslate-next 2.9.0 · mono",
            page_count=1,
            glossary_path=glossary_path,
            dual_pdf=dual_pdf,
        )

    monkeypatch.setattr(document_pipeline, "run_worker", fake_run_worker)
    return calls


def _run(record, **overrides):
    arguments = {
        "override_api_key": "test-key",
        "override_base_url": "https://llm.example/v1",
        "override_model": "test-model",
    }
    arguments.update(overrides)
    return process_document(record, **arguments)


def test_a_finished_run_registers_its_manifest_and_glossary(isolated_storage, monkeypatch, tmp_path):
    calls = _stub_worker(monkeypatch, tmp_path, glossary=True)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert [call["document_id"] for call in calls] == [record.document_id]
    assert result.status == "done"
    assert result.failure is None
    manifest_artifact = next(item for item in result.artifacts if item.kind == MANIFEST_KIND)
    assert Path(manifest_artifact.path).is_file()
    assert Path(manifest_artifact.path).name == "manifest.json"
    glossary_artifact = next(item for item in result.artifacts if item.kind == GLOSSARY_KIND)
    assert Path(glossary_artifact.path).is_file()
    assert Path(glossary_artifact.path).name == "glossary.csv"
    # The reader gets the published PDF under the document's own name, not the
    # worker's scratch path.
    assert result.translated_pdf_url.endswith("paper_Chinese_ver.pdf")
    published = next(item for item in result.artifacts if item.kind == "translated_pdf")
    assert Path(published.path).is_file()
    assert published.url == result.translated_pdf_url
    assert result.extracted_text.startswith("# Attention Is All You Need")


def test_pipeline_passes_selected_translation_domain_to_worker(isolated_storage, monkeypatch, tmp_path):
    from app.services.app_settings import AppSettings

    calls = _stub_worker(monkeypatch, tmp_path)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    result = _run(record, provider_settings=AppSettings(
        api_key="test-key", model="test-model", translation_domain="medical",
    ))
    assert result.status == "done"
    assert calls[0]["translation_domain"] == "medical"


def test_a_run_without_a_worker_glossary_registers_only_the_manifest(
    isolated_storage, monkeypatch, tmp_path
):
    _stub_worker(monkeypatch, tmp_path, glossary=False)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert [item.kind for item in result.artifacts if item.kind == GLOSSARY_KIND] == []
    assert any(item.kind == MANIFEST_KIND for item in result.artifacts)


def test_a_finished_run_publishes_no_annotated_pdf_by_default(
    isolated_storage, monkeypatch, tmp_path
):
    _stub_worker(monkeypatch, tmp_path)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "done"
    assert [item.kind for item in result.artifacts if item.kind == "annotated_pdf"] == []
    assert any("reading preference off" in line for line in result.logs)


def test_a_finished_run_publishes_the_annotated_pdf_when_the_preference_is_on(
    isolated_storage, monkeypatch, tmp_path
):
    from app.services import app_settings

    app_settings.update_settings(
        api_key="test-key",
        base_url="https://llm.example/v1",
        model="test-model",
        show_annotated_pdf=True,
    )
    _stub_worker(monkeypatch, tmp_path)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    annotated = next(
        (item for item in result.artifacts if item.kind == "annotated_pdf"), None
    )
    assert annotated is not None
    assert Path(annotated.path).is_file()


def test_worker_progress_events_drive_the_document_stages(isolated_storage, tmp_path):
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    init_stages(record)
    switcher = document_pipeline._StageSwitcher(record)
    report = _event_reporter(record, switcher)

    stream = io.StringIO()
    writer = EventWriter(stream)
    writer.stage_summary([{"name": "Parse Page Layout", "percent": 45.0}])
    writer.progress("progress_update", "Parse Page Layout", {"stage_progress": 100.0})
    writer.progress(
        "progress_update",
        "Translate Paragraphs",
        {"stage_progress": 50.0, "stage_total": 4, "stage_current": 2},
    )
    writer.progress("progress_end", "Typesetting", {"stage_progress": 100.0})
    writer.finish({"job_id": record.document_id})

    events = _read_events(stream.getvalue().splitlines(), report)
    switcher.close()

    assert [event["type"] for event in events][0] == "stage_summary"
    statuses = {stage.key: stage.status for stage in record.stages}
    assert statuses["parse"] == "done"
    assert statuses["translate"] == "done"
    assert statuses["render"] == "done"
    translate = next(stage for stage in record.stages if stage.key == "translate")
    assert translate.label == "翻译段落 2/4"
    assert record.progress > 0


def test_progress_never_regresses_when_a_sub_stage_restarts(isolated_storage, tmp_path):
    """BabelDOC sub-stages restart at 0% within one pipeline stage; the overall
    bar must not slide backwards when a new sub-stage begins."""
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    init_stages(record)
    switcher = document_pipeline._StageSwitcher(record)
    report = _event_reporter(record, switcher)

    report({
        "type": "progress_end",
        "stage": "Parse Page Layout",
        "group": "parse",
        "progress": 1.0,
    })
    peak = record.progress
    report({
        "type": "progress_update",
        "stage": "Parse Table",
        "group": "parse",
        "progress": 0.0,
    })
    assert record.progress >= peak
    report({
        "type": "progress_update",
        "stage": "Parse Table",
        "group": "parse",
        "progress": 0.5,
    })
    assert record.progress >= peak
    switcher.close()


def test_a_stage_summary_alone_leaves_the_stage_list_untouched(isolated_storage, tmp_path):
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    init_stages(record)
    switcher = document_pipeline._StageSwitcher(record)
    report = _event_reporter(record, switcher)

    report({"type": "stage_summary", "stages": [{"name": "Typesetting", "group": "render"}]})

    assert record.current_stage is None
    assert {stage.status for stage in record.stages} == {"pending"}


def test_an_unknown_stage_group_is_reported_as_parse():
    assert _stage_key("translate") == "translate"
    assert _stage_key("render") == "render"
    assert _stage_key("Typesetting") == "parse"
    assert _stage_key("") == "parse"


def test_a_retry_reruns_the_worker_and_resets_the_parse_stage(
    isolated_storage, monkeypatch, tmp_path
):
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    init_stages(record)
    for stage in record.stages:
        stage.status = "done"
    record.status = "failed"
    record.failure = FailureEntry(stage="translate", message="worker exceeded its budget")
    save_document(record)

    stages_at_worker_start: list[dict[str, str]] = []
    calls = _stub_worker(
        monkeypatch,
        tmp_path,
        on_call=lambda _kwargs: stages_at_worker_start.append(
            {stage.key: stage.status for stage in record.stages}
        ),
    )

    result = _run(record, resume_from="translate")

    assert len(calls) == 1, "a retry has to run the worker again"
    assert stages_at_worker_start[0]["parse"] == "pending"
    assert stages_at_worker_start[0]["translate"] == "pending"
    assert result.status == "done"
    assert result.failure is None


def test_a_dual_run_registers_the_bilingual_pdf(
    isolated_storage, monkeypatch, tmp_path
):
    _stub_worker(monkeypatch, tmp_path, dual=True)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "done"
    dual = next(item for item in result.artifacts if item.kind == "dual_pdf")
    assert Path(dual.path).is_file()
    assert Path(dual.path).name == "paper_双语对照.pdf"
    assert dual.url == "/data/outputs/{}/paper_双语对照.pdf".format(record.document_id)
    # The monolingual PDF stays the document's translated artifact.
    assert result.translated_pdf_url.endswith("paper_Chinese_ver.pdf")


def test_a_mono_run_registers_no_bilingual_pdf(
    isolated_storage, monkeypatch, tmp_path
):
    _stub_worker(monkeypatch, tmp_path, dual=False)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "done"
    assert [item.kind for item in result.artifacts if item.kind == "dual_pdf"] == []


def test_a_retry_drops_a_bilingual_pdf_the_new_run_did_not_produce(
    isolated_storage, monkeypatch, tmp_path
):
    _stub_worker(monkeypatch, tmp_path, dual=True)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    _run(record)

    _stub_worker(monkeypatch, tmp_path, dual=False)
    record.status = "failed"
    result = _run(record, resume_from="parse")

    assert result.status == "done"
    assert [item.kind for item in result.artifacts if item.kind == "dual_pdf"] == []


def test_a_cancelled_run_publishes_no_products(
    isolated_storage, monkeypatch, tmp_path
):
    from app.services.pdf_translation_worker import WorkerCancelled

    def cancel(_kwargs):
        raise WorkerCancelled("worker cancelled")

    _stub_worker(monkeypatch, tmp_path, on_call=cancel)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "cancelled"
    assert result.failure is None
    assert result.current_stage is None
    assert result.translated_pdf_url is None
    published = {
        "translated_pdf",
        "dual_pdf",
        "manifest",
        "glossary",
        "annotated_pdf",
    }
    assert [item.kind for item in result.artifacts if item.kind in published] == []
    assert any("Cancelled" in line for line in result.logs)
    # Nothing is left holding the document back from a reprocess.
    assert result.status not in {"queued", "processing"}


def test_a_finished_run_schedules_background_term_extraction(
    isolated_storage, monkeypatch, tmp_path
):
    _stub_worker(monkeypatch, tmp_path)
    scheduled = []
    monkeypatch.setattr(
        document_pipeline, "schedule_extraction", lambda **kwargs: scheduled.append(kwargs)
    )
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "done"
    assert len(scheduled) == 1
    call = scheduled[0]
    assert call["document_id"] == record.document_id
    assert call["domain"] == "general"
    assert call["api_key"] == "test-key"
    assert call["base_url"] == "https://llm.example/v1"
    assert call["model"] == "test-model"
    assert call["manifest"].page_count == 1


def test_a_failed_run_schedules_no_term_extraction(
    isolated_storage, monkeypatch, tmp_path
):
    from app.services.pdf_translation_worker import WorkerError

    def broken_worker(_kwargs):
        raise WorkerError("boom", stage="translate")

    _stub_worker(monkeypatch, tmp_path, on_call=broken_worker)
    scheduled = []
    monkeypatch.setattr(
        document_pipeline, "schedule_extraction", lambda **kwargs: scheduled.append(kwargs)
    )
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "failed"
    assert scheduled == []


def test_a_failed_run_still_publishes_the_original_pdf(
    isolated_storage, monkeypatch, tmp_path
):
    from app.services.pdf_translation_worker import WorkerError

    def broken_worker(_kwargs):
        raise WorkerError("boom", stage="translate")

    _stub_worker(monkeypatch, tmp_path, on_call=broken_worker)
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))

    result = _run(record)

    assert result.status == "failed"
    assert result.original_pdf_url == f"/data/outputs/{record.document_id}/original.pdf"
    original = tmp_path / "outputs" / record.document_id / "original.pdf"
    assert original.is_file() and original.read_bytes()[:4] == b"%PDF"


def test_a_finish_event_does_not_reopen_a_completed_stage(isolated_storage, tmp_path):
    """The finish event carries no group; it must not be read as a parse event."""
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    init_stages(record)
    switcher = document_pipeline._StageSwitcher(record)
    report = _event_reporter(record, switcher)

    report({"type": "progress_start", "stage": "Parse Page Layout", "group": "parse"})
    report({"type": "progress_end", "stage": "Parse Page Layout", "group": "parse", "progress": 1.0})
    report({"type": "progress_start", "stage": "Translate Paragraphs", "group": "translate"})

    parse = next(stage for stage in record.stages if stage.key == "parse")
    assert parse.status == "done"
    ended_at = parse.ended_at

    report({"type": "finish", "status": "finished", "job_id": record.document_id})

    assert parse.status == "done"
    assert parse.ended_at == ended_at
    assert record.current_stage == "translate"
    switcher.close()


def test_a_new_sub_stage_in_the_same_group_updates_the_label(isolated_storage, tmp_path):
    """Term extraction ends at 100%; translation restarts at 0% — both translate."""
    record = create_document_record(_write_pdf(tmp_path / "paper.pdf"))
    init_stages(record)
    switcher = document_pipeline._StageSwitcher(record)
    report = _event_reporter(record, switcher)

    report({
        "type": "progress_end",
        "stage": "Automatic Term Extraction",
        "group": "translate",
        "progress": 1.0,
        "current": 32,
        "total": 32,
    })
    report({
        "type": "progress_update",
        "stage": "Translate Paragraphs",
        "group": "translate",
        "progress": 0.03125,
        "current": 1,
        "total": 32,
    })

    translate = next(stage for stage in record.stages if stage.key == "translate")
    assert translate.label == "翻译段落 1/32"
    switcher.close()
