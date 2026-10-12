"""Regression tests for idempotent Alembic migrations.

Reproduces the startup failure::

    ERROR: relation "missing_album_tracks" already exists

Root cause: ``db.bootstrap`` pre-creates every table in
``db.schema.TABLES_TO_ENSURE`` (including ``missing_album_tracks``), while
revisions 001/002/004/005/009 issued bare ``op.create_table`` calls.  When
``alembic_version`` lagged behind head, ``upgrade head`` aborted on the first
existing table and the entrypoint's blind ``stamp head`` fallback masked the
failure — so the error recurred on every boot.

These tests run the REAL revision chain (via alembic's command API) against
an in-memory SQLite database in two states:

1. Fresh database — full chain must apply cleanly.
2. Bootstrap-built database — tables pre-created via ``db.schema`` DDL with
   ``alembic_version`` stamped at an OLDER revision; upgrading to head must
   converge without "already exists" errors.

SQLite is used because the test suite standardises on it
(``DATABASE_URL=sqlite:///:memory:``); the guarded DDL helpers are
dialect-agnostic (SQLAlchemy inspector based).
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import pytest

# The suite runs against SQLite; ensure env is set before db.engine imports.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
import sqlalchemy as sa  # noqa: E402

import db.schema as schema_module  # noqa: E402

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _make_alembic_config(engine_url: str) -> Config:
    cfg = Config(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_PROJECT_ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", engine_url)
    return cfg


@pytest.fixture()
def sqlite_engine(tmp_path: Any) -> Iterator[sa.Engine]:
    """File-based SQLite engine (in-memory DBs don't survive reconnects)."""
    url = f"sqlite:///{tmp_path / 'migrations_test.db'}"
    engine = sa.create_engine(url)
    yield engine
    engine.dispose()


def _stamp(engine: sa.Engine, revision: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(64) NOT NULL)"
        )
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(
            f"INSERT INTO alembic_version (version_num) VALUES ('{revision}')"
        )

def _chain_head() -> str:
    """The current head revision id, read from the real migration chain.

    Tests used to hard-code ``"011_add_tracks_album_artist"``; the chain has
    moved (it is at 020 now), so a magic string rots silently. Reading the
    head from ``ScriptDirectory`` means the assertion always matches whatever
    the chain actually is.
    """
    from alembic.script import ScriptDirectory

    return ScriptDirectory(os.path.join(_PROJECT_ROOT, "migrations")).get_current_head()

def _precreate_bootstrap_tables(engine: sa.Engine) -> None:
    """Create the bootstrap-built schema state using db.schema DDL.

    Only PostgreSQL-flavoured statements are translated where needed:
    - JSONB → TEXT (SQLite has no JSONB)
    - TIMESTAMPTZ → TIMESTAMP
    """
    ddl_overrides = {
        "JSONB": "TEXT",
        "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
        "'{}'::jsonb": "'{}'",
    }

    def translate(ddl: str) -> str:
        out = ddl
        for pg, lite in ddl_overrides.items():
            out = out.replace(pg, lite)
        return out

    with engine.begin() as conn:
        for _table_name, raw_ddl in schema_module.TABLES_TO_ENSURE.items():
            conn.exec_driver_sql(translate(raw_ddl))


class TestMigrationChainIdempotency:
    def test_fresh_database_upgrade_head_succeeds(self, sqlite_engine: sa.Engine) -> None:
        """A fresh database gets the complete chain without errors."""
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")

        inspector = sa.inspect(sqlite_engine)
        tables = set(inspector.get_table_names())
        assert "tracks" in tables
        assert "download_queue" in tables
        assert "upcoming_releases" in tables
        assert "folder_matches" in tables
        assert "user_favourites" in tables
        assert "missing_album_tracks" in tables

    def test_upgrade_head_twice_is_noop(self, sqlite_engine: sa.Engine) -> None:
        """Running upgrade head twice must not error (idempotent re-run)."""
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")
        # Second run: everything already applied — must be a clean no-op.
        command.upgrade(cfg, "head")

    def test_bootstrap_built_db_upgrade_converges(self, sqlite_engine: sa.Engine) -> None:
        """THE regression: bootstrap-built tables + stale alembic_version.

        Simulates the production state that produced
        ``relation "missing_album_tracks" already exists``: all tables exist
        (created by db.bootstrap), but alembic_version points at an older
        revision.  upgrade head must skip existing objects and converge.
        """
        # 1. Pre-create all bootstrap tables.
        _precreate_bootstrap_tables(sqlite_engine)

        # 2. Stamp at 008 — one revision BEFORE missing_album_tracks was
        #    introduced, exactly like the failing deployment.
        _stamp(sqlite_engine, "008_add_missing_releases_tracklist")

        # 3. Upgrade to head — previously raised
        #    "relation missing_album_tracks already exists".
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        assert "missing_album_tracks" in set(inspector.get_table_names())

        # Version recorded at the REAL chain head (was hard-coded to 011).
        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == _chain_head()

    def test_partially_bootstrapped_db_upgrade_converges(self, sqlite_engine: sa.Engine) -> None:
        """Partial bootstrap state (some tables, older stamp) also converges."""
        # Create only SOME of the tables, stamp at 001.  Use full-column
        # definitions for tracks (revision 007's functional index references
        # tracks.artist / tracks.album_artist), plus the minimal bootstrap
        # stubs for artists / scan_history / bookmarks.
        partial_ddls = [
            schema_module.TABLES_TO_ENSURE["artists"],
            schema_module.TABLES_TO_ENSURE["scan_history"],
            schema_module.TABLES_TO_ENSURE["bookmarks"],
            # Revision 017 (``017_add_download_queue_release_group``)
            # inspector-guards ``download_queue`` columns; without the table
            # the harness raises ``NoSuchTableError`` — production always has
            # it (the bootstrap creates it), so the harness must too.
            schema_module.TABLES_TO_ENSURE["download_queue"],
        ]
        def translate(ddl: str) -> str:
            return (
                ddl.replace("JSONB", "TEXT")
                .replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
                .replace("'{}'::jsonb", "'{}'")
            )

        with sqlite_engine.begin() as conn:
            for stmt in partial_ddls:
                conn.exec_driver_sql(translate(stmt))

        # Full tracks table via alembic op semantics is overkill here; a raw
        # CREATE TABLE with the columns later revisions index on suffices.
        with sqlite_engine.begin() as c2:
            c2.exec_driver_sql(
                """
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY,
                    artist TEXT,
                    album_artist TEXT,
                    album TEXT,
                    title TEXT,
                    file_path TEXT,
                    duration DOUBLE PRECISION,
                    stars INTEGER,
                    final_score DOUBLE PRECISION,
                    is_single BOOLEAN DEFAULT FALSE
                )
                """
            )

        _stamp(sqlite_engine, "001_initial_schema")
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        tables = set(inspector.get_table_names())
        assert "missing_album_tracks" in tables
        assert "user_favourites" in tables


class TestBareTracksUpgradeConverges:
    """Regression: a legacy BARE ``tracks`` table (id only) must not abort
    the migration chain.

    The original system created ``tracks (id TEXT PRIMARY KEY)`` only.
    Revisions 001/007/010 used to index columns that don't exist on such a
    table (artist/album_artist/album/...), aborting ``alembic upgrade head``
    on the first revision — so the rebuilt container kept failing with
    ``column "album_artist" does not exist`` because 011 never ran.  The
    chain must now converge on the bare state and end at 011 with
    ``album_artist`` added + backfilled.
    """

    def test_bare_tracks_id_only_upgrade_to_head(self, sqlite_engine: sa.Engine) -> None:
        # The production legacy state: tracks holds ONLY id.
        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE tracks (id TEXT PRIMARY KEY)")
            conn.exec_driver_sql(
                "INSERT INTO tracks (id) VALUES ('t1')"
            )

        # Stamp at 001 so 001's index block runs against the bare table.
        _stamp(sqlite_engine, "001_initial_schema")
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        cols = {c["name"] for c in inspector.get_columns("tracks")}
        assert "album_artist" in cols

        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == _chain_head()

    def test_bare_tracks_stamped_at_010_upgrade_to_head(self, sqlite_engine: sa.Engine) -> None:
        """Even when alembic_version is at 010 (so only 011 remains), the
        chain must apply 011 and add album_artist."""
        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE tracks (id TEXT PRIMARY KEY)")

        _stamp(sqlite_engine, "010_add_pg_trgm_indexes")
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        cols = {c["name"] for c in inspector.get_columns("tracks")}
        assert "album_artist" in cols

        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == _chain_head()


class TestMigration011AddsTracksAlbumArtist:
    """Regression: legacy bare ``tracks`` tables are missing ``album_artist``.

    Simulates the production state behind
    ``column "album_artist" does not exist`` on the Search page: a ``tracks``
    table holding only ``id`` (created by the original system) with
    ``alembic_version`` already stamped at 010.  upgrade head must add the
    column, backfill it from ``artist``, and create the btree index.
    """

    def test_legacy_bare_tracks_gets_album_artist(self, sqlite_engine: sa.Engine) -> None:
        # Bare legacy tracks table — exactly the drift state.
        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE tracks (id TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT)"
            )
            conn.exec_driver_sql(
                "INSERT INTO tracks (id, artist, album, title) VALUES "
                "('t1', 'Spice Girls', 'Spice', 'Wannabe')"
            )

        _stamp(sqlite_engine, "010_add_pg_trgm_indexes")
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        tracks_cols = {col["name"] for col in inspector.get_columns("tracks")}
        assert "album_artist" in tracks_cols

        with sqlite_engine.connect() as conn:
            value = conn.exec_driver_sql(
                "SELECT album_artist FROM tracks WHERE id = 't1'"
            ).scalar()
        assert value == "Spice Girls"

        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == _chain_head()

    def test_fresh_db_011_is_noop(self, sqlite_engine: sa.Engine) -> None:
        """A fresh chain already declares album_artist in 001 — no-op."""
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == _chain_head()


class TestMigration019OldKeyForms:
    """Migration 019 drops the old upcoming-releases unique key.

    The old key has existed BOTH as a table constraint and (via the schema
    bootstrap) as a bare unique index under the same name
    (``uq_upcoming_artist_album``). On PostgreSQL an unconditional
    ``DROP CONSTRAINT`` for the absent constraint form ABORTS the migration
    transaction (transactional DDL), so the very next statement fails with
    ``InFailedSqlTransaction`` even inside a Python ``try/except`` — the
    reported production boot failure. The constraint drop must be gated on an
    inspector check for the CONSTRAINT (not the index).
    """

    def _drop_019_source(self) -> str:
        """The migration source with comments stripped (the docstrings quote
        ``op.drop_constraint``, so matching must ignore prose)."""
        import re as _re

        path = os.path.join(_PROJECT_ROOT, "migrations", "versions",
                            "019_upcoming_releases_type_in_key.py")
        src = open(path, encoding="utf-8").read()
        src = _re.sub(r'""".*?"""', "", src, flags=_re.S)
        src = _re.sub(r"'''.*?'''", "", src, flags=_re.S)
        return "\n".join(line.split("#", 1)[0] for line in src.splitlines())

    def test_the_constraint_drop_is_gated_on_a_constraint_inspector_check(self):
        src = self._drop_019_source()
        # The guard helper must exist and look at TABLE CONSTRAINTS (not
        # indexes) — that is what distinguishes the two forms.
        assert "def _has_table_constraint" in src, (
            "019 must inspect TABLE CONSTRAINTS before dropping one"
        )
        assert "get_unique_constraints" in src, (
            "_has_table_constraint must read get_unique_constraints"
        )
        # Exactly ONE DROP CONSTRAINT, and it must sit INSIDE the guarded
        # branch (guard defined BEFORE the drop). The old code dropped it
        # unconditionally inside ``try/except`` — with no ``_has_table_constraint``
        # symbol at all, so ``src.index`` below would raise and the test fail.
        assert src.count("op.drop_constraint(") == 1
        guard_at = src.index("_has_table_constraint(bind, OLD_KEY)")
        drop_at = src.index("op.drop_constraint(OLD_KEY, TABLE, type_=\"unique\")")
        assert guard_at < drop_at, (
            "op.drop_constraint must be unreachable without the constraint "
            "inspector guard — a bare drop aborts the PostgreSQL migration "
            "transaction when only the same-named index exists"
        )
        # The index form is still handled safely afterwards.
        assert "DROP INDEX IF EXISTS" in src

    def test_the_index_form_of_the_old_key_upgrades_cleanly(self, sqlite_engine):
        # The production shape that failed on PostgreSQL: the old key exists
        # only as a bare unique INDEX (the pre-019 bootstrap form), NOT as a
        # table constraint. 019 must drop the index and create the new key
        # WITHOUT ever attempting DROP CONSTRAINT.
        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE upcoming_releases (
                    id INTEGER PRIMARY KEY,
                    artist_name TEXT NOT NULL,
                    album_name TEXT NOT NULL,
                    primary_type TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'wikipedia',
                    release_date TEXT,
                    release_year INTEGER
                )
                """
            )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_upcoming_artist_album "
                "ON upcoming_releases (artist_name, album_name)"
            )

        _stamp(sqlite_engine, "018_track_popularity_cache_recording_mbid")
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        unique_names = {i["name"] for i in inspector.get_indexes("upcoming_releases") if i.get("unique")}
        assert "uq_upcoming_artist_album" not in unique_names, (
            "the old-key INDEX must be dropped (only the constraint form "
            "would have been dropped before — the index survived, and the "
            "attempted DROP CONSTRAINT then poisoned the PG transaction)"
        )
        assert "uq_upcoming_artist_album_type" in unique_names, (
            "the new 3-column key must be created"
        )

        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == _chain_head()


class TestIdempotentHelpers:
    def test_create_table_if_missing_skips_existing(self, sqlite_engine: sa.Engine) -> None:
        from migrations.idempotent import create_table_if_missing

        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE helper_probe (id INTEGER PRIMARY KEY)")

        called = False

        def _factory() -> None:
            nonlocal called
            called = True
            raise AssertionError("factory must NOT run when table exists")

        result = create_table_if_missing(sqlite_engine, "helper_probe", _factory)
        assert result is False
        assert called is False

    def test_create_table_if_missing_creates_absent(self, sqlite_engine: sa.Engine) -> None:
        from migrations.idempotent import create_table_if_missing

        created: list[bool] = []

        def _factory() -> None:
            created.append(True)

        result = create_table_if_missing(sqlite_engine, "helper_new", _factory)
        assert result is True
        assert created == [True]

    def test_index_exists_detects_indexes(self, sqlite_engine: sa.Engine) -> None:
        from migrations.idempotent import index_exists

        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE idx_probe (val TEXT)")
            conn.exec_driver_sql("CREATE INDEX idx_probe_val ON idx_probe (val)")

        assert index_exists(sqlite_engine, "idx_probe_val", "idx_probe") is True
        assert index_exists(sqlite_engine, "idx_missing_idx", "idx_probe") is False