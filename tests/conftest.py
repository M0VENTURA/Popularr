"""
Pytest configuration and shared fixtures for Popularr.

Provides:
- ``app`` fixture — Quart application instance
- ``client`` fixture — authenticated HTTP test client for requests
- ``unauthed_client`` fixture — HTTP test client with NO session
- ``db_session`` fixture — isolated database session per test
- ``sample_track`` fixture — a sample track record for testing
"""

from __future__ import annotations

import os
import re
import pytest

os.environ.setdefault("CONFIG_PATH", "/dev/null")
# The app bootstraps its log file at import time (``ensure_default_log_files``);
# point it at a writable scratch path so test environments without /config do
# not blow up on import.
import tempfile
os.environ.setdefault("LOG_PATH", os.path.join(tempfile.mkdtemp(), "app.log"))
# Production is PostgreSQL-only; an explicit DATABASE_URL keeps the unit
# test suite self-contained on an in-memory SQLite engine.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Mark the app as "configured" (Navidrome present) so the auth gate treats
# tests as normal operation rather than first-run setup.  Without this every
# request would be redirected to the setup wizard.
os.environ.setdefault("POPULARLR_NAV_URL", "http://navidrome:4533")
os.environ.setdefault("POPULARLR_NAV_USER", "admin")
os.environ.setdefault("POPULARLR_NAV_PASS", "password")


def register_sqlite_regexp_replace(engine) -> None:
    """Register a ``REGEXP_REPLACE`` function on a SQLite engine.

    Postgres-only SQL in the upcoming-releases dedupe
    (``LOWER(REGEXP_REPLACE(...))``) has no SQLite equivalent.  Registering the
    function on the test engine lets the unit suite exercise the same SQL the
    production Postgres path runs.
    """
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _add_regexp_replace(dbapi_connection, connection_record):
        def _regexp_replace(value, pattern, repl, flags=""):
            if value is None:
                return None
            try:
                return re.sub(pattern, repl, str(value), flags=re.IGNORECASE if "i" in flags else 0)
            except Exception:
                return str(value)

        try:
            dbapi_connection.create_function("regexp_replace", -1, _regexp_replace)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """Create the minimal schema on the shared in-memory SQLite engine.

    ``import app`` runs ``initialize_app_services`` whose
    ``reset_stale_scan_states`` ORM query fails against the empty test DB;
    the resulting ``db_session`` transient-error path disposes the engine,
    wiping an in-memory database.  The ``app`` fixture re-creates the schema
    after the import, and the function-scoped ``_recreate_test_schema``
    autouse below re-creates it before EVERY test so any mid-suite dispose
    never leaves a later test without a ``tracks`` table.
    """
    yield


@pytest.fixture(autouse=True)
def _recreate_test_schema():
    """Idempotently ensure the ``tracks`` table exists before each test.

    The ``db_session`` context manager disposes the engine on any transient
    DB error (see ``db.engine``), which erases an in-memory database.  Rather
    than fight the ordering of session-scoped fixtures, this cheap
    ``checkfirst`` create runs before every single test.
    """
    from db.engine import get_engine
    from db.models import Track

    Track.__table__.create(get_engine(), checkfirst=True)
    yield


@pytest.fixture(autouse=True)
def _no_real_navidrome_rescan(monkeypatch):
    """Route tests must never fire a real Navidrome rescan (or its HTTP).

    Album/track/tag saves request a coalesced background rescan
    (``services.scanning.navidrome_rescan_service.request_rescan``) since
    the "import puts the old data back" fix.  Left unpatched, EVERY route
    test that saves metadata would spawn a daemon thread opening a real
    connection to the fake ``POPULARLR_NAV_URL``.

    The stub just reports "accepted".  Tests that exercise the rescan
    logic itself hold a module-level reference captured at import time
    (before this patch applies) or re-patch with their own recorder.
    """
    from services.scanning import navidrome_rescan_service as _rescan_svc

    monkeypatch.setattr(_rescan_svc, "request_rescan", lambda *args, **kwargs: True)


@pytest.fixture(autouse=True)
def _reset_host_timeout_cooldowns():
    """No host may still be "cooling down" from the previous test.

    ``api_clients.http_utils`` opens a per-host window after two consecutive
    requests time out on every attempt (so a dead peer costs the scan ~0s
    instead of attempts x read-timeout). That window is deliberately GLOBAL — it
    is the whole point — which means one test's simulated outage would otherwise
    make the next test's *successful* call to the same host raise
    ``httpx.ReadTimeout`` and fail for no reason it can see.
    """
    from api_clients import http_utils

    # getattr-guarded: if the cooldown were ever removed, its own tests must
    # fail on their assertions rather than every test erroring in this fixture.
    windows = getattr(http_utils, "_HOST_TIMEOUT_COOLDOWN", None)
    strikes = getattr(http_utils, "_HOST_TIMEOUT_STRIKES", None)
    if windows is not None:
        windows.clear()
    if strikes is not None:
        strikes.clear()
    yield
    if windows is not None:
        windows.clear()
    if strikes is not None:
        strikes.clear()


@pytest.fixture(scope="session")
def app():
    """Create and configure the Quart application for testing.

    Schema is (re)created AFTER importing ``app`` because the import itself
    disposes the engine (``initialize_app_services`` → ``reset_stale_scan_states``
    queries the missing ``scan_states`` table → transient-error dispose wipes
    the in-memory DB).  The create is idempotent (``checkfirst``), so always
    running it here survives any earlier dispose.
    """
    from app import app as _app
    from db.engine import get_engine
    from db.models import Track

    engine = get_engine()
    Track.__table__.create(engine, checkfirst=True)
    engine._popularr_test_schema_ready = True

    _app.config["TESTING"] = True
    return _app


@pytest.fixture
async def client(app):
    """Create an authenticated HTTP test client (session has a username)."""
    client = app.test_client()
    async with client.session_transaction() as sess:
        sess["username"] = "testuser"
    return client


@pytest.fixture
async def unauthed_client(app):
    """Create an HTTP test client with NO authenticated session."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Provide an isolated database session per test.

    The in-memory SQLite engine is shared across connections (StaticPool, see
    ``db.engine``).  Schema is (re)created idempotently on every request — the
    ``db_session`` context manager disposes the engine on transient errors,
    which wipes an in-memory DB, so relying on a one-time flag would leave
    later tests without a ``tracks`` table.
    """
    from db.engine import get_engine
    from db.models import Track

    engine = get_engine()
    Track.__table__.create(engine, checkfirst=True)

    from db.engine import db_session as _db_session
    with _db_session() as session:
        yield session


@pytest.fixture
def sample_track(db_session):
    """Insert and return a sample track record."""
    from sqlalchemy import text as _text
    db_session.execute(
        _text("""
            INSERT INTO tracks (id, artist, album, title, file_path)
            VALUES (:id, :artist, :album, :title, :file_path)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": "test-track-001",
            "artist": "Test Artist",
            "album": "Test Album",
            "title": "Test Track",
            "file_path": "/music/test.mp3",
        },
    )
    db_session.commit()
    return {"id": "test-track-001", "artist": "Test Artist", "album": "Test Album", "title": "Test Track"}
