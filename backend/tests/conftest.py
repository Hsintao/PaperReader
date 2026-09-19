"""Always run the suite against disposable storage, never a developer's data."""

import os
import tempfile

_test_data = tempfile.TemporaryDirectory(prefix="paperreader-tests-")
os.environ["DATA_DIR"] = _test_data.name
os.environ["PAPERREADER_ENV_FILE"] = os.path.join(_test_data.name, "missing.env")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.core.database import init_database
    from app.models import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(store, "DOCUMENTS", {})
    settings.upload_dir.mkdir()
    settings.output_dir.mkdir()
    init_database()
    return tmp_path


@pytest.fixture
def client(isolated_storage):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def configure_provider(monkeypatch):
    """Point the app at a throwaway LLM endpoint so uploads pass preflight."""
    from app.services import app_settings

    def _configure(**overrides) -> None:
        values = {
            "api_key": "test-key",
            "base_url": "https://llm.example/v1",
            "model": "test-model",
            "pdf_parser": "local",
        }
        values.update(overrides)
        app_settings.update_settings(**values)

    return _configure
