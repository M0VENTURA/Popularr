"""Album tag correction and consistency service.

Detects albums whose tracks have inconsistent album-level metadata values
(e.g. different album_artist, year, label, genres, etc.) that cause
Navidrome to display the same album as multiple entries.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


def _table_columns(cursor: Any = None, table_name: str = "") -> set[str]:
    """Return the set of column names for *table_name* (information_schema)."""
    if not table_name:
        return set()
    try:
        with db_session() as session:
            rows = session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :tbl"),
                {"tbl": table_name},
            ).fetchall() or []
        return {str(r[0]) for r in rows}
    except Exception:
        return set()


# Fields whose values MUST be consistent across all tracks of the same album.
ALBUM_CONSISTENCY_FIELDS: list[tuple[str, str]] = [
    ("album_artist", "Album Artist"),
    ("musicbrainz_album_mbid", "MusicBrainz Album ID"),
    ("musicbrainz_releasegroupid", "MusicBrainz Release Group ID"),
    ("releasetype", "Release Type"),
    ("releasestatus", "Release Status"),
    ("releasecountry", "Release Country"),
    ("compilation", "Compilation"),
    ("genres", "Genre"),
    ("mood", "Mood"),
    ("year", "Year"),
    ("label", "Label"),
    ("recordlabel", "Record Label"),
    ("tracktotal", "Track Total"),
    ("disctotal", "Disc Total"),
    ("grouping", "Grouping"),
    ("media", "Media"),
    ("albumversion", "Album Version"),
    ("discsubtitle", "Disc Subtitle"),
    ("script", "Script"),
    ("replaygain_album_gain", "ReplayGain Album Gain"),
    ("replaygain_album_peak", "ReplayGain Album Peak"),
]


def get_album_tag_inconsistencies(artist_filter: str | None = None) -> list[dict[str, Any]]:
    """Return albums that have inconsistent album-level tag values across their tracks."""
    try:
        track_columns = _table_columns(None, "tracks")
        has_album_artist = "album_artist" in track_columns
        artist_expr = "COALESCE(NULLIF(album_artist, ''), artist)" if has_album_artist else "artist"

        effective_fields = [
            (f, lbl) for f, lbl in ALBUM_CONSISTENCY_FIELDS
            if f in track_columns
        ]

        having_parts = []
        for field, _ in effective_fields:
            having_parts.append(f"COUNT(DISTINCT COALESCE(CAST({field} AS TEXT), '')) > 1")
        if not having_parts:
            return []
        having_sql = " OR ".join(having_parts)

        where_sql = "WHERE album IS NOT NULL AND TRIM(album) != ''"
        params: dict[str, Any] = {}
        if artist_filter:
            where_sql += f" AND {artist_expr} = :artist_filter"
            params["artist_filter"] = artist_filter

        with db_session() as session:
            flagged_albums = session.execute(
                text(
                    f"""
                    SELECT {artist_expr} AS album_artist, album, COUNT(*) AS track_count
                    FROM tracks {where_sql}
                    GROUP BY {artist_expr}, album
                    HAVING COUNT(*) >= 2 AND ({having_sql})
                    ORDER BY {artist_expr}, album
                    """
                ),
                params or None,
            ).mappings().all()

            ignored_keys: set[tuple[str, str, str]] = set()
            try:
                ignore_rows = session.execute(
                    text("SELECT album_artist, album, field FROM correction_ignores")
                ).mappings().all() or []
                ignored_keys = {
                    (str(r.get("album_artist") or ""), str(r.get("album") or ""), str(r.get("field") or ""))
                    for r in ignore_rows
                }
            except Exception:
                pass

            results = []
            for row in flagged_albums:
                row_dict = dict(row)
                aa = row_dict.get("album_artist") or ""
                al = row_dict.get("album") or ""
                tc = int(row_dict.get("track_count") or 0)

                try:
                    detail_rows = session.execute(
                        text(
                            f"SELECT id, {', '.join(f for f, _ in effective_fields)} "
                            f"FROM tracks WHERE {artist_expr} = :aa AND album = :al"
                        ),
                        {"aa": aa, "al": al},
                    ).mappings().all()
                except Exception:
                    continue

                inconsistencies = []
                for field, field_label in effective_fields:
                    value_map: dict[str, dict[str, Any]] = {}
                    for dr in detail_rows:
                        val_str = str(dr.get(field) or "").strip()
                        bucket = value_map.setdefault(
                            val_str, {"value": val_str, "count": 0, "track_ids": []}
                        )
                        bucket["count"] += 1
                        if dr.get("id") is not None:
                            bucket["track_ids"].append(str(dr.get("id")))

                    non_empty = {v: d for v, d in value_map.items() if v}
                    if len(non_empty) >= 2 and (aa, al, field) not in ignored_keys:
                        inconsistencies.append({
                            "field": field,
                            "field_label": field_label,
                            "values": sorted(non_empty.values(), key=lambda x: -x["count"]),
                        })

                if inconsistencies:
                    results.append({
                        "album_artist": aa,
                        "album": al,
                        "track_count": tc,
                        "inconsistencies": inconsistencies,
                    })

        return results
    except Exception as exc:
        logger.warning("Failed to get album tag inconsistencies", error=str(exc), exc_info=True)
        return []


def fix_album_field(album_artist: str, album: str, field: str, value: Any) -> tuple[int, int]:
    """Apply a single field value to all tracks in an album.

    Returns (tracks_updated, files_updated).
    """
    allowed = {f for f, _ in ALBUM_CONSISTENCY_FIELDS}
    if field not in allowed:
        logger.warning("Field not in allowed consistency fields", field=field)
        return 0, 0

    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, file_path FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :aa AND album = :al"
                ),
                {"aa": album_artist, "al": album},
            ).mappings().all()
            affected = [dict(r) for r in rows]
            if not affected:
                return 0, 0

            session.execute(
                text(
                    f"UPDATE tracks SET {field} = :value "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :aa AND album = :al"
                ),
                {"value": value, "aa": album_artist, "al": album},
            )
            updated_count = len(affected)

        files_updated = 0
        files_failed = 0
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            write_tags_to_file,
        )
        for track in affected:
            fp = str(track.get("file_path") or "").strip()
            if not fp:
                files_failed += 1
                continue
            # Resolve before writing: the DB may hold a path relative to the
            # music root, and the writer returns False for anything that does
            # not exist on disk.
            resolved = resolve_music_file_path(fp)
            if not resolved:
                files_failed += 1
                logger.warning(
                    "fix_album_field: audio file not resolvable",
                    file_path=fp, album=album, field=field,
                )
                continue
            try:
                # ⚠️ The RETURN VALUE is checked. This counted `files_updated`
                # for any call that did not RAISE — so a writer that returned
                # False (no file, tags disabled, unsupported format) was still
                # reported as a successful write.
                if write_tags_to_file(resolved, {field: value}):
                    files_updated += 1
                else:
                    files_failed += 1
            except Exception as tag_exc:
                files_failed += 1
                logger.warning(
                    "fix_album_field: file tag write failed",
                    file_path=resolved, field=field, error=str(tag_exc),
                )

        if files_failed:
            logger.warning(
                "fix_album_field: some audio files were not updated",
                album=album, files_updated=files_updated, files_failed=files_failed,
            )

        return updated_count, files_updated
    except Exception as exc:
        logger.error("fix_album_field failed", error=str(exc), exc_info=True)
        return 0, 0
