"""Popularity persistence repository."""

from __future__ import annotations
import json
import logging
import re
import time
from typing import Dict

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)

_TRACKS_COLUMN_CACHE: set[str] | None = None
_TRACKS_COLUMN_TYPES_CACHE: Dict[str, str] | None = None

PG_INT_TYPES = {"smallint", "integer", "bigint"}
PG_FLOAT_TYPES = {"real", "double precision", "numeric", "decimal"}
PG_BOOL_TYPES = {"boolean"}
PG_JSON_TYPES = {"json", "jsonb"}

DB_LOCK_MAX_RETRIES = 5
DB_LOCK_BASE_DELAY_SECONDS = 0.25


def invalidate_tracks_column_cache() -> None:
    """Drop the cached tracks columns/types.

    The caches are module-level singletons populated on first write.  If the
    first write happened while ``tracks`` was still bare (id only) — e.g. a
    scan racing the bootstrap's ``ADD COLUMN IF NOT EXISTS`` pass — the cache
    would permanently omit artist/title/album/album_artist and every later
    upsert would silently drop those columns, leaving search empty while the
    artists page (which reads rows directly) still shows data.  Call this
    whenever the schema is known to have changed (bootstrap, migrations,
    self-heal) so the next write re-reads the real columns.
    """
    global _TRACKS_COLUMN_CACHE, _TRACKS_COLUMN_TYPES_CACHE
    _TRACKS_COLUMN_CACHE = None
    _TRACKS_COLUMN_TYPES_CACHE = None


def get_tracks_table_columns(session=None) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    if _TRACKS_COLUMN_CACHE:
        return _TRACKS_COLUMN_CACHE

    own_session = session is None
    if own_session:
        from db.engine import db_session as _db_session
        with _db_session() as s:
            return _do_get_tracks_table_columns(s)
    return _do_get_tracks_table_columns(session)


def _do_get_tracks_table_columns(session) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    import sqlalchemy as _sa
    inspector = _sa.inspect(session.get_bind())
    _TRACKS_COLUMN_CACHE = {
        str(col["name"])
        for col in inspector.get_columns("tracks") or []
    }
    return _TRACKS_COLUMN_CACHE


def get_tracks_table_column_types(session=None) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    if _TRACKS_COLUMN_TYPES_CACHE:
        return _TRACKS_COLUMN_TYPES_CACHE

    own_session = session is None
    if own_session:
        from db.engine import db_session as _db_session
        with _db_session() as s:
            return _do_get_tracks_table_column_types(s)
    return _do_get_tracks_table_column_types(session)


def _do_get_tracks_table_column_types(session) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    import sqlalchemy as _sa
    inspector = _sa.inspect(session.get_bind())
    _TRACKS_COLUMN_TYPES_CACHE = {
        str(col["name"]): str(col["type"])
        for col in inspector.get_columns("tracks") or []
    }
    return _TRACKS_COLUMN_TYPES_CACHE


def parse_genre_value(value):
    """Parse ANY genre value into a list of clean strings.

    JSONB columns come back from psycopg as a Python **list**, not a string, so
    read-side helpers written as ``raw.replace("\\\\", ",").split(",")`` raise
    ``AttributeError`` on them — and ``str(value)`` "works" but yields the Python
    repr ``"['a', 'b']"`` (braces and quotes included), which then round-trips
    back into the column as a single junk genre.

    Accepts a list/tuple, a JSON array literal, a CSV string, and the legacy
    backslash-separated form. Always returns ``list[str]`` with no empties.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(parse_genre_value(item))
        return out
    if isinstance(value, dict):
        return [str(v).strip() for v in value.values() if str(v).strip()]

    raw = str(value).strip()
    if not raw:
        return []

    if raw[0] in "[{":
        try:
            return parse_genre_value(json.loads(raw))
        except (TypeError, ValueError):
            # A leading bracket that is not valid JSON (e.g. a title beginning
            # with "[") — keep split as free text below.
            pass

    sep = "\\" if "\\" in raw else ","
    if sep == "\\":
        parts = raw.split("\\")
    else:
        parts = re.split(r"[,;/]+", raw)
    return [p.strip() for p in parts if p.strip()]


def coerce_json_value(value):
    """Serialise ANY value to JSON TEXT for a JSON/JSONB column.

    THE ONE PLACE a value becomes JSON text. Public because the raw-SQL writers
    (``db/repositories/metadata.update_track_genres``, ``routes/misc_routes.py``,
    ``services/metadata/album_service.py``) do their own ``UPDATE`` and never go
    through :func:`save_to_db` — so they must call this themselves. Leaving them
    out is exactly how the album-genres box kept failing after the column path
    was fixed.

    ⚠️ ALWAYS returns ``str`` (JSON text) or ``None`` — never a Python list.
    ``services/scanning/payload_builder.py`` already writes these columns as
    ``json.dumps([...])`` strings and that path works; a Python list is adapted
    by the driver to a Postgres ARRAY literal, which a ``jsonb`` column rejects
    (``column is of type jsonb but expression is of type text[]``).

    ``None`` -> ``None``; ``''``/blank -> ``'[]'`` (an empty list, never NULL,
    matching ``db/schema.py``'s ``'' -> '[]'::jsonb`` rule); a list/dict -> JSON;
    a JSON literal -> re-serialised (normalised); any other string -> split into
    genre parts and serialised.
    """
    if value is None:
        return None
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return json.dumps(value)

    raw = str(value).strip()
    if not raw:
        return "[]"

    if raw[0] in "[{":
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False)
        except (TypeError, ValueError):
            pass

    return json.dumps(parse_genre_value(raw), ensure_ascii=False)


#: Back-compat alias — the JSON branch of the column coercer predates the
#: standalone helper and is referenced by that name in tests/notes.
_coerce_json_value = coerce_json_value


def coerce_track_value_for_pg_type(column: str, value, pg_type: str):
    if value is None:
        return None

    pg_type = (pg_type or "").lower()

    if pg_type in PG_BOOL_TYPES:
        return bool(value)
    if pg_type in PG_INT_TYPES:
        try:
            return int(value) if value != "" else None
        except (TypeError, ValueError):
            return None
    if pg_type in PG_FLOAT_TYPES:
        try:
            return float(value) if value != "" else None
        except (TypeError, ValueError):
            return None
    if pg_type in PG_JSON_TYPES:
        return coerce_json_value(value)

    return value


def _is_transient_db_error(exc: Exception) -> bool:
    """Return True for connection-level DB errors worth retrying.

    PostgreSQL can drop pooled connections (restart, idle timeout, network
    blip).  ``pool_pre_ping`` only helps on fresh checkouts; a session already
    holding a stale connection fails at commit with ``OperationalError``.
    """
    try:
        from sqlalchemy.exc import OperationalError as SAOperationalError
        from sqlalchemy.exc import InterfaceError as SAInterfaceError
        if isinstance(exc, (SAOperationalError, SAInterfaceError)):
            return True
    except Exception:
        pass
    orig = getattr(exc, "orig", None)
    if orig is not None:
        cls_name = type(orig).__name__.lower()
        if any(k in cls_name for k in ("operationalerror", "interfaceerror", "connectionerror")):
            return True
    return False


def run_with_db_lock_retry(operation):
    """Run ``operation`` with retries for lock contention AND transient DB errors."""
    for attempt in range(DB_LOCK_MAX_RETRIES):
        try:
            return operation()
        except Exception as exc:
            transient = _is_transient_db_error(exc) or "lock" in str(exc).lower()
            if not transient or attempt >= DB_LOCK_MAX_RETRIES - 1:
                raise
            # Drop pooled connections so the next attempt uses fresh ones.
            try:
                from db.engine import get_engine
                get_engine().dispose()
            except Exception:
                pass
            time.sleep(DB_LOCK_BASE_DELAY_SECONDS * (attempt + 1))
            continue
    raise RuntimeError("retry loop exhausted")


def save_to_db(track_data: dict) -> bool:
    """Save or update a track in the database.

    Uses db_session for safe connection handling.

    When the payload carries ``_navidrome_sync=True``, popularity/scoring
    columns are excluded from the UPDATE clause so that an incremental
    metadata sync never overwrites scores computed by the popularity pipeline.

    Args:
        track_data: Dictionary of track data to save
        conn: Optional existing connection (deprecated, kept for compatibility)

    Returns:
        True if successfully saved, False otherwise
    """
    if not track_data:
        return False

    def operation():
        with db_session() as session:
            return _execute_save(session, track_data)

    return run_with_db_lock_retry(operation)


def upsert_tracks_bulk(track_payloads: list[dict]) -> bool:
    """Persist many track payloads in ONE session + commit.

    The per-track scan workers open a fresh ``db_session`` (one transaction,
    one commit) per track — tens of thousands of transactions for a full
    library scan.  A bulk upsert runs the whole batch (typically one album)
    through one session so the writes commit once per album instead of once
    per row.  Rows that fail validation are logged and skipped so a single
    malformed payload never aborts the album's remaining writes.

    Args:
        track_payloads: List of track payload dicts (each must include ``id``).

    Returns:
        True when every row saved, False if any row failed.
    """
    if not track_payloads:
        return False

    def operation():
        with db_session() as session:
            ok = True
            for payload in track_payloads:
                try:
                    _execute_save(session, payload)
                except Exception as exc:
                    ok = False
                    logger.debug(
                        "Bulk track upsert skipped for %s: %s",
                        payload.get("id"), exc,
                    )
            return ok

    return run_with_db_lock_retry(operation)


# ── Columns whose values are owned by the popularity pipeline and must
#    NEVER be overwritten by a Navidrome metadata sync.  The UPSERT UPDATE
#    clause skips these columns when ``_navidrome_sync`` is set.
_POPULARITY_PROTECTED_COLUMNS: frozenset[str] = frozenset({
    # Scoring
    "popularity_score", "score", "final_score",
    "spotify_score", "lastfm_score", "listenbrainz_score", "age_score",
    "combined_score",
    # Ratings
    "stars", "star_rating",
    # Single detection
    "is_single", "single_confidence", "single_sources",
    "single_detection_last_updated", "single_manual_override",
    # Genres / tags (owned by the popularity pipeline's enrichment pass)
    "spotify_genres", "lastfm_tags", "listenbrainz_genres",
    "discogs_genres", "musicbrainz_genres", "audiodb_genres", "essentia_genres",
    # Album type (owned by the album stage's enrichment pass — a Navidrome
    # sync reads albumtype/releasetype from file tags, which are usually
    # empty, and would otherwise wipe the type the scan just detected)
    "musicbrainz_albumtype", "spotify_album_type", "releasetype",
    # Popularity meta
    "spotify_popularity", "lastfm_ratio", "lastfm_track_playcount",
    "popularity_frozen",
})


def _execute_save(session, track_data: dict) -> bool:
    """Execute the actual save operation with schema validation.

    When ``_navidrome_sync`` is True in the payload, popularity/scoring
    columns are excluded from the UPDATE clause of the upsert so that
    Navidrome metadata syncs never clobber the math engine's results.
    """
    columns = get_tracks_table_columns(session)
    types = get_tracks_table_column_types(session)

    # Self-heal a stale column cache: if the payload carries a metadata
    # column the cache doesn't know about, the cache was populated before
    # the schema finished bootstrapping (ADD COLUMN pass) — re-read the real
    # columns so metadata (artist/title/album/album_artist) is never silently
    # dropped from writes.  Without this, a scan racing the bootstrap
    # permanently wrote id-only rows and search stayed empty forever.
    if columns and track_data:
        missing_meta = [
            k for k in ("artist", "title", "album", "album_artist")
            if k in track_data and k not in columns
        ]
        if missing_meta:
            invalidate_tracks_column_cache()
            columns = get_tracks_table_columns(session)
            types = get_tracks_table_column_types(session)

    is_navidrome_sync = bool(track_data.get("_navidrome_sync"))

    data = {
        k: coerce_track_value_for_pg_type(k, v, types.get(k, ""))
        for k, v in track_data.items()
        if k in columns and k != "_navidrome_sync"
    }

    if "id" not in data:
        raise ValueError("track must include id")

    keys = list(data.keys())

    named_placeholders = ", ".join([f":{k}" for k in keys])

    # Build the UPDATE SET clause, skipping popularity-protected columns
    # when this is a Navidrome metadata sync.
    update_parts = []
    for k in keys:
        if k == "id":
            continue
        if is_navidrome_sync and k in _POPULARITY_PROTECTED_COLUMNS:
            continue
        update_parts.append(f"{k}=EXCLUDED.{k}")

    update_set = ", ".join(update_parts)

    query = text(f"""
        INSERT INTO tracks ({', '.join(keys)})
        VALUES ({named_placeholders})
        ON CONFLICT (id)
        DO UPDATE SET {update_set}
    """)

    params = {k: data[k] for k in keys}
    session.execute(query, params)

    return True