"""Serve stored artifacts and uploaded sources.

Only files the application itself produced are reachable: the database, the
settings file, and anything else outside the upload and output directories
stay private even though the server is reachable on localhost.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings


router = APIRouter()


@router.api_route("/data/{file_path:path}", methods=["GET", "HEAD"])
def get_data_file(file_path: str) -> FileResponse:
    target = (settings.data_dir / file_path).resolve()
    allowed_roots = (settings.upload_dir.resolve(), settings.output_dir.resolve())
    if not target.is_file() or not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, headers={"Cache-Control": "private, no-store"})
