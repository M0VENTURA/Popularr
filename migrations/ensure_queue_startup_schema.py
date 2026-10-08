#!/usr/bin/env python3
"""
Minimal startup schema bootstrap (PostgreSQL).

Purpose:
- Ensure critical tables and columns exist BEFORE the app starts.
- Flexible connection handling for DATABASE_URL or component variables.
- Idempotent operations.
"""

import os
import sys
import logging
import socket
import threading
import time

# Ensure the project root is on sys.path so ``from db import ...`` works
# regardless of which directory the script is invoked from.
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import psycopg2
except ImportError:
    print("❌ psycopg2 driver not found. Please install it to proceed.")
    sys.exit(1)

logger = logging.getLogger(__name__)

from db.schema_helpers import get_table_columns as _get_table_columns

# Module-level state for idempotent schema checks
_fme_schema_checked = False
_fme_schema_lock = threading.Lock()
_fme_conflict_target_checked = False
_fme_conflict_target_lock = threading.Lock()


def _safe_int(value, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def get_connection(retries: int = 5, delay: float = 2.0):
    """Builds connection string from environment variables.

    Retries up to ``retries`` times with exponential backoff so the
    script survives the database still starting up in a compose stack.
    """
    db_url = os.environ.get("DATABASE_URL")
    
    # Fallback to granular variables if DATABASE_URL is missing
    if not db_url:
        host = os.environ.get("PG_HOST", "localhost")
        user = os.environ.get("PG_USER")
        password = os.environ.get("PG_PASSWORD")
        db = os.environ.get("PG_DATABASE")
        port = os.environ.get("PG_PORT", "5432")
        db_url = f"postgresql://{user}:{password}@{host}:{port}/{db}"

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(
                db_url,
                connect_timeout=5,
                options="-c lock_timeout=5000 -c statement_timeout=30000"
            )
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                wait = delay * (2 ** (attempt - 1))
                print(f"  ⏳ DB connection attempt {attempt}/{retries} failed, retrying in {wait:.0f}s: {exc}")
                time.sleep(wait)
            else:
                raise last_error


def ensure_schema(cursor):
    """Ensure minimum required tables and columns exist."""

    # 1. Ensure core tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS download_queue (
            id BIGSERIAL PRIMARY KEY,
            artist TEXT,
            title TEXT,
            album TEXT,
            status TEXT DEFAULT 'queued',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY
        );
    """)

    # 2. Ensure core columns
    columns = {
        "release_id": "TEXT",
        "release_source": "TEXT",
        "track_number": "TEXT",
        "album_artist": "TEXT",
        "release_mbid": "TEXT",
        "recording_mbid": "TEXT",
        # Album-scoped: the artist page keys albums on this field, so a queue row
        # that never stores it imports into a SEPARATE album from the release it
        # was downloaded for.  Mirrors COLUMN_REGISTRY["download_queue"].
        "musicbrainz_releasegroupid": "TEXT",
        "release_year": "INTEGER",
        "matched_file_path": "TEXT",
        "music_file_path": "TEXT",
        "status_changed_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "source": "TEXT DEFAULT 'soulseek'",
    }

    for col, col_type in columns.items():
        # Note: ADD COLUMN IF NOT EXISTS is valid in Postgres 9.6+
        cursor.execute(f"""
            ALTER TABLE download_queue 
            ADD COLUMN IF NOT EXISTS {col} {col_type};
        """)


def _is_pg_configured() -> bool:
    """Return True when the environment has PostgreSQL configuration."""
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url and db_url.startswith("postgres"):
        return True
    pg_host = os.environ.get("PG_HOST", "").strip()
    if pg_host:
        try:
            socket.getaddrinfo(pg_host, 5432, socket.AF_UNSPEC, socket.SOCK_STREAM)
            return True
        except socket.gaierror:
            print(f"  ⚠ Hostname '{pg_host}' could not be resolved — treating as not configured")
    return False


def main():
    # Skip entirely if no PG config is present.
    if not _is_pg_configured():
        print("⏭️  No PostgreSQL config found (set PG_HOST or DATABASE_URL) — skipping startup schema.")
        return 0

    try:
        conn = get_connection(retries=2, delay=2.0)
        with conn:
            with conn.cursor() as cursor:
                ensure_schema(cursor)
        conn.close()
        print("✅ Startup schema successfully ensured.")
        return 0

    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())



def _ensure_release_track_cache_columns(cursor) -> None:
    global _fme_schema_checked
    if _fme_schema_checked:
        return
    with _fme_schema_lock:
        if _fme_schema_checked:
            return
        required_track_columns = {
            'disc_number': 'INTEGER',
            'recording_title': 'TEXT',
            'recording_mbid': 'TEXT',
        }
        existing_track_columns = _get_table_columns(cursor, 'musicbrainz_release_tracks')
        for column_name, column_type in required_track_columns.items():
            if column_name in existing_track_columns:
                continue
            cursor.execute(f"ALTER TABLE musicbrainz_release_tracks ADD COLUMN {column_name} {column_type}")

        # Ensure release_year exists on musicbrainz_releases for tables created before
        # the column was added to the schema definition.
        existing_release_columns = _get_table_columns(cursor, 'musicbrainz_releases')
        if 'release_year' not in existing_release_columns:
            cursor.execute(
                "ALTER TABLE musicbrainz_releases ADD COLUMN IF NOT EXISTS release_year INTEGER"
            )

        _fme_schema_checked = True


def _ensure_musicbrainz_release_conflict_target(cursor) -> None:
    global _fme_conflict_target_checked
    if _fme_conflict_target_checked:
        return
    with _fme_conflict_target_lock:
        if _fme_conflict_target_checked:
            return

        # Serialize this schema fix across workers to avoid DDL races on restart.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('uq_musicbrainz_releases_release_id'))")

        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'musicbrainz_releases'
                  AND indexdef ILIKE 'CREATE UNIQUE INDEX% (release_id)%'
            )
            """
        )
        exists_row = cursor.fetchone()
        has_unique = bool(exists_row and exists_row[0])
        if not has_unique:
            # If legacy rows contain duplicates, keep the newest row per release_id first.
            cursor.execute(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY release_id
                               ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM musicbrainz_releases
                    WHERE release_id IS NOT NULL
                )
                DELETE FROM musicbrainz_releases m
                USING ranked r
                WHERE m.id = r.id
                  AND r.rn > 1
                """
            )

            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_musicbrainz_releases_release_id
                ON musicbrainz_releases (release_id)
                """
            )

        _fme_conflict_target_checked = True


def _cache_fm_release_metadata(conn, release_id: str, tracks: list) -> bool:
    """Cache MusicBrainz release tracks for a given release ID."""
    cursor = conn.cursor()
    placeholder = "%s"
    try:
        for track in tracks:
            cursor.execute(
                f"""
                INSERT INTO musicbrainz_release_tracks
                (release_id, queue_id, disc_number, track_number, track_title, track_artist,
                 duration, isrc, recording_title, recording_mbid, status, created_at, updated_at)
                VALUES ({placeholder}, NULL, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'cached', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    release_id,
                    _safe_int(track.get('disc_number'), 1),
                    _safe_int(track.get('track_number'), None),
                    track.get('title') or '',
                    track.get('artist') or '',
                    _safe_int(track.get('duration'), None),
                    track.get('isrc') or '',
                    track.get('recording_title') or '',
                    track.get('recording_mbid') or '',
                ),
            )

        conn.commit()
        return True
    except Exception as cache_err:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.debug(f"Could not cache MusicBrainz metadata for {release_id}: {cache_err}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass