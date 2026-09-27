from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import urlparse, urlunparse

import requests
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.config import settings
from app.models.schemas import UploadResponse
from app.models.store import get_document, mark_document_failed
from app.services.app_settings import load_settings, require_provider_settings
from app.services.document_pipeline import create_document_record, process_document
from app.services.pdf_translation_worker import WorkerUnavailable, require_worker_ready


router = APIRouter()

_UPLOAD_CHUNK_BYTES = 1024 * 1024

_PDF_MAGIC = b"%PDF"
_ARXIV_HOST = "arxiv.org"
_ARXIV_ABS_PATH = re.compile(r"^/abs/(?P<identifier>[^/?#]+)")


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
    try:
        with target_path.open("wb") as sink:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                sink.write(chunk)
    except OSError:
        # A half-written upload is not a document and must not be left behind
        # for the next run to trip over.
        target_path.unlink(missing_ok=True)
        raise

    record = create_document_record(target_path, "pdf")

    background_tasks.add_task(_run_pipeline, record.document_id)
    return UploadResponse(document_id=record.document_id, status=record.status)


class SourceImportRequest(BaseModel):
    source: str


def _is_http_url(source: str) -> bool:
    return urlparse(source).scheme in ("http", "https")


def _resolve_pdf_url(source: str) -> str:
    """Point arXiv abstract pages at their PDF download address."""
    parsed = urlparse(source)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[len("www."):]
    match = _ARXIV_ABS_PATH.match(parsed.path)
    if host == _ARXIV_HOST and match:
        path = f"/pdf/{match.group('identifier')}"
        return urlunparse(parsed._replace(path=path))
    return source


def _safe_pdf_name(source: str, is_url: bool) -> str:
    raw = Path(urlparse(source).path).name if is_url else Path(source).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not name:
        name = "source"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name


def _copy_local_pdf(source: str, target_path: Path) -> None:
    path = Path(source).expanduser()
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_source",
                "message": f"本地路径必须指向已存在的 .pdf 文件：{source}",
            },
        )
    with path.open("rb") as handle, target_path.open("wb") as sink:
        shutil.copyfileobj(handle, sink, _UPLOAD_CHUNK_BYTES)


def _download_pdf(url: str, target_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with target_path.open("wb") as sink:
            for chunk in response.iter_content(chunk_size=_UPLOAD_CHUNK_BYTES):
                if chunk:
                    sink.write(chunk)


def _check_pdf_header(target_path: Path) -> None:
    with target_path.open("rb") as handle:
        if handle.read(len(_PDF_MAGIC)) != _PDF_MAGIC:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "not_a_pdf",
                    "message": "来源内容不是 PDF 文件。",
                },
            )


def _import_failure_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        return str(detail)
    return str(exc)


def _run_import_pipeline(record_id: str, url: str, target_path: Path) -> None:
    """Download the source off the request path, then run the pipeline.

    The document already exists and shows as queued while the download runs; a
    failed download fails the document in place instead of blocking the import
    request for the whole transfer.
    """
    try:
        _download_pdf(url, target_path)
        _check_pdf_header(target_path)
    except Exception as exc:  # noqa: BLE001 - every failure becomes document state
        target_path.unlink(missing_ok=True)
        mark_document_failed(record_id, "download", _import_failure_message(exc))
        return
    record = get_document(record_id)
    if record is None or record.deleted_at is not None:
        # Deleted while the download was running; the purge could not see a
        # file that did not exist yet, so remove it here.
        target_path.unlink(missing_ok=True)
        return
    _run_pipeline(record_id)


@router.post("/import", response_model=UploadResponse)
def import_source(
    background_tasks: BackgroundTasks,
    body: SourceImportRequest,
) -> UploadResponse:
    if not getattr(load_settings(), "enable_source_links", False):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_links_disabled",
                "message": "请先在界面设置中开启“路径/链接导入”。",
            },
        )
    require_provider_settings()
    try:
        require_worker_ready()
    except WorkerUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "worker_unavailable", "message": str(exc)},
        ) from exc

    source = body.source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="来源不能为空。")
    if urlparse(source).scheme and not _is_http_url(source):
        raise HTTPException(
            status_code=400,
            detail="仅支持 http:// 或 https:// 链接，或后端机器上的 .pdf 本地路径。",
        )

    is_url = _is_http_url(source)
    if is_url:
        source = _resolve_pdf_url(source)
    safe_name = _safe_pdf_name(source, is_url=is_url)
    target_path = settings.upload_dir / f"{uuid.uuid4()}_{safe_name}"
    if is_url:
        record = create_document_record(target_path, "pdf")
        background_tasks.add_task(
            _run_import_pipeline, record.document_id, source, target_path
        )
        return UploadResponse(document_id=record.document_id, status=record.status)
    try:
        _copy_local_pdf(source, target_path)
        _check_pdf_header(target_path)
    except HTTPException:
        target_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=502,
            detail={"code": "source_fetch_failed", "message": str(exc)},
        ) from exc

    record = create_document_record(target_path, "pdf")

    background_tasks.add_task(_run_pipeline, record.document_id)
    return UploadResponse(document_id=record.document_id, status=record.status)
