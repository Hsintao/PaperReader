import pytest

from app.core.config import settings
from app.services.llm_client import llm_client


def test_upload_streams_file_to_disk(client, configure_provider, monkeypatch):
    from app.api import routes_upload

    launched: list[str] = []
    monkeypatch.setattr(routes_upload, "_run_pipeline", lambda record_id: launched.append(record_id))
    configure_provider()

    payload = b"%PDF-1.7\n" + b"x" * (256 * 1024)
    response = client.post(
        "/api/upload",
        files={"file": ("paper.pdf", payload, "application/pdf")},
        data={"vision_check_enabled": "false", "vision_check_mode": "auto"},
    )

    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert launched == [document_id]

    payload_size = len(payload)
    matches = [
        path for path in settings.upload_dir.iterdir()
        if path.name.endswith("paper.pdf") and path.stat().st_size == payload_size
    ]
    assert matches, "uploaded file must land on disk"
    assert matches[0].read_bytes() == payload


def test_upload_rejects_non_pdf_sources(client, configure_provider, monkeypatch):
    from app.api import routes_upload

    launched: list[str] = []
    monkeypatch.setattr(routes_upload, "_run_pipeline", lambda record_id: launched.append(record_id))
    configure_provider()

    response = client.post(
        "/api/upload",
        files={"file": ("paper.tex", b"\\documentclass{article}", "application/x-tex")},
    )

    assert response.status_code == 400, response.text
    assert launched == []


def test_upload_requires_provider_configuration(client, monkeypatch):
    from app.api import routes_upload

    monkeypatch.setattr(routes_upload, "_run_pipeline", lambda record_id: None)

    response = client.post("/api/upload", files={"file": ("paper.pdf", b"%PDF-1.4")})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "config_required"


def test_mineru_parser_requires_mineru_key(client, configure_provider, monkeypatch):
    from app.api import routes_upload

    monkeypatch.setattr(routes_upload, "_run_pipeline", lambda record_id: None)
    configure_provider(pdf_parser="mineru")

    response = client.post("/api/upload", files={"file": ("paper.pdf", b"%PDF-1.4")})

    assert response.status_code == 409
    assert "MinerU" in response.json()["detail"]["message"]


def test_somark_parser_requires_somark_key(client, configure_provider, monkeypatch):
    from app.api import routes_upload

    monkeypatch.setattr(routes_upload, "_run_pipeline", lambda record_id: None)
    configure_provider(pdf_parser="somark")

    response = client.post("/api/upload", files={"file": ("paper.pdf", b"%PDF-1.4")})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "config_required"
    assert "SoMark" in response.json()["detail"]["message"]


def test_chat_without_api_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")

    with pytest.raises(RuntimeError, match="No API key configured"):
        llm_client.chat(message="hello", system_prompt="be brief", override_api_key="")
