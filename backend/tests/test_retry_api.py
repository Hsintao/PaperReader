from fastapi import HTTPException
import pytest

from app.core.config import settings
from app.core.database import db_cursor, init_database
from app.models import store
from app.services.stage_tracker import init_stages, with_stage


def test_failed_document_can_queue_retry_once(client, monkeypatch):
    dispatched: list[tuple[str, str]] = []

    def fake_retry(document_id: str, resume_from: str) -> None:
        dispatched.append((document_id, resume_from))

    from app.api import routes_document

    monkeypatch.setattr(routes_document, "_run_retry_pipeline", fake_retry)
    source = settings.upload_dir / "failed.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord(
        "failed-doc",
        "pdf",
        source,
        status="failed",
        failure=store.FailureEntry(
            stage="translate", message="chunk 7 translation failed", chunk=7
        ),
    )
    init_stages(record)
    record.current_stage = "translate"
    next(stage for stage in record.stages if stage.key == "translate").status = "failed"
    store.save_document(record)

    response = client.post("/api/document/failed-doc/retry")
    assert response.status_code == 202, response.text
    assert response.json() == {
        "document_id": "failed-doc",
        "status": "queued",
        "resume_from": "translate",
    }
    assert dispatched == [("failed-doc", "translate")]

    status = client.get("/api/document/failed-doc").json()
    assert status["failure"]["chunk"] == 7
    assert status["failure"]["retry_count"] == 1
    assert "latex_recovery" not in status
    assert client.post("/api/document/failed-doc/retry").status_code == 409


def test_retry_rejects_non_failed_and_non_retryable_documents(client):
    source = settings.upload_dir / "queued.pdf"
    source.write_bytes(b"pdf")
    store.save_document(store.DocumentRecord("queued-doc", "pdf", source))
    assert client.post("/api/document/queued-doc/retry").status_code == 409

    failed = store.DocumentRecord(
        "unsafe-doc",
        "pdf",
        source,
        status="failed",
        failure=store.FailureEntry(stage="parse", message="bad input", retryable=False),
    )
    store.save_document(failed)
    assert client.post("/api/document/unsafe-doc/retry").status_code == 409


def test_startup_converts_interrupted_work_into_retryable_failure(isolated_storage):
    source = settings.upload_dir / "interrupted.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord(
        "interrupted-doc", "pdf", source, status="processing", current_stage="render"
    )
    store.save_document(record)
    store.DOCUMENTS.clear()

    init_database()

    recovered = store.get_document("interrupted-doc")
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.failure is not None
    assert recovered.failure.stage == "render"
    assert recovered.failure.retryable is True


def test_startup_converts_orphaned_queued_work_into_retryable_failure(isolated_storage):
    source = settings.upload_dir / "queued-at-exit.pdf"
    source.write_bytes(b"pdf")
    store.save_document(
        store.DocumentRecord("queued-at-exit", "pdf", source, status="queued")
    )
    store.DOCUMENTS.clear()

    init_database()

    recovered = store.get_document("queued-at-exit")
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.failure is not None
    assert recovered.failure.stage == "upload"
    assert recovered.failure.retryable is True


def test_retry_claim_uses_database_compare_and_set(isolated_storage):
    source = settings.upload_dir / "cas.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord(
        "cas-doc",
        "pdf",
        source,
        status="failed",
        failure=store.FailureEntry(stage="translate", message="failed"),
    )
    store.save_document(record)
    # Simulate another worker claiming the DB row while this process still
    # holds a stale failed record in its in-memory cache.
    with db_cursor() as conn:
        conn.execute("UPDATE documents SET status = 'queued' WHERE document_id = ?", (record.document_id,))

    with pytest.raises(HTTPException) as exc_info:
        store.queue_document_retry(record.document_id)

    assert exc_info.value.status_code == 409


def test_stage_transitions_are_persisted_even_when_stage_raises(isolated_storage):
    source = settings.upload_dir / "stage.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord("stage-doc", "pdf", source)
    init_stages(record)
    store.save_document(record)

    try:
        with with_stage(record, "parse"):
            raise RuntimeError("parser unavailable")
    except RuntimeError:
        pass
    store.DOCUMENTS.clear()

    persisted = store.get_document("stage-doc")
    assert persisted is not None
    parse = next(stage for stage in persisted.stages if stage.key == "parse")
    assert parse.status == "failed"
    assert persisted.current_stage == "parse"


def test_background_pipeline_failure_marks_document_failed(isolated_storage, monkeypatch):
    from app.api import routes_document, routes_upload

    def boom():
        raise RuntimeError("settings store unavailable")

    monkeypatch.setattr(routes_document, "load_settings", boom)
    monkeypatch.setattr(routes_upload, "load_settings", boom)

    source = settings.upload_dir / "wrapper.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord("wrapper-doc", "pdf", source, status="queued")
    init_stages(record)
    store.save_document(record)

    routes_document._run_retry_pipeline("wrapper-doc", "translate")

    reloaded = store.get_document("wrapper-doc")
    assert reloaded.status == "failed"
    assert reloaded.failure.retryable is True
    assert "Retry pipeline failed to start" in reloaded.failure.message
    assert any("Error:" in line for line in reloaded.logs)
