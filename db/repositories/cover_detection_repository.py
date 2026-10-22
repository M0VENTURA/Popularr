"""Cover detection repository — ALL database read/write for cover detection.

Extracted from the legacy ``CoverDetector`` class to enforce the
repository-only-DB-access rule.  Every function opens its own SQLAlchemy
session (``db_session``) with named binds — the legacy ``conn`` parameter is
kept only for backward compatibility and is ignored.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from db.engine import db_session
from db.utils import row_get

logger = logging.getLogger(__name__)

# Per-process memo: artist lookup -> {normalised writer name: count}.  The
# cover detector's writer step calls ``is_common_writer_for_artist`` once per
# writer per track; without this every call re-read and JSON-parsed the
# artist's ENTIRE writer column.  Bounded so a long scan cannot balloon
# memory.
_writer_counts_cache: dict[str, dict[str, int]] = {}
_WRITER_COUNTS_CACHE_MAX = 500


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def track_has_original_by_artist(
    conn,
    artist: str,
    title: str,
) -> bool:
    """Return True when *artist* already has a non-cover recording of *title*."""
    if not artist or not title:
        return False
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT 1 FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(title) = LOWER(:title)
                      AND COALESCE(CAST(is_cover AS INTEGER), 0) = 0
                    LIMIT 1
                """),
                {"artist": artist, "title": title},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def is_common_writer_for_artist(
    conn,
    writer: str,
    artist: str,
    track_artist: Optional[str] = None,
    min_count: int = 2,
) -> bool:
    """Return True when *writer* appears on at least *min_count* tracks by *artist*."""
    if not writer or not artist:
        return False

    lookup = track_artist if track_artist and not _loose_match(track_artist, artist) else artist
    writer_norm = _loose_normalize(writer)

    # Memoised per-artist writer counts — the cover writer step calls this
    # once per writer per track, and the full-column read + JSON parse is the
    # dominant cost.
    counts = _writer_counts_cache.get(lookup)
    if counts is None:
        counts = {}
        try:
            with db_session() as session:
                rows = session.execute(
                    text("""
                        SELECT writer FROM tracks
                        WHERE (LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:lookup)
                               OR LOWER(artist) = LOWER(:lookup))
                          AND writer IS NOT NULL AND writer != '' AND writer != '[]'
                    """),
                    {"lookup": lookup},
                ).fetchall() or []
        except Exception:
            return False
        for row in rows:
            raw = row_get(row, "writer", 0, "")
            if not raw:
                continue
            try:
                names = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(names, list):
                    names = [str(names)]
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            for n in names:
                k = _loose_normalize(str(n))
                if k:
                    counts[k] = counts.get(k, 0) + 1
        while len(_writer_counts_cache) >= _WRITER_COUNTS_CACHE_MAX:
            try:
                _writer_counts_cache.pop(next(iter(_writer_counts_cache)))
            except (StopIteration, KeyError):
                break
        _writer_counts_cache[lookup] = counts

    return counts.get(writer_norm, 0) >= min_count


def writer_coverage_for_artist(conn, artist: str) -> float:
    """Return fraction (0.0–1.0) of tracks by *artist* that have a non-null writer field."""
    if not artist:
        return 1.0
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN writer IS NOT NULL
                                 AND TRIM(CAST(writer AS TEXT)) NOT IN ('', '[]', 'null', 'None')
                            THEN 1 ELSE 0 END) AS with_writer
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                """),
                {"artist": artist},
            ).mappings().first()
        if row:
            total = int(row.get("total") or 0)
            with_writer = int(row.get("with_writer") or 0)
            return float(with_writer) / float(total) if total else 1.0
    except Exception:
        pass
    return 1.0


def get_track_writers_from_db(conn, track_id: str) -> List[str]:
    """Fetch writer JSON from the tracks table for a single track."""
    if not track_id:
        return []
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT writer FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
        if row:
            raw = row[0]
            if raw:
                try:
                    w = json.loads(raw) if isinstance(raw, str) else raw
                    return w if isinstance(w, list) else [str(w)]
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        pass
    return []


def is_cover_fully_confirmed(conn, track_id: str) -> bool:
    """Return True when the track already has all three cover confirmation signals:
    is_cover=1, original_cover_artist set, and cover_manual_override is not True.
    
    Once confirmed, the track should not be re-checked on subsequent scans
    unless explicitly forced.
    """
    if not track_id:
        return False
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT is_cover, original_cover_artist, cover_manual_override "
                    "FROM tracks WHERE CAST(id AS TEXT) = :id"
                ),
                {"id": str(track_id)},
            ).fetchone()
        if not row:
            return False
        is_cover = row[0]
        original_artist = row[1] or ""
        manual_override = row[2]
        if manual_override:
            return True  # user-override means skip detection entirely
        if is_cover and str(original_artist).strip():
            return True
        return False
    except Exception:
        return False


def get_track_genres(conn, track_id: str) -> dict:
    """Return (genres, musicbrainz_genres) for a track."""
    if not track_id:
        return {"genres": "", "musicbrainz_genres": ""}
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT genres, musicbrainz_genres FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
        if row:
            return {
                "genres": row[0] or "",
                "musicbrainz_genres": row[1] or "",
            }
    except Exception:
        pass
    return {"genres": "", "musicbrainz_genres": ""}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _genre_list_from_db(raw: Any) -> Optional[list]:
    """A stored genre column as a list, or ``None`` when it cannot be read.

    ⚠️ ``None`` means "do NOT overwrite". A JSONB column arrives from the
    driver as a Python LIST, which ``json.loads`` rejects — the old code then
    fell back to ``[]`` and saved ``["Cover"]``, WIPING the track's real
    MusicBrainz genres. An unreadable value is now left exactly as it is.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, tuple):
        return list(raw)
    text_value = str(raw).strip()
    if not text_value or text_value.lower() in ("null", "none", "[]"):
        return []
    try:
        parsed = json.loads(text_value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def apply_cover_metadata_batch(conn, updates: List[Dict], max_retries: int = 5) -> List[str]:
    """Persist cover metadata for a batch of tracks.

    For every update: "Cover" is added to ``musicbrainz_genres`` (never
    replacing the real genres), the title gains its "(Original Artist Cover)"
    suffix, and ``is_cover`` / ``is_cover_reason`` / ``original_cover_artist``
    are stored.

    Returns the list of successfully-updated track IDs (as strings).
    Uses deterministic retry with exponential back-off.
    """
    if not updates:
        return []

    rows = sorted(
        [dict(u) for u in updates if u.get("track_id")],
        key=lambda x: str(x["track_id"]),
    )
    if not rows:
        return []

    delay = 0.15
    for attempt in range(max_retries):
        try:
            successful: List[str] = []
            with db_session() as session:
                for update in rows:
                    tid = str(update["track_id"])
                    title = update.get("title", "") or ""
                    orig = str(update.get("original_artist", "") or "").strip()
                    reason = update.get("is_cover_reason") or (
                        f"Originally by {orig}" if orig else "Cover detection"
                    )
                    new_title = build_cover_title(title, orig)

                    # ── "Cover" genre, merged — never replacing ───────────
                    try:
                        row = session.execute(
                            text(
                                "SELECT musicbrainz_genres FROM tracks "
                                "WHERE CAST(id AS TEXT) = :id"
                            ),
                            {"id": tid},
                        ).fetchone()
                    except Exception:
                        row = None
                    mb_list = _genre_list_from_db(row[0] if row else None)
                    if mb_list is not None and "cover" not in {
                        str(g).strip().casefold() for g in mb_list
                    }:
                        mb_list.insert(0, "Cover")
                        session.execute(
                            text(
                                "UPDATE tracks SET musicbrainz_genres = :genres "
                                "WHERE CAST(id AS TEXT) = :id"
                            ),
                            {"genres": json.dumps(mb_list, ensure_ascii=False), "id": tid},
                        )

                    if new_title and new_title != title:
                        session.execute(
                            text("UPDATE tracks SET title = :title WHERE CAST(id AS TEXT) = :id"),
                            {"title": new_title, "id": tid},
                        )

                    session.execute(
                        text(
                            "UPDATE tracks SET is_cover = 1, is_cover_reason = :reason, "
                            "original_cover_artist = :orig "
                            "WHERE CAST(id AS TEXT) = :id "
                            "AND cover_manual_override IS NOT TRUE"
                        ),
                        {"reason": reason, "orig": orig, "id": tid},
                    )
                    successful.append(tid)
            return successful
        except Exception as exc:
            if attempt < max_retries - 1:
                sleep_for = min(delay * (2 ** attempt), 2.0)
                logger.warning(
                    "Cover batch transient DB error, retry %d/%d in %.2fs: %s",
                    attempt + 1, max_retries, sleep_for, exc,
                )
                time.sleep(sleep_for)
            else:
                raise
    return []


# ---------------------------------------------------------------------------
# Title helpers
# ---------------------------------------------------------------------------

#: A trailing "(X Cover)" annotation. ONE definition, shared with the
#: detector, so the suffix that is added and the suffix that is recognised
#: can never drift apart.
COVER_SUFFIX_RE = re.compile(r"\s*\([^)]+\s+cover\)\s*$", re.IGNORECASE)


def build_cover_title(title: str, original_artist: Optional[str]) -> str:
    """``title`` with its "(Original Artist Cover)" suffix — idempotent.

    A title that already carries ANY "(X Cover)" suffix is returned
    unchanged, so re-applying a verdict can never stack a second suffix.
    """
    title = str(title or "").strip()
    if not title or COVER_SUFFIX_RE.search(title):
        return title
    original_artist = str(original_artist or "").strip()
    if original_artist:
        return f"{title} ({original_artist} Cover)"
    return title


# Backwards-compatible private name (imported elsewhere).
_build_cover_title = build_cover_title
_COVER_SUFFIX_RE_INTERNAL = COVER_SUFFIX_RE


def _loose_normalize(value: str) -> str:
    """Minimal normalisation for writer-name comparison."""
    if not value:
        return ""
    return value.lower().strip()


def _loose_match(left: str, right: str) -> bool:
    """Minimal case-insensitive match for artist names."""
    return _loose_normalize(left) == _loose_normalize(right)
