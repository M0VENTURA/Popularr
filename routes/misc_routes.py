"""Miscellaneous API routes — genres, corrections, bookmarks, country, essentia, etc."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import unquote

import httpx
import structlog
from quart import Blueprint, jsonify, request, Response
from sqlalchemy import text

from db.engine import db_session
from db.repositories.genres import log_genre_update
from db.repositories.scan_repository import (
    normalize_existing_artist_rows,
    sanitize_artist_file_paths_and_duplicates,
)
from db.repositories.tag_repository import get_track_tags, update_track_tags
from helpers.config_helpers import (
    get_all_services_status,
    get_config,
    get_state_directory,
    save_partial_config,
)
from services.catalog.album_classification_service import classify_album_type
from services.enrichment.musicbrainz_service import get_shared_mb_client
from services.infrastructure.api_rate_limiter import get_rate_limiter
from services.metadata.correction_service import fix_album_field, get_album_tag_inconsistencies
from services.metadata.tag_file_service import sync_track_tags_to_file, update_file_metadata

logger = structlog.get_logger(__name__)

misc_api_bp = Blueprint("misc_api", __name__, url_prefix="/api")


def _trigger_scan_after_tag_write() -> bool:
    """REMOVED: remote Navidrome auto-syncs are disabled.

    Previously this fired a Navidrome ``startScan`` in a daemon thread after
    every genre/tag write, repeatedly pausing the server and locking the
    database.  The ONLY automatic remote sync now runs once, BEFORE the full
    Navidrome import (see ``run_navidrome_import_scan`` →
    ``trigger_and_wait_for_scan``), and waits for completion before importing.
    """
    return True


# ===========================================================================
# ALGORITHM SANDBOX
# ===========================================================================

_SANDBOX_MAX_TRACKS = 30000
_SANDBOX_REF_WEIGHTS = {"lf": 0.55, "lb": 0.35, "age": 0.10}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@misc_api_bp.route("/sandbox/metrics")
def api_sandbox_metrics() -> Any:
    """Flattened track metrics for the Algorithm Sandbox."""
    try:
        scope = str(request.args.get("scope", "global") or "global").strip().lower()
        artist = str(request.args.get("artist", "") or "").strip()

        where = "COALESCE(final_score, 0) > 0"
        params: dict[str, Any] = {}
        if scope == "recent":
            where += " AND last_scanned >= (NOW() - INTERVAL '14 days')"
        elif scope == "artist":
            if not artist:
                return jsonify({"error": "artist parameter required for scope=artist"}), 400
            where += " AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            params["artist"] = artist
        elif scope != "global":
            return jsonify({"error": "scope must be global, recent or artist"}), 400

        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT CAST(id AS TEXT) AS id, title, artist, album, "
                    "COALESCE(lastfm_score, 0) AS lf, COALESCE(listenbrainz_score, 0) AS lb, "
                    "COALESCE(age_score, 0) AS age, COALESCE(final_score, 0) AS score, "
                    "COALESCE(stars, 0) AS stars, COALESCE(is_single, 0) AS single "
                    f"FROM tracks WHERE {where} ORDER BY artist, album, track_number "
                    f"LIMIT {_SANDBOX_MAX_TRACKS}"
                ),
                params,
            ).fetchall()

        tracks = [dict(r._mapping) for r in rows or []]

        by_album: dict[str, list[float]] = {}
        for t in tracks:
            raw = (t.get("lf") or 0) * _SANDBOX_REF_WEIGHTS["lf"] \
                + (t.get("lb") or 0) * _SANDBOX_REF_WEIGHTS["lb"] \
                + (t.get("age") or 0) * _SANDBOX_REF_WEIGHTS["age"]
            t["raw"] = round(raw, 2)
            if raw > 0:
                by_album.setdefault(str(t.get("album") or ""), []).append(raw)
        for t in tracks:
            ref = by_album.get(str(t.get("album") or "")) or []
            if len(ref) >= 3:
                med = _median(ref)
                mad = _median([abs(v - med) for v in ref])
                t["a_med"] = round(med, 2)
                t["a_mad"] = round(mad, 2)
            else:
                t["a_med"] = None
                t["a_mad"] = None

        return jsonify({
            "tracks": tracks,
            "count": len(tracks),
            "truncated": len(tracks) >= _SANDBOX_MAX_TRACKS,
            "ref_weights": _SANDBOX_REF_WEIGHTS,
        })
    except Exception as exc:
        logger.error("Sandbox metrics fetch failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# SEARCH
# ===========================================================================

# Set by ``api_search`` when a search query fails with a missing column
# (UndefinedColumn).  The retry then FORCES the artist-based SQL instead of
# re-trusting the column probe, which can disagree with the table the query
# actually runs against (a probe resolving a different ``tracks`` via a
# pooled connection's ``search_path`` while the query hits another — the
# recurring live-log failure where the probe reports album_artist but
# ``FROM tracks`` says it does not exist).  ``artist`` provably exists in
# those logs, so the degraded path always succeeds.
_SEARCH_FORCE_ARTIST: bool = False


def _resolve_tracks_columns(session: Any, table_name: str = "tracks") -> set[str]:
    """Return the ACTUAL column names on the search_path-resolved ``tracks``
    table.

    Delegates to ``db.schema_helpers.get_table_columns`` (to_regclass-based,
    dialect-aware) so the probe can never 500 the search: on PostgreSQL it
    reads ``pg_attribute`` exactly as ``FROM tracks`` resolves; on other
    engines (SQLite test engine) it falls back to the SQLAlchemy inspector so
    the query layer is still exercised.

    The catalog probe is only trusted when it returns a NON-EMPTY set: an
    empty result means the probe could not resolve the table (regclass text
    formatting quirks, a truly bare table, or a non-Postgres engine), in
    which case the inspector reflection below reflects the real table.
    """
    try:
        from db.schema_helpers import get_table_columns
        cols = get_table_columns(session, table_name)
        if cols:
            return cols
    except Exception:
        pass
    # Dialect fallback: SQLAlchemy inspector (Postgres + SQLite alike).
    try:
        import sqlalchemy as _sa
        inspector = _sa.inspect(session.get_bind())
        return {
            str(col["name"])
            for col in inspector.get_columns(table_name) or []
        }
    except Exception:
        return set()


def _self_heal_tracks_schema() -> None:
    """Best-effort ADD of the search-critical ``tracks`` columns.

    Runs when a search query proves ``album_artist``/``artist``/``title`` are
    absent (legacy bare table).  ``ADD COLUMN IF NOT EXISTS`` is idempotent,
    so this converges on any starting state and is safe to run on every
    request until the columns exist.

    The ALTER targets bare ``tracks`` — the SAME table the artists / albums /
    tracks browse pages query (which provably work with album_artist).  An
    earlier version targeted the schema-qualified name
    (``resolve_table_qualified_name`` → ``public.tracks``), but the browse
    pages resolve bare ``tracks`` to a DIFFERENT physical table (the one
    WITH album_artist), so the qualified-name ALTER kept missing the real
    table and the error repeated forever.

    IMPORTANT: the ALTERs run UNCONDITIONALLY (no ``if col in existing``
    skip).  The column probe can disagree with reality (e.g. it resolves a
    different ``tracks`` via ``search_path``, or returns ORM-declared
    columns for a bare table), and a skip based on that wrong probe would
    permanently prevent the column from being added.  ``ADD COLUMN IF NOT
    EXISTS`` is a no-op when the column is already present, so unconditional
    execution is always safe.

    The ALTERs run in their OWN committed ``db_session`` each (a separate
    transaction per column) and the backfill in another.  PostgreSQL aborts
    the whole transaction when a statement errors — and SQLAlchemy marks the
    session for rollback on any SQL error even when the Python exception is
    caught — so a failing statement inside the same transaction would roll
    back the already executed ``ADD COLUMN`` statements and the heal would
    never persist (the "errors repeat forever" failure).  One transaction
    per statement guarantees each successful ALTER commits independently.
    """
    try:
        from db.engine import db_session as _ds
        from sqlalchemy import text as _text

        # ── Phase 1: add the columns — one committed tx per column so a
        # failing statement can never roll back a successful ALTER. ──────
        for col, ddl in (("album_artist", "TEXT"), ("artist", "TEXT"),
                         ("title", "TEXT"), ("album", "TEXT")):
            try:
                with _ds() as session:
                    session.execute(_text(
                        f"ALTER TABLE tracks ADD COLUMN IF NOT EXISTS {col} {ddl}"
                    ))
            except Exception as col_exc:
                logger.debug(
                    "Search self-heal ADD COLUMN skipped",
                    column=col, error=str(col_exc),
                )

        # ── Phase 2: backfill album_artist in a SEPARATE committed tx so a
        # failure here can never roll back the ALTERs above. ─────────────
        try:
            with _ds() as session:
                session.execute(_text(
                    "UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL"
                ))
        except Exception as backfill_exc:
            logger.debug(
                "Search self-heal album_artist backfill skipped",
                error=str(backfill_exc),
            )
    except Exception as exc:
        logger.warning("Search schema self-heal failed", error=str(exc))


async def _api_search_impl() -> Any:
    """Search artists, albums and tracks with legacy ranking behaviour.

    Ranking uses the existing exact → prefix → contains tiers (fast, indexed
    by the pg_trgm GIN indexes added in migration 010), with a trigram
    ``similarity()`` tiebreaker so typo'd queries ("Sipce Girls") still find
    "Spice Girls".  When pg_trgm is unavailable (a DB that skipped migration
    010) the query falls back to plain ``LIKE`` ranking.

    The SQL is built from the ACTUAL ``tracks`` columns resolved via
    ``to_regclass`` — album_artist → artist → none — so a legacy bare-tracks
    table never 500s; it returns an empty result with a hint instead.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        query = str(data.get("query") or "").strip().lower()

        if not query or len(query) < 2:
            return jsonify({"error": "Search query must be at least 2 characters"}), 400

        exact_pattern = query
        starts_pattern = f"{query}%"
        contains_pattern = f"%{query}%"

        # Discover the ACTUAL columns on the search_path-resolved ``tracks``
        # table (via to_regclass, not information_schema — which could see a
        # tracks table in another schema).  A legacy bare-tracks DB may lack
        # artist/album_artist entirely, so the SQL is built from what really
        # exists and the search NEVER 500s on a missing column.
        #
        # The canonical helper (db.schema_helpers.get_table_columns) resolves
        # through to_regclass and is dialect-safe; the inline pg_catalog
        # probe previously used here was Postgres-only and 500'd on any other
        # engine (e.g. the SQLite test engine), turning every search into a
        # "Search failed" page instead of results or a graceful empty state.
        #
        # The search SQL runs against bare ``FROM tracks`` — the SAME table
        # the artists / albums / tracks browse pages query (which provably
        # work with album_artist).  The probe reads the same bare name, so
        # probe and query always agree.  NOTE: an earlier attempt pinned the
        # search to the schema-qualified name (``resolve_table_qualified_name``
        # → ``public.tracks``) — but the browse pages resolve bare ``tracks``
        # to a DIFFERENT physical table (the one WITH album_artist), so the
        # qualified name hit a stale bare table and produced the recurring
        # "column album_artist does not exist" against ``FROM public.tracks``.
        # Bare ``tracks`` is the correct target.
        with db_session() as session:
            _tracks_cols = _resolve_tracks_columns(session, "tracks")
            _trgm_ok = False
            try:
                from helpers.config_helpers import get_search_fuzzy_config
                if get_search_fuzzy_config().get("enabled", True):
                    _trgm_ok = bool(
                        session.execute(
                            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
                        ).scalar()
                    )
            except Exception:
                _trgm_ok = False

        # Proactive schema self-heal: if the resolved ``tracks`` table is
        # missing any search-critical column (a legacy bare ``tracks(id)``
        # table that never got its column-ensure applied to the RIGHT schema),
        # add the columns now — BEFORE building the SQL — so the very first
        # search after boot converges the schema.  The failure-driven heal in
        # ``api_search`` only fires when a query throws, but with the
        # column-aware builder the query degrades (artist → album → none)
        # instead of throwing, so the heal would never run and the rest of
        # the app (artists page, album detail, repository upserts — all of
        # which reference album_artist) stays broken.
        _missing_meta = [c for c in ("album_artist", "artist", "title", "album")
                         if c not in _tracks_cols]
        if _missing_meta:
            _self_heal_tracks_schema()
            # Re-probe: the heal may have added the columns; pick the artist
            # expression from the post-heal reality.
            with db_session() as session:
                _tracks_cols = _resolve_tracks_columns(session, "tracks")

        # Pick the artist expression from what the table actually has:
        #   album_artist (best) -> artist -> (fallback) no artist search.
        # When ``_SEARCH_FORCE_ARTIST`` is set (a previous search proved
        # album_artist unusable on the query's connection), skip the probe
        # entirely and go straight to ``artist`` — the provably-working path
        # in the live logs.
        if _SEARCH_FORCE_ARTIST:
            _artist_expr = "artist" if "artist" in _tracks_cols else ""
            _artist_like = "COALESCE(artist, '')" if _artist_expr else ""
        elif "album_artist" in _tracks_cols:
            _artist_expr = "COALESCE(NULLIF(album_artist, ''), artist)"
            _artist_like = "COALESCE(album_artist, '')"
        elif "artist" in _tracks_cols:
            _artist_expr = "artist"
            _artist_like = "COALESCE(artist, '')"
        else:
            _artist_expr = ""
            _artist_like = ""

        _can_search = bool(_artist_expr) and ("title" in _tracks_cols or "album" in _tracks_cols)

        def _sql(template: str):
            """Inject the artist expression into a SQL template and return a
            SQLAlchemy text() object ready to execute."""
            return text(
                template
                .replace("{artist_expr}", _artist_expr)
                .replace("{artist_like}", _artist_like)
            )

        # No searchable columns (truly bare tracks(id) table) — return an
        # empty-but-successful response instead of 500ing.
        if not _can_search:
            return jsonify({
                "artists": [],
                "albums": [],
                "compilations": [],
                "live_albums": [],
                "eps": [],
                "singles": [],
                "tracks": [],
                "note": "Library tracks table is not fully populated yet — run a Navidrome import scan to populate track metadata.",
            })

        with db_session() as session:
            # ── Per-connection pre-flight (decisive) ──────────────────────
            # The probe above ran on a DIFFERENT connection.  A pooled
            # connection can carry a different ``search_path`` than another
            # (the recurring live-log failure: probe reports album_artist,
            # but the query's connection says it does not exist).  Verify the
            # artist expression's columns on THIS exact connection; if the
            # selected column is unusable here, degrade to ``artist`` before
            # any query runs.  ``_sql`` closes over ``_artist_expr`` /
            # ``_artist_like`` by reference, so reassigning them here changes
            # the SQL below.
            if _artist_expr and "album_artist" in _artist_expr:
                try:
                    session.execute(text("SELECT album_artist FROM tracks LIMIT 0"))
                except Exception:
                    # album_artist unusable on this connection — degrade to
                    # artist (provably present: the browse pages work).
                    if "artist" in _tracks_cols:
                        _artist_expr = "artist"
                        _artist_like = "COALESCE(artist, '')"
                    else:
                        _artist_expr = ""
                        _artist_like = ""

            if _trgm_ok:
                # Trigram similarity fallback column: 0 when the extension is
                # absent.  Used only as a tiebreaker AFTER the exact/prefix/
                # contains tiers, so a typo'd query still ranks by how close
                # the trigrams are while exact matches keep priority.
                artist_result = session.execute(
                    _sql("""
                        WITH variants AS (
                            SELECT
                                {artist_expr} AS variant,
                                LOWER({artist_expr}) AS name_key,
                                COUNT(*) AS cnt,
                                COUNT(DISTINCT album) AS album_count
                            FROM tracks
                            WHERE LOWER(COALESCE(artist, '')) LIKE :contains
                               OR LOWER({artist_like}) LIKE :contains
                            GROUP BY {artist_expr}
                        ),
                        ranked AS (
                            SELECT
                                variant, name_key, cnt, album_count,
                                ROW_NUMBER() OVER (
                                    PARTITION BY name_key
                                    ORDER BY cnt DESC,
                                             (initcap(variant) = variant) DESC,
                                             LENGTH(variant) DESC
                                ) AS rn
                            FROM variants
                        )
                        SELECT
                            variant AS name,
                            SUM(cnt) AS track_count,
                            SUM(album_count) AS album_count,
                            CASE
                                WHEN name_key = :exact THEN 0
                                WHEN name_key LIKE :starts THEN 1
                                ELSE 2
                            END AS match_rank,
                            GREATEST(
                                similarity(variant, :query),
                                similarity(COALESCE(variant, ''), :query)
                            ) AS sim
                        FROM ranked
                        WHERE rn = 1
                        GROUP BY name_key, variant
                        ORDER BY
                            match_rank ASC,
                            sim DESC,
                            SUM(cnt) DESC
                        LIMIT 20
                    """),
                    {
                        "exact": exact_pattern,
                        "starts": starts_pattern,
                        "contains": contains_pattern,
                        "query": query,
                    },
                )
            else:
                artist_result = session.execute(
                    _sql("""
                        WITH variants AS (
                            SELECT
                                {artist_expr} AS variant,
                                LOWER({artist_expr}) AS name_key,
                                COUNT(*) AS cnt,
                                COUNT(DISTINCT album) AS album_count
                            FROM tracks
                            WHERE LOWER(COALESCE(artist, '')) LIKE :contains
                               OR LOWER({artist_like}) LIKE :contains
                            GROUP BY {artist_expr}
                        ),
                        ranked AS (
                            SELECT
                                variant, name_key, cnt, album_count,
                                ROW_NUMBER() OVER (
                                    PARTITION BY name_key
                                    ORDER BY cnt DESC,
                                             (initcap(variant) = variant) DESC,
                                             LENGTH(variant) DESC
                                ) AS rn
                            FROM variants
                        )
                        SELECT
                            variant AS name,
                            SUM(cnt) AS track_count,
                            SUM(album_count) AS album_count,
                            CASE
                                WHEN name_key = :exact THEN 0
                                WHEN name_key LIKE :starts THEN 1
                                ELSE 2
                            END AS match_rank,
                            0.0 AS sim
                        FROM ranked
                        WHERE rn = 1
                        GROUP BY name_key, variant
                        ORDER BY
                            match_rank ASC,
                            SUM(cnt) DESC
                        LIMIT 20
                    """),
                    {
                        "exact": exact_pattern,
                        "starts": starts_pattern,
                        "contains": contains_pattern,
                    },
                )

            artists = [
                {
                    "name": row._mapping["name"],
                    "album_count": int(row._mapping["album_count"] or 0),
                    "track_count": int(row._mapping["track_count"] or 0),
                }
                for row in artist_result.fetchall()
            ]

            if _trgm_ok:
                album_result = session.execute(
                    _sql("""
                        WITH variants AS (
                            SELECT
                                {artist_expr} AS variant,
                                LOWER({artist_expr}) AS name_key,
                                album,
                                COUNT(*) AS track_count,
                                AVG(stars) AS avg_stars,
                                SUM(duration) AS album_duration,
                                MAX(COALESCE(
                                    NULLIF(SUBSTRING(year FROM '^[0-9]{4}'), '')::INTEGER,
                                    release_year,
                                    0
                                )) AS album_year,
                                MAX(COALESCE(NULLIF(musicbrainz_albumtype, ''),
                                             NULLIF(spotify_album_type, ''))) AS album_type,
                                CASE
                                    WHEN LOWER(COALESCE(album, '')) = :exact THEN 0
                                    WHEN LOWER(COALESCE(album, '')) LIKE :starts THEN 1
                                    ELSE 2
                                END AS match_rank,
                                GREATEST(
                                    similarity(COALESCE(album, ''), :query),
                                    similarity({artist_expr}, :query)
                                ) AS sim
                            FROM tracks
                            WHERE LOWER(COALESCE(album, '')) LIKE :contains
                               OR LOWER({artist_like}) LIKE :contains
                               OR LOWER(COALESCE(artist, '')) LIKE :contains
                            GROUP BY
                                {artist_expr},
                                album
                        ),
                        ranked AS (
                            SELECT
                                variant, name_key, album, track_count, avg_stars,
                                album_duration, album_year, album_type, match_rank, sim,
                                ROW_NUMBER() OVER (
                                    PARTITION BY name_key, album
                                    ORDER BY track_count DESC,
                                             (initcap(variant) = variant) DESC,
                                             LENGTH(variant) DESC
                                ) AS rn
                            FROM variants
                        )
                        SELECT
                            variant AS artist,
                            album,
                            track_count,
                            avg_stars,
                            album_duration,
                            album_year,
                            album_type,
                            match_rank
                        FROM ranked
                        WHERE rn = 1
                        ORDER BY
                            match_rank ASC,
                            sim DESC,
                            track_count DESC
                        LIMIT 20
                    """),
                    {
                        "exact": exact_pattern,
                        "starts": starts_pattern,
                        "contains": contains_pattern,
                        "query": query,
                    },
                )
            else:
                album_result = session.execute(
                    _sql("""
                        WITH variants AS (
                            SELECT
                                {artist_expr} AS variant,
                                LOWER({artist_expr}) AS name_key,
                                album,
                                COUNT(*) AS track_count,
                                AVG(stars) AS avg_stars,
                                SUM(duration) AS album_duration,
                                MAX(COALESCE(
                                    NULLIF(SUBSTRING(year FROM '^[0-9]{4}'), '')::INTEGER,
                                    release_year,
                                    0
                                )) AS album_year,
                                MAX(COALESCE(NULLIF(musicbrainz_albumtype, ''),
                                             NULLIF(spotify_album_type, ''))) AS album_type,
                                CASE
                                    WHEN LOWER(COALESCE(album, '')) = :exact THEN 0
                                    WHEN LOWER(COALESCE(album, '')) LIKE :starts THEN 1
                                    ELSE 2
                                END AS match_rank
                            FROM tracks
                            WHERE LOWER(COALESCE(album, '')) LIKE :contains
                               OR LOWER({artist_like}) LIKE :contains
                               OR LOWER(COALESCE(artist, '')) LIKE :contains
                            GROUP BY
                                {artist_expr},
                                album
                        ),
                        ranked AS (
                            SELECT
                                variant, name_key, album, track_count, avg_stars,
                                album_duration, album_year, album_type, match_rank,
                                ROW_NUMBER() OVER (
                                    PARTITION BY name_key, album
                                    ORDER BY track_count DESC,
                                             (initcap(variant) = variant) DESC,
                                             LENGTH(variant) DESC
                                ) AS rn
                            FROM variants
                        )
                        SELECT
                            variant AS artist,
                            album,
                            track_count,
                            avg_stars,
                            album_duration,
                            album_year,
                            album_type,
                            match_rank
                        FROM ranked
                        WHERE rn = 1
                        ORDER BY
                            match_rank ASC,
                            track_count DESC
                        LIMIT 20
                    """),
                    {
                        "exact": exact_pattern,
                        "starts": starts_pattern,
                        "contains": contains_pattern,
                    },
                )

            _bucket_map = {
                "album": "albums",
                "remix_album": "albums",
                "compilation": "compilations",
                "live_album": "live_albums",
                "ep": "eps",
                "single": "singles",
            }
            _type_labels = {
                "albums": "Studio Album",
                "remix_album": "Remix Album",
                "compilations": "Compilation",
                "live_albums": "Live Album",
                "eps": "EP",
                "singles": "Single",
            }
            albums_by_bucket: dict[str, list[dict[str, Any]]] = {
                "albums": [],
                "compilations": [],
                "live_albums": [],
                "eps": [],
                "singles": [],
            }
            for row in album_result.fetchall():
                _m = row._mapping
                _raw_type = classify_album_type(dict(_m))
                _bucket = _bucket_map.get(_raw_type, "albums")
                _year = int(_m["album_year"] or 0) or None
                albums_by_bucket[_bucket].append({
                    "artist": _m["artist"],
                    "album": _m["album"],
                    "year": _year,
                    "track_count": int(_m["track_count"] or 0),
                    "duration_total": (
                        float(_m["album_duration"])
                        if _m["album_duration"] is not None
                        else None
                    ),
                    "avg_stars": (
                        float(_m["avg_stars"])
                        if _m["avg_stars"] is not None
                        else None
                    ),
                    "type": _bucket,
                    "type_label": _type_labels.get(_raw_type, _type_labels[_bucket]),
                    "in_library": True,
                })

            if _trgm_ok:
                track_result = session.execute(
                    _sql("""
                        SELECT
                            id,
                            title,
                            {artist_expr} AS artist,
                            album,
                            stars,
                            CASE
                                WHEN LOWER(COALESCE(title, '')) = :exact THEN 0
                                WHEN LOWER(COALESCE(title, '')) LIKE :starts THEN 1
                                WHEN LOWER(COALESCE(title, '')) LIKE :contains THEN 2
                                ELSE 3
                            END AS match_rank,
                            GREATEST(
                                similarity(COALESCE(title, ''), :query),
                                similarity(COALESCE(artist, ''), :query),
                                similarity({artist_like}, :query)
                            ) AS sim
                        FROM tracks
                        WHERE LOWER(COALESCE(title, '')) LIKE :contains
                           OR LOWER(COALESCE(artist, '')) LIKE :contains
                           OR LOWER({artist_like}) LIKE :contains
                        ORDER BY
                            match_rank ASC,
                            sim DESC,
                            stars DESC NULLS LAST,
                            LOWER(COALESCE(title, '')) ASC
                        LIMIT 50
                    """),
                    {
                        "exact": exact_pattern,
                        "starts": starts_pattern,
                        "contains": contains_pattern,
                        "query": query,
                    },
                )
            else:
                track_result = session.execute(
                    _sql("""
                        SELECT
                            id,
                            title,
                            {artist_expr} AS artist,
                            album,
                            stars,
                            CASE
                                WHEN LOWER(COALESCE(title, '')) = :exact THEN 0
                                WHEN LOWER(COALESCE(title, '')) LIKE :starts THEN 1
                                WHEN LOWER(COALESCE(title, '')) LIKE :contains THEN 2
                                ELSE 3
                            END AS match_rank
                        FROM tracks
                        WHERE LOWER(COALESCE(title, '')) LIKE :contains
                           OR LOWER(COALESCE(artist, '')) LIKE :contains
                           OR LOWER({artist_like}) LIKE :contains
                        ORDER BY
                            match_rank ASC,
                            stars DESC NULLS LAST,
                            LOWER(COALESCE(title, '')) ASC
                        LIMIT 50
                    """),
                    {
                        "exact": exact_pattern,
                        "starts": starts_pattern,
                        "contains": contains_pattern,
                    },
                )

            tracks = [
                {
                    "id": row._mapping["id"],
                    "title": row._mapping["title"],
                    "artist": row._mapping["artist"],
                    "album": row._mapping["album"],
                    "stars": row._mapping["stars"],
                }
                for row in track_result.fetchall()
            ]

        return jsonify({
            "artists": artists,
            **albums_by_bucket,
            "tracks": tracks,
        })
    except Exception as exc:
        # The SQL is built from the ACTUAL resolved columns, so a missing
        # column here is a genuine race/schema drift.  Re-raise missing-column
        # errors so the public route's self-heal (ALTER + retry) can recover
        # the schema; anything else is a graceful 500.
        msg = str(exc)
        if "column" in msg and "does not exist" in msg:
            raise
        logger.error("Search error", error=msg, exc_info=True)
        return jsonify({"error": msg}), 500


@misc_api_bp.route("/search", methods=["POST"])
async def api_search() -> Any:
    """Public search route.

    The search SQL is built from the actual ``tracks`` columns resolved via
    ``to_regclass`` (album_artist → artist → none), so it never 500s on a
    bare legacy table.  If the album_artist/artist columns are absent it
    returns a graceful empty result with a hint to run a Navidrome import.

    Failure-driven self-heal: when a query proves a search-critical column
    is unusable on the query's connection (``UndefinedColumn``), the schema
    is healed and the retry FORCES the ``artist``-based SQL (via
    ``_SEARCH_FORCE_ARTIST``) instead of re-trusting the probe — the
    provably-working path even when the probe keeps resolving a different
    ``tracks`` table.
    """
    global _SEARCH_FORCE_ARTIST
    try:
        result = await _api_search_impl()
        _SEARCH_FORCE_ARTIST = False
        return result
    except Exception as exc:
        # A search query proved the schema is missing search-critical
        # columns (UndefinedColumn) — heal the table and retry ONCE before
        # falling back to the 500.  ADD COLUMN IF NOT EXISTS is idempotent,
        # so concurrent healers (hypercorn workers) are safe.
        msg = str(exc)
        if "column" in msg and "does not exist" in msg:
            _self_heal_tracks_schema()
            _SEARCH_FORCE_ARTIST = True
            try:
                return await _api_search_impl()
            except Exception as retry_exc:
                retry_msg = str(retry_exc)
                if "column" in retry_msg and "does not exist" in retry_msg:
                    # The heal could not add the column (permissions, locked
                    # table, or a probe that keeps resolving a different
                    # table).  Never surface a 500 for a library that simply
                    # has not converged — return a graceful empty result with
                    # the hint instead, exactly like the bare-table path.
                    logger.warning(
                        "Search degraded after heal: column still missing",
                        error=retry_msg,
                    )
                    return jsonify({
                        "artists": [],
                        "albums": [],
                        "compilations": [],
                        "live_albums": [],
                        "eps": [],
                        "singles": [],
                        "tracks": [],
                        "note": "Library tracks table is missing metadata columns — run a Navidrome import scan to populate track metadata.",
                    })
                logger.error("Search error after schema heal", error=retry_msg, exc_info=True)
                return jsonify({"error": retry_msg}), 500
        logger.error("Search error", error=msg, exc_info=True)
        return jsonify({"error": msg}), 500


# ===========================================================================
# STATS & UTILS
# ===========================================================================

@misc_api_bp.route("/stats", methods=["GET"])
def api_stats() -> Any:
    """Get library statistics."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT COUNT(*) as track_count, COUNT(DISTINCT album) as album_count, "
                       "COUNT(DISTINCT COALESCE(NULLIF(album_artist, ''), artist)) as artist_count, "
                       "AVG(stars) as avg_stars, SUM(duration) as total_duration FROM tracks"))
            stats = dict(result.fetchone()._mapping)
        return jsonify({"success": True, **stats})
    except Exception as exc:
        logger.error("Failed to fetch stats", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/track-count", methods=["GET"])
def api_track_count() -> Any:
    """Get total track count for progress calculation."""
    try:
        with db_session() as session:
            count = session.execute(text("SELECT COUNT(*) as count FROM tracks")).scalar()
        return jsonify({"count": count or 0})
    except Exception as exc:
        logger.error("Failed to fetch track count", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/integrations/status", methods=["GET"])
def api_integrations_status() -> Any:
    """Return health/status information for all configured integrations."""
    status = get_all_services_status()
    return jsonify({"success": True, "integrations": status})


@misc_api_bp.route("/features/update", methods=["POST"])
async def api_features_update() -> Any:
    """Update individual feature flags in config.yaml."""
    try:
        data = (await request.get_json(silent=True)) or {}
    except Exception:
        data = {}
    if not data:
        return jsonify({"success": False, "error": "No JSON data provided"}), 400

    allowed_bool_keys = {
        "perpetual", "force", "launch_on_startup", "startup_scan_restart",
        "sync_ratings_to_all_users",
    }
    updates = {k: bool(v) for k, v in data.items() if k in allowed_bool_keys}
    if not updates:
        return jsonify({"success": False, "error": "No supported feature flags in payload"}), 400

    try:
        ok = save_partial_config({"features": updates})
        if not ok:
            return jsonify({"success": False, "error": "Failed to write config.yaml"}), 500
        logger.info("Updated feature flags", updates=updates)
        return jsonify({"success": True, **updates})
    except Exception as exc:
        logger.error("Error updating feature flags", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ===========================================================================
# ARTIST COUNTRY
# ===========================================================================

@misc_api_bp.route("/artist/country", methods=["POST"])
async def api_fetch_artist_country() -> Any:
    """Fetch artist country from MusicBrainz and update database."""
    data = (await request.get_json()) or {}
    artist = str(data.get("artist_name") or "").strip()
    if not artist:
        return jsonify({"error": "artist_name required"}), 400
    try:
        client = get_shared_mb_client()
        country = client.get_artist_country(artist)
        if country:
            with db_session() as session:
                session.execute(text("INSERT INTO artists (name, country) VALUES (:artist, :country) ON CONFLICT (name) DO UPDATE SET country = EXCLUDED.country"), {"artist": artist, "country": country})
                session.execute(text("UPDATE tracks SET artist_country = :country WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"), {"country": country, "artist": artist})
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        logger.error("Fetch artist country failed", artist=artist, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/artist/country/update", methods=["POST"])
async def api_update_artist_country() -> Any:
    """Manually update artist country."""
    data = (await request.get_json()) or {}
    artist = str(data.get("artist_name") or "").strip()
    country = str(data.get("country") or "").strip()
    if not artist or not country:
        return jsonify({"error": "artist_name and country required"}), 400
    try:
        with db_session() as session:
            session.execute(text("INSERT INTO artists (name, country) VALUES (:artist, :country) ON CONFLICT (name) DO UPDATE SET country = EXCLUDED.country"), {"artist": artist, "country": country})
            session.execute(text("UPDATE tracks SET artist_country = :country WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"), {"country": country, "artist": artist})
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        logger.error("Update artist country failed", artist=artist, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/artist/country/apply-as-genre", methods=["POST"])
async def api_apply_country_as_genre() -> Any:
    """Apply artist country as genre tag to all tracks."""
    data = (await request.get_json()) or {}
    artist = str(data.get("artist_name") or "").strip()
    if not artist:
        return jsonify({"error": "artist_name required"}), 400
    try:
        with db_session() as session:
            result = session.execute(text("""SELECT country FROM artists WHERE name = :artist """), {"artist": artist})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Artist not found or no country"}), 404
            country = row[0]
            session.execute(
                text("""UPDATE tracks SET genres = CONCAT_WS(' \\ ', COALESCE(NULLIF(genres, ''), ''), :country) WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"""),
                {"country": country, "artist": artist},
            )
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        logger.error("Apply country as genre failed", artist=artist, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# DUPLICATE ARTISTS
# ===========================================================================

@misc_api_bp.route("/duplicate-artists/<path:artist>", methods=["GET"])
def api_get_duplicate_artists(artist: str) -> Any:
    """Get duplicate artists for a specific artist."""
    artist = unquote(artist)
    try:
        with db_session() as session:
            row = session.execute(text("""
                SELECT DISTINCT musicbrainz_artistid
                FROM tracks
                WHERE artist = :artist
                  AND musicbrainz_artistid IS NOT NULL
                  AND musicbrainz_artistid != ''
                LIMIT 1
            """), {"artist": artist}).fetchone()
            artist_mbid = str(row[0]) if row else None

            duplicates = []
            if artist_mbid:
                rows = session.execute(text("""
                    SELECT artist, COUNT(*) AS track_count
                    FROM tracks
                    WHERE musicbrainz_artistid = :mbid
                    GROUP BY artist
                    ORDER BY track_count DESC
                """), {"mbid": artist_mbid}).fetchall()
                variations_data = [dict(r._mapping) for r in rows]

                if len(variations_data) > 1:
                    canonical_mb = variations_data[0].get("artist") or ""
                    try:
                        mb_artist = get_shared_mb_client().get_artist(artist_mbid) or {}
                        if (mb_artist.get("name") or "").strip():
                            canonical_mb = str(mb_artist["name"]).strip()
                    except Exception:
                        pass
                    variations = [v.get("artist") for v in variations_data if v.get("artist")]
                    track_counts = {
                        v.get("artist"): int(v.get("track_count") or 0)
                        for v in variations_data if v.get("artist")
                    }
                    duplicates.append({
                        "mbid": artist_mbid,
                        "canonical_mb": canonical_mb,
                        "variations": variations,
                        "track_counts": track_counts,
                        "current_artist": artist,
                    })

        return jsonify({
            "success": True,
            "duplicates": duplicates,
            "artist_info": {"name": artist, "mbid": artist_mbid},
        })
    except Exception as exc:
        logger.error("Get duplicate artists failed", error=str(exc))
        return jsonify({"success": False, "error": str(exc), "duplicates": []}), 500


@misc_api_bp.route("/duplicate-artists/merge", methods=["POST"])
async def api_merge_duplicate_artists() -> Any:
    """Merge duplicate artist variants into a canonical name."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        new_artist = str(payload.get("new_artist") or "").strip()
        source_artists = payload.get("source_artists") or []
        dry_run = bool(payload.get("dry_run", False))

        if not new_artist:
            return jsonify({"success": False, "error": "new_artist is required"}), 400

        if isinstance(source_artists, str):
            source_artists = [source_artists]
        sources = [str(s).strip() for s in (source_artists or []) if str(s).strip()]
        sources = [s for s in sources if s.lower() != new_artist.lower()]
        sources.append(new_artist.lower())
        sources.append(new_artist.upper())
        sources.append(new_artist.title())
        sources = list(dict.fromkeys(s for s in sources if s != new_artist))

        if dry_run:
            estimated = normalize_existing_artist_rows(
                canonical_artist_name=new_artist, aliases=sources,
            )
            return jsonify({
                "success": True, "dry_run": True,
                "updated_db": int(estimated or 0), "updated_files": 0,
                "moved_files": 0, "errors": [],
                "message": f"[DRY RUN] Would merge {len(sources)} variant(s) into '{new_artist}': {int(estimated or 0)} tracks",
            })

        errors: list[str] = []
        updated_db = 0
        try:
            updated_db = int(normalize_existing_artist_rows(
                canonical_artist_name=new_artist, aliases=sources,
            ) or 0)
        except Exception as exc:
            errors.append(f"DB normalize failed: {exc}")

        updated_files = 0
        if updated_db and not errors:
            try:
                with db_session() as session:
                    rows = session.execute(
                        text("SELECT id, file_path FROM tracks "
                             "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                             "AND file_path IS NOT NULL AND TRIM(file_path) != ''"),
                        {"artist": new_artist},
                    ).fetchall() or []
                for row in rows:
                    fp = str(row[1] or "")
                    if not fp or not os.path.isfile(fp):
                        continue
                    try:
                        if update_file_metadata(fp, {"artist": new_artist, "album_artist": new_artist}):
                            updated_files += 1
                    except Exception:
                        pass
            except Exception as exc:
                errors.append(f"Tag sync failed: {exc}")

        return jsonify({
            "success": True, "dry_run": False,
            "updated_db": updated_db, "updated_files": updated_files,
            "moved_files": 0, "errors": errors,
            "message": (f"Merged {len(sources)} variant(s) into '{new_artist}': "
                        f"{updated_db} tracks updated, {updated_files} file tags synced"),
        })
    except Exception as exc:
        logger.error("Duplicate artist merge failed", error=str(exc), exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ===========================================================================
# GENRES
# ===========================================================================

@misc_api_bp.route("/genres/track/<path:track_id>", methods=["GET"])
def api_genres_track(track_id: str) -> Any:
    """Get all genre sources for a single track."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                       essentia_genres, mood, listenbrainz_genres
                FROM tracks WHERE CAST(id AS TEXT) = :id
            """), {"id": track_id})
            row = result.fetchone()
            
        if not row:
            return jsonify({"error": "Track not found"}), 404

        mapping = row._mapping
        genres: dict[str, list[dict[str, str | int]]] = {}
        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        
        for output_key in source_keys:
            raw = mapping.get(output_key)
            if not raw:
                genres[output_key] = []
                continue
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
            if isinstance(parsed, list):
                genres[output_key] = [{"name": g["name"] if isinstance(g, dict) else str(g), "count": g.get("count", 1) if isinstance(g, dict) else 1} for g in parsed]
            else:
                genres[output_key] = []
                
        return jsonify({"success": True, "genres": genres})
    except Exception as exc:
        logger.error("Failed to get track genres", track_id=track_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/album/<path:album>/<path:artist>", methods=["GET"])
def api_genres_album(album: str, artist: str) -> Any:
    """Get aggregated genres across all tracks in an album."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                       essentia_genres, mood, listenbrainz_genres
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
            """), {"artist": artist, "album": album})
            rows = result.fetchall()

        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        result_genres: dict[str, list[dict[str, str | int]]] = {k: [] for k in source_keys}

        for row in rows:
            mapping = row._mapping
            for key in source_keys:
                raw = mapping.get(key)
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
                if isinstance(parsed, list):
                    for g in parsed:
                        name = g["name"] if isinstance(g, dict) else str(g)
                        result_genres[key].append(name)
                        
        for key in source_keys:
            counter = Counter(result_genres[key])
            result_genres[key] = [{"name": name, "count": count} for name, count in counter.most_common(25)]

        return jsonify({"success": True, "genres": result_genres})
    except Exception as exc:
        logger.error("Failed to get album genres", artist=artist, album=album, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/artist/<path:artist>", methods=["GET"])
def api_genres_artist(artist: str) -> Any:
    """Get aggregated genres across all tracks by an artist."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                       essentia_genres, mood, listenbrainz_genres
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
            """), {"artist": artist})
            rows = result.fetchall()

        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        result_genres: dict[str, list[dict[str, str | int]]] = {k: [] for k in source_keys}

        for row in rows:
            mapping = row._mapping
            for key in source_keys:
                raw = mapping.get(key)
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
                if isinstance(parsed, list):
                    for g in parsed:
                        name = g["name"] if isinstance(g, dict) else str(g)
                        result_genres[key].append(name)
                        
        for key in source_keys:
            counter = Counter(result_genres[key])
            result_genres[key] = [{"name": name, "count": count} for name, count in counter.most_common(30)]

        return jsonify({"success": True, "genres": result_genres})
    except Exception as exc:
        logger.error("Failed to get artist genres", artist=artist, error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/remove", methods=["POST"])
async def api_remove_genres() -> Any:
    """Remove specific genres from artist or album's tracks."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        artist_name = str(payload.get("artist_name") or "").strip()
        album_name = str(payload.get("album_name") or "").strip()
        genres_to_remove = payload.get("genres") or []

        if not artist_name and not album_name:
            return jsonify({"error": "artist_name or album_name required"}), 400
        if not genres_to_remove or not isinstance(genres_to_remove, list):
            return jsonify({"error": "genres must be a non-empty list"}), 400

        remove_lower = {str(g).strip().lower() for g in genres_to_remove if str(g).strip()}
        if not remove_lower:
            return jsonify({"error": "genres must be a non-empty list"}), 400

        def _strip_genres(raw: str | None) -> str:
            if not raw:
                return ""
            cleaned = [
                g for g in (str(g).strip() for g in raw.replace("\\", ",").split(","))
                if g and g.lower() not in remove_lower
            ]
            return ", ".join(dict.fromkeys(cleaned))

        affected = 0
        with db_session() as session:
            if album_name:
                rows = session.execute(
                    text("SELECT id, genres, manual_genres FROM tracks "
                         "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                    {"artist": artist_name, "album": album_name},
                ).fetchall() or []
            else:
                rows = session.execute(
                    text("SELECT id, genres, manual_genres FROM tracks "
                         "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"),
                    {"artist": artist_name},
                ).fetchall() or []

            for row in rows:
                track_id, genres_raw, manual_raw = row[0], row[1] or "", row[2] or ""
                new_genres = _strip_genres(genres_raw)
                new_manual = _strip_genres(manual_raw)
                if new_genres == (genres_raw or "") and new_manual == (manual_raw or ""):
                    continue
                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": new_genres, "manual": new_manual, "id": track_id},
                )
                affected += 1

        if affected:
            try:
                log_genre_update(
                    artist_name=artist_name, album_name=album_name, track_id=None,
                    genres_before="", genres_after="",
                    action_type="remove_from_album" if album_name else "remove_from_artist",
                    affected_count=affected,
                    change_summary=f"Removed genres from {affected} tracks: {', '.join(sorted(remove_lower))}",
                )
            except Exception as exc:
                logger.debug("Audit log failed", error=str(exc))

        def _trigger_scan() -> None:
            # REMOVED: remote Navidrome auto-sync disabled (see
            # _trigger_scan_after_tag_write) — the full import syncs once.
            return None

        threading.Thread(target=_trigger_scan, daemon=True).start()
        return jsonify({
            "success": True,
            "affected_tracks": affected,
            "message": f"Removed genres from {affected} track(s)",
            "scan_triggered": True,
        }), 200
    except Exception as exc:
        logger.error("Genre removal failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/tags/track/<track_id>", methods=["GET", "POST"])
async def api_track_tags(track_id: str) -> Any:
    """Get or update a single track's metadata tags."""
    if request.method == "GET":
        try:
            tags = get_track_tags(track_id)
            if not tags:
                return jsonify({"error": "Track not found"}), 404
            return jsonify({"success": True, "track_id": track_id, "tags": tags})
        except Exception as exc:
            logger.error("Error getting track tags", track_id=track_id, error=str(exc), exc_info=True)
            return jsonify({"error": str(exc)}), 500

    try:
        data = (await request.get_json(silent=True)) or {}
        sync_to_file = bool(data.get("sync_to_file", False))
        tag_updates = data.get("tags") if isinstance(data.get("tags"), dict) else {}

        genre = str(data.get("genre") or "").strip()
        if genre:
            with db_session() as session:
                row = session.execute(
                    text("SELECT genres, manual_genres FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).fetchone()
                if not row:
                    return jsonify({"error": "Track not found"}), 404

                def _merge(raw: str | None) -> str:
                    existing = [g.strip() for g in (raw or "").replace("\\", ",").split(",") if g.strip()]
                    if genre.lower() not in {x.lower() for x in existing}:
                        existing.append(genre)
                    return ", ".join(existing)

                merged_genres = _merge(row[0])
                merged_manual = _merge(row[1])
                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": merged_genres, "manual": merged_manual, "id": track_id},
                )
            tag_updates = {"genres": merged_genres, "manual_genres": merged_manual}

        if not tag_updates:
            return jsonify({"error": "No tags to update"}), 400

        try:
            ok = update_track_tags(track_id, tag_updates)
            if not ok:
                return jsonify({"error": "Failed to update tags in database"}), 500
        except Exception as exc:
            logger.error("Database update failed for track tags", track_id=track_id, error=str(exc))
            return jsonify({"error": f"Database error: {exc}"}), 500

        file_synced = False
        if sync_to_file:
            try:
                file_synced = bool(sync_track_tags_to_file(track_id))
            except Exception as exc:
                logger.debug("File sync failed", track_id=track_id, error=str(exc))

        navidrome_scan_triggered = False
        if file_synced:
            navidrome_scan_triggered = _trigger_scan_after_tag_write()

        return jsonify({
            "success": True,
            "track_id": track_id,
            "file_synced": file_synced,
            "navidrome_scan_triggered": navidrome_scan_triggered,
            "message": f"Updated {len(tag_updates)} field(s) for track {track_id}",
        })
    except Exception as exc:
        logger.error("Unexpected error updating track tags", track_id=track_id, error=str(exc), exc_info=True)
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@misc_api_bp.route("/genres/apply", methods=["POST"])
async def api_apply_genres() -> Any:
    """Apply genres to an artist's tracks."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        artist_name = str(payload.get("artist_name") or "").strip()
        genres = payload.get("genres") or []
        if not artist_name:
            return jsonify({"error": "artist_name required"}), 400
        clean = [str(g).strip() for g in genres if str(g).strip()]
        if not clean:
            return jsonify({"error": "genres must be a non-empty list"}), 400

        def _merge_genres(raw: str | None) -> str:
            existing = [g.strip() for g in (raw or "").replace("\\", ",").split(",") if g.strip()]
            merged = existing[:]
            for g in clean:
                if g.lower() not in {x.lower() for x in merged}:
                    merged.append(g)
            return ", ".join(merged)

        affected = 0
        with db_session() as session:
            rows = session.execute(
                text("SELECT id, genres, manual_genres FROM tracks "
                     "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"),
                {"artist": artist_name},
            ).fetchall() or []
            for row in rows:
                track_id, genres_raw, manual_raw = row[0], row[1] or "", row[2] or ""
                new_genres = _merge_genres(genres_raw)
                new_manual = _merge_genres(manual_raw)
                if new_genres == genres_raw and new_manual == manual_raw:
                    continue
                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": new_genres, "manual": new_manual, "id": track_id},
                )
                affected += 1

        if affected:
            try:
                log_genre_update(
                    artist_name=artist_name, track_id=None,
                    action_type="apply_to_artist", affected_count=affected,
                    change_summary=f"Applied genres to {affected} tracks: {', '.join(clean)}",
                )
            except Exception as exc:
                logger.debug("Audit log failed", error=str(exc))

        def _trigger_scan() -> None:
            # REMOVED: remote Navidrome auto-sync disabled (see
            # _trigger_scan_after_tag_write) — the full import syncs once.
            return None

        threading.Thread(target=_trigger_scan, daemon=True).start()
        return jsonify({"success": True, "affected_tracks": affected,
                        "message": f"Applied genres to {affected} track(s)"}), 200
    except Exception as exc:
        logger.error("Genre apply failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/recent-updates", methods=["GET"])
def api_recent_genre_updates() -> Any:
    """Get recent genre updates for the logs page."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT * FROM genre_updates ORDER BY created_at DESC LIMIT 50"))
            rows = result.fetchall()
        return jsonify({"success": True, "updates": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        logger.error("Failed to get recent genre updates", error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# CORRECTIONS
# ===========================================================================

@misc_api_bp.route("/correcting/fix-album-field", methods=["POST"])
async def api_correcting_fix_album_field() -> Any:
    """Apply a single field value to all tracks in an album."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        value = payload.get("value")
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        updated, files = fix_album_field(album_artist, album, field, value)
        return jsonify({"success": True, "updated_count": updated, "files_updated": files})
    except Exception as exc:
        logger.error("Fix album field failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/albums")
def api_correcting_albums() -> Any:
    """Albums whose album-level tags disagree across their own tracks.

    The JSON twin of the /correcting page. get_album_tag_inconsistencies()
    already powered that page server-side, but nothing exposed the same data in
    a machine-readable form — so the results could be listed on that one page and
    nowhere else, and every other surface had to re-derive them (or, more
    usually, didn't).

    These are the albums Navidrome shows as several separate entries: 21
    album-level fields (album_artist, MBIDs, release type/status/country,
    compilation, genres, mood, year, label, track/disc totals, grouping, media,
    album version, disc subtitle, script, ReplayGain album gain/peak) must agree
    across every track of an album.

    Per album+field ignores from the correction_ignores table are already
    honoured by the service, so ignored conflicts do not appear here either.

    Query params:
        artist  optional — scope to one album artist (case-sensitive, matching
                COALESCE(NULLIF(album_artist, ''), artist))

    Returns the compact "albums" map the client annotates rows with, keyed
    "<album_artist>::<album>" lowercased, plus a total. The full per-field value
    breakdown stays server-side: only the /correcting page edits values.
    """
    artist_filter = (request.args.get("artist") or "").strip() or None
    try:
        albums = get_album_tag_inconsistencies(artist_filter=artist_filter)

        compact: dict[str, dict[str, Any]] = {}
        for entry in albums:
            album_artist = str(entry.get("album_artist") or "").strip()
            album = str(entry.get("album") or "").strip()
            inconsistencies = entry.get("inconsistencies") or []
            key = f"{album_artist.lower()}::{album.lower()}"
            compact[key] = {
                "album_artist": album_artist,
                "album": album,
                "track_count": int(entry.get("track_count") or 0),
                "field_labels": [i.get("field_label") for i in inconsistencies if i.get("field_label")],
            }

        return jsonify({"success": True, "total": len(compact), "albums": compact})
    except Exception as exc:
        logger.error("Fetch album inconsistencies failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/mb-suggestions")
def api_correcting_mb_suggestions() -> Any:
    """Fetch MusicBrainz authoritative values for an album."""
    album_artist = request.args.get("album_artist", "").strip()
    album = request.args.get("album", "").strip()
    if not album:
        return jsonify({"error": "album required"}), 400
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT musicbrainz_albumid FROM tracks WHERE COALESCE(NULLIF(album_artist,''), artist) = :artist AND album = :album "
                "AND musicbrainz_albumid IS NOT NULL AND TRIM(musicbrainz_albumid) != '' GROUP BY musicbrainz_albumid ORDER BY COUNT(*) DESC LIMIT 1"),
                {"artist": album_artist, "album": album},
            )
            row = result.fetchone()
        if not row:
            return jsonify({"success": True, "suggestions": {}, "mbid": None}), 200
        mbid = str(row[0])
        
        mb_client = get_shared_mb_client()
        get_rate_limiter().throttle_musicbrainz()
        data = mb_client.get_release(mbid, inc="release-groups+labels", timeout=12.0)
        
        if not data:
            return jsonify({"success": True, "suggestions": {}, "mbid": mbid}), 200
        suggestions = {}
        rg = data.get("release-group") or {}
        raw_date = (rg.get("first-release-date") or data.get("date") or "").strip()
        if raw_date:
            suggestions["year"] = raw_date[:4]
        country = (data.get("country") or "").strip()
        if country:
            suggestions["releasecountry"] = country
        status = (data.get("status") or "").strip()
        if status:
            suggestions["releasestatus"] = status
        primary_type = (rg.get("primary-type") or "").strip()
        secondary_types = [t for t in (rg.get("secondary-types") or []) if t]
        if primary_type or secondary_types:
            all_types = ([primary_type] if primary_type else []) + secondary_types
            suggestions["releasetype"] = ", ".join(all_types)
        label_info = data.get("label-info") or []
        if label_info:
            label_obj = label_info[0].get("label") or {}
            label_name = (label_obj.get("name") or "").strip()
            if label_name:
                suggestions["label"] = label_name
                suggestions["recordlabel"] = label_name
        media_list = data.get("media") or []
        if media_list:
            suggestions["disctotal"] = str(len(media_list))
            total_tracks = sum(len(m.get("tracks") or []) or int(m.get("track-count") or 0) for m in media_list)
            if total_tracks:
                suggestions["tracktotal"] = str(total_tracks)
            formats = [m.get("format") for m in media_list if m.get("format")]
            if formats:
                suggestions["media"] = formats[0]
        return jsonify({"success": True, "suggestions": suggestions, "mbid": mbid})
    except Exception as exc:
        logger.error("MB suggestions fetch failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/ignore", methods=["POST"])
async def api_correcting_ignore() -> Any:
    """Persist an ignore rule for a specific (album_artist, album, field)."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        with db_session() as session:
            session.execute(
                text("CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
                "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
                "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))")
            )
            session.execute(
                text("INSERT INTO correction_ignores (album_artist, album, field) VALUES (:artist, :album, :field) ON CONFLICT DO NOTHING"),
                {"artist": album_artist, "album": album, "field": field},
            )
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("Correction ignore failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/unignore", methods=["POST"])
async def api_correcting_unignore() -> Any:
    """Remove an ignore rule."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        with db_session() as session:
            session.execute(
                text("CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
                "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
                "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))")
            )
            session.execute(
                text("DELETE FROM correction_ignores WHERE album = :album AND field = :field AND COALESCE(album_artist, '') = :artist"),
                {"album": album, "field": field, "artist": album_artist},
            )
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("Correction unignore failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/ignores")
async def api_correcting_list_ignores() -> Any:
    """Return all active ignore rules."""
    try:
        with db_session() as session:
            session.execute(
                text("CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
                "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
                "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))")
            )
            result = session.execute(text("SELECT album_artist, album, field, ignored_at FROM correction_ignores"))
            rows = result.fetchall()
        ignores = [{"album_artist": str(r[0] or ""), "album": str(r[1] or ""),
                     "field": str(r[2] or ""), "ignored_at": str(r[3] or "")}
                    for r in rows]
        return jsonify({"success": True, "ignores": ignores})
    except Exception as exc:
        logger.error("List ignores failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# BOOKMARKS
# ===========================================================================

@misc_api_bp.route("/bookmarks", methods=["GET", "POST"])
async def api_bookmarks() -> Any:
    """Get all bookmarks or add a new bookmark."""
    if request.method == "GET":
        with db_session() as session:
            result = session.execute(text("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 100"))
            rows = result.fetchall()
        return jsonify({"success": True, "bookmarks": [dict(r._mapping) for r in rows]})
    elif request.method == "POST":
        data = (await request.get_json()) or {}
        btype = str(data.get("type") or "custom").strip()
        name = str(data.get("name") or "").strip()
        url = str(data.get("url") or "").strip()
        artist_name = str(data.get("artist") or data.get("artist_name") or "").strip()
        album_name = str(data.get("album") or data.get("album_name") or "").strip()
        title = str(data.get("title") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        with db_session() as session:
            result = session.execute(
                text("""
                    INSERT INTO bookmarks (type, name, url, artist_name, album_name, title)
                    VALUES (:type, :name, :url, :artist_name, :album_name, :title)
                    RETURNING id
                """),
                {
                    "type": btype, "name": name, "url": url,
                    "artist_name": artist_name, "album_name": album_name, "title": title,
                },
            )
            return jsonify({"success": True, "id": result.scalar()}), 201
    return jsonify({"error": "Unsupported method"}), 405


@misc_api_bp.route("/bookmarks/<int:bookmark_id>", methods=["DELETE"])
def api_delete_bookmark(bookmark_id: int) -> Any:
    """Delete a bookmark."""
    try:
        with db_session() as session:
            session.execute(text("DELETE FROM bookmarks WHERE id = :id"), {"id": bookmark_id})
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("Delete bookmark failed", bookmark_id=bookmark_id, error=str(exc))
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# ESSENTIA
# ===========================================================================

_ESSENTIA_MODEL_URLS = [
    "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.pb",
    "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
    "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
    "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb",
    "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json",
]
_ESSENTIA_REPO_URL = "https://github.com/WB2024/Essentia-to-Metadata.git"
_ESSENTIA_SCRIPT_DEFAULT = "/opt/Essentia-to-Metadata/tag_music.py"
_ESSENTIA_MODELS_DEFAULT = "/opt/essentia_models"

_essentia_download_thread = None
_essentia_download_lock = threading.Lock()


def _essentia_progress_file() -> str:
    return os.path.join(get_state_directory(), "essentia_download_progress.json")


def _write_essentia_progress(is_running: bool, **extra: Any) -> None:
    try:
        payload = {
            "is_running": is_running,
            "scan_type": "essentia_download",
            "last_updated": datetime.now().isoformat(),
        }
        payload.update(extra)
        path = _essentia_progress_file()
        _dir = os.path.dirname(path) or "."
        os.makedirs(_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        logger.debug("Failed writing essentia download progress", error=str(exc))


def _do_essentia_download() -> None:
    try:
        cfg = get_config() or {}
        essentia_cfg = cfg.get("essentia", {}) if isinstance(cfg, dict) else {}
        script_path = str(essentia_cfg.get("script_path") or "").strip()
        models_dir_cfg = str(essentia_cfg.get("models_dir") or "").strip()

        clone_dir = os.path.dirname(script_path) if script_path else os.path.dirname(_ESSENTIA_SCRIPT_DEFAULT)
        target_models_dir = models_dir_cfg or _ESSENTIA_MODELS_DEFAULT
        total_files = len(_ESSENTIA_MODEL_URLS)

        _write_essentia_progress(
            True, status="cloning_script",
            current_step=f"Cloning Essentia-to-Metadata to {clone_dir}…",
            files_done=0, files_total=total_files,
        )
        parent_dir = os.path.dirname(clone_dir)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
            
        if os.path.isdir(os.path.join(clone_dir, ".git")):
            result = subprocess.run(
                ["git", "-C", clone_dir, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                logger.warning("Essentia git pull returned non-zero", error=result.stderr.strip())
        else:
            result = subprocess.run(
                ["git", "clone", "--depth=1", _ESSENTIA_REPO_URL, clone_dir],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed: {result.stderr.strip()[:300]}")

        os.makedirs(target_models_dir, exist_ok=True)
        for idx, url in enumerate(_ESSENTIA_MODEL_URLS):
            filename = os.path.basename(url)
            dest_path = os.path.join(target_models_dir, filename)
            _write_essentia_progress(
                True, status="downloading_models",
                current_step=f"Downloading {filename} ({idx + 1}/{total_files})…",
                files_done=idx, files_total=total_files,
            )
            if os.path.isfile(dest_path):
                continue
            tmp_path = dest_path + ".tmp"
            try:
                with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1 << 20):
                            if chunk:
                                fh.write(chunk)
                os.replace(tmp_path, dest_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

        _write_essentia_progress(
            False, status="complete", current_step="Download complete",
            files_done=total_files, files_total=total_files,
            models_dir=target_models_dir, script_dir=clone_dir,
        )
    except Exception as exc:
        logger.error("Essentia model download failed", error=str(exc), exc_info=True)
        _write_essentia_progress(False, status="error", current_step=f"Error: {exc}", error=str(exc))


@misc_api_bp.route("/essentia/download-models", methods=["POST"])
def api_essentia_download_models() -> Any:
    """Download the Essentia-to-Metadata script and ML model files."""
    global _essentia_download_thread
    with _essentia_download_lock:
        if _essentia_download_thread is not None and _essentia_download_thread.is_alive():
            return jsonify({"status": "already_running", "message": "Download already in progress"}), 409
        _essentia_download_thread = threading.Thread(
            target=_do_essentia_download, daemon=True, name="essentia-download"
        )
        _essentia_download_thread.start()
    return jsonify({"status": "started", "message": "Essentia download started"}), 202


@misc_api_bp.route("/essentia/download-status")
def api_essentia_download_status() -> Any:
    """Return download status for Essentia models/script."""
    progress = None
    try:
        with open(_essentia_progress_file(), "r", encoding="utf-8") as fh:
            progress = json.load(fh)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("Essentia progress read failed", error=str(exc))

    if isinstance(progress, dict) and progress.get("is_running"):
        return jsonify(progress)
    if isinstance(progress, dict) and progress.get("status") == "error":
        return jsonify(progress), 500

    cfg = get_config()
    models_dir = (
        (cfg.get("essentia", {}) or {}).get("models_dir")
        or os.environ.get("ESSENTIA_MODELS_DIR")
        or _ESSENTIA_MODELS_DEFAULT
    )
    if os.path.isdir(models_dir):
        model_files = [f for f in os.listdir(models_dir) if f.endswith((".pb", ".json"))]
        if model_files:
            return jsonify({"status": "installed", "models_dir": models_dir, "file_count": len(model_files)}), 200

    if isinstance(progress, dict) and progress.get("status") == "complete":
        return jsonify(progress)

    return jsonify({"status": "idle", "models_dir": models_dir}), 200


# ===========================================================================
# DATABASE CLEANUP
# ===========================================================================

@misc_api_bp.route("/database/cleanup-duplicates", methods=["POST"])
async def api_cleanup_duplicates() -> Any:
    """Remove duplicate track rows that share the same file path."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        artist = str(payload.get("artist") or "").strip()

        if artist:
            summary = sanitize_artist_file_paths_and_duplicates(artist)
            return jsonify({
                "success": True,
                "artist": artist,
                "stats": summary,
                "message": f"Removed {summary['duplicates_removed']} duplicate track(s) for '{artist}'",
            })

        total = {"path_updates": 0, "duplicates_removed": 0}
        artists: list[str] = []
        with db_session() as session:
            rows = session.execute(
                text("SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) FROM tracks "
                     "WHERE file_path IS NOT NULL AND TRIM(file_path) != ''")
            ).fetchall() or []
            artists = [str(r[0]) for r in rows if r[0]]
            
        for artist_name in artists:
            try:
                s = sanitize_artist_file_paths_and_duplicates(artist_name)
                total["path_updates"] += int(s.get("path_updates") or 0)
                total["duplicates_removed"] += int(s.get("duplicates_removed") or 0)
            except Exception as exc:
                logger.debug("Skip duplicate cleanup for artist", artist=artist_name, error=str(exc))
                
        return jsonify({
            "success": True,
            "stats": total,
            "message": f"Removed {total['duplicates_removed']} duplicate track(s) across {len(artists)} artist(s)",
        })
    except Exception as exc:
        logger.error("Duplicate cleanup failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500
