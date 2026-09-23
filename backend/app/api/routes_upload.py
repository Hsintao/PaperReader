from pathlib import Path
import uuid

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from app.core.config import settings
from app.models.schemas import UploadResponse
from app.models.store import get_document, mark_document_failed
from app.services.app_settings import load_settings, require_provider_settings
from app.services.document_pipeline import create_document_record, process_document
from app.services.pdf_translation_worker import WorkerUnavailable, require_worker_ready


router = APIRouter()

_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _run_pipeline(record_id: str) -> None:
    record = get_document(record_id)
    if not record:
        return
    try:
        process_document(record, provider_settings=load_settings())
    except Exception as exc:  # noqa: BLE001 - never strand the document as queued
        mark_document_failed(record_id, "upload", f"Pipeline failed to start: {exc}")


@router.post("/upload", response_model=UploadResponse)
async def upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    vision_check_enabled: bool = Form(False),
    vision_check_mode: str = Form("auto"),
) -> UploadResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".pdf":
        raise HTTPException(status_code=400, detail="Only .pdf files are supported")
    require_provider_settings()
    try:
        require_worker_ready()
    except WorkerUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "worker_unavailable", "message": str(exc)},
        ) from exc

    safe_name = Path(file.filename or "uploaded_file").name
    target_path = settings.upload_dir / f"{uuid.uuid4()}_{safe_name}"
    with target_path.open("wb") as sink:
        while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
            sink.write(chunk)

    record = create_document_record(target_path, "pdf")
    record.vision_check_enabled = bool(vision_check_enabled)
    record.vision_check_mode = vision_check_mode if vision_check_mode in ("auto", "manual") else "auto"

    background_tasks.add_task(_run_pipeline, record.document_id)
    return UploadResponse(document_id=record.document_id, status=record.status)
