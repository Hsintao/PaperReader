"""Upload contract: file type, translation provider, worker availability."""

import sys
from pathlib import Path

import pytest

from app.core.config import settings


def _capture_launches(monkeypatch) -> list[str]:
    from app.api import routes_upload

    launched: list[str] = []
    monkeypatch.setattr(
        routes_upload, "_run_pipeline", lambda document_id: launched.append(document_id)
    )
    return launched


def _point_at_a_runnable_worker(monkeypatch) -> None:
    monkeypatch.setattr(settings, "pdfmathtranslate_worker", sys.executable)


def _uploaded_files() -> list[Path]:
    return list(settings.upload_dir.iterdir())


def test_upload_accepts_only_pdf_files(client, configure_provider, monkeypatch):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    response = client.post(
        "/api/upload",
        files={"file": ("paper.tex", b"\\documentclass{article}", "application/x-tex")},
    )

    assert response.status_code == 400, response.text
    assert launched == []
    assert _uploaded_files() == []


def test_upload_requires_a_translation_provider(client, monkeypatch):
    launched = _capture_launches(monkeypatch)
    _point_at_a_runnable_worker(monkeypatch)

    response = client.post("/api/upload", files={"file": ("paper.pdf", b"%PDF-1.4")})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "config_required"
    assert launched == []
    assert _uploaded_files() == []


def test_upload_reports_a_worker_that_cannot_be_started(client, configure_provider, monkeypatch):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    monkeypatch.setattr(
        settings, "pdfmathtranslate_worker", "/nonexistent/pdfmathtranslate-worker"
    )

    response = client.post("/api/upload", files={"file": ("paper.pdf", b"%PDF-1.4")})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "worker_unavailable"
    assert "/nonexistent/pdfmathtranslate-worker" in detail["message"]
    assert launched == []
    assert _uploaded_files() == []


def test_upload_stores_the_pdf_and_queues_the_pipeline(client, configure_provider, monkeypatch):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    payload = b"%PDF-1.7\n" + b"x" * (256 * 1024)
    response = client.post(
        "/api/upload",
        files={"file": ("paper.pdf", payload, "application/pdf")},
    )

    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert response.json()["status"] == "queued"
    assert launched == [document_id]

    stored = [path for path in _uploaded_files() if path.name.endswith("paper.pdf")]
    assert len(stored) == 1
    assert stored[0].read_bytes() == payload

    document = client.get(f"/api/document/{document_id}")
    assert document.status_code == 200, document.text
    assert document.json()["source_filename"] == "paper.pdf"
    assert document.json()["status"] == "queued"


def test_upload_removes_a_partial_file_when_the_write_fails(
    client, configure_provider, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    real_open = Path.open

    class _PartialWrite:
        """Writes the first chunk, then fails like a full disk would."""

        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, chunk) -> int:
            written = self._handle.write(chunk)
            raise OSError("no space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            self._handle.close()
            return False

    def failing_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self.parent == settings.upload_dir:
            return _PartialWrite(handle)
        return handle

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(OSError):
        client.post(
            "/api/upload",
            files={"file": ("paper.pdf", b"%PDF-1.7\n" + b"x" * 4096, "application/pdf")},
        )

    # The partial file is gone and no document was queued for it.
    assert _uploaded_files() == []
    assert launched == []
