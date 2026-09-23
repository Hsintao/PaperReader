from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.core.database import db_cursor


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime:
    if not value:
        return _utcnow()
    return datetime.fromisoformat(value)


@dataclass
class ArtifactEntry:
    name: str
    kind: str
    path: str
    url: str | None = None
    # Format revision of a derived artifact; 0 for artifacts that have none.
    revision: int = 0


@dataclass
class ReferenceEntry:
    index: int
    text: str


@dataclass
class StageEntry:
    key: str
    label: str
    weight: float
    status: str = "pending"
    started_at: float | None = None
    ended_at: float | None = None
    duration_ms: int | None = None


@dataclass
class FailureEntry:
    stage: str
    message: str
    retryable: bool = True
    chunk: int | None = None
    retry_count: int = 0


@dataclass
class AnnotationEntry:
    id: str
    document_id: str
    page: int = 1
    quote: str = ""
    color: str = "yellow"
    note: str = ""
    position_ratio: float = 0.0
    created_at: str = ""


@dataclass
class DocumentRecord:
    document_id: str
    source_type: str
    source_path: Path
    source_filename: str = ""
    status: str = "queued"
    original_pdf_url: str | None = None
    translated_pdf_url: str | None = None
    extracted_text: str = ""
    translated_text: str = ""
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    references: list[ReferenceEntry] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    last_opened_at: datetime | None = None
    size_bytes: int = 0
    progress: int = 0
    current_stage: str | None = None
    current_stage_label: str | None = None
    stage_started_at: float | None = None
    eta_seconds: int | None = None
    stages: list[StageEntry] = field(default_factory=list)
    failure: FailureEntry | None = None
    retry_count: int = 0
    last_read_page: int = 0
    last_read_ratio: float = 0.0
    metadata: dict = field(default_factory=dict)
    deleted_at: datetime | None = None


DOCUMENTS: dict[str, DocumentRecord] = {}
_RETRY_LOCK = threading.RLock()


def normalized_source_filename(name: str, original_name: str = "document.pdf") -> str:
    """Return a safe display filename while preserving the source file type."""
    raw = (name or "").strip()
    if not raw or Path(raw).name != raw or "/" in raw or "\\" in raw:
        raise ValueError("invalid document name")
    original_suffix = Path(original_name).suffix.lower()
    suffix = Path(raw).suffix.lower()
    stem = Path(raw).stem if suffix else raw
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    if not stem:
        raise ValueError("invalid document name")
    if original_suffix == ".pdf":
        suffix = original_suffix
    elif not suffix:
        suffix = ".pdf"
    return f"{stem[:120]}{suffix}"


def translated_pdf_filename(source_filename: str) -> str:
    """Return the user-facing translated PDF filename for a source document."""
    stem = Path(source_filename or "document.pdf").stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "document"
    return f"{stem[:120]}_Chinese_ver.pdf"


def annotated_pdf_filename(source_filename: str) -> str:
    """Return the user-facing annotated-source PDF filename for a document."""
    stem = Path(source_filename or "document.pdf").stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "document"
    return f"{stem[:120]}_原文标注.pdf"


def dual_pdf_filename(source_filename: str) -> str:
    """Return the user-facing bilingual PDF filename for a source document."""
    stem = Path(source_filename or "document.pdf").stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "document"
    return f"{stem[:120]}_双语对照.pdf"


def _serialize_items(items: list) -> str:
    return json.dumps([asdict(item) for item in items], ensure_ascii=False)


def _document_from_row(row) -> DocumentRecord:
    failure_payload = json.loads(row["failure_json"] or "null")
    return DocumentRecord(
        document_id=row["document_id"],
        source_type=row["source_type"],
        source_path=Path(row["source_path"]),
        source_filename=row["source_filename"] or "",
        status=row["status"],
        original_pdf_url=row["original_pdf_url"],
        translated_pdf_url=row["translated_pdf_url"],
        extracted_text=row["extracted_text"] or "",
        translated_text=row["translated_text"] or "",
        artifacts=[ArtifactEntry(**item) for item in json.loads(row["artifacts_json"] or "[]")],
        references=[ReferenceEntry(**item) for item in json.loads(row["references_json"] or "[]")],
        logs=json.loads(row["logs_json"] or "[]"),
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
        last_opened_at=_from_iso(row["last_opened_at"]) if row["last_opened_at"] else None,
        size_bytes=int(row["size_bytes"] or 0),
        progress=int(row["progress"] or 0),
        current_stage=row["current_stage"],
        current_stage_label=row["current_stage_label"],
        stage_started_at=row["stage_started_at"],
        eta_seconds=row["eta_seconds"],
        stages=[StageEntry(**item) for item in json.loads(row["stages_json"] or "[]")],
        failure=FailureEntry(**failure_payload) if isinstance(failure_payload, dict) else None,
        retry_count=int(row["retry_count"] or 0),
        last_read_page=int(row["last_read_page"] or 0),
        last_read_ratio=float(row["last_read_ratio"] or 0.0),
        metadata=json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {},
        deleted_at=_from_iso(row["deleted_at"]) if row["deleted_at"] else None,
    )


def save_document(record: DocumentRecord) -> DocumentRecord:
    record.updated_at = _utcnow()
    DOCUMENTS[record.document_id] = record
    with db_cursor() as conn:
        conn.execute(
            """
            INSERT INTO documents (
                document_id, source_type, source_path, source_filename, status,
                original_pdf_url, translated_pdf_url, extracted_text, translated_text,
                artifacts_json, references_json, logs_json, created_at, updated_at, last_opened_at,
                size_bytes, progress, current_stage, current_stage_label, stage_started_at,
                eta_seconds, stages_json, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                source_type = excluded.source_type,
                source_path = excluded.source_path,
                source_filename = excluded.source_filename,
                status = excluded.status,
                original_pdf_url = excluded.original_pdf_url,
                translated_pdf_url = excluded.translated_pdf_url,
                extracted_text = excluded.extracted_text,
                translated_text = excluded.translated_text,
                artifacts_json = excluded.artifacts_json,
                references_json = excluded.references_json,
                logs_json = excluded.logs_json,
                updated_at = excluded.updated_at,
                last_opened_at = excluded.last_opened_at,
                size_bytes = excluded.size_bytes,
                progress = excluded.progress,
                current_stage = excluded.current_stage,
                current_stage_label = excluded.current_stage_label,
                stage_started_at = excluded.stage_started_at,
                eta_seconds = excluded.eta_seconds,
                stages_json = excluded.stages_json,
                deleted_at = excluded.deleted_at
            """,
            (
                record.document_id,
                record.source_type,
                str(record.source_path),
                record.source_filename,
                record.status,
                record.original_pdf_url,
                record.translated_pdf_url,
                record.extracted_text,
                record.translated_text,
                _serialize_items(record.artifacts),
                _serialize_items(record.references),
                json.dumps(record.logs, ensure_ascii=False),
                _to_iso(record.created_at),
                _to_iso(record.updated_at),
                _to_iso(record.last_opened_at),
                record.size_bytes,
                record.progress,
                record.current_stage,
                record.current_stage_label,
                record.stage_started_at,
                record.eta_seconds,
                _serialize_items(record.stages),
                _to_iso(record.deleted_at),
            ),
        )
        conn.execute(
            """
            UPDATE documents
            SET failure_json = ?, retry_count = ?
            WHERE document_id = ?
            """,
            (
                json.dumps(asdict(record.failure), ensure_ascii=False) if record.failure else None,
                record.retry_count,
                record.document_id,
            ),
        )
    return record


def get_document(document_id: str) -> DocumentRecord | None:
    cached = DOCUMENTS.get(document_id)
    if cached and not cached.deleted_at:
        return cached
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE document_id = ? AND deleted_at IS NULL",
            (document_id,),
        ).fetchone()
    if not row:
        return None
    record = _document_from_row(row)
    DOCUMENTS[document_id] = record
    return record


def list_documents() -> list[DocumentRecord]:
    results: dict[str, DocumentRecord] = {
        doc_id: doc for doc_id, doc in DOCUMENTS.items() if not doc.deleted_at
    }
    with db_cursor() as conn:
        rows = conn.execute(
            """
            SELECT * FROM documents
            WHERE deleted_at IS NULL
            ORDER BY COALESCE(last_opened_at, updated_at, created_at) DESC
            """
        ).fetchall()
    for row in rows:
        if row["document_id"] not in results:
            results[row["document_id"]] = _document_from_row(row)
    return sorted(
        results.values(),
        key=lambda item: (
            item.last_opened_at or item.updated_at or item.created_at,
            item.created_at,
        ),
        reverse=True,
    )


def touch_document_opened(record: DocumentRecord) -> DocumentRecord:
    record.last_opened_at = _utcnow()
    return save_document(record)


def update_reading_progress(document_id: str, page: int, ratio: float) -> None:
    """Persist reading position without bumping updated_at (history order)."""
    with db_cursor() as conn:
        conn.execute(
            "UPDATE documents SET last_read_page = ?, last_read_ratio = ? WHERE document_id = ?",
            (max(0, int(page)), max(0.0, min(1.0, float(ratio))), document_id),
        )
    record = DOCUMENTS.get(document_id)
    if record:
        record.last_read_page = max(0, int(page))
        record.last_read_ratio = max(0.0, min(1.0, float(ratio)))


def set_document_metadata(document_id: str, metadata: dict) -> None:
    with db_cursor() as conn:
        conn.execute(
            "UPDATE documents SET metadata_json = ? WHERE document_id = ?",
            (json.dumps(metadata, ensure_ascii=False), document_id),
        )
    record = DOCUMENTS.get(document_id)
    if record:
        record.metadata = metadata


def create_annotation(entry: AnnotationEntry) -> AnnotationEntry:
    with db_cursor() as conn:
        conn.execute(
            """
            INSERT INTO annotations (id, document_id, page, quote, color, note, position_ratio, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.document_id,
                entry.page,
                entry.quote,
                entry.color,
                entry.note,
                entry.position_ratio,
                entry.created_at,
            ),
        )
    return entry


def list_annotations_for_document(document_id: str) -> list[AnnotationEntry]:
    with db_cursor() as conn:
        rows = conn.execute(
            """
            SELECT * FROM annotations
            WHERE document_id = ?
            ORDER BY created_at ASC
            """,
            (document_id,),
        ).fetchall()
    return [
        AnnotationEntry(
            id=row["id"],
            document_id=row["document_id"],
            page=int(row["page"] or 1),
            quote=row["quote"] or "",
            color=row["color"] or "yellow",
            note=row["note"] or "",
            position_ratio=float(row["position_ratio"] or 0.0),
            created_at=row["created_at"] or "",
        )
        for row in rows
    ]


def delete_annotation(annotation_id: str, document_id: str) -> bool:
    with db_cursor() as conn:
        deleted = conn.execute(
            "DELETE FROM annotations WHERE id = ? AND document_id = ?",
            (annotation_id, document_id),
        ).rowcount
    return deleted == 1


def require_document(document_id: str) -> DocumentRecord:
    record = get_document(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found")
    return record


def queue_document_retry(document_id: str) -> tuple[DocumentRecord, str]:
    """Claim one failed document using a process lock plus SQLite compare-and-set."""
    with _RETRY_LOCK:
        record = require_document(document_id)
        if record.status != "failed":
            raise HTTPException(status_code=409, detail="Document is not in a retryable failed state")
        if record.failure and not record.failure.retryable:
            raise HTTPException(status_code=409, detail="This failure cannot be retried automatically")
        resume_from = (record.failure.stage if record.failure else record.current_stage) or "upload"
        with db_cursor() as conn:
            claimed = conn.execute(
                """
                UPDATE documents
                SET status = 'queued', retry_count = retry_count + 1
                WHERE document_id = ? AND status = 'failed'
                """,
                (document_id,),
            )
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Document retry was already claimed",
                )
        record.retry_count += 1
        if record.failure:
            record.failure.retry_count = record.retry_count
        record.status = "queued"
        record.logs.append(f"Retry {record.retry_count} queued from stage: {resume_from}")
        save_document(record)
        return record, resume_from


def queue_document_reprocess(
    document_id: str, resume_from: str
) -> tuple[DocumentRecord, str]:
    """Claim one document for reprocessing, whatever its current status.

    ``resume_from`` is decided by the caller from the caches that survived the
    previous run, so a reprocess reuses completed parse/translation work instead
    of paying for it again.
    """
    with _RETRY_LOCK:
        record = require_document(document_id)
        if record.status in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="Document is already being processed")
        if not record.source_path.is_file():
            raise HTTPException(status_code=409, detail="The source PDF is no longer available")
        with db_cursor() as conn:
            claimed = conn.execute(
                """
                UPDATE documents
                SET status = 'queued', retry_count = retry_count + 1, failure_json = NULL
                WHERE document_id = ? AND status NOT IN ('queued', 'processing')
                """,
                (document_id,),
            )
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Document reprocess was already claimed",
                )
        record.retry_count += 1
        record.status = "queued"
        record.failure = None
        record.logs.append(f"Reprocess {record.retry_count} queued from stage: {resume_from}")
        save_document(record)
        return record, resume_from


def mark_document_failed(document_id: str, stage: str, message: str) -> None:
    """Fail a queued/processing document that never reached the pipeline's own
    error handling (e.g. the background task died while loading settings).

    Leaves records in any other status untouched so completed or already
    failed documents keep their state.
    """
    record = DOCUMENTS.get(document_id)
    if record is None or record.status not in {"queued", "processing"}:
        return
    record.status = "failed"
    record.failure = FailureEntry(
        stage=stage,
        message=message,
        retryable=record.source_path.is_file(),
        retry_count=record.retry_count,
    )
    record.logs.append(f"Error: {message}")
    try:
        save_document(record)
    except Exception:  # noqa: BLE001 - best effort; startup heal covers restarts
        pass


def purge_document_artifacts(record: DocumentRecord) -> list[str]:
    """Delete everything the pipeline derived for a document.

    That is the document's whole output directory (translated and annotated
    PDFs, the extraction manifest and glossary, the alignment index, crops and
    previews) plus the source file uploaded for it. Returns a human-readable
    line per removed path, so the caller can report what happened.
    """
    import shutil

    from app.core.config import settings

    removed: list[str] = []

    output_dir = settings.output_dir / record.document_id
    if output_dir.is_dir():
        try:
            shutil.rmtree(output_dir)
        except OSError as exc:
            removed.append(f"could not remove output dir {output_dir.name}: {exc}")
        else:
            removed.append(f"removed output dir {output_dir.name}")

    # The upload is removed only when it really is this document's own file in
    # the upload directory: a record may point anywhere (tests, fixtures).
    source = Path(record.source_path)
    try:
        inside_uploads = source.parent.resolve() == settings.upload_dir.resolve()
    except OSError:
        inside_uploads = False
    if inside_uploads and source.is_file():
        try:
            size = source.stat().st_size
        except OSError:
            size = 0
        try:
            source.unlink()
        except OSError as exc:
            removed.append(f"could not remove source {source.name}: {exc}")
        else:
            removed.append(f"removed source {source.name} ({size} B)")

    return removed


def soft_delete_document(document_id: str) -> list[str]:
    """Remove a document from the library and delete its derived artifacts."""
    record = require_document(document_id)
    removed = purge_document_artifacts(record)
    record.deleted_at = _utcnow()
    save_document(record)
    DOCUMENTS.pop(document_id, None)
    return removed
