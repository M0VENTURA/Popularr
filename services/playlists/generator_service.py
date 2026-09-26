"""Playlist generator — recommendations → library check → queue missing.

Pipeline (recommendations generator):

    1. Collect candidate tracks from Last.fm and/or ListenBrainz, grounded
       in the library's top artists (by track count).
    2. Match each candidate against the local ``tracks`` table (ISRC first,
       then normalised title+artist).
    3. In-library tracks are written into an ``{name}.m3u`` playlist in the
       Playlists directory.
    4. Missing tracks are pushed to the download queue via ``queue_add``
       (Soulseek/slskd) so the existing import pipeline handles the rest.

API rules honoured:
- Last.fm:     ~5 req/s ceiling — sequential artist calls are spaced by
               ``LASTFM_THROTTLE_S`` (0.3s); the HTTP client also retries
               429s with backoff.
- ListenBrainz: 1 req/s anonymous limit — the client's shared rate limiter
               already throttles every request (``_throttle``); we add no
               extra parallelism.
- slskd:       queueing goes through the same ``queue_add`` service the
               /api/queue/add route uses — policy-gated, no direct slskd
               API access.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

LASTFM_THROTTLE_S = 0.3


# ---------------------------------------------------------------------------
# Library context
# ---------------------------------------------------------------------------

def _library_top_artists(limit: int = 20) -> list[dict[str, Any]]:
    """Top artists in the library by track count (album_artist aggregate)."""
    from sqlalchemy import text
    from db.engine import db_session

    with db_session() as session:
        rows = session.execute(
            text("""
                SELECT COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                       MAX(musicbrainz_artistid) AS artist_mbid,
                       COUNT(*) AS track_count
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) <> ''
                GROUP BY 1
                ORDER BY track_count DESC
                LIMIT :limit
            """),
            {"limit": max(4, int(limit or 20))},
        ).fetchall()
        return [
            {
                "artist": str(r[0] or ""),
                "artist_mbid": str(r[1] or "") or None,
                "track_count": int(r[2] or 0),
            }
            for r in rows
            if r[0]
        ]


def _match_local_track(title: str, artist: str, isrc: str | None = None) -> dict[str, Any] | None:
    """Return the local track row for a candidate, or None.

    ISRC match first (unique per recording), then normalised title+artist
    against the album artist key (tracks are stored under the album artist).
    """
    from sqlalchemy import text
    from db.engine import db_session
    from helpers.normalization_service import normalize_title_for_lookup

    with db_session() as session:
        if isrc:
            row = session.execute(
                text("""
                    SELECT id, title, artist, album, file_path, duration
                    FROM tracks
                    WHERE isrc = :isrc AND file_path IS NOT NULL AND file_path <> ''
                    LIMIT 1
                """),
                {"isrc": str(isrc).strip()},
            ).fetchone()
            if row:
                return dict(row._mapping)

        artist_key = str(artist or "").strip().lower()
        if not artist_key:
            return None
        rows = session.execute(
            text("""
                SELECT id, title, artist, album, file_path, duration
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = :artist
                  AND file_path IS NOT NULL AND file_path <> ''
                LIMIT 250
            """),
            {"artist": artist_key},
        ).fetchall()
        target = normalize_title_for_lookup(title or "")
        if not target:
            return None
        for r in rows:
            if normalize_title_for_lookup(str(r[1] or "")) == target:
                return dict(r._mapping)
    return None


# ---------------------------------------------------------------------------
# Source collectors (API rules respected — see module docstring)
# ---------------------------------------------------------------------------

def _collect_lastfm(artists: list[dict[str, Any]], per_artist: int = 1) -> list[dict[str, Any]]:
    """Top track per library artist from Last.fm (throttled to ~3 req/s)."""
    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    api_key = str((cfg.get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "") or "")
    if not api_key:
        logger.info("[GEN] Last.fm source skipped — no api_integrations.lastfm.api_key configured")
        return []

    from api_clients.lastfm import LastFmClient
    client = LastFmClient(api_key)
    tracks: list[dict[str, Any]] = []
    for entry in artists:
        try:
            top = client.get_artist_top_tracks(entry["artist"], limit=per_artist + 2) or []
        except Exception as exc:
            logger.debug("[GEN] Last.fm top tracks failed for %s: %s", entry["artist"], exc)
            top = []
        for t in top[:per_artist]:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or t.get("title") or "").strip()
            if name:
                tracks.append({
                    "title": name,
                    "artist": entry["artist"],
                    "album": str(t.get("album") or "") if isinstance(t.get("album"), str) else "",
                    "isrc": None,
                    "source": "lastfm",
                })
        time.sleep(LASTFM_THROTTLE_S)
    return tracks


def _collect_listenbrainz(artists: list[dict[str, Any]], per_artist: int = 1) -> list[dict[str, Any]]:
    """Top recording per library artist from ListenBrainz (client throttles)."""
    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    token = str((cfg.get("api_integrations", {}).get("listenbrainz", {}) or {}).get("token", "") or "")
    if not token:
        logger.info("[GEN] ListenBrainz source skipped — no api_integrations.listenbrainz.token configured")
        return []

    from api_clients.listenbrainz import ListenBrainzClient
    client = ListenBrainzClient(token)
    tracks: list[dict[str, Any]] = []
    for entry in artists:
        if not entry.get("artist_mbid"):
            continue
        try:
            recs = client.get_top_recordings_for_artist(entry["artist_mbid"]) or []
        except Exception as exc:
            logger.debug("[GEN] ListenBrainz top recordings failed for %s: %s", entry["artist"], exc)
            recs = []
        for rec in recs[:per_artist]:
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("track_name") or rec.get("title") or "").strip()
            artist = str(rec.get("artist_name") or rec.get("artist") or entry["artist"] or "").strip()
            if name:
                tracks.append({
                    "title": name,
                    "artist": artist,
                    "album": "",
                    "isrc": None,
                    "source": "listenbrainz",
                })
    return tracks


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def interleave_by_source(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Round-robin several candidate lists into one, preserving each list's order.

    ⚠️ WHY THIS EXISTS. ``generate_recommendations`` used to append the Last.fm
    candidates and THEN the ListenBrainz ones, and truncate the combined list to
    ``limit``. Because Last.fm is collected first and contributes one track per
    library artist, the truncation was always reached inside the Last.fm block:
    with 24 library artists and ``limit=12`` the playlist held **12 Last.fm
    tracks and 0 ListenBrainz tracks**, even though the UI's default source is
    "Last.fm + ListenBrainz". At ``limit=25`` it was 24/1.

    Round-robinning BEFORE the truncation means each source contributes its best
    candidates in turn, so ``source="both"`` genuinely mixes both.

    ⭐ ``zip_longest`` (not ``zip``) is load-bearing: the lists are usually of
    DIFFERENT lengths, and plain ``zip`` would silently stop at the shortest one,
    discarding every remaining candidate from the longer list.
    """
    from itertools import zip_longest

    out: list[dict[str, Any]] = []
    for row in zip_longest(*(groups or [])):
        for item in row:
            if item is not None:
                out.append(item)
    return out


def generate_recommendations(
    source: str = "both",
    name: str = "Recommended Mix",
    limit: int = 12,
    per_artist: int = 1,
) -> dict[str, Any]:
    """Generate a recommendations playlist and queue missing tracks.

    Returns ``{playlist_name, playlist_path, added_now, queued_for_download,
    queued_ok, queued_failed, missing, matched}``.
    """
    from services.playlists.playlist_service import create_m3u_file, sanitize_playlist_name

    artists = _library_top_artists(limit=max(8, limit * 2))

    # ⚠️ Collect each source SEPARATELY and INTERLEAVE them before the limit is
    # applied. Appending one after the other made the truncation fall entirely
    # inside the first source, so `source="both"` never actually used
    # ListenBrainz (12 Last.fm / 0 ListenBrainz at limit=12).
    source_groups: list[list[dict[str, Any]]] = []
    if source in ("lastfm", "both"):
        source_groups.append(_collect_lastfm(artists, per_artist))
    if source in ("listenbrainz", "both"):
        source_groups.append(_collect_listenbrainz(artists, per_artist))
    candidates: list[dict[str, Any]] = interleave_by_source(source_groups)

    # Dedupe by normalised title + artist, then cap at the requested limit.
    from helpers.normalization_service import normalize_title_for_lookup
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for c in candidates:
        key = (
            normalize_title_for_lookup(c.get("title") or "").lower(),
            str(c.get("artist") or "").strip().lower(),
        )
        if not key[0] or key in seen:
            continue
        seen.add(key)
        unique.append(c)
    unique = unique[: max(1, min(int(limit or 12), 25))]

    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for c in unique:
        row = _match_local_track(c.get("title"), c.get("artist"), c.get("isrc"))
        if row:
            matched.append({**c, "match": row})
        else:
            missing.append(c)

    playlist_path = None
    if matched:
        playlist_path = create_m3u_file(
            sanitize_playlist_name(name) or "Recommended Mix",
            [{
                "file_path": str(m.get("match", {}).get("file_path") or ""),
                "title": str(m.get("match", {}).get("title") or m.get("title") or ""),
                "artist": str(m.get("match", {}).get("artist") or m.get("artist") or ""),
                "duration": m.get("match", {}).get("duration"),
            } for m in matched],
        )

    # Queue missing tracks through the SAME service /api/queue/add uses.
    queued_ok = 0
    queued_failed = 0
    if missing:
        try:
            from services.downloads.download_processing_service import queue_add
        except Exception as exc:
            logger.warning("[GEN] queue_add unavailable: %s", exc)
            queue_add = None
        for m in missing:
            if not queue_add:
                queued_failed += 1
                continue
            try:
                result = queue_add({
                    "artist": m.get("artist") or "",
                    "title": m.get("title") or "",
                    "album": m.get("album") or "",
                    "source": "soulseek",
                    "priority": 5,
                    "origin": "playlist_generator",
                })
                ok = bool(result) and (result.get("success") is not False)
                if ok:
                    queued_ok += 1
                    m["queued"] = True
                else:
                    queued_failed += 1
                    m["queued_error"] = str(result.get("error") or "queue_add returned failure")
            except Exception as exc:
                queued_failed += 1
                m["queued_error"] = str(exc)

    return {
        "success": True,
        "playlist_name": str(name or "Recommended Mix").strip(),
        "playlist_path": playlist_path,
        "added_now": len(matched),
        "queued_for_download": len(missing),
        "queued_ok": queued_ok,
        "queued_failed": queued_failed,
        "matched": [
            {"title": m["title"], "artist": m["artist"], "source": m.get("source", "")}
            for m in matched
        ],
        "missing": [
            {"title": m.get("title"), "artist": m.get("artist"), "source": m.get("source", ""),
             "queued": bool(m.get("queued")), "error": m.get("queued_error")}
            for m in missing
        ],
    }
