"""Artist metadata and correction service.

This replaces the duplicated combination of:
- artist_service.py
- artist_metadata_service.py
- artist_corrections_service.py

Scan-specific MusicBrainz comparison stays in artist_scan_service.py.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.metadata import (
    fetch_track_for_delete,
    delete_track_row,
    merge_album_names,
    count_album_disc_numbers,
    clear_album_disc_numbers,
    artist_track_count,
    fetch_cached_missing_releases,
    update_album_mbid_fields,
)
from services.metadata.album_mbid_guard import (
    guard_album_mbid,
    is_album_mbid_guard_enabled,
    log_verdict,
    rejection_payload,
    resolve_mbid_text,
)

logger = structlog.get_logger(__name__)


def delete_track(track_id: str, delete_file: bool = True) -> tuple[dict[str, Any], int]:
    """Delete a track DB row and optionally remove its local file."""
    row = fetch_track_for_delete(None, track_id)
    if not row:
        return {"success": False, "error": "Track not found"}, 404

    data = {
        "id": row[0],
        "file_path": row[1],
        "artist": row[2],
        "album": row[3],
        "title": row[4],
    }

    deleted_file = False
    file_path = data.get("file_path")
    if delete_file and file_path:
        normalized = str(file_path).replace("\\", "/")
        if not normalized.startswith("__queued_for_download__") and os.path.exists(file_path):
            try:
                os.remove(file_path)
                deleted_file = True
            except Exception as exc:
                logger.warning("File delete failed during correction", error=str(exc))

    delete_track_row(None, track_id)

    return {"success": True, "deleted_track_id": track_id, "deleted_file": deleted_file}, 200


def merge_albums(artist: str, source_albums: list[str], canonical_name: str) -> tuple[dict[str, Any], int]:
    if not source_albums:
        return {"success": False, "error": "source_albums is required"}, 400
    rows_updated = merge_album_names(None, artist, source_albums, canonical_name)
    return {"success": True, "rows_updated": rows_updated}, 200


def clear_disc_number(artist: str, album: str, force: bool = False) -> tuple[dict[str, Any], int]:
    disc_count = count_album_disc_numbers(None, artist, album)
    if disc_count > 1 and not force:
        return {"success": False, "error": "Likely multi-disc album", "needs_manual_review": True}, 409
    cleared = clear_album_disc_numbers(None, artist, album)
    return {"success": True, "cleared": cleared}, 200


def artist_exists(artist: str) -> tuple[dict[str, Any], int]:
    count = artist_track_count(None, artist)
    return {"exists": count > 0}, 200


def get_cached_missing(artist: str) -> tuple[dict[str, Any], int]:
    rows = fetch_cached_missing_releases(None, artist)
    return {
        "artist": artist,
        "missing": [{"title": r[0], "id": r[1]} for r in rows],
    }, 200


def apply_album_mbid(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Apply album MBID to all tracks for an artist/album and sync file tags.

    Guarded by the same sanity check as the album page's apply action: this is
    a fan-out to the DB columns AND the file tags of every track, so an ID
    whose own title/artist disagrees with the album must not be forced on.
    """
    artist_name = str(payload.get("artist") or "").strip()
    album_name = str(payload.get("album") or "").strip()
    album_mbid = str(payload.get("mbid") or "").strip()
    release_group_mbid = str(payload.get("release_group_mbid") or "").strip()

    if not artist_name or not album_name or not album_mbid:
        return {"success": False, "error": "artist, album, and mbid are required"}, 400

    if is_album_mbid_guard_enabled():
        try:
            with db_session() as session:
                rows = session.execute(text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                """), {"artist": artist_name, "album": album_name}).fetchall() or []
            file_paths = [str(r[0] or "") for r in rows]
        except Exception:
            file_paths = []

        mb_text = resolve_mbid_text(mbid=album_mbid, rg_mbid=release_group_mbid)
        verdict = guard_album_mbid(
            artist=artist_name,
            album=album_name,
            mb_artist=mb_text.get("artist", ""),
            mb_album=mb_text.get("title", ""),
            file_paths=file_paths or None,
        )
        if not verdict["ok"]:
            log_verdict(verdict, artist=artist_name, album=album_name, mbid=album_mbid, rg_mbid=release_group_mbid)
            return rejection_payload(
                verdict,
                artist=artist_name,
                album=album_name,
                mbid=album_mbid,
                rg_mbid=release_group_mbid,
            ), 409

    rows_updated = update_album_mbid_fields(None, artist_name, album_name, album_mbid, release_group_mbid, None)

    files_updated = 0
    if rows_updated:
        try:
            from services.metadata.tag_file_service import update_file_tags as update_tags
            with db_session() as session:
                result = session.execute(text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                """), {"artist": artist_name, "album": album_name})
                for row in result.fetchall() or []:
                    fp = str(row[0] or "").strip()
                    if fp and os.path.exists(fp):
                        try:
                            tags = {"musicbrainz_album_mbid": album_mbid}
                            if release_group_mbid:
                                tags["musicbrainz_releasegroupid"] = release_group_mbid
                            if update_tags(fp, tags):
                                files_updated += 1
                        except Exception:
                            pass
        except Exception:
            pass

    return {
        "success": True,
        "rows_updated": rows_updated or 0,
        "files_updated": files_updated,
        "message": f"Applied album MBID to {rows_updated or 0} track(s)",
    }, 200


def get_correction_albums(artist_name: str) -> tuple[dict[str, Any], int]:
    """Return per-album correction data for an artist (for corrections UI)."""
    artist_name = str(artist_name or "").strip()
    if not artist_name:
        return {"success": False, "error": "artist required", "albums": []}, 400

    try:
        with db_session() as session:
            rows = session.execute(text("""
                SELECT
                    album,
                    COUNT(*) AS track_count,
                    COUNT(*) FILTER (WHERE disc_number IS NULL OR disc_number = '') AS disc_issue_count,
                    COUNT(*) FILTER (WHERE mbid IS NULL OR mbid = '') AS mbid_issue_count,
                    COUNT(*) FILTER (WHERE file_path IS NULL OR file_path = '') AS missing_track_count,
                    COUNT(*) FILTER (WHERE file_path IS NOT NULL AND file_path != '') AS present_track_count,
                    MAX(CASE WHEN musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != '' THEN 1 ELSE 0 END) AS has_mbid,
                    MAX(year) AS album_year
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
                   OR LOWER(REGEXP_REPLACE(
                          COALESCE(NULLIF(album_artist, ''), artist),
                          '(\s+[\[\(]?\s*(feat\.?|ft\.?|featuring|with|w\/|&|and)\s+.*?[\]\)]?$)',
                          '',
                          'i'
                      )) = LOWER(:name)
                GROUP BY album
                ORDER BY album
            """), {"name": artist_name})

            def _classify(album_row: dict[str, Any]) -> str:
                # Delegates to the canonical registry so this per-artist view
                # agrees with the artist page.  It used to classify by TITLE
                # only, which meant a library album whose stored type was
                # "album+remix" or "album+soundtrack" was bucketed from its
                # name and could differ from the artist page's section.
                from services.catalog.release_categories import category_for_album_row

                return category_for_album_row(album_row)

            albums = []
            for r in rows.fetchall():
                row_dict = dict(r._mapping)
                present_count = int(row_dict.get("present_track_count") or 0)
                total_count = int(row_dict["track_count"] or 0)
                albums.append({
                    "album": row_dict["album"],
                    "track_count": total_count,
                    "disc_issues": int(row_dict["disc_issue_count"] or 0) > 0,
                    "disc_issue_count": int(row_dict["disc_issue_count"] or 0),
                    "mbid_issues": int(row_dict["mbid_issue_count"] or 0) > 0,
                    "mbid_issue_count": int(row_dict["mbid_issue_count"] or 0),
                    "missing_tracks": int(row_dict["missing_track_count"] or 0) > 0,
                    "missing_track_count": int(row_dict["missing_track_count"] or 0),
                    "has_mbid": bool(row_dict["has_mbid"]),
                    "album_year": int(row_dict["album_year"]) if row_dict.get("album_year") else None,
                    "is_missing": present_count == 0 and total_count > 0,
                    "album_type": _classify(row_dict),
                })
    except Exception as exc:
        logger.error("Query failed for correction albums", artist=artist_name, error=str(exc))
        albums = []

    albums.sort(key=lambda a: (a.get("is_missing") or False, a.get("album_year") is None, -(a.get("album_year") or 0), str(a.get("album") or "").lower()))

    return {"success": True, "albums": albums}, 200


def get_artist_corrections(artist_name: str) -> tuple[dict[str, Any], int]:
    """Return the full corrections-page context for one artist."""
    import re as _re
    from difflib import SequenceMatcher

    artist_name = str(artist_name or "").strip()
    if not artist_name:
        return {"success": False, "error": "artist required"}, 400

    artist_expr = "COALESCE(NULLIF(album_artist, ''), artist)"

    def _duration_to_display(raw: Any) -> str:
        if raw in (None, ""):
            return "—"
        try:
            seconds = float(raw)
            if seconds > 10000:
                seconds = seconds / 1000.0
            seconds = int(round(seconds))
            return f"{seconds // 60}:{seconds % 60:02d}"
        except Exception:
            return "—"

    def _normalize_component(value: Any) -> str:
        from helpers.normalization_service import normalize_filename
        return normalize_filename(str(value or ""))

    def _safe_track_number(value: Any) -> str:
        try:
            num = int(str(value or "").strip() or 0)
            return f"{num:02d}" if num > 0 else "00"
        except (TypeError, ValueError):
            return "00"

    def _read_naming_format() -> str:
        from helpers.config_helpers import get_config
        return get_config().get(
            "naming_format",
            "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}",
        )

    def _build_expected_filename(row: dict[str, Any]) -> str:
        file_name_format = _read_naming_format()
        file_ext = os.path.splitext(str(row.get("file_path") or ""))[1] or ".mp3"
        year_value = str(row.get("year") or "").strip()[:4] if row.get("year") else "Unknown"
        format_vars = {
            "track_number": _safe_track_number(row.get("track_number")),
            "artist": _normalize_component(row.get("track_artist") or "Unknown Artist") or "Unknown Artist",
            "album_artist": _normalize_component(row.get("album_artist") or row.get("track_artist") or "Unknown Artist") or "Unknown Artist",
            "title": _normalize_component(row.get("title") or "Unknown Title") or "Unknown Title",
            "album": _normalize_component(row.get("album") or "Unknown Album") or "Unknown Album",
            "year": year_value or "Unknown",
        }
        fallback_rel = (
            f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
            f"{format_vars['track_number']} - {format_vars['artist']} - {format_vars['title']}{file_ext}"
        )
        try:
            rendered = file_name_format.format(**format_vars)
        except Exception:
            rendered = fallback_rel
        if not isinstance(rendered, str) or not rendered.strip():
            rendered = fallback_rel
        rendered = rendered.strip().replace("\\", "/")
        base_name = os.path.basename(rendered)
        if not base_name:
            base_name = os.path.basename(fallback_rel)
        if not os.path.splitext(base_name)[1]:
            base_name = f"{base_name}{file_ext}"
        return base_name

    version_variant_keywords = [
        "instrumental", "karaoke", "a cappella", "acapella",
        "acoustic", "demo", "orchestral", "symphonic",
    ]

    def file_variant_key(file_path: str) -> frozenset[str]:
        name = os.path.basename(str(file_path or "")).lower()
        return frozenset(
            kw for kw in version_variant_keywords
            if _re.search(r"\b" + _re.escape(kw) + r"\b", name)
        )

    _ALBUM_BRACKET_SUFFIX_RE = _re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
    _ALBUM_SUBTITLE_KEYWORDS_RE = _re.compile(
        r"\s*[-–—]\s*(?:original\s+)?(?:soundtrack|remaster(?:ed)?|deluxe|expanded|"
        r"reissue|anniversary|bonus|single|ep|collection|ultimate|gold|platinum)\s*$",
        _re.IGNORECASE,
    )

    def _normalize_album_for_dedup(title: str) -> str:
        if not title:
            return ""
        t = title.strip()
        prev = None
        iterations = 0
        while t != prev and iterations < 10:
            prev = t
            t = _ALBUM_BRACKET_SUFFIX_RE.sub("", t).strip()
            iterations += 1
        t = _ALBUM_SUBTITLE_KEYWORDS_RE.sub("", t).strip()
        t = _re.sub(r"[\s\-_:]+", " ", t).strip().lower()
        return t

    duplicate_groups: list[dict[str, Any]] = []
    missing_tracks: list[dict[str, Any]] = []
    mb_albums: list[dict[str, Any]] = []
    mbid_inconsistent_albums: list[dict[str, Any]] = []
    disc_inconsistent_albums: list[dict[str, Any]] = []
    duplicate_albums: list[dict[str, Any]] = []

    try:
        with db_session() as session:
            rows = session.execute(text(f"""
                SELECT
                    id,
                    album,
                    title,
                    COALESCE(NULLIF(artist, ''), '—') AS track_artist,
                    TRIM(COALESCE(CAST(track_number AS TEXT), '')) AS track_number,
                    TRIM(COALESCE(CAST(disc_number AS TEXT), '')) AS disc_number,
                    file_path,
                    duration,
                    year,
                    COALESCE(NULLIF(mbid, ''), '') AS mbid,
                    COALESCE(NULLIF(suggested_mbid, ''), '') AS suggested_mbid,
                    COALESCE(NULLIF(album_artist, ''), '') AS album_artist
                FROM tracks
                WHERE LOWER({artist_expr}) = LOWER(:artist)
                  AND title IS NOT NULL AND TRIM(title) != ''
            """), {"artist": artist_name})
            candidate_rows = [dict(r._mapping) for r in rows.fetchall()]

            grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            for row in candidate_rows:
                group_key = (
                    row.get("album") or "",
                    row.get("title") or "",
                    row.get("track_artist") or "",
                    row.get("track_number") or "",
                    row.get("disc_number") or "",
                )
                grouped.setdefault(group_key, []).append(row)

            for (album, title, track_artist, track_number, disc_number), tracks in grouped.items():
                if len(tracks) < 2:
                    continue
                variant_keys = [file_variant_key(t.get("file_path", "")) for t in tracks]
                if len(set(variant_keys)) > 1:
                    continue

                mbid_counts: dict[str, int] = {}
                for track in tracks:
                    mbid_value = (track.get("mbid") or "").strip()
                    if mbid_value:
                        mbid_counts[mbid_value] = mbid_counts.get(mbid_value, 0) + 1
                consensus_mbid = max(mbid_counts, key=mbid_counts.get) if mbid_counts else ""

                scored_tracks = []
                for track in tracks:
                    file_path = str(track.get("file_path") or "")
                    file_name = os.path.basename(file_path) if file_path else ""
                    expected_name = _build_expected_filename(track)

                    actual_name_norm = _normalize_component(os.path.splitext(file_name)[0])
                    expected_name_norm = _normalize_component(os.path.splitext(expected_name)[0])
                    filename_similarity = (
                        SequenceMatcher(None, actual_name_norm, expected_name_norm).ratio()
                        if actual_name_norm and expected_name_norm
                        else 0.0
                    )

                    mbid_score = 0.0
                    if (track.get("mbid") or "").strip():
                        mbid_score += 4.0
                    if (track.get("suggested_mbid") or "").strip():
                        mbid_score += 2.0
                    if consensus_mbid:
                        if (track.get("mbid") or "").strip() == consensus_mbid:
                            mbid_score += 3.0
                        elif (track.get("mbid") or "").strip():
                            mbid_score -= 2.0

                    path_score = 0.0
                    lowered_path = file_path.replace("\\", "/").lower()
                    if lowered_path.startswith("__queued_for_download__"):
                        path_score -= 2.5
                    elif "/downloads/" in lowered_path:
                        path_score -= 1.5
                    elif "/music/" in lowered_path:
                        path_score += 1.0

                    duration_bonus = 0.3 if track.get("duration") not in (None, "", 0, "0") else 0.0
                    keep_score = mbid_score + (filename_similarity * 2.5) + path_score + duration_bonus

                    recommendation_notes = []
                    if consensus_mbid and (track.get("mbid") or "").strip() == consensus_mbid:
                        recommendation_notes.append("Matches consensus MBID")
                    if filename_similarity >= 0.9:
                        recommendation_notes.append("Strong queue filename format match")
                    elif filename_similarity >= 0.75:
                        recommendation_notes.append("Good queue filename format match")
                    if "/downloads/" in lowered_path or lowered_path.startswith("__queued_for_download__"):
                        recommendation_notes.append("Located in downloads/queued path")

                    scored_tracks.append({
                        **track,
                        "file_name": file_name or "—",
                        "duration_display": _duration_to_display(track.get("duration")),
                        "expected_filename": expected_name,
                        "filename_similarity": round(filename_similarity, 3),
                        "mbid_score": round(mbid_score, 2),
                        "keep_score": round(keep_score, 3),
                        "recommendation_notes": recommendation_notes,
                    })

                sorted_by_keep = sorted(
                    scored_tracks,
                    key=lambda t: (
                        t.get("keep_score", 0),
                        -t.get("filename_similarity", 0),
                        str(t.get("id") or ""),
                    ),
                )
                recommended_delete_id = sorted_by_keep[0].get("id") if sorted_by_keep else None

                for track in scored_tracks:
                    track["recommend_delete"] = str(track.get("id")) == str(recommended_delete_id)

                track_ids = ", ".join(str(t.get("id")) for t in scored_tracks)
                recommendation_basis = "MusicBrainz confidence + queue filename format similarity"
                if consensus_mbid:
                    recommendation_basis += f" (consensus MBID: {consensus_mbid})"

                duplicate_groups.append({
                    "album": album,
                    "title": title,
                    "track_artist": track_artist,
                    "track_number": track_number,
                    "disc_number": disc_number,
                    "duplicate_count": len(scored_tracks),
                    "track_ids": track_ids,
                    "tracks": sorted(scored_tracks, key=lambda t: str(t.get("id") or "")),
                    "recommended_delete_id": recommended_delete_id,
                    "recommendation_basis": recommendation_basis,
                })

            duplicate_groups.sort(
                key=lambda g: (
                    -(int(g.get("duplicate_count") or 0)),
                    str(g.get("album") or ""),
                    str(g.get("title") or ""),
                )
            )

            rows = session.execute(text(f"""
                SELECT
                    id,
                    title,
                    album,
                    track_number,
                    disc_number,
                    year,
                    duration,
                    COALESCE(NULLIF(mbid, ''), '') AS mbid,
                    COALESCE(NULLIF(suggested_mbid, ''), '') AS suggested_mbid,
                    file_path,
                    CONCAT_WS(', ',
                        CASE WHEN title IS NULL OR TRIM(title) = '' THEN 'Title' END,
                        CASE WHEN album IS NULL OR TRIM(album) = '' THEN 'Album' END,
                        CASE WHEN track_number IS NULL OR TRIM(CAST(track_number AS TEXT)) = '' THEN 'Track Number' END,
                        CASE WHEN CAST(COALESCE(NULLIF(TRIM(CAST(track_number AS TEXT)), ''), '0') AS INTEGER) <= 0 THEN 'Track Number <= 0' END,
                        CASE WHEN duration IS NULL OR CAST(duration AS DOUBLE PRECISION) <= 0 THEN 'Duration' END,
                        CASE WHEN year IS NOT NULL AND TRIM(CAST(year AS TEXT)) != ''
                              AND (CAST(COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '0') AS INTEGER) < 1900
                                  OR CAST(COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '0') AS INTEGER) > 2100)
                            THEN 'Suspicious Year' END,
                        CASE WHEN (mbid IS NULL OR TRIM(mbid) = '') AND (suggested_mbid IS NULL OR TRIM(suggested_mbid) = '')
                            THEN 'Recording MBID' END,
                        CASE WHEN writer IS NULL OR TRIM(CAST(writer AS TEXT)) IN ('', '[]', 'null', 'None')
                            THEN 'Writer/Lyricist (optional)' END
                     ) AS metadata_missing_fields,
                    CASE
                        WHEN title IS NULL OR TRIM(title) = '' THEN 'Missing track title'
                        WHEN album IS NULL OR TRIM(album) = '' THEN 'Missing album name'
                        WHEN track_number IS NULL OR TRIM(CAST(track_number AS TEXT)) = '' THEN 'Missing track number'
                        WHEN CAST(COALESCE(NULLIF(TRIM(CAST(track_number AS TEXT)), ''), '0') AS INTEGER) <= 0 THEN 'Invalid track number (<= 0)'
                        WHEN duration IS NULL OR CAST(duration AS DOUBLE PRECISION) <= 0 THEN 'Missing or invalid duration'
                        WHEN year IS NOT NULL AND TRIM(CAST(year AS TEXT)) != ''
                             AND (CAST(COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '0') AS INTEGER) < 1900
                                  OR CAST(COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '0') AS INTEGER) > 2100)
                             THEN 'Suspicious year value'
                        WHEN (mbid IS NULL OR TRIM(mbid) = '') AND (suggested_mbid IS NULL OR TRIM(suggested_mbid) = '') THEN 'Missing recording MBID and suggested MBID'
                        ELSE 'Metadata needs review'
                    END AS metadata_issue_reason
                FROM tracks
                WHERE LOWER({artist_expr}) = LOWER(:artist)
                  AND (
                        title IS NULL OR TRIM(title) = '' OR
                        album IS NULL OR TRIM(album) = '' OR
                        track_number IS NULL OR TRIM(CAST(track_number AS TEXT)) = '' OR
                        CAST(COALESCE(NULLIF(TRIM(CAST(track_number AS TEXT)), ''), '0') AS INTEGER) <= 0 OR
                        duration IS NULL OR CAST(duration AS DOUBLE PRECISION) <= 0 OR
                        (
                            year IS NOT NULL AND TRIM(CAST(year AS TEXT)) != '' AND
                            (
                                CAST(COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '0') AS INTEGER) < 1900 OR
                                CAST(COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '0') AS INTEGER) > 2100
                            )
                        ) OR
                        ((mbid IS NULL OR TRIM(mbid) = '') AND (suggested_mbid IS NULL OR TRIM(suggested_mbid) = ''))
                  )
                ORDER BY album, track_number, title
            """), {"artist": artist_name})
            missing_tracks = [dict(r._mapping) for r in rows.fetchall()]

            rows = session.execute(text(f"""
                SELECT album, COUNT(*) AS track_count, MAX(musicbrainz_album_mbid) AS mb_mbid
                FROM tracks
                WHERE LOWER({artist_expr}) = LOWER(:artist)
                  AND musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != ''
                GROUP BY album
                ORDER BY album
            """), {"artist": artist_name})
            for r in rows.fetchall():
                row_dict = dict(r._mapping)
                mb_albums.append({
                    "album": row_dict.get("album"),
                    "track_count": int(row_dict.get("track_count") or 0),
                    "mb_mbid": row_dict.get("mb_mbid"),
                })

            rows = session.execute(text(f"""
                SELECT
                    album,
                    COUNT(*) AS track_count,
                    COUNT(DISTINCT TRIM(musicbrainz_album_mbid)) AS distinct_mbid_count,
                    STRING_AGG(DISTINCT TRIM(musicbrainz_album_mbid), ', ') AS mbid_list
                FROM tracks
                WHERE LOWER({artist_expr}) = LOWER(:artist)
                  AND album IS NOT NULL AND TRIM(album) != ''
                  AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != ''
                GROUP BY album
                HAVING COUNT(DISTINCT TRIM(musicbrainz_album_mbid)) > 1
                ORDER BY album
            """), {"artist": artist_name})
            for r in rows.fetchall():
                row_dict = dict(r._mapping)
                mbids = [m.strip() for m in str(row_dict.get("mbid_list") or "").split(",") if m.strip()]
                mbid_inconsistent_albums.append({
                    "album": row_dict.get("album"),
                    "track_count": int(row_dict.get("track_count") or 0),
                    "mbid_count": int(row_dict.get("distinct_mbid_count") or len(mbids)),
                    "mbids": mbids,
                })

            rows = session.execute(text(f"""
                SELECT
                    album,
                    COUNT(*) AS track_count,
                    SUM(CASE WHEN disc_number IS NOT NULL
                                  AND TRIM(CAST(disc_number AS TEXT)) != ''
                                  AND CAST(disc_number AS TEXT) != '0'
                             THEN 1 ELSE 0 END) AS tracks_with_disc,
                    SUM(CASE WHEN disc_number IS NULL
                                  OR TRIM(CAST(disc_number AS TEXT)) = ''
                                  OR CAST(disc_number AS TEXT) = '0'
                             THEN 1 ELSE 0 END) AS tracks_without_disc,
                    COUNT(DISTINCT CASE WHEN disc_number IS NOT NULL
                                        AND TRIM(CAST(disc_number AS TEXT)) != ''
                                        AND CAST(disc_number AS TEXT) != '0'
                                    THEN CAST(disc_number AS TEXT) END) AS distinct_disc_values,
                    MAX(CASE WHEN disc_number IS NOT NULL
                                  AND TRIM(CAST(disc_number AS TEXT)) != ''
                                  AND CAST(disc_number AS TEXT) != '0'
                             THEN CAST(disc_number AS TEXT) END) AS disc_value
                FROM tracks
                WHERE LOWER({artist_expr}) = LOWER(:artist)
                  AND album IS NOT NULL AND TRIM(album) != ''
                GROUP BY album
                HAVING SUM(CASE WHEN disc_number IS NOT NULL
                                     AND TRIM(CAST(disc_number AS TEXT)) != ''
                                     AND CAST(disc_number AS TEXT) != '0'
                                THEN 1 ELSE 0 END) > 0
                   AND SUM(CASE WHEN disc_number IS NULL
                                     OR TRIM(CAST(disc_number AS TEXT)) = ''
                                     OR CAST(disc_number AS TEXT) = '0'
                                THEN 1 ELSE 0 END) > 0
                ORDER BY album
            """), {"artist": artist_name})
            for r in rows.fetchall():
                row_dict = dict(r._mapping)
                disc_inconsistent_albums.append({
                    "album": row_dict.get("album"),
                    "track_count": int(row_dict.get("track_count") or 0),
                    "tracks_with_disc": int(row_dict.get("tracks_with_disc") or 0),
                    "tracks_without_disc": int(row_dict.get("tracks_without_disc") or 0),
                    "distinct_disc_values": int(row_dict.get("distinct_disc_values") or 0),
                    "disc_value": row_dict.get("disc_value"),
                })

            rows = session.execute(text(f"""
                SELECT
                    album,
                    COUNT(*) AS track_count,
                    MIN(year) AS album_year,
                    MAX(musicbrainz_album_mbid) AS mb_mbid
                FROM tracks
                WHERE LOWER({artist_expr}) = LOWER(:artist)
                  AND album IS NOT NULL AND TRIM(album) != ''
                GROUP BY album
                ORDER BY album
            """), {"artist": artist_name})
            all_album_rows = []
            for r in rows.fetchall():
                row_dict = dict(r._mapping)
                all_album_rows.append({
                    "album": str(row_dict.get("album") or ""),
                    "track_count": int(row_dict.get("track_count") or 0),
                    "album_year": row_dict.get("album_year"),
                    "mb_mbid": str(row_dict.get("mb_mbid") or "").strip(),
                })

            mbid_groups: dict[str, list[dict[str, Any]]] = {}
            for album_row in all_album_rows:
                mb = album_row["mb_mbid"]
                if mb:
                    mbid_groups.setdefault(mb, []).append(album_row)

            mbid_grouped_names = {
                r["album"]
                for group in mbid_groups.values()
                for r in group
                if len(group) > 1
            }

            norm_groups: dict[str, list[dict[str, Any]]] = {}
            for album_row in all_album_rows:
                if album_row["album"] in mbid_grouped_names:
                    continue
                norm = _normalize_album_for_dedup(album_row["album"])
                if norm:
                    norm_groups.setdefault(norm, []).append(album_row)

            seen_album_sets: set[tuple[str, ...]] = set()

            def _add_dup_group(albums_in_group: list[dict[str, Any]], signal: str) -> None:
                if len(albums_in_group) < 2:
                    return
                key = tuple(sorted(a["album"] for a in albums_in_group))
                if key in seen_album_sets:
                    return
                seen_album_sets.add(key)
                sorted_albums = sorted(albums_in_group, key=lambda a: -(a.get("track_count") or 0))
                duplicate_albums.append({
                    "albums": sorted_albums,
                    "canonical_name": sorted_albums[0]["album"],
                    "signal": signal,
                })

            for _mb, group in mbid_groups.items():
                _add_dup_group(group, "mbid")
            for _norm, group in norm_groups.items():
                _add_dup_group(group, "name")
    except Exception as exc:
        logger.error("Failed to fetch artist corrections", artist=artist_name, error=str(exc), exc_info=True)

    return {
        "success": True,
        "artist_name": artist_name,
        "duplicates": duplicate_groups,
        "missing_tracks": missing_tracks,
        "mb_albums": mb_albums,
        "mbid_inconsistent_albums": mbid_inconsistent_albums,
        "disc_inconsistent_albums": disc_inconsistent_albums,
        "duplicate_albums": duplicate_albums,
        "duplicate_count": sum(max(int(d.get("duplicate_count") or 0) - 1, 0) for d in duplicate_groups),
        "missing_count": len(missing_tracks),
        "mbid_inconsistent_count": len(mbid_inconsistent_albums),
        "disc_inconsistent_count": len(disc_inconsistent_albums),
        "duplicate_album_count": len(duplicate_albums),
    }, 200
