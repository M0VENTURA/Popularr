"""Shared test helper: create the ``download_queue`` table on SQLite.

The test suite's in-memory database is shared for the whole session, and only
``tracks`` is recreated per test. A test module that hand-creates a REDUCED
queue table leaves it behind, and every later queue test then fails on a
missing column. Building from ``db.schema.COLUMN_REGISTRY`` keeps the shape
aligned with production (with the Postgres-only type/default syntax reduced to
what SQLite accepts).
"""

from __future__ import annotations

from sqlalchemy import text

from db.engine import db_session

_BASE_COLUMNS: dict[str, str] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "title": "TEXT",
    "artist": "TEXT",
    "album": "TEXT",
    "status": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}


def _sqlite_decl(declaration: str) -> str:
    value = str(declaration or "TEXT")
    value = value.replace("::jsonb", "").replace("::text", "")
    value = value.replace("DOUBLE PRECISION", "REAL")
    value = value.replace("BIGSERIAL", "INTEGER")
    return value


def ensure_queue_table() -> None:
    """Create or widen the shared ``download_queue`` table for SQLite."""
    try:
        from db.schema import COLUMN_REGISTRY

        registry = dict(COLUMN_REGISTRY.get("download_queue") or {})
    except Exception:
        registry = {}

    columns = dict(_BASE_COLUMNS)
    for name, declaration in registry.items():
        columns.setdefault(name, _sqlite_decl(declaration))

    with db_session() as session:
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS download_queue ("
            + ", ".join(f"{name} {decl}" for name, decl in columns.items())
            + ")"
        ))
        existing = {
            str(row[1])
            for row in session.execute(text("PRAGMA table_info(download_queue)")).fetchall()
        }

    for name, declaration in columns.items():
        if name == "id" or name in existing:
            continue
        with db_session() as session:
            try:
                session.execute(
                    text(f"ALTER TABLE download_queue ADD COLUMN {name} {declaration}")
                )
            except Exception:
                pass
