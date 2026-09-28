import json

from fastapi.testclient import TestClient

from app.main import app
from app.services import app_settings
from app.services.app_settings import load_settings, settings_path

_SETTINGS_KEYS = {
    "api_key_configured",
    "provider",
    "base_url",
    "model",
    "theme",
    "show_annotated_pdf",
    "enable_source_links",
    "enable_thinking",
    "translation_domain",
    "favorites",
}


def test_settings_roundtrip_masks_keys(isolated_storage):
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/me/providers",
            json={
                "api_key": "secret-key",
                "base_url": "https://llm.example/v1",
                "model": "paper-model",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["api_key_configured"] is True
        assert "secret-key" not in response.text
        assert payload["base_url"] == "https://llm.example/v1"
        assert payload["model"] == "paper-model"

        stored = client.get("/api/settings/me")
        assert stored.status_code == 200
        assert stored.json() == payload


def test_settings_contract_has_no_parser_fields(isolated_storage):
    with TestClient(app) as client:
        payload = client.get("/api/settings/me").json()

    assert set(payload) == _SETTINGS_KEYS


def test_a_legacy_parser_payload_is_ignored(isolated_storage):
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/me/providers",
            json={"api_key": "secret-key", "pdf_parser": "mineru", "mineru_api_key": "m"},
        )
        assert response.status_code == 200, response.text
        assert set(response.json()) == _SETTINGS_KEYS

    stored = json.loads(settings_path().read_text(encoding="utf-8"))
    assert "pdf_parser" not in stored
    assert "mineru_api_key" not in stored
    assert stored["api_key"] == "secret-key"


def test_startup_purges_retired_parser_settings(isolated_storage):
    settings_path().write_text(
        json.dumps(
            {
                "api_key": "kept-key",
                "pdf_parser": "somark",
                "somark_base_url": "https://somark.cn/api/v1",
                "mineru_api_key": "mineru-secret",
                "vision_model": "vision-model",
                "vision_enabled": True,
                "vision_mode": "manual",
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )

    assert app_settings.purge_removed_keys() is True
    stored = json.loads(settings_path().read_text(encoding="utf-8"))
    assert (stored["api_key"], stored["theme"]) == ("kept-key", "dark")
    assert "vision_model" not in stored
    assert app_settings.purge_removed_keys() is False


def test_settings_file_is_owner_only(isolated_storage):
    path = settings_path()
    assert path == isolated_storage / "settings.json"
    app_settings.update_settings(
        api_key="secret-key", base_url="https://llm.example/v1", model="m"
    )

    assert path.is_file()
    assert path.stat().st_mode & 0o077 == 0


def test_blank_key_keeps_stored_value_and_clear_resets_it(isolated_storage):
    app_settings.update_settings(
        api_key="first-key", base_url="https://llm.example/v1", model="m"
    )
    app_settings.update_settings(api_key="", base_url="https://llm.example/v1", model="m")
    assert load_settings().api_key == "first-key"

    app_settings.update_settings(clear_api_key=True)
    assert load_settings().api_key == ""


def test_provider_validation_rejects_bad_urls(isolated_storage):
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/me/providers",
            json={"base_url": "llm.example.com", "model": "m"},
        )
        assert response.status_code == 400, response.text


def test_provider_roundtrip_and_unknown_falls_back_to_custom(isolated_storage):
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/me/providers",
            json={
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "Qwen/Qwen2.5-7B-Instruct",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["provider"] == "siliconflow"

        stored = json.loads(settings_path().read_text(encoding="utf-8"))
        assert stored["provider"] == "siliconflow"

        fallback = client.put("/api/settings/me/providers", json={"provider": "unknown-llm"})
        assert fallback.status_code == 200, fallback.text
        assert fallback.json()["provider"] == "custom"


def test_preferences_persist_across_clients(isolated_storage):
    with TestClient(app) as first:
        response = first.put("/api/settings/me", json={"theme": "dark", "favorites": ["doc-1"]})
        assert response.status_code == 200, response.text
        assert response.json()["theme"] == "dark"

    with TestClient(app) as second:
        payload = second.get("/api/settings/me").json()
        assert payload["theme"] == "dark"
        assert payload["favorites"] == ["doc-1"]


def test_show_annotated_pdf_defaults_off_and_keeps_explicit_choice(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["show_annotated_pdf"] is False

        updated = client.put("/api/settings/me", json={"show_annotated_pdf": True})
        assert updated.status_code == 200, updated.text
        assert updated.json()["show_annotated_pdf"] is True

    with TestClient(app) as second:
        assert second.get("/api/settings/me").json()["show_annotated_pdf"] is True


def test_enable_source_links_defaults_off_and_keeps_explicit_choice(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["enable_source_links"] is False

        updated = client.put("/api/settings/me", json={"enable_source_links": True})
        assert updated.status_code == 200, updated.text
        assert updated.json()["enable_source_links"] is True

    with TestClient(app) as second:
        assert second.get("/api/settings/me").json()["enable_source_links"] is True
        turned_off = second.put("/api/settings/me", json={"enable_source_links": False})
        assert turned_off.status_code == 200, turned_off.text
        assert turned_off.json()["enable_source_links"] is False

    with TestClient(app) as third:
        assert third.get("/api/settings/me").json()["enable_source_links"] is False


def test_enable_thinking_defaults_off_and_keeps_explicit_choice(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["enable_thinking"] is False

        updated = client.put("/api/settings/me/providers", json={"enable_thinking": True})
        assert updated.status_code == 200, updated.text
        assert updated.json()["enable_thinking"] is True

    with TestClient(app) as second:
        assert second.get("/api/settings/me").json()["enable_thinking"] is True
        turned_off = second.put("/api/settings/me", json={"enable_thinking": False})
        assert turned_off.status_code == 200, turned_off.text
        assert turned_off.json()["enable_thinking"] is False

    with TestClient(app) as third:
        assert third.get("/api/settings/me").json()["enable_thinking"] is False


def test_translation_domain_roundtrip_and_validation(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["translation_domain"] == "general"

        updated = client.put("/api/settings/me", json={"translation_domain": "medical"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["translation_domain"] == "medical"

    with TestClient(app) as second:
        assert second.get("/api/settings/me").json()["translation_domain"] == "medical"
        assert (
            second.put("/api/settings/me", json={"translation_domain": "astrology"})
            .json()["translation_domain"]
            == "general"
        )


class _ProbeResponse:
    """Stand-in for the provider's chat completion response."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "pong"}}]
        }
        self.text = text
        self.reason = "reason"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def _stub_probe_request(monkeypatch, response=None, exc=None):
    """Replace app_settings.requests so connectivity probes stay offline."""
    import requests as real_requests

    captured: dict = {}

    class _RequestsStub:
        RequestException = real_requests.RequestException
        HTTPError = real_requests.HTTPError

        @staticmethod
        def post(url, **kwargs):
            captured.update({"url": url, **kwargs})
            if exc is not None:
                raise exc
            return response

    monkeypatch.setattr(app_settings, "requests", _RequestsStub)
    return captured


def test_test_provider_requires_an_api_key(isolated_storage):
    with TestClient(app) as client:
        response = client.post("/api/settings/test-provider", json={})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "config_required"


def test_test_provider_pings_the_draft_configuration(isolated_storage, monkeypatch):
    captured = _stub_probe_request(monkeypatch, response=_ProbeResponse())

    with TestClient(app) as client:
        response = client.post(
            "/api/settings/test-provider",
            json={
                "api_key": "fresh-key",
                "base_url": "https://llm.example/v1/",
                "model": "paper-model",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "message": "连接成功，模型 paper-model 响应正常。"}
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer fresh-key"
    assert captured["json"]["model"] == "paper-model"
    assert captured["json"]["max_tokens"] == 1


def test_test_provider_falls_back_to_the_saved_settings(isolated_storage, monkeypatch):
    captured = _stub_probe_request(monkeypatch, response=_ProbeResponse())

    with TestClient(app) as client:
        saved = client.put(
            "/api/settings/me/providers",
            json={
                "api_key": "stored-key",
                "base_url": "https://saved.example/v1",
                "model": "saved-model",
            },
        )
        assert saved.status_code == 200, saved.text

        response = client.post("/api/settings/test-provider", json={})

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert captured["url"] == "https://saved.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer stored-key"
    assert captured["json"]["model"] == "saved-model"


def test_test_provider_reports_http_failures(isolated_storage, monkeypatch):
    _stub_probe_request(
        monkeypatch, response=_ProbeResponse(status_code=401, text='{"error":"bad key"}')
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/settings/test-provider",
            json={"api_key": "k", "base_url": "https://x.example/v1", "model": "m"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False
    assert "HTTP 401" in payload["message"]
    assert "bad key" in payload["message"]


def test_test_provider_reports_connection_failures(isolated_storage, monkeypatch):
    import requests

    _stub_probe_request(monkeypatch, exc=requests.ConnectionError("connection refused"))

    with TestClient(app) as client:
        response = client.post(
            "/api/settings/test-provider",
            json={"api_key": "k", "base_url": "https://x.example/v1", "model": "m"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False
    assert "无法连接到服务" in payload["message"]
