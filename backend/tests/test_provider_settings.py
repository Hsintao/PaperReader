from fastapi.testclient import TestClient

from app.main import app
from app.services import app_settings
from app.services.app_settings import load_settings, settings_path


def test_settings_roundtrip_masks_keys(isolated_storage):
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/me/providers",
            json={
                "api_key": "secret-key",
                "base_url": "https://llm.example/v1",
                "model": "paper-model",
                "pdf_parser": "local",
                "somark_api_key": "somark-secret",
                "mineru_api_key": "mineru-secret",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["api_key_configured"] is True
        assert payload["somark_api_key_configured"] is True
        assert payload["mineru_api_key_configured"] is True
        assert "secret-key" not in response.text
        assert "somark-secret" not in response.text
        assert "mineru-secret" not in response.text
        assert payload["base_url"] == "https://llm.example/v1"
        assert payload["model"] == "paper-model"

        stored = client.get("/api/settings/me")
        assert stored.status_code == 200
        assert stored.json() == payload


def test_default_parser_is_somark(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["pdf_parser"] == "somark"


def test_somark_base_url_defaults_and_rejects_bad_values(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["somark_base_url"] == "https://somark.cn/api/v1"

        updated = client.put(
            "/api/settings/me/providers", json={"somark_base_url": "https://somark.ai/api/v1"}
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["somark_base_url"] == "https://somark.ai/api/v1"

        rejected = client.put("/api/settings/me/providers", json={"somark_base_url": "somark.cn"})
        assert rejected.status_code == 400, rejected.text
        assert "SoMark Base URL" in rejected.text


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


def test_provider_keys_update_independently(isolated_storage):
    from app.services.app_settings import load_settings

    app_settings.update_settings(api_key="llm-one", mineru_api_key="mineru-one")
    app_settings.update_settings(api_key="llm-two", mineru_api_key="  ")
    stored = load_settings()
    assert (stored.api_key, stored.mineru_api_key) == ("llm-two", "mineru-one")

    app_settings.update_settings(api_key="", mineru_api_key="mineru-two")
    stored = load_settings()
    assert (stored.api_key, stored.mineru_api_key) == ("llm-two", "mineru-two")

    app_settings.update_settings(clear_mineru_api_key=True)
    stored = load_settings()
    assert (stored.api_key, stored.mineru_api_key) == ("llm-two", "")


def test_somark_key_updates_independently_of_mineru(isolated_storage):
    from app.services.app_settings import load_settings

    app_settings.update_settings(somark_api_key="somark-one", mineru_api_key="mineru-one")
    app_settings.update_settings(somark_api_key="  ", mineru_api_key="mineru-two")
    stored = load_settings()
    assert (stored.somark_api_key, stored.mineru_api_key) == ("somark-one", "mineru-two")

    app_settings.update_settings(clear_somark_api_key=True)
    stored = load_settings()
    assert (stored.somark_api_key, stored.mineru_api_key) == ("", "mineru-two")


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


def test_vision_check_defaults_off_and_keeps_explicit_choice(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/settings/me").json()["vision_enabled"] is False

        updated = client.put("/api/settings/me", json={"vision_enabled": True})
        assert updated.status_code == 200
        assert updated.json()["vision_enabled"] is True
        assert client.get("/api/settings/me").json()["vision_enabled"] is True


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
