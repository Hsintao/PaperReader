from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

from app.core.config import settings


# Tables that belonged to the multi-account and TeX-project designs. Nothing
# reads them anymore, so startup clears them instead of carrying dead columns.
_LEGACY_TABLES = ("sessions", "user_settings", "users", "projects")

# Document and annotation columns added since the multi-account schema. A
# rebuilt table starts without them; ALTER TABLE re-adds whatever is missing.
_DOCUMENT_ADDED_COLUMNS = {
    "failure_json": "TEXT",
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
    "last_read_page": "INTEGER NOT NULL DEFAULT 0",
    "last_read_ratio": "REAL NOT NULL DEFAULT 0",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}


def _db_path() -> Path:
    path = settings.data_dir / settings.sqlite_db_name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _initialize_schema(conn)
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rebuild_table(conn: sqlite3.Connection, table: str, create_sql: str) -> None:
    """Rebuild `table` with the current column set, keeping its data.

    Databases created before the accounts were removed still carry
    `owner_user_id INTEGER NOT NULL` on `documents` and `annotations`. The new
    code never supplies that column, so every INSERT and UPDATE fails the
    constraint. SQLite cannot drop a column that participates in a constraint,
    hence the copy-and-swap. Columns are carried over by name, so a table that
    predates a column simply leaves it at its default.
    """
    rebuilt = f"{table}__rebuild"
    conn.execute(f"DROP TABLE IF EXISTS {rebuilt}")
    conn.execute(create_sql.format(name=rebuilt))
    keep = sorted(_table_columns(conn, table) & _table_columns(conn, rebuilt))
    columns = ", ".join(keep)
    conn.execute(f"INSERT INTO {rebuilt} ({columns}) SELECT {columns} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {rebuilt} RENAME TO {table}")


def _initialize_schema(conn: sqlite3.Connection) -> None:
        for table in _LEGACY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_filename TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                original_pdf_url TEXT,
                translated_pdf_url TEXT,
                extracted_text TEXT NOT NULL DEFAULT '',
                translated_text TEXT NOT NULL DEFAULT '',
                artifacts_json TEXT NOT NULL DEFAULT '[]',
                references_json TEXT NOT NULL DEFAULT '[]',
                logs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                progress INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT,
                current_stage_label TEXT,
                stage_started_at REAL,
                eta_seconds INTEGER,
                stages_json TEXT NOT NULL DEFAULT '[]',
                failure_json TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT
            )
            """
        )
        existing_document_columns = _table_columns(conn, "documents")
        if "owner_user_id" in existing_document_columns:
            _rebuild_table(
                conn,
                "documents",
                """
                CREATE TABLE {name} (
                    document_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_filename TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    original_pdf_url TEXT,
                    translated_pdf_url TEXT,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    translated_text TEXT NOT NULL DEFAULT '',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    references_json TEXT NOT NULL DEFAULT '[]',
                    logs_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    progress INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT,
                    current_stage_label TEXT,
                    stage_started_at REAL,
                    eta_seconds INTEGER,
                    stages_json TEXT NOT NULL DEFAULT '[]',
                    failure_json TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                        deleted_at TEXT
                )
                """,
            )
            existing_document_columns = _table_columns(conn, "documents")
        for column, declaration in _DOCUMENT_ADDED_COLUMNS.items():
            if column not in existing_document_columns:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {declaration}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page INTEGER NOT NULL DEFAULT 1,
                quote TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT 'yellow',
                note TEXT NOT NULL DEFAULT '',
                position_ratio REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
            )
            """
        )
        if "owner_user_id" in _table_columns(conn, "annotations"):
            _rebuild_table(
                conn,
                "annotations",
                """
                CREATE TABLE {name} (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    page INTEGER NOT NULL DEFAULT 1,
                    quote TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT 'yellow',
                    note TEXT NOT NULL DEFAULT '',
                    position_ratio REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
                )
                """,
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_annotations_document ON annotations(document_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at DESC)"
        )


def init_database() -> None:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        _initialize_schema(conn)
        rows = conn.execute(
            """
            SELECT document_id, current_stage, retry_count, stages_json
            FROM documents WHERE status IN ('queued', 'processing')
            """
        ).fetchall()
        for row in rows:
            stage = row["current_stage"] or "upload"
            try:
                stages = json.loads(row["stages_json"] or "[]")
            except json.JSONDecodeError:
                stages = []
            for entry in stages:
                if entry.get("status") == "running":
                    entry["status"] = "failed"
            failure = {
                "stage": stage,
                "message": "The application exited before this queued or running stage completed",
                "retryable": True,
                "chunk": None,
                "retry_count": int(row["retry_count"] or 0),
            }
            conn.execute(
                """
                UPDATE documents
                SET status = 'failed', failure_json = ?, stages_json = ?, stage_started_at = NULL
                WHERE document_id = ?
                """,
                (
                    json.dumps(failure, ensure_ascii=False),
                    json.dumps(stages, ensure_ascii=False),
                    row["document_id"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


init_database()
