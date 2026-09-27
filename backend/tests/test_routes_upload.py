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


class _FakeResponse:
    """Stand-in for a streamed requests response."""

    def __init__(self, chunks, status_code=200) -> None:
        self._chunks = chunks
        self.status_code = status_code

    def raise_for_status(self) -> None:
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _stub_http_get(monkeypatch, chunks, captured_urls) -> None:
    """Replace routes_upload.requests so downloads stay offline."""
    import requests

    from app.api import routes_upload

    class _RequestsStub:
        RequestException = requests.RequestException

        @staticmethod
        def get(url, **kwargs):
            captured_urls.append(url)
            return _FakeResponse(chunks)

    monkeypatch.setattr(routes_upload, "requests", _RequestsStub)


@pytest.fixture
def enable_source_links(monkeypatch):
    """Turn the switch on without depending on how the setting is stored."""
    from app.api import routes_upload

    real_load = routes_upload.load_settings

    def _load():
        loaded = real_load()
        loaded.enable_source_links = True
        return loaded

    monkeypatch.setattr(routes_upload, "load_settings", _load)


def test_import_rejects_when_source_links_are_disabled(
    client, configure_provider, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    response = client.post("/api/import", json={"source": "/tmp/paper.pdf"})

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "source_links_disabled"
    assert launched == []
    assert _uploaded_files() == []


def test_import_copies_a_local_pdf(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    incoming = settings.data_dir / "incoming"
    incoming.mkdir()
    payload = b"%PDF-1.7\n" + b"y" * (128 * 1024)
    (incoming / "local paper.pdf").write_bytes(payload)

    response = client.post("/api/import", json={"source": str(incoming / "local paper.pdf")})

    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert response.json()["status"] == "queued"
    assert launched == [document_id]

    stored = [path for path in _uploaded_files() if path.name.endswith("local_paper.pdf")]
    assert len(stored) == 1
    assert stored[0].read_bytes() == payload

    document = client.get(f"/api/document/{document_id}")
    assert document.status_code == 200, document.text
    assert document.json()["source_filename"] == "local_paper.pdf"
    assert document.json()["status"] == "queued"


def test_import_downloads_a_direct_pdf_url(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    payload = b"%PDF-1.4\n" + b"z" * 4096
    captured_urls: list[str] = []
    _stub_http_get(monkeypatch, [payload[:100], payload[100:]], captured_urls)

    response = client.post("/api/import", json={"source": "https://example.com/paper.pdf"})

    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert launched == [document_id]
    assert captured_urls == ["https://example.com/paper.pdf"]

    stored = _uploaded_files()
    assert len(stored) == 1
    assert stored[0].name.endswith("paper.pdf")
    assert stored[0].read_bytes() == payload


def test_import_rewrites_arxiv_abstract_links(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    payload = b"%PDF-1.4\narxiv"
    captured_urls: list[str] = []
    _stub_http_get(monkeypatch, [payload], captured_urls)

    response = client.post(
        "/api/import", json={"source": "https://arxiv.org/abs/1706.03762v2"}
    )

    assert response.status_code == 200, response.text
    assert captured_urls == ["https://arxiv.org/pdf/1706.03762v2"]
    assert launched == [response.json()["document_id"]]
    stored = _uploaded_files()
    assert len(stored) == 1
    assert stored[0].name.endswith("1706.03762v2.pdf")
    assert stored[0].read_bytes() == payload


def test_import_rejects_an_invalid_local_path(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    response = client.post("/api/import", json={"source": "/nonexistent/paper.pdf"})

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_source"
    assert launched == []
    assert _uploaded_files() == []


def test_import_rejects_a_non_pdf_local_path(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    notes = settings.data_dir / "notes.txt"
    notes.write_text("not a pdf")

    response = client.post("/api/import", json={"source": str(notes)})

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_source"
    assert launched == []
    assert _uploaded_files() == []


def test_import_rejects_an_unsupported_url_scheme(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    response = client.post("/api/import", json={"source": "ftp://example.com/paper.pdf"})

    assert response.status_code == 400, response.text
    assert launched == []
    assert _uploaded_files() == []


def test_import_fails_the_document_when_a_pdf_url_returns_html(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    captured_urls: list[str] = []
    _stub_http_get(monkeypatch, [b"<html><body>404</body></html>"], captured_urls)

    response = client.post("/api/import", json={"source": "https://example.com/paper.pdf"})

    # URL imports return immediately; the download runs in the background and
    # its failure lands on the document instead of the response.
    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert launched == []

    document = client.get(f"/api/document/{document_id}")
    assert document.status_code == 200, document.text
    assert document.json()["status"] == "failed"
    assert document.json()["failure"]["stage"] == "download"
    assert "不是 PDF" in document.json()["failure"]["message"]
    assert _uploaded_files() == []


def test_import_fails_the_document_and_cleans_up_when_the_download_breaks(
    client, configure_provider, enable_source_links, monkeypatch
):
    launched = _capture_launches(monkeypatch)
    configure_provider()
    _point_at_a_runnable_worker(monkeypatch)

    import requests

    from app.api import routes_upload

    def _breaking_chunks(chunks):
        yield chunks[0]
        raise requests.ConnectionError("connection reset")

    captured_urls: list[str] = []
    chunks = [b"%PDF-1.4\n", b"never delivered"]

    class _RequestsStub:
        RequestException = requests.RequestException

        @staticmethod
        def get(url, **kwargs):
            captured_urls.append(url)
            return _FakeResponse(_breaking_chunks(chunks))

    monkeypatch.setattr(routes_upload, "requests", _RequestsStub)

    response = client.post("/api/import", json={"source": "https://example.com/paper.pdf"})

    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert captured_urls == ["https://example.com/paper.pdf"]
    assert launched == []

    document = client.get(f"/api/document/{document_id}")
    assert document.status_code == 200, document.text
    assert document.json()["status"] == "failed"
    assert document.json()["failure"]["stage"] == "download"
    assert "connection reset" in document.json()["failure"]["message"]
    assert _uploaded_files() == []
