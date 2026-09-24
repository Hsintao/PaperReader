"""Always run the suite against disposable storage, never a developer's data."""

import os
import sys
import tempfile
from pathlib import Path

_test_data = tempfile.TemporaryDirectory(prefix="paperreader-tests-")
os.environ["DATA_DIR"] = _test_data.name
os.environ["PAPERREADER_ENV_FILE"] = os.path.join(_test_data.name, "missing.env")

# The translation worker is a package at the repository root, outside the
# backend's import root, so the suite can only import it with that root on the
# path.
_repo_root = str(Path(__file__).resolve().parents[2])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

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
        }
        values.update(overrides)
        app_settings.update_settings(**values)

    return _configure


@pytest.fixture(autouse=True)
def no_background_term_extraction(monkeypatch):
    """A finished document spawns a background LLM pass; tests stay offline."""
    from app.services import document_pipeline

    monkeypatch.setattr(document_pipeline, "schedule_extraction", lambda **kwargs: None)
