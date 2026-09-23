import json

from fastapi.testclient import TestClient

from app.main import app
from app.services import app_settings
from app.services.app_settings import load_settings, settings_path

_SETTINGS_KEYS = {
    "api_key_configured",
    "base_url",
    "model",
    "theme",
    "show_annotated_pdf",
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
