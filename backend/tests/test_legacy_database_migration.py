"""Databases created before the accounts were removed must still open.

The old `documents` and `annotations` tables carried
`owner_user_id INTEGER NOT NULL`, which the post-accounts code never supplies.
Without a rebuild every insert and update fails the constraint. These tests
build that exact schema and assert the data survives the upgrade.
"""

import sqlite3
from contextlib import contextmanager

import pytest

_OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, avatar_path TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, last_login_at TEXT
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE user_settings (user_id INTEGER PRIMARY KEY, llm_api_key_enc TEXT);
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, name TEXT NOT NULL,
    dir TEXT NOT NULL, files_json TEXT NOT NULL DEFAULT '[]', main_tex TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, source_type TEXT NOT NULL,
    source_path TEXT NOT NULL, source_filename TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    original_pdf_url TEXT, translated_pdf_url TEXT, extracted_text TEXT NOT NULL DEFAULT '',
    translated_text TEXT NOT NULL DEFAULT '', artifacts_json TEXT NOT NULL DEFAULT '[]',
    references_json TEXT NOT NULL DEFAULT '[]', logs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_opened_at TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0, progress INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT, current_stage_label TEXT, stage_started_at REAL, eta_seconds INTEGER,
    stages_json TEXT NOT NULL DEFAULT '[]', main_tex TEXT,
    vision_check_enabled INTEGER NOT NULL DEFAULT 0, vision_check_mode TEXT NOT NULL DEFAULT 'auto',
    pending_reviews_json TEXT NOT NULL DEFAULT '[]', last_compile_warning TEXT,
    translated_tex_path TEXT, failure_json TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
    latex_recovery_json TEXT, deleted_at TEXT
);
CREATE TABLE annotations (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
    page INTEGER NOT NULL DEFAULT 1, quote TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT 'yellow', note TEXT NOT NULL DEFAULT '',
    position_ratio REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
INSERT INTO users VALUES (1, 'olduser', 'hash', NULL, '2025-01-01T00:00:00+00:00',
    '2025-01-01T00:00:00+00:00', NULL);
INSERT INTO documents (document_id, owner_user_id, source_type, source_path, source_filename,
    status, created_at, updated_at, extracted_text, translated_text) VALUES
    ('legacy-doc', 1, 'pdf', '/tmp/legacy/uploads/a.pdf', 'a.pdf', 'failed',
     '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', 'legacy text', '旧译文');
INSERT INTO annotations (id, document_id, owner_user_id, page, quote, color, note,
    position_ratio, created_at) VALUES
    ('ann-1', 'legacy-doc', 1, 1, 'legacy quote', 'yellow', 'legacy note', 0.1,
     '2025-01-01T00:00:00+00:00');
"""


@contextmanager
def _started_client():
    """Start the app on the already-written legacy database."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def legacy_database(isolated_storage):
    """Write a pre-accounts database where the app will look for it."""
    from app.core.config import settings

    path = settings.data_dir / settings.sqlite_db_name
    # isolated_storage already created the current schema; replace it wholesale.
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.commit()
    conn.close()
    return path


def test_open_document_succeeds_after_owner_column_removal(legacy_database):
    """Opening a document writes `last_opened_at`, the exact path that failed."""
    with _started_client() as client:
        response = client.get("/api/document/legacy-doc")
        assert response.status_code == 200, response.text

        saved = client.patch("/api/document/legacy-doc/progress", json={"page": 5, "ratio": 0.5})
        assert saved.status_code == 200, saved.text
        assert client.get("/api/document/legacy-doc").json()["last_read_page"] == 5


def test_legacy_rows_and_annotations_survive(legacy_database):
    with _started_client() as client:
        listed = client.get("/api/documents").json()
        assert [item["document_id"] for item in listed] == ["legacy-doc"]

        document = client.get("/api/document/legacy-doc").json()
        assert document["source_filename"] == "a.pdf"
        assert document["status"] == "failed"

        annotations = client.get("/api/document/legacy-doc/annotations").json()
        assert len(annotations) == 1
        assert annotations[0]["quote"] == "legacy quote"
        assert annotations[0]["note"] == "legacy note"


def test_legacy_tables_and_owner_columns_are_gone(legacy_database):
    with _started_client() as client:
        client.get("/api/documents")

    conn = sqlite3.connect(legacy_database)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        document_columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        annotation_columns = {row[1] for row in conn.execute("PRAGMA table_info(annotations)")}
    finally:
        conn.close()

    assert not ({"users", "sessions", "user_settings", "projects"} & tables)
    assert "owner_user_id" not in document_columns
    assert "owner_user_id" not in annotation_columns
    # The rebuild must not leave its scratch table behind.
    assert not any(name.endswith("__rebuild") for name in tables)


def test_rebuild_is_idempotent(legacy_database):
    """A second startup sees the new schema and leaves it alone."""
    from app.core.database import init_database

    with _started_client() as client:
        before = client.get("/api/documents").json()
        init_database()
        init_database()
        assert client.get("/api/documents").json() == before
        assert client.get("/api/document/legacy-doc").status_code == 200


def test_importing_the_database_module_recovers_nothing(isolated_storage):
    """Schema creation and recovery belong to startup, not to importing the module."""
    import importlib

    from app.core import database
    from app.core.config import settings
    from app.models import store

    source = settings.upload_dir / "queued.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    store.save_document(
        store.DocumentRecord("queued-doc", "pdf", source, status="queued")
    )
    store.DOCUMENTS.clear()

    importlib.reload(database)

    def _row() -> tuple[str, str | None]:
        conn = sqlite3.connect(settings.data_dir / settings.sqlite_db_name)
        try:
            return conn.execute(
                "SELECT status, failure_json FROM documents WHERE document_id = 'queued-doc'"
            ).fetchone()
        finally:
            conn.close()

    status, failure = _row()
    assert status == "queued"
    assert failure is None

    # The explicit startup call is what turns interrupted work into a retryable
    # failure, exactly once.
    database.init_database()

    status, failure = _row()
    assert status == "failed"
    assert failure is not None and "exited before" in failure
