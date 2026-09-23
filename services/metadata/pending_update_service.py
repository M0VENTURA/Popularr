"""Persisted MusicBrainz recommendations awaiting the user's decision.

When a scan would NOT update metadata (``metadata_update.apply_during_scan`` is
off on the Config page) the recommendations it *would* have written are stashed
on the track rows (``tracks.pending_mb_updates``) instead of being applied.
Browsing the album — or the artist — then offers them with save/discard.

The column is not new: ``pending_mb_updates`` (TEXT) has existed since the
initial schema and ``routes/artist_routes.py::api_missing_overview`` already
reads it to list albums with gap updates.  Its documented purpose — *"persistent
MusicBrainz update banners"* — is exactly this.  It simply had no writer; this
module is that writer.

Stored shape (one JSON object per track row that has something to recommend)::

    {
      "version": 1,
      "release_mbid": "…",
      "release_group_mbid": "…",
      "release_title": "…",
      "stashed_at": "2026-09-23T12:00:00",
      "album_changes": [{"field", "label", "current", "proposed"}, …],
      "changes":       [{"field", "label", "current", "proposed"}, …]
    }

``changes`` are that track's own proposals; ``album_changes`` are the album-wide
ones, repeated on every row so a reader never has to decide which row is
"first".  They are small strings and the read path de-duplicates.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

#: Current envelope version. Bump only for an incompatible shape change — a
#: reader that meets an unknown version skips the row rather than guessing.
_ENVELOPE_VERSION = 1

_ALBUM_TRACK_IDS_SQL = """
    SELECT CAST(id AS TEXT) AS id
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
"""


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def stash_album_recommendations(
    artist: str,
    album: str,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Persist a proposal so an album/artist page can offer it later.

    ``proposal`` is the return value of
    :func:`services.metadata.metadata_proposal_service.propose_album_metadata`.

    Clears any previous stashed recommendations for the album FIRST, so a track
    that is now up to date stops showing a stale banner — the same lifecycle the
    legacy comparison persistence used.
    """
    artist = _as_text(artist)
    album = _as_text(album)

    if not artist or not album:
        return {"stashed": 0, "cleared": 0, "reason": "artist and album are required"}
    if not isinstance(proposal, dict) or not proposal.get("success"):
        return {"stashed": 0, "cleared": 0, "reason": "proposal was not successful"}

    album_changes = proposal.get("album_changes") or []
    track_changes = {
        _as_text(entry.get("track_id")): (entry.get("changes") or [])
        for entry in (proposal.get("track_changes") or [])
        if _as_text(entry.get("track_id"))
    }

    if not album_changes and not track_changes:
        return {
            "stashed": 0,
            "cleared": _clear_album(artist, album),
            "reason": "nothing to recommend",
        }

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    common = {
        "version": _ENVELOPE_VERSION,
        "release_mbid": _as_text(proposal.get("release_mbid")),
        "release_group_mbid": _as_text(proposal.get("release_group_mbid")),
        "release_title": _as_text(proposal.get("release_title")),
        "stashed_at": now,
        "album_changes": album_changes,
    }

    stashed = 0
    cleared = _clear_album(artist, album)

    with db_session() as session:
        # Every track of the album carries the albums-level block; a track with
        # its own changes carries those too. We must know the album's track ids
        # to write the album-level block onto rows that have no per-track change.
        rows = session.execute(
            text(_ALBUM_TRACK_IDS_SQL), {"artist": artist, "album": album}
        ).fetchall()
        track_ids = [_as_text(getattr(r, "id", None) or r[0]) for r in rows or []]

        for track_id in track_ids:
            own_changes = track_changes.get(track_id) or []
            # Only rows with something to say are written — an album with only
            # album-level changes still needs one row to hang them on, so the
            # first track carries them and the rest are left clean.
            is_first = track_id == track_ids[0]
            if not own_changes and not (album_changes and is_first):
                continue

            envelope = dict(common)
            envelope["changes"] = own_changes if own_changes else []
            if not is_first:
                # Avoid repeating the album block on rows that do not need it.
                envelope.pop("album_changes", None)

            try:
                session.execute(
                    text("""
                        UPDATE tracks
                        SET pending_mb_updates = :payload
                        WHERE CAST(id AS TEXT) = :track_id
                    """),
                    {"payload": json.dumps(envelope), "track_id": track_id},
                )
                stashed += 1
            except Exception as exc:
                logger.warning(
                    "Could not stash pending recommendations",
                    track_id=track_id, artist=artist, album=album, error=str(exc),
                )
        session.commit()

    logger.info(
        "Stashed MusicBrainz recommendations for review",
        artist=artist, album=album, stashed=stashed, cleared=cleared,
    )
    return {"stashed": stashed, "cleared": cleared, "reason": ""}


def _clear_album(artist: str, album: str) -> int:
    """Drop every stashed recommendation for the album."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET pending_mb_updates = NULL
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                      AND pending_mb_updates IS NOT NULL
                """),
                {"artist": artist, "album": album},
            )
            session.commit()
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.warning(
            "Could not clear pending recommendations",
            artist=artist, album=album, error=str(exc),
        )
        return 0


def discard_album_recommendations(artist: str, album: str) -> dict[str, Any]:
    """Public discard — the album page's "Discard all" action."""
    cleared = _clear_album(_as_text(artist), _as_text(album))
    return {"success": True, "cleared": cleared}


def fetch_album_recommendations(artist: str, album: str) -> dict[str, Any]:
    """Read the album's stashed recommendations for rendering.

    Returns the album-level changes once, plus a per-track map, with the
    user's permanently-ignored fields (``mb_ignored_fields``) filtered out.
    """
    artist = _as_text(artist)
    album = _as_text(album)

    with db_session() as session:
        rows = session.execute(
            text("""
                SELECT CAST(id AS TEXT) AS id, title, pending_mb_updates,
                       mb_ignored_fields
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND LOWER(COALESCE(album, '')) = LOWER(:album)
                  AND pending_mb_updates IS NOT NULL
                  AND pending_mb_updates != ''
            """),
            {"artist": artist, "album": album},
        ).fetchall()

    album_changes: list[dict[str, Any]] = []
    track_changes: list[dict[str, Any]] = []
    release_mbid = ""
    release_title = ""

    for row in rows or []:
        mapping = dict(row._mapping)
        raw = mapping.get("pending_mb_updates")
        if not raw:
            continue
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(envelope, dict):
            continue
        if envelope.get("version") not in (None, _ENVELOPE_VERSION):
            # Unknown envelope: skip rather than mis-render.
            continue

        if not release_mbid:
            release_mbid = _as_text(envelope.get("release_mbid"))
            release_title = _as_text(envelope.get("release_title"))

        if not album_changes:
            proposed = envelope.get("album_changes") or []
            if proposed:
                album_changes = _filter_ignored(proposed, mapping.get("mb_ignored_fields"))

        changes = _filter_ignored(
            envelope.get("changes") or [], mapping.get("mb_ignored_fields")
        )
        if changes:
            track_changes.append({
                "track_id": _as_text(mapping.get("id")),
                "title": _as_text(mapping.get("title")),
                "changes": changes,
            })

    return {
        "success": True,
        "album_changes": album_changes,
        "track_changes": track_changes,
        "release_mbid": release_mbid,
        "release_title": release_title,
        "counts": {
            "album_changes": len(album_changes),
            "tracks_changed": len(track_changes),
            "track_changes": sum(len(t["changes"]) for t in track_changes),
        },
        "has_any": bool(album_changes or track_changes),
    }


def _filter_ignored(
    changes: list[Any],
    ignored_raw: Any,
) -> list[dict[str, Any]]:
    """Drop proposals whose field the user permanently ignored."""
    ignored: set[str] = set()
    if ignored_raw:
        try:
            parsed = json.loads(ignored_raw) if isinstance(ignored_raw, str) else ignored_raw
            if isinstance(parsed, list):
                ignored = {str(item) for item in parsed}
        except (TypeError, ValueError):
            ignored = set()

    kept: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        field = _as_text(change.get("field"))
        if not field or field in ignored:
            continue
        kept.append(change)
    return kept


def fetch_artist_recommendations(artist: str) -> dict[str, Any]:
    """Aggregate an artist's stashed recommendations by album.

    Backs the artist page's per-album "N pending updates" summary.
    """
    artist = _as_text(artist)
    with db_session() as session:
        rows = session.execute(
            text("""
                SELECT COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                       album,
                       COUNT(*) AS track_count
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND pending_mb_updates IS NOT NULL
                  AND pending_mb_updates != ''
                GROUP BY COALESCE(NULLIF(album_artist, ''), artist), album
            """),
            {"artist": artist},
        ).fetchall()

    albums = [
        {
            "album": _as_text(dict(r._mapping).get("album")),
            "track_count": int(dict(r._mapping).get("track_count") or 0),
        }
        for r in rows or []
    ]
    return {
        "success": True,
        "artist": artist,
        "albums": albums,
        "album_count": len(albums),
        "total": sum(a["track_count"] for a in albums),
    }
