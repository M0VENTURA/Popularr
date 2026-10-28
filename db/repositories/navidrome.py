"""Navidrome-specific database write helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from db.engine import db_session


def bulk_upsert_navidrome_tracks(conn: Any = None, tracks: list[dict[str, Any]] | None = None) -> None:
    """Bulk insert/update Navidrome tracks using SQLAlchemy session."""
    if not tracks:
        return
    with db_session() as session:
        for track in tracks:
            session.execute(
                text("""
                    INSERT INTO tracks (id, title, artist, album, duration)
                    VALUES (:id, :title, :artist, :album, :duration)
                    ON CONFLICT (id) DO UPDATE SET
                        title = CASE WHEN COALESCE(tracks.metadata_dirty_for_navidrome, FALSE)
                                      AND tracks.local_tags_written_at IS NOT NULL
                                      AND (tracks.navidrome_imported_at IS NULL
                                           OR tracks.local_tags_written_at >= tracks.navidrome_imported_at)
                                     THEN tracks.title ELSE EXCLUDED.title END,
                        artist = CASE WHEN COALESCE(tracks.metadata_dirty_for_navidrome, FALSE)
                                       AND tracks.local_tags_written_at IS NOT NULL
                                       AND (tracks.navidrome_imported_at IS NULL
                                            OR tracks.local_tags_written_at >= tracks.navidrome_imported_at)
                                      THEN tracks.artist ELSE EXCLUDED.artist END,
                        album = CASE WHEN COALESCE(tracks.metadata_dirty_for_navidrome, FALSE)
                                      AND tracks.local_tags_written_at IS NOT NULL
                                      AND (tracks.navidrome_imported_at IS NULL
                                           OR tracks.local_tags_written_at >= tracks.navidrome_imported_at)
                                     THEN tracks.album ELSE EXCLUDED.album END,
                        duration = EXCLUDED.duration
                """),
                {
                    "id": track.get("id"),
                    "title": track.get("title", ""),
                    "artist": track.get("artist", ""),
                    "album": track.get("album", ""),
                    "duration": track.get("duration", 0),
                },
            )

# MusicBrainz freshness threshold: re-check recording metadata if older than this.
_MB_FRESHNESS_THRESHOLD_SECONDS = 86400  # 24 hours

# Seconds to wait after triggering a Navidrome scan before querying it.
_NAVIDROME_INDEXING_WAIT_SECONDS = 5

# Window (minutes) within which completed import groups are verified.
_IMPORT_GROUP_COMPLETION_WINDOW_MINUTES = 30