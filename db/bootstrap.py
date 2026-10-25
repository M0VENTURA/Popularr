"""Full PostgreSQL schema bootstrap for Popularr."""

from __future__ import annotations

import threading
import time
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session, run_migrations_on_startup
from db.schema import COLUMN_REGISTRY, INDEXES_TO_ENSURE, TABLES_TO_ENSURE
from db.schema_helpers import get_postgres_column_types, get_table_columns, table_exists
from db.utils import is_transient_pg_startup_error

logger = structlog.get_logger(__name__)

_SCHEMA_BOOTSTRAP_LOCK_NAME = "popularr_schema_bootstrap"
_ALBUM_ART_DATA_LOCK_KEY = 915317411
_ALBUM_ART_SCHEMA_LOCK_KEY = 1986627450

# =============================================================================
# CORE HELPERS
# =============================================================================

def _ensure_table(cursor: Any, table_name: str, ddl: str) -> None:
    cursor.execute(text("SAVEPOINT popularr_schema_table_create"))
    try:
        cursor.execute(text(ddl))
    except Exception as exc:
        cursor.execute(text("ROLLBACK TO SAVEPOINT popularr_schema_table_create"))
        if "already exists" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
    finally:
        try: cursor.execute(text("RELEASE SAVEPOINT popularr_schema_table_create"))
        except Exception: pass

def _ensure_columns(cursor: Any, table_name: str, columns: dict[str, str]) -> None:
    # No introspection: ``ADD COLUMN IF NOT EXISTS`` is idempotent and runs
    # against whatever table the connection's search_path resolves — the
    # SAME table the application's ``FROM tracks`` queries (the browse pages
    # provably work against it).  An earlier version resolved the
    # schema-qualified name (``resolve_table_qualified_name`` → e.g.
    # ``public.tracks``) which could land on a DIFFERENT physical table than
    # bare ``tracks`` resolves to, so the column never got added to the real
    # table and searches kept failing with "column album_artist does not
    # exist" even after rebuilds.  Bare ``table_name`` is the correct target.
    for col_name, col_def in columns.items():
        try:
            cursor.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_def}"))
            logger.info("Added column", table=table_name, column=col_name)
        except Exception as e:
            logger.warning("Could not add column", table=table_name, column=col_name, error=str(e))

def _ensure_index(cursor: Any, ddl: str) -> None:
    try:
        cursor.execute(text(ddl))
    except Exception as e:
        msg = str(e).lower()
        # Tolerate "already exists" / duplicate-key (concurrent bootstrap).
        if "already exists" in msg or "duplicate" in msg:
            return
        # pg_trgm GIN indexes are best-effort: the extension may not be
        # installed (CREATE EXTENSION needs superuser) — a missing extension
        # must never fail the whole schema bootstrap.  A missing INDEXED
        # COLUMN (legacy bare ``tracks`` table that predates the column
        # registries) is also tolerated here — the column is added by
        # ``_ensure_columns`` on the NEXT boot once the table has it, and
        # migration 011 converges the Alembic path.
        if "pg_trgm" in ddl.lower() and (
            "extension" in msg or "does not exist" in msg or "operator class" in msg
        ):
            logger.warning("Skipped pg_trgm index (extension unavailable)", ddl=ddl.split("ON")[0].strip(), error=str(e))
            return
        if "does not exist" in msg and "column" in msg:
            logger.warning("Skipped index (column missing — will be created on next boot)", ddl=ddl.split("ON")[0].strip(), error=str(e))
            return
        raise

def _try_advisory_lock(conn_or_session: Any, key: int | str, attempts: int = 10) -> bool:
    for _ in range(max(1, attempts)):
        query = "SELECT pg_try_advisory_lock(hashtext(:key))" if isinstance(key, str) else "SELECT pg_try_advisory_lock(:key)"
        result = conn_or_session.execute(text(query), {"key": key})
        if bool(result.scalar()): 
            return True
        time.sleep(0.3)
    return False

def _release_advisory_lock(conn_or_session: Any, key: int | str) -> None:
    try:
        query = "SELECT pg_advisory_unlock(hashtext(:key))" if isinstance(key, str) else "SELECT pg_advisory_unlock(:key)"
        conn_or_session.execute(text(query), {"key": key})
    except Exception: 
        pass


def _ddl_savepoint(session: Any, statement: str, params: dict[str, Any] | None = None) -> bool:
    """Run one DDL statement inside its OWN savepoint; return True on success.

    ⭐⭐ WHY A SAVEPOINT AND NOT A BARE ``try/except``. On PostgreSQL a failed
    statement ABORTS THE WHOLE TRANSACTION: the ``except`` swallows the error,
    and then the NEXT statement fails with "current transaction is aborted,
    commands ignored until end of transaction block" — so one tolerated failure
    took every statement after it with it, and the function reported only the
    first error. ``begin_nested()`` rolls back to the savepoint, leaving the
    transaction usable, which is the same fix already applied per row in
    ``wikipedia_scraper_service._sanitize_stored_rows``.

    Returns False (and logs) when the statement failed, so a caller can decide
    whether the failure is tolerable — rather than the old behaviour of
    silently continuing with a broken transaction.
    """
    try:
        with session.begin_nested():
            session.execute(text(statement), params or {})
        return True
    except Exception as exc:
        logger.warning(
            "Tolerated DDL failure",
            statement=" ".join(statement.split())[:90],
            error=str(exc),
        )
        return False

# =============================================================================
# DATA FIXUPS & MIGRATION LOGIC
# =============================================================================

def ensure_status_changed_trigger(cursor: Any) -> None:
    cursor.execute(text("""
        CREATE OR REPLACE FUNCTION fn_dq_status_changed_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status THEN NEW.status_changed_at = CURRENT_TIMESTAMP; END IF;
            RETURN NEW;
        END; $$"""))
    cursor.execute(text("DROP TRIGGER IF EXISTS trg_dq_status_changed_at ON download_queue"))
    cursor.execute(text("CREATE TRIGGER trg_dq_status_changed_at BEFORE UPDATE ON download_queue FOR EACH ROW EXECUTE FUNCTION fn_dq_status_changed_at()"))

def ensure_musicbrainz_release_unique_constraint(cursor: Any) -> None:
    cursor.execute(text("DELETE FROM musicbrainz_releases WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY release_id ORDER BY updated_at DESC) as rn FROM musicbrainz_releases) t WHERE rn > 1)"))
    cursor.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_musicbrainz_releases_release_id ON musicbrainz_releases (release_id)"))

def ensure_album_artist_column_data() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "tracks") or not _try_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY, attempts=1): 
                return True
            try:
                # A truly bare tracks(id) table has no ``artist`` column yet —
                # the backfill then fails and must NOT abort the bootstrap.
                # Use a savepoint so the failure is contained.
                session.execute(text("SAVEPOINT popularr_backfill_album_artist"))
                session.execute(text("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL"))
                session.execute(text("RELEASE SAVEPOINT popularr_backfill_album_artist"))
            except Exception as e:
                try: session.execute(text("ROLLBACK TO SAVEPOINT popularr_backfill_album_artist"))
                except Exception: pass
                logger.debug("Album-artist backfill skipped (columns still bootstrapping)", error=str(e))
            finally:
                # ⭐ In a ``finally``: the old code released only on the success
                # path, so a failure left the session-level lock held for the
                # life of the pooled connection.
                _release_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY)
        return True
    except Exception as e:
        logger.error("Backfill failed", error=str(e))
        return False

def ensure_artists_name_unique_constraint() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "artists"): return False
            session.execute(text("DELETE FROM artists WHERE ctid NOT IN (SELECT DISTINCT ON (name) ctid FROM artists ORDER BY name, id ASC)"))
            session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_artists_name_unique ON artists (name)"))
        return True
    except Exception as e:
        logger.warning("Unique constraint error", error=str(e))
        return False

def ensure_album_art_schema() -> bool:
    try:
        with db_session() as session:
            if not _try_advisory_lock(session, _ALBUM_ART_SCHEMA_LOCK_KEY, attempts=5): return False
            try:
                _ensure_table(session, "album_art", TABLES_TO_ENSURE["album_art"])
                session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_album_art_artist_album ON album_art (artist_name, album_name)"))
            finally:
                _release_advisory_lock(session, _ALBUM_ART_SCHEMA_LOCK_KEY)
        return True
    except Exception as e:
        logger.error("Album art schema error", error=str(e))
        return False

#: Rank of a row when choosing which duplicate to KEEP. Higher wins, so a
#: manually-confirmed MBID is never discarded in favour of a scrape.
#:
#: ⭐⭐ WHY THIS EXISTS. The dedupe DELETEs used to key on
#: ``(artist_name, album_name)`` alone and keep whichever row had the LOWER
#: ``id``. That is a data-loss rule, not a reconciliation rule:
#:
#: * a row carrying a resolved ``release_group_mbid`` lost to a scraped row
#:   that had none, silently undoing MBID matching;
#: * a row the user had confirmed by hand (``mbid_manual_override``) was
#:   deletable, silently undoing a manual correction;
#: * the FIRST delete had no ``primary_type`` term at all, so a distinct
#:   single could be removed because an album with the same artist/title
#:   existed — two different releases, one deleted.
#:
#: All three run on EVERY BOOT, so the damage repeated.
_DEDUPE_RANK_SQL = """(
    CASE WHEN COALESCE({t}.mbid_manual_override, FALSE) THEN 2 ELSE 0 END
  + CASE WHEN COALESCE({t}.release_group_mbid, '') <> '' THEN 1 ELSE 0 END
)"""

#: Common "these two rows are the same release" predicate. ``primary_type`` is
#: part of the identity because the unique index is declared on it.
_SAME_RELEASE_SQL = """
      AND a.artist_name = b.artist_name
      AND a.album_name = b.album_name
      AND COALESCE(a.primary_type, '') = COALESCE(b.primary_type, '')
"""


def _dedupe_upcoming_releases(session: Any) -> None:
    """Reconcile duplicate ``upcoming_releases`` rows WITHOUT losing information.

    Order of preference, best first:
      1. ``mbid_manual_override`` — a human confirmed it;
      2. a resolved ``release_group_mbid``;
      3. ``MusicBrainz Daily Collection`` provenance over a Wikipedia scrape;
      4. the lower ``id`` (deterministic, and only as a last resort).

    A row with ``mbid_manual_override`` is NEVER the one deleted.
    """
    rank_a = _DEDUPE_RANK_SQL.format(t="a")
    rank_b = _DEDUPE_RANK_SQL.format(t="b")
    protected = "COALESCE(a.mbid_manual_override, FALSE) = FALSE"

    # (1) b is strictly richer than a → drop a.
    session.execute(
        text(
            f"""
            DELETE FROM upcoming_releases a
            USING upcoming_releases b
            WHERE a.id <> b.id
            {_SAME_RELEASE_SQL}
              AND {protected}
              AND (
                    {rank_a} < {rank_b}
                 OR (
                        {rank_a} = {rank_b}
                    AND a.source <> 'MusicBrainz Daily Collection'
                    AND b.source =  'MusicBrainz Daily Collection'
                 )
              )
            """
        )
    )

    # (2) equally rich → keep the lowest id, the same tie-break as before, but
    #     now only among rows that carry the SAME amount of identity.
    session.execute(
        text(
            f"""
            DELETE FROM upcoming_releases a
            USING upcoming_releases b
            WHERE a.id > b.id
            {_SAME_RELEASE_SQL}
              AND {protected}
              AND {rank_a} = {rank_b}
              AND COALESCE(a.source, '') = COALESCE(b.source, '')
            """
        )
    )


def ensure_upcoming_releases_schema() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "upcoming_releases"): return True

            _dedupe_upcoming_releases(session)

            # ⚠️ Every DDL statement goes through ``_ddl_savepoint``. The old
            # code wrapped ONLY the ALTER in ``try/except: pass``, so any
            # failure there aborted the transaction and the following
            # DROP/CREATE INDEX failed with "current transaction is aborted"
            # — the function then returned False having applied nothing.
            _ddl_savepoint(session, "ALTER TABLE upcoming_releases DROP CONSTRAINT IF EXISTS uq_upcoming_artist_album_source")
            _ddl_savepoint(session, "DROP INDEX IF EXISTS uq_upcoming_artist_album")

            # ⭐ The unique index is created LAST and its failure is REPORTED,
            # not fatal. If two genuinely distinct rows still collide (e.g. a
            # manually-overridden duplicate we deliberately kept), refusing to
            # delete either is the correct outcome — the table keeps working
            # without the index, and the log says why.
            index_ok = _ddl_savepoint(
                session,
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_upcoming_artist_album_type "
                "ON upcoming_releases (artist_name, album_name, primary_type)",
            )
            if not index_ok:
                logger.warning(
                    "Upcoming-releases unique index NOT created — duplicate "
                    "artist/album/type rows remain (protected by "
                    "mbid_manual_override or a differing MBID). Nothing was "
                    "deleted to force it."
                )

            _ddl_savepoint(
                session,
                "UPDATE upcoming_releases SET last_seen_at = "
                "COALESCE(last_seen_at, updated_at, created_at, CURRENT_TIMESTAMP)",
            )
        return True
    except Exception as e:
        logger.error("Upcoming releases schema error", error=str(e))
        return False

def migrate_queue_source_data() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "download_queue"): return True
            cols = get_table_columns(session, "download_queue")
            if "source" in cols and "source_id" in cols:
                session.execute(text(
                    "UPDATE download_queue SET source = source_id "
                    "WHERE (source IS NULL OR source = '') "
                    "AND source_id IS NOT NULL AND source_id != ''"
                ))
        return True
    except Exception as e:
        logger.warning("Queue source column backfill error", error=str(e))
        return False

def migrate_single_confidence_type() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "tracks"): return True
            if "single_confidence" not in get_table_columns(session, "tracks"): return True
            
            types = get_postgres_column_types(session, "tracks", ["single_confidence"])
            current_type = (types.get("single_confidence") or "").lower()
            numeric_types = {"smallint", "integer", "bigint", "real", "double precision", "numeric", "decimal"}
            
            if current_type in numeric_types:
                session.execute(text("""
                    ALTER TABLE tracks
                    ALTER COLUMN single_confidence TYPE TEXT
                    USING (
                        CASE
                            WHEN single_confidence IS NULL THEN NULL
                            WHEN single_confidence >= 0.9 THEN 'high'
                            WHEN single_confidence >= 0.5 THEN 'medium'
                            ELSE 'low'
                        END
                    )
                """))
                logger.info("Migrated tracks.single_confidence to TEXT", previous_type=current_type)
        return True
    except Exception as exc:
        logger.warning("Could not migrate single_confidence column", error=str(exc))
        return False

# =============================================================================
# MAIN BOOTSTRAP & ENTRY
# =============================================================================

def ensure_full_schema() -> bool:
    """Create/upgrade every table, column and index this app needs.

    ⭐⭐ THE ADVISORY LOCK IS RELEASED ON THE SESSION THAT TOOK IT.

    ``pg_try_advisory_lock`` is **session-scoped**: PostgreSQL ties the lock to
    the physical CONNECTION, and ``pg_advisory_unlock`` from a different
    connection returns ``false`` and releases nothing. The previous code took
    the lock inside one ``with db_session() as session:`` block and released it
    in a ``finally`` that opened a SECOND ``db_session()`` — a different
    checkout. It appeared to work only because SQLAlchemy's pool is LIFO and
    usually hands the same connection back; whenever it did not, the unlock was
    a silent no-op and the lock stayed held until that connection died, so a
    crashed bootstrap could wedge every later one.

    The release now happens in a ``finally`` INSIDE the acquiring block, after
    an explicit commit so no other process can acquire the lock and see
    uncommitted DDL.

    ⚠️ The lock therefore covers the schema work only. The data fixups below
    each own their idempotency (their own advisory lock, or statements that are
    safe to repeat), and keeping one session open across them would need a
    second pooled connection while the first is still checked out — which is
    fatal for the in-memory SQLite test harness (``StaticPool`` has exactly one).
    """
    lock_acquired = False
    try:
        with db_session() as session:
            lock_acquired = _try_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)
            if not lock_acquired:
                return False

            try:
                # 1. Base Tables
                for table, ddl in TABLES_TO_ENSURE.items():
                    _ensure_table(session, table, ddl)

                # 2. Base Columns
                for table, cols in COLUMN_REGISTRY.items():
                    _ensure_columns(session, table, cols)

                # 3. Triggers, Constraints & Indexes
                ensure_status_changed_trigger(session)
                ensure_musicbrainz_release_unique_constraint(session)
                for ddl in INDEXES_TO_ENSURE:
                    _ensure_index(session, ddl)

                # Commit BEFORE unlocking: another process must never take the
                # lock and then see a half-applied schema.
                session.commit()
            finally:
                _release_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)

        # 4. Independent Data Fixups & Migrations
        ensure_album_artist_column_data()
        ensure_artists_name_unique_constraint()
        ensure_album_art_schema()
        ensure_upcoming_releases_schema()
        
        # Type alterations and data backfills
        migrate_single_confidence_type()
        migrate_queue_source_data()

        # The tracks column cache (db.repositories.popularity_repository) is
        # populated on first write; if that happened before this bootstrap
        # pass finished adding columns, it would omit metadata columns and
        # every later upsert would silently drop them (search stays empty).
        # Invalidate so the next write re-reads the real schema.
        try:
            from db.repositories.popularity_repository import invalidate_tracks_column_cache
            invalidate_tracks_column_cache()
        except Exception:
            pass

        return True
    except Exception as exc:
        if is_transient_pg_startup_error(exc): return False
        logger.error("Bootstrap failed", exc_info=True)
        raise
    # ⚠️ NO unlock here any more: the ``finally`` above releases the lock on the
    # connection that took it. A second ``db_session()`` here is a DIFFERENT
    # checkout, and ``pg_advisory_unlock`` from it is a no-op — which is exactly
    # the bug this function used to have.

def verify_all_tables_exist() -> dict[str, Any]:
    expected = set(TABLES_TO_ENSURE.keys())
    with db_session() as session:
        present = {t for t in expected if table_exists(session, t)}
    return {"ok": expected.issubset(present), "missing": list(expected - present)}

def _reset_stale_scan_states() -> None:
    try:
        from services.scanning.scan_state import reset_stale_scan_states
        reset_stale_scan_states()
    except Exception as exc:
        logger.warning("Stale scan-state reset skipped", error=str(exc))

def _prune_genre_playlists_at_boot() -> None:
    def _run() -> None:
        try:
            from services.popularity.stages.finalise_stage import prune_genre_playlists_for_deletion
            prune_genre_playlists_for_deletion()
        except Exception as exc:
            logger.warning("Genre playlist prune skipped at boot", error=str(exc))

    threading.Thread(target=_run, daemon=True, name="boot-genre-playlist-prune").start()

def _repair_jsonb_drift_at_boot() -> None:
    """Run the JSONB/TEXT drift check-repair.  **test-site only.**

    Runs in a background thread for the same reason as the genre-playlist
    prune: it issues DDL and must never delay startup.  The pass itself
    declines to do anything unless ``features.use_test_site`` is on, so on a
    live install this thread starts, checks the flag, and exits — there is no
    configuration under which it touches live data.

    Kept out of ``ensure_full_schema`` deliberately.  The bootstrap runs under
    an advisory lock and is allowed to fail hard (``raise``); this is
    best-effort by design and must never be able to prevent a boot.
    """
    def _run() -> None:
        try:
            from db.jsonb_drift_repair import run_startup_check_repair

            result = run_startup_check_repair()
            if result.get("converted"):
                logger.info(
                    "JSONB drift repaired at boot",
                    converted=result["converted"],
                )
        except Exception as exc:
            logger.warning("JSONB drift check-repair skipped at boot", error=str(exc))

    threading.Thread(target=_run, daemon=True, name="boot-jsonb-drift-repair").start()

def _repair_shallowly_cleared_cover_verdicts_at_boot() -> None:
    """Re-flag cover verdicts a removed branch cleared without evidence.

    ``track_stage`` used to clear ``is_cover`` from the result of a SHALLOW
    check, so genuine covers were unflagged before the deep detection pass ran.
    The rows it touched are identifiable by the reason string it wrote, which is
    why this repair is possible at all. See
    ``services/enrichment/cover_verdict_repair_service``.

    Background thread, like the other boot repairs: it is a plain UPDATE and
    must never delay — or be able to prevent — a boot. It is also idempotent
    (the repair overwrites the fingerprint it searches for), so running it on
    every start is free once the backlog is cleared.

    Note this only re-flags; it does not decide the cover question. The deep
    detector does that on the next album pass, with real evidence, and clears
    the row again if nothing confirms it.
    """
    def _run() -> None:
        try:
            from services.enrichment.cover_verdict_repair_service import (
                repair_shallowly_cleared_cover_verdicts,
            )

            result = repair_shallowly_cleared_cover_verdicts()
            if result.get("repaired"):
                logger.info(
                    "Cover verdicts re-flagged at boot for deep re-detection",
                    repaired=result["repaired"],
                )
        except Exception as exc:
            logger.warning(
                "Cover verdict repair skipped at boot", error=str(exc)
            )

    threading.Thread(
        target=_run, daemon=True, name="boot-cover-verdict-repair"
    ).start()

def init_database_and_schema() -> bool:
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            if ensure_full_schema():
                verify_all_tables_exist()
                
                # Automatically apply any newer Alembic migrations on top of the
                # legacy bootstrap.  All revisions are idempotent (existence-guarded
                # DDL), so this converges even when the bootstrap already created
                # every table.  A failure is logged loudly — never silently — so a
                # stuck alembic_version cannot mask unapplied DDL across boots.
                if not run_migrations_on_startup():
                    logger.error(
                        "Alembic migrations FAILED after schema bootstrap — "
                        "check error.log; DDL may be missing until fixed"
                    )
                
                _reset_stale_scan_states()
                _prune_genre_playlists_at_boot()
                _repair_jsonb_drift_at_boot()
                _repair_shallowly_cleared_cover_verdicts_at_boot()
                return True
        except Exception as exc:
            msg = str(exc)
            if attempt < max_attempts and ("shutting down" in msg or "database system is shutting down" in msg):
                logger.warning("Postgres not ready yet, retrying...", attempt=attempt, max_attempts=max_attempts)
                time.sleep(5)
                continue
            logger.warning("Schema bootstrap failed", attempt=attempt, max_attempts=max_attempts, error=msg)
            break
        break

    threading.Thread(target=_run_deferred_startup_migrations, daemon=True, name="deferred-startup-migrations").start()
    return False

def _run_deferred_startup_migrations() -> None:
    max_wait_seconds = 300
    poll_interval_seconds = 15
    elapsed = 0
    while elapsed < max_wait_seconds:
        time.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds
        try:
            from db.engine import db_session
            with db_session():
                pass
            break
        except Exception:
            continue
    else:
        logger.warning("PostgreSQL did not become available for deferred schema bootstrap")
        return
    try:
        ensure_full_schema()
        verify_all_tables_exist()
        run_migrations_on_startup()
        _reset_stale_scan_states()
        _prune_genre_playlists_at_boot()
        _repair_jsonb_drift_at_boot()
        _repair_shallowly_cleared_cover_verdicts_at_boot()
    except Exception as exc:
        logger.error("Deferred schema bootstrap failed", exc_info=True)


if __name__ == "__main__":
    print("")
    print("── PostgreSQL Schema Bootstrap ──────────────────────────────")
    immediate = init_database_and_schema()
    if immediate:
        print("  ✓ Schema tables created/verified & Migrations Applied")
    else:
        print("  ⚠ Partial schema bootstrap — some tables may be deferred")

    # ⚠️ The verification must be as tolerant as the init above.
    #
    # `init_database_and_schema()` retries four times and swallows transient
    # startup errors, but this SECOND connection was unguarded: when
    # PostgreSQL is not reachable yet the run died with a full psycopg2
    # traceback — even though `entrypoint.sh` invokes this with `|| true` and
    # the runtime bootstrap retries later. Two facts make that the common case
    # rather than an edge case:
    #   * `wait_for_db()` returns IMMEDIATELY when no PG_HOST is configured
    #     (the DATABASE_URL-driven setup), so nothing waits for the server;
    #   * the entrypoint's fallback branch re-runs this WITHOUT `>/dev/null`,
    #     which is exactly where the traceback surfaced.
    # A one-line explanation is the honest outcome; the exit code stays
    # non-zero because "verified" would be a lie.
    try:
        result = verify_all_tables_exist()
    except Exception as exc:
        if not is_transient_pg_startup_error(exc):
            raise
        print(
            "  ⚠ PostgreSQL not reachable yet — table verification deferred "
            "to the runtime bootstrap (it retries once connected): "
            f"{type(exc).__name__}"
        )
        raise SystemExit(1) from None
    if result.get("ok"):
        print(f"  ✓ All {len(COLUMN_REGISTRY)} table groups verified")
    else:
        missing = result.get("missing", [])
        print(f"  ⚠ Missing tables (will be created on first use): {', '.join(missing)}")
    print("────────────────────────────────────────────────────────────")
