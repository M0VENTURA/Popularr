"""
Targeted Navidrome import pipelines.

These functions are used by UI routes that trigger Navidrome-only scans for a
single artist or album.

Routes should call these functions asynchronously and avoid direct DB lookups
or direct scan_artist_to_db calls.
"""

from __future__ import annotations

from typing import Any

from db.repositories.scan_repository import lookup_artist_id
from helpers.logging_config import log_unified
from services.scanning.navidrome_import import scan_artist_to_db


def _resolve_artist_id(artist: str) -> str | None:
    """Resolve a Navidrome artist id, falling back to the live Navidrome index.

    ``artist_stats`` may be empty on a fresh database — without a fallback the
    targeted pipeline would skip the import and leave the artist unscanned.
    """
    from db.repositories.scan_repository import lookup_artist_id

    artist_id = lookup_artist_id(artist)
    if artist_id:
        return artist_id

    try:
        from helpers.text_utils import _normalize_artist_key as _norm_key
        from services.scanning.navidrome_scan_service import build_artist_index
        index = build_artist_index() or {}
        target_key = _norm_key(artist)
        for _name, _info in index.items():
            if _info.get("id") and _norm_key(_name) == target_key:
                return str(_info.get("id"))
    except Exception as exc:
        log_unified(f"Could not resolve Navidrome artist id for '{artist}' from index: {exc}")
    return None


def run_navidrome_artist_pipeline(
    artist: str,
    force: bool = False,
) -> dict[str, Any]:
    """
    Run Navidrome import for a single artist.
    """
    artist_id = _resolve_artist_id(artist)

    if not artist_id:
        log_unified(f"Artist not found for Navidrome import: {artist}")
        return {
            "success": False,
            "skipped": True,
            "reason": "artist not found",
            "artist": artist,
        }

    result = scan_artist_to_db(
        artist,
        artist_id,
        verbose=True,
        force=force,
        # Differential mode: unchanged albums are skipped instead of being
        # fetched and upserted again. The single-artist "Navidrome" scan used to
        # process every album on every run.
        diff_mode=True,
    )
    if isinstance(result, dict) and (
        result.get("deferred") or result.get("success") is False
    ):
        return {
            "success": False,
            "deferred": bool(result.get("deferred")),
            "reason": result.get("reason") or "remote scan not verified",
            "artist": artist,
        }

    log_unified(f"Navidrome import completed for artist: {artist}")

    return {
        "success": True,
        "artist": artist,
    }


def run_navidrome_album_pipeline(
    artist: str,
    album: str,
    force: bool = False,
) -> dict[str, Any]:
    """
    Run Navidrome import for a specific album.
    """
    artist_id = _resolve_artist_id(artist)

    if not artist_id:
        log_unified(f"Artist not found for Navidrome album import: {artist}")
        return {
            "success": False,
            "skipped": True,
            "reason": "artist not found",
            "artist": artist,
            "album": album,
        }

    result = scan_artist_to_db(
        artist,
        artist_id,
        verbose=True,
        force=force,
        album_filter=album,
    )
    if isinstance(result, dict) and (
        result.get("deferred") or result.get("success") is False
    ):
        return {
            "success": False,
            "deferred": bool(result.get("deferred")),
            "reason": result.get("reason") or "remote scan not verified",
            "artist": artist,
            "album": album,
        }

    log_unified(f"Navidrome import completed for album: {artist} - {album}")

    return {
        "success": True,
        "artist": artist,
        "album": album,
    }