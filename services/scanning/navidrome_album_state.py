"""Per-album Navidrome import state for pre-fetch fast skips.

Flow supported:

    album summary
      -> known album?
           -> no: full import
           -> yes: summary + scan marker unchanged since last successful import?
                -> yes: FAST SKIP
                -> no: fetch tracklist, compare IDs + metadata fingerprint
                     -> unchanged: SKIP
                     -> changed: reconcile/import
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import structlog

from db.engine import db_session
from sqlalchemy import bindparam, text

logger = structlog.get_logger(__name__)

#: Fields of the Navidrome album summary that can change without a tracklist
#: fetch. `created` is Navidrome's album timestamp; the global scan marker is
#: what makes the fast skip safe when no new scan has run.
_SUMMARY_FIELDS = (
    "id", "name", "artist", "artistId", "songCount", "duration", "created",
    "version", "musicBrainzId", "year", "genre", "genres", "releaseDate",
    "originalReleaseDate", "releaseTypes", "recordLabels", "isCompilation",
    "discTitles",
)


def album_summary_hash(album: dict[str, Any]) -> str:
    payload = {field: album.get(field) for field in _SUMMARY_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def track_ids_hash(track_ids: Iterable[Any]) -> str:
    values = sorted({str(value) for value in track_ids if str(value or "").strip()})
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def can_fast_skip_album(
    state: dict[str, Any] | None,
    album: dict[str, Any],
    *,
    cached_ids: set[str],
    scan_marker: str | None,
    album_dirty: bool,
    needs_reimport: bool,
) -> bool:
    """True only when the summary + scan marker prove no Navidrome change."""
    if not state or not scan_marker or not cached_ids:
        return False
    if album_dirty or needs_reimport:
        return False
    if str(state.get("last_scan_marker") or "") != str(scan_marker):
        return False
    if str(state.get("summary_hash") or "") != album_summary_hash(album):
        return False
    try:
        if int(state.get("song_count") or 0) != len(cached_ids):
            return False
    except (TypeError, ValueError):
        return False
    return True


def load_album_states(album_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [str(value) for value in album_ids if str(value or "").strip()]
    if not ids:
        return {}
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT album_id, album_key, album_name, album_artist, "
                    "song_count, summary_hash, track_ids_hash, "
                    "metadata_fingerprint, last_scan_marker, last_imported_at "
                    "FROM navidrome_album_state "
                    "WHERE album_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            ).mappings().all()
        return {str(row["album_id"]): dict(row) for row in rows}
    except Exception as exc:
        logger.debug("Album state load failed", count=len(ids), error=str(exc))
        return {}


def save_album_state(
    *,
    album: dict[str, Any],
    album_key: str,
    track_ids: Iterable[Any],
    metadata_fingerprint: str,
    scan_marker: str | None,
) -> None:
    album_id = str(album.get("id") or "").strip()
    if not album_id:
        return
    ids = [str(value) for value in track_ids if str(value or "").strip()]
    try:
        with db_session() as session:
            session.execute(
                text(
                    "INSERT INTO navidrome_album_state "
                    "(album_id, album_key, album_name, album_artist, song_count, "
                    "summary_hash, track_ids_hash, metadata_fingerprint, "
                    "last_scan_marker, last_imported_at) "
                    "VALUES (:album_id, :album_key, :album_name, :album_artist, "
                    ":song_count, :summary_hash, :track_ids_hash, "
                    ":metadata_fingerprint, :last_scan_marker, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (album_id) DO UPDATE SET "
                    "album_key=EXCLUDED.album_key, "
                    "album_name=EXCLUDED.album_name, "
                    "album_artist=EXCLUDED.album_artist, "
                    "song_count=EXCLUDED.song_count, "
                    "summary_hash=EXCLUDED.summary_hash, "
                    "track_ids_hash=EXCLUDED.track_ids_hash, "
                    "metadata_fingerprint=EXCLUDED.metadata_fingerprint, "
                    "last_scan_marker=COALESCE("
                    "EXCLUDED.last_scan_marker, "
                    "navidrome_album_state.last_scan_marker), "
                    "last_imported_at=CURRENT_TIMESTAMP"
                ),
                {
                    "album_id": album_id,
                    "album_key": album_key,
                    "album_name": str(album.get("name") or ""),
                    "album_artist": str(album.get("artist") or ""),
                    "song_count": int(album.get("songCount") or len(ids)),
                    "summary_hash": album_summary_hash(album),
                    "track_ids_hash": track_ids_hash(ids),
                    "metadata_fingerprint": str(metadata_fingerprint or ""),
                    "last_scan_marker": scan_marker,
                },
            )
    except Exception as exc:
        logger.debug("Album state save failed", album_id=album_id, error=str(exc))
