"""Upcoming releases routes — migrated from old app.py.

Uses ``services.enrichment.musicbrainz_service`` for all MusicBrainz lookups
to ensure proper rate limiting, User-Agent, and Lucene-escaping.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from quart import Blueprint, jsonify, request
from sqlalchemy import text

from api_clients.discogs_http import DiscogsHttpClient
from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars
from db.engine import db_session
from helpers.config_helpers import get_api_integration
from services.upcoming_releases.matching_service import match_to_musicbrainz
from services.upcoming_releases.musicbrainz_fetcher_service import get_refresh_status, start_musicbrainz_refresh
from services.upcoming_releases.wikipedia_scraper_service import get_release_window, scrape

logger = structlog.get_logger(__name__)

upcoming_bp = Blueprint("upcoming_releases", __name__, url_prefix="/api/upcoming-releases")

# Shared MusicBrainz client (lazy-init, respects 1 req/sec rate limit)
_mb_client: MusicBrainzHttpClient | None = None


def _get_mb_client() -> MusicBrainzHttpClient:
    global _mb_client
    if _mb_client is None:
        _mb_client = MusicBrainzHttpClient(enabled=True)
    return _mb_client


def _search_musicbrainz_release_group(artist: str, album: str, track: str = "") -> list[dict[str, Any]]:
    """Search MusicBrainz release-groups with proper rate limiting and escaping."""
    client = _get_mb_client()
    parts = []
    if artist:
        parts.append(f'artist:"{escape_lucene_special_chars(artist)}"')
    if album:
        parts.append(f'release:"{escape_lucene_special_chars(album)}"')
    if track:
        parts.append(f'recording:"{escape_lucene_special_chars(track)}"')
        
    query = " AND ".join(parts) if parts else (escape_lucene_special_chars(artist or album or track))
    return client.search_release_groups(query, limit=20)


def _persist_match_result(release_id: int, result: dict[str, Any]) -> None:
    """Persist a pipeline match outcome (matched / candidate / unmatched)."""
    status = result.get("status", "unmatched")
    score = float(result.get("score") or 0.0)
    now = datetime.now().isoformat()
    
    with db_session() as session:
        if status == "matched":
            best = result["candidates"][0]
            release_date = (best.get("first_release_date") or "")[:10]
            primary_type = str(
                best.get("primary-type") or best.get("primary_type") or best.get("category") or ""
            )
            session.execute(
                text("""UPDATE upcoming_releases SET
                        release_group_mbid = :mbid,
                        match_source = 'auto_match',
                        mbid_match_status = 'matched',
                        mbid_match_score = :score,
                        mbid_confidence = 'high',
                        mbid_source = 'auto_match',
                        mbid_last_checked_at = :checked,
                        candidate_release_group_mbid = NULL,
                        release_date = CASE
                            WHEN release_date IS NULL OR (:date IS NOT NULL AND :date < release_date)
                                THEN :date
                            ELSE release_date
                        END,
                        primary_type = :ptype
                    WHERE id = :id"""),
                {"mbid": best["mbid"], "score": score, "checked": now,
                 "date": release_date or None, "ptype": primary_type, "id": release_id},
            )
        elif status == "candidate":
            best = result["candidates"][0]
            session.execute(
                text("""UPDATE upcoming_releases SET
                        candidate_release_group_mbid = :mbid,
                        mbid_match_status = 'candidate',
                        mbid_match_score = :score,
                        mbid_confidence = 'medium',
                        mbid_source = 'auto_candidate',
                        mbid_last_checked_at = :checked
                    WHERE id = :id"""),
                {"mbid": best["mbid"], "score": score, "checked": now, "id": release_id},
            )
        else:
            session.execute(
                text("""UPDATE upcoming_releases SET
                        mbid_match_status = 'unmatched',
                        mbid_match_score = :score,
                        mbid_confidence = NULL,
                        mbid_source = NULL,
                        mbid_last_checked_at = :checked,
                        candidate_release_group_mbid = NULL
                    WHERE id = :id"""),
                {"score": score, "checked": now, "id": release_id},
            )


@upcoming_bp.route("", methods=["GET"])
def api_upcoming_releases() -> Any:
    """Get upcoming releases with collection/queue annotations and pagination."""
    try:
        release_filter = (request.args.get("filter", "") or "all").strip().lower()
        if release_filter not in ("all", "collection", "discovered"):
            release_filter = "all"
            
        if (request.args.get("collection") or "").strip().lower() == "true":
            release_filter = "collection"
            
        source_filter = (request.args.get("source", "") or "").strip()
        include_queue = request.args.get("include_queue", "").strip().lower() == "true"
        page = max(1, request.args.get("page", 1, type=int))
        limit = max(1, min(request.args.get("limit", 50, type=int), 1000))
        hide_in_library = request.args.get("hide_in_library", "1").strip().lower() != "0"
        window_days = max(0, request.args.get("window", 0, type=int))

        where_sql = ""
        params: dict[str, Any] = {}
        
        if source_filter and source_filter != "all":
            if source_filter == "musicbrainz_daily_collection":
                where_sql = " WHERE source = 'MusicBrainz Daily Collection'"
            else:
                where_sql = " WHERE COALESCE(source_key, source) = :source_key"
                params["source_key"] = source_filter

        try:
            _win_start, _win_end = get_release_window()
            _date_clause = (
                " (release_date IS NULL"
                " OR release_date BETWEEN :win_start AND :win_end"
                " OR (LENGTH(release_date) = 4 AND release_date BETWEEN :win_start_year AND :win_end_year))"
            )
            where_sql = (
                f" WHERE {_date_clause}"
                if not where_sql
                else where_sql + f" AND {_date_clause}"
            )
            params["win_start"] = _win_start.strftime("%Y-%m-%d")
            params["win_end"] = _win_end.strftime("%Y-%m-%d")
            params["win_start_year"] = _win_start.strftime("%Y")
            params["win_end_year"] = _win_end.strftime("%Y")
        except Exception:
            pass

        total_where_sql = where_sql
        count_params = dict(params)

        if window_days > 0:
            _today = datetime.now().date()
            _tight_clause = (
                " (release_date IS NULL"
                " OR release_date BETWEEN :win7_start AND :win7_end"
                " OR (LENGTH(release_date) = 4 AND release_date BETWEEN :win7_start_year AND :win7_end_year))"
            )
            where_sql = (
                f" WHERE {_tight_clause}"
                if not where_sql
                else where_sql + f" AND {_tight_clause}"
            )
            params["win7_start"] = (_today - timedelta(days=window_days)).isoformat()
            params["win7_end"] = (_today + timedelta(days=window_days)).isoformat()
            params["win7_start_year"] = str((_today - timedelta(days=window_days)).year)
            params["win7_end_year"] = str((_today + timedelta(days=window_days)).year)

        if hide_in_library:
            _lib_clause = """NOT EXISTS (
                SELECT 1 FROM tracks t
                WHERE LOWER(REGEXP_REPLACE(
                          COALESCE(NULLIF(t.album_artist, ''), t.artist),
                          '[^a-zA-Z0-9]', '', 'g'))
                      = LOWER(REGEXP_REPLACE(upcoming_releases.artist_name,
                          '[^a-zA-Z0-9]', '', 'g'))
                  AND LOWER(REGEXP_REPLACE(t.album, '[^a-zA-Z0-9]', '', 'g'))
                      = LOWER(REGEXP_REPLACE(upcoming_releases.album_name,
                          '[^a-zA-Z0-9]', '', 'g'))
            )"""
            where_sql = (
                f" WHERE {_lib_clause}"
                if not where_sql
                else where_sql + f" AND {_lib_clause}"
            )
            total_where_sql = (
                f" WHERE {_lib_clause}"
                if not total_where_sql
                else total_where_sql + f" AND {_lib_clause}"
            )

        with db_session() as session:
            total = session.execute(
                text(f"SELECT COUNT(*) FROM upcoming_releases{total_where_sql}"),
                count_params,
            ).scalar() or 0
            result = session.execute(
                text(
                    f"SELECT * FROM upcoming_releases{where_sql} "
                    "ORDER BY release_date ASC NULLS LAST, id ASC LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": limit + 1, "offset": (page - 1) * limit},
            )
            rows = result.fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        releases = [dict(r._mapping) for r in rows]

        if release_filter == "discovered":
            releases = [r for r in releases if (r.get("source") or "") != "MusicBrainz Daily Collection"]

        artist_names = list({r.get("artist_name", "") or "" for r in releases if r.get("artist_name")})

        def _norm(value: str | None) -> str:
            return re.sub(r"[^a-zA-Z0-9]", "", (value or "").lower())

        artists_in_collection: set[str] = set()
        if artist_names:
            try:
                with db_session() as session:
                    placeholders = ", ".join(f":a{i}" for i in range(len(artist_names)))
                    params = {f"a{i}": _norm(name) for i, name in enumerate(artist_names)}
                    batch = session.execute(
                        text(f"""SELECT DISTINCT LOWER(REGEXP_REPLACE(
                                      COALESCE(NULLIF(album_artist, ''), artist),
                                      '[^a-zA-Z0-9]', '', 'g')) AS aname
                                FROM tracks
                                WHERE LOWER(REGEXP_REPLACE(
                                      COALESCE(NULLIF(album_artist, ''), artist),
                                      '[^a-zA-Z0-9]', '', 'g')) IN ({placeholders})"""),
                        params,
                    )
                    artists_in_collection = {str(row[0]) for row in batch.fetchall()}
            except Exception:
                pass

        album_pairs = [(r.get("artist_name", "") or "", r.get("album_name", "") or "")
                       for r in releases if r.get("artist_name") and r.get("album_name")]
        albums_in_collection: set[tuple[str, str]] = set()
        if album_pairs:
            try:
                with db_session() as session:
                    pair_conditions = " OR ".join(
                        f"(LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist, ''), artist), '[^a-zA-Z0-9]', '', 'g')) = :a{i} "
                        f"AND LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g')) = :b{i})"
                        for i in range(len(album_pairs))
                    )
                    pair_params = {}
                    for i, (a, b) in enumerate(album_pairs):
                        pair_params[f"a{i}"] = _norm(a)
                        pair_params[f"b{i}"] = _norm(b)
                    batch2 = session.execute(
                        text(f"""SELECT DISTINCT
                                LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist, ''), artist), '[^a-zA-Z0-9]', '', 'g')),
                                LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g'))
                              FROM tracks WHERE {pair_conditions}"""),
                        pair_params,
                    )
                    albums_in_collection = {(str(row[0]), str(row[1])) for row in batch2.fetchall()}
            except Exception:
                pass

        queued_pairs: set[tuple[str, str]] = set()
        if include_queue and album_pairs:
            try:
                with db_session() as session:
                    q_conditions = " OR ".join(
                        f"(LOWER(REGEXP_REPLACE(artist, '[^a-zA-Z0-9]', '', 'g')) = :qa{i} "
                        f"AND LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g')) = :qb{i})"
                        for i in range(len(album_pairs))
                    )
                    q_params = {}
                    for i, (a, b) in enumerate(album_pairs):
                        q_params[f"qa{i}"] = _norm(a)
                        q_params[f"qb{i}"] = _norm(b)
                    batch3 = session.execute(
                        text(f"""SELECT DISTINCT
                                LOWER(REGEXP_REPLACE(artist, '[^a-zA-Z0-9]', '', 'g')),
                                LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g'))
                              FROM download_queue
                              WHERE ({q_conditions}) AND status IN ('queued', 'downloading', 'processing')"""),
                        q_params,
                    )
                    queued_pairs = {(str(row[0]), str(row[1])) for row in batch3.fetchall()}
            except Exception:
                pass

        for release in releases:
            artist_name = _norm(release.get("artist_name"))
            album_name = _norm(release.get("album_name"))
            release["artist_in_collection"] = artist_name in artists_in_collection
            release["album_in_collection"] = (artist_name, album_name) in albums_in_collection
            
            if include_queue and artist_name and album_name:
                in_queue = (artist_name, album_name) in queued_pairs
                release["in_queue"] = in_queue
                release["queue_status"] = "queued" if in_queue else None
            else:
                release["in_queue"] = False
                release["queue_status"] = None

        if release_filter == "collection":
            releases = [r for r in releases if r.get("artist_in_collection")]

        return jsonify({
            "success": True,
            "releases": releases,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": has_more,
        })
    except Exception as exc:
        logger.error("Failed to fetch upcoming releases", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/sources", methods=["GET"])
def api_upcoming_sources() -> Any:
    """Distinct release sources with counts, for the Source filter dropdown."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT
                        CASE
                            WHEN source = 'MusicBrainz Daily Collection' THEN 'musicbrainz_daily_collection'
                            ELSE COALESCE(source_key, source)
                        END AS key,
                        CASE
                            WHEN source = 'MusicBrainz Daily Collection' THEN 'MusicBrainz Daily Collection'
                            ELSE COALESCE(source_key, source)
                        END AS label,
                        COUNT(*) AS count
                    FROM upcoming_releases
                    GROUP BY key, label
                    ORDER BY count DESC, label ASC
                """)
            ).fetchall()
            
        sources = [{"key": str(r._mapping["key"]), "label": str(r._mapping["label"]), "count": int(r._mapping["count"] or 0)} for r in rows]
        return jsonify({"success": True, "sources": sources})
    except Exception as exc:
        logger.error("Failed to fetch upcoming release sources", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/<int:release_id>/match", methods=["POST"])
async def api_match_upcoming_release(release_id: int) -> Any:
    """Match an upcoming release to a MusicBrainz release-group."""
    data = (await request.get_json(silent=True)) or {}
    rg_mbid = (data.get("release_group_mbid") or "").strip()
    source = (data.get("source") or "manual_selection").strip()

    try:
        if rg_mbid:
            with db_session() as session:
                session.execute(
                    text("""UPDATE upcoming_releases SET
                            release_group_mbid = :mbid,
                            match_source = :source,
                            mbid_match_status = 'matched',
                            mbid_confidence = 'high',
                            mbid_source = :source,
                            mbid_last_checked_at = :checked,
                            candidate_release_group_mbid = NULL
                        WHERE id = :id"""),
                    {"mbid": rg_mbid, "source": source, "id": release_id, "checked": datetime.now().isoformat()},
                )
            return jsonify({"success": True, "release_group_mbid": rg_mbid, "match_source": source})

        with db_session() as session:
            row = session.execute(
                text("SELECT id, artist_name, album_name, release_date FROM upcoming_releases WHERE id = :id"),
                {"id": release_id},
            ).fetchone()
            
        if row is None:
            return jsonify({"error": "release not found"}), 404

        mapping = row._mapping
        artist = str(mapping.get("artist_name") or "").strip()
        album = str(mapping.get("album_name") or "").strip()
        if not artist or not album:
            return jsonify({"error": "release has no artist/album to search"}), 400

        result = match_to_musicbrainz(artist, album, str(mapping.get("release_date") or "").strip() or None)
        _persist_match_result(release_id, result)

        return jsonify({
            "success": True,
            "status": result["status"],
            "score": result["score"],
            "release_group_mbid": result["mbid"],
            "artist": artist,
            "album": album,
            "candidates": result["candidates"],
        })
    except Exception as exc:
        logger.error("Failed to match upcoming release", release_id=release_id, error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/scrape", methods=["POST"])
def api_scrape_upcoming_releases() -> Any:
    """Scrape Wikipedia for upcoming releases and store in DB."""
    try:
        results = scrape()
        total = results.get("total_found", 0)
        new_count = results.get("total_new", 0)
        updated_count = results.get("total_updated", 0)

        mb_started = False
        try:
            mb_started = start_musicbrainz_refresh()
        except Exception as exc:
            logger.warning("MusicBrainz upcoming refresh start failed", error=str(exc))

        message = f"Scraped {total} releases ({new_count} new, {updated_count} updated)"
        message += "; MusicBrainz refresh " + ("started" if mb_started else "already running")

        logger.info(
            "Scraped upcoming releases",
            total=total, new=new_count, updated=updated_count, mb_started=mb_started
        )
        return jsonify({
            "success": True,
            "message": message,
            "results": results,
            "musicbrainz": {"started": mb_started},
        })
    except Exception as exc:
        logger.error("Failed to scrape Wikipedia", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/scrape/status", methods=["GET"])
def api_upcoming_scrape_status() -> Any:
    """Live status of the background MusicBrainz refresh."""
    try:
        return jsonify(get_refresh_status())
    except Exception as exc:
        logger.error("Failed to read scrape status", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/refresh-musicbrainz", methods=["POST"])
def api_refresh_upcoming_releases_musicbrainz() -> Any:
    """Refresh upcoming releases with MusicBrainz metadata."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""SELECT id, artist_name, album_name, release_date
                        FROM upcoming_releases
                       WHERE release_group_mbid IS NULL
                         AND COALESCE(mbid_manual_override, FALSE) = FALSE
                       ORDER BY id""")
            )
            rows = result.fetchall()

        matched = 0
        candidates = 0
        unmatched = 0
        
        for row in rows:
            mapping = row._mapping
            release_id = mapping.get("id")
            artist = mapping.get("artist_name") or ""
            album = mapping.get("album_name") or ""
            release_date = mapping.get("release_date") or ""
            
            if not artist or not album:
                continue

            try:
                mresult = match_to_musicbrainz(artist, album, str(release_date).strip() or None)
            except Exception:
                continue

            _persist_match_result(release_id, mresult)
            if mresult["status"] == "matched":
                matched += 1
            elif mresult["status"] == "candidate":
                candidates += 1
            else:
                unmatched += 1

        logger.info(
            "Refreshed upcoming releases from MusicBrainz",
            total=len(rows), matched=matched, candidates=candidates, unmatched=unmatched,
        )
        return jsonify({
            "success": True,
            "message": f"Refreshed {len(rows)} releases ({matched} matched, {candidates} candidates)",
            "matched": matched,
            "candidates": candidates,
            "unmatched": unmatched,
            "total": len(rows),
        })
    except Exception as exc:
        logger.error("Failed to refresh from MusicBrainz", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/clear", methods=["POST"])
def api_clear_upcoming_releases() -> Any:
    """Clear all upcoming releases from the database."""
    try:
        with db_session() as session:
            session.execute(text("DELETE FROM upcoming_releases"))
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("Failed to clear upcoming releases", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/add", methods=["POST"])
async def api_add_upcoming_release() -> Any:
    """Add a release found in the MusicBrainz search modal to the upcoming list."""
    try:
        data = (await request.get_json(silent=True)) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or data.get("title") or "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album are required"}), 400

    release_date = str(data.get("release_date") or data.get("date") or "").strip()[:10]
    mbid = str(data.get("release_group_mbid") or data.get("mbid") or "").strip()
    primary_type = str(data.get("primary_type") or "").strip().lower()
    release_year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
    now = datetime.now().isoformat()

    try:
        with db_session() as session:
            # Same-typed row first, then the untyped (Wikipedia/legacy) row
            # for absorption — never a differently-typed row, so a
            # same-titled single and album stay distinct.
            dup = session.execute(
                text("""
                    SELECT id FROM upcoming_releases
                    WHERE LOWER(REGEXP_REPLACE(artist_name, '[^a-zA-Z0-9]', '', 'g'))
                          = LOWER(REGEXP_REPLACE(:artist, '[^a-zA-Z0-9]', '', 'g'))
                      AND LOWER(REGEXP_REPLACE(album_name, '[^a-zA-Z0-9]', '', 'g'))
                          = LOWER(REGEXP_REPLACE(:album, '[^a-zA-Z0-9]', '', 'g'))
                      AND (LOWER(COALESCE(TRIM(primary_type), '')) = :ptype
                           OR COALESCE(NULLIF(TRIM(primary_type), ''), '') = '')
                    ORDER BY CASE
                        WHEN LOWER(COALESCE(TRIM(primary_type), '')) = :ptype THEN 0
                        ELSE 1
                    END
                    LIMIT 1
                """),
                {"artist": artist, "album": album, "ptype": primary_type},
            ).fetchone()

            if dup:
                session.execute(
                    text("""
                        UPDATE upcoming_releases SET
                            last_seen_at = CURRENT_TIMESTAMP,
                            release_date = CASE
                                WHEN upcoming_releases.release_date IS NULL
                                     OR :date IS NULL
                                    THEN COALESCE(:date, upcoming_releases.release_date)
                                WHEN :date < upcoming_releases.release_date
                                    THEN :date
                                ELSE upcoming_releases.release_date
                            END,
                            release_year = COALESCE(:year, upcoming_releases.release_year),
                            release_group_mbid = COALESCE(:mbid, upcoming_releases.release_group_mbid),
                            primary_type = COALESCE(:ptype, upcoming_releases.primary_type)
                        WHERE id = :id
                    """),
                    {"id": dup[0], "date": release_date or None, "year": release_year,
                     "mbid": mbid or None, "ptype": primary_type or None},
                )
                return jsonify({"success": True, "merged": True, "id": dup[0]})

            result = session.execute(
                text("""
                    INSERT INTO upcoming_releases (
                        artist_name, album_name, release_date, release_year, source,
                        primary_type, release_group_mbid, mbid_match_status, mbid_source,
                        mbid_confidence, mbid_match_score, mbid_last_checked_at, status,
                        last_seen_at, updated_at
                    ) VALUES (
                        :artist, :album, :date, :year, 'Manual (MusicBrainz search)',
                        :ptype, :mbid, 'matched', 'manual_search', 'high',
                        1.0, :checked, 'discovered',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (artist_name, album_name, primary_type) DO UPDATE SET
                        last_seen_at = CURRENT_TIMESTAMP,
                        release_date = CASE
                            WHEN upcoming_releases.release_date IS NULL
                                 OR EXCLUDED.release_date IS NULL
                                THEN COALESCE(EXCLUDED.release_date, upcoming_releases.release_date)
                            WHEN EXCLUDED.release_date < upcoming_releases.release_date
                                THEN EXCLUDED.release_date
                            ELSE upcoming_releases.release_date
                        END,
                        release_year = COALESCE(EXCLUDED.release_year, upcoming_releases.release_year),
                        release_group_mbid = COALESCE(EXCLUDED.release_group_mbid, upcoming_releases.release_group_mbid),
                        primary_type = COALESCE(EXCLUDED.primary_type, upcoming_releases.primary_type)
                    RETURNING id
                """),
                {
                    "artist": artist, "album": album, "date": release_date or None,
                    "year": release_year, "ptype": primary_type,
                    "mbid": mbid or None, "checked": now,
                },
            )
            row = result.fetchone()
            return jsonify({"success": True, "merged": False, "id": row[0] if row else None})
    except Exception as exc:
        logger.error("Failed to add upcoming release", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/search-musicbrainz", methods=["POST"])
async def api_search_musicbrainz_release() -> Any:
    """Search MusicBrainz for a release (artist, album, or track)."""
    data = (await request.get_json()) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    track = data.get("track", "").strip()
    
    if not artist and not album and not track:
        return jsonify({"error": "provide artist, album, or track"}), 400

    try:
        results = _search_musicbrainz_release_group(artist, album, track)
        return jsonify({"success": True, "results": results})
    except Exception as exc:
        logger.error("MusicBrainz search failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/search-discogs", methods=["POST"])
async def api_search_discogs_release() -> Any:
    """Search Discogs for a release."""
    data = (await request.get_json()) or {}
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()
    
    if not artist or not album:
        return jsonify({"success": True, "results": []})
        
    try:
        discogs_cfg = get_api_integration("discogs")
        token = discogs_cfg.get("token") or ""
        
        if token:
            client = DiscogsHttpClient(token=token)
            params = {"q": f"{artist} {album}", "type": "release", "per_page": 10}
            results = client.search_database(params)
            return jsonify({"success": True, "results": results})
            
        return jsonify({"success": True, "results": []})
    except Exception as exc:
        logger.error("Discogs search failed", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500
