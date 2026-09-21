"""Navidrome playlist API wrapper.

Thin wrapper around ``NavidromeClient`` playlist operations for
interacting with the Navidrome Subsonic API.

Key Functions:
    - list_playlists(): Fetch all playlists from Navidrome.
    - load_playlist(): Get detailed playlist contents by ID.
    - create_navidrome_playlist(): Create a new playlist via the
      Subsonic ``createPlaylist`` endpoint.
    - sync_playlist_by_name(): Create OR update (in place) a playlist,
      deleting same-name duplicates so scans never recreate entries.

Used by:
    - Playlist WebUI routes for browsing and managing playlists.
    - Playlist sync workflows (Essential Collections, Genre Top Tracks,
      New Music — every generated playlist that must not duplicate).

Architecture:
    Delegates HTTP calls to ``api_clients.navidrome.NavidromeClient``.
    No business logic — pure API wrapper.

Change-detection contract:
    ``sync_playlist_by_name`` compares the playlist's CURRENT ordered song
    list against the requested list and returns ``unchanged=True`` without
    issuing a write when they already match.

    NOTE ON SHAPE: ``NavidromeClient.fetch_playlist`` pops the Subsonic
    ``entry`` key and re-exposes it as ``tracks``. Reading ``entry`` from
    that return value always misses, which made change detection report
    "current song list could not be read" for every playlist.
"""

from __future__ import annotations

from typing import Any

import structlog

from api_clients.navidrome import NavidromeClient

logger = structlog.get_logger(__name__)


def list_playlists(base_url, user, password):
    client = NavidromeClient(base_url, user, password)
    return client.fetch_all_playlists()


def load_playlist(base_url, user, password, playlist_id):
    client = NavidromeClient(base_url, user, password)
    return client.fetch_playlist(playlist_id)


def create_navidrome_playlist(base_url, user, password, name, tracks):
    """Create a playlist via Navidrome Subsonic API (form-encoded POST).

    Uses the client's ``create_playlist`` (POST body), NOT
    ``_get_subsonic_response`` with ``params=`` — that GET path passed the
    ``songId`` list through httpx's query serialiser and produced the
    ``sequence item 1: expected a bytes-like object, tuple found`` TypeError
    for large playlists.  The POST body path has no URL-length limit and
    flattens the list into repeated ``songId=`` form fields.
    """
    client = NavidromeClient(base_url, user, password)
    return client.create_playlist(name, tracks)


def _is_smart(playlist: dict[str, Any]) -> bool:
    """True when the playlist is a smart playlist (from .nsp files)."""
    try:
        return NavidromeClient._is_smart_playlist(playlist)
    except Exception:
        return bool(playlist.get("smart") or playlist.get("isSmart") or playlist.get("criteria"))


def _entry_song_id(entry: Any) -> str:
    """Extract a song ID from a Subsonic playlist entry."""
    if isinstance(entry, dict):
        for key in ("id", "songId", "childId"):
            value = str(entry.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(entry or "").strip()


def _extract_song_ids(payload: Any) -> list[str] | None:
    """Pull ordered song IDs out of a playlist payload.

    Returns ``None`` when the contents genuinely cannot be determined.
    Distinguishing "unknown" from "empty" matters: an unknown list must never
    be treated as an empty playlist, or a populated playlist would be wrongly
    reported as changed (or worse, an empty one as up to date).

    Handles the shape returned by ``NavidromeClient.fetch_playlist`` (which
    exposes entries under ``tracks``) as well as the raw Subsonic shapes.
    """
    if not isinstance(payload, dict):
        return None

    # Unwrap the envelope if a raw Subsonic response is handed in.
    container = payload
    if isinstance(container.get("subsonic-response"), dict):
        container = container["subsonic-response"]
    if isinstance(container.get("playlist"), dict):
        container = container["playlist"]

    entries = None
    for key in ("tracks", "entry", "entries", "song"):
        value = container.get(key)
        if value is not None:
            entries = value
            break

    if entries is None:
        # A playlist with songCount 0 legitimately carries no entries key.
        song_count = container.get("songCount")
        if song_count is not None:
            try:
                if int(song_count) == 0:
                    return []
            except (TypeError, ValueError):
                pass
        return None

    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        return None

    ids = [_entry_song_id(entry) for entry in entries]
    return [value for value in ids if value]


def _current_song_ids(client: NavidromeClient, playlist_id: str) -> list[str] | None:
    """Return the playlist's current ordered song IDs, or None if unknown."""
    if not playlist_id:
        return None
    try:
        data = client.fetch_playlist(playlist_id)
    except Exception as exc:
        logger.debug(
            "[PLAYLISTS] playlist fetch failed during change detection",
            playlist_id=playlist_id,
            error=str(exc),
        )
        return None

    ids = _extract_song_ids(data)
    if ids is None:
        logger.warning(
            "[PLAYLISTS] could not read current playlist contents",
            playlist_id=playlist_id,
            payload_type=type(data).__name__,
            keys=sorted(data.keys())[:15] if isinstance(data, dict) else None,
        )
    return ids


def sync_playlist_by_name(
    client: NavidromeClient,
    name: str,
    song_ids: list[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create or update (IN PLACE) a Navidrome playlist by name.

    The generated playlists (Essential Collection, ``{Genre} - Top Tracks``,
    New Music) must not accumulate duplicates across scans. This syncs the
    Navidrome playlist to match the requested song list WITHOUT recreating it:

    1. Find every playlist with the same name.
    2. If more than one exists → delete the extras (dedupe).
    3. If one exists → compare its current ordered song list with the
       requested one. If they match, do nothing and report ``unchanged``.
       Otherwise ``updatePlaylist`` replaces the song list in place.
    4. If none exists → create it with the song list.

    Set ``force=True`` to write even when the contents already match.

    Returns ``{"success": bool, "updated": bool, "created": bool,
    "unchanged": bool, "deduped": int, "playlist_id": str,
    "song_count": int}``. Never raises.
    """
    result: dict[str, Any] = {
        "success": False,
        "updated": False,
        "created": False,
        "unchanged": False,
        "deduped": 0,
        "playlist_id": "",
        "song_count": 0,
    }
    if not client or not name:
        return result

    # De-duplicate the requested list while preserving order. Sending the same
    # song twice would otherwise make the stored list permanently differ from
    # the requested one, so the playlist could never settle as unchanged.
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in song_ids or []:
        text_value = str(value or "").strip()
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        cleaned.append(text_value)
    song_ids = cleaned
    result["song_count"] = len(song_ids)

    try:
        all_playlists = client.fetch_all_playlists() or []
        wanted = str(name or "").strip().lower()
        matches = [
            p for p in all_playlists
            if str(p.get("name") or "").strip().lower() == wanted
        ]
        # Smart playlists (from .nsp files) carry the same name — leave them
        # alone; only sync the REGULAR (file-imported) playlists.
        matches = [p for p in matches if not _is_smart(p)]
    except Exception as exc:
        logger.warning(
            "[PLAYLISTS] playlist lookup failed",
            name=name,
            error=str(exc),
        )
        return result

    # Dedupe: keep the first regular playlist, delete the rest.
    primary = matches[0] if matches else None
    primary_id = str((primary or {}).get("id") or "")
    for dup in matches[1:]:
        dup_id = str(dup.get("id") or "")
        if dup_id and dup_id != primary_id:
            try:
                if client.delete_playlist(dup_id):
                    result["deduped"] += 1
                    logger.info(
                        "[PLAYLISTS] Deleted duplicate Navidrome playlist",
                        name=name,
                        playlist_id=dup_id,
                    )
            except Exception as exc:
                logger.warning(
                    "[PLAYLISTS] duplicate deletion failed",
                    name=name,
                    playlist_id=dup_id,
                    error=str(exc),
                )

    if primary:
        result["playlist_id"] = primary_id

        existing = _current_song_ids(client, primary_id)

        # ── Change detection ────────────────────────────────────────────
        if not force and existing is not None and existing == song_ids:
            result["unchanged"] = True
            result["success"] = True
            logger.info(
                "[PLAYLISTS] Navidrome playlist already up to date",
                name=name,
                playlist_id=primary_id,
                songs=len(song_ids),
            )
            return result

        if existing is None:
            # The replace is implemented as "remove every current index, then
            # add the new ids". Without a reliable current count the removal
            # is skipped and the additions are APPENDED, silently doubling the
            # playlist. Refuse the write rather than corrupt it.
            logger.warning(
                "[PLAYLISTS] skipping playlist write",
                name=name,
                playlist_id=primary_id,
                reason=(
                    "current contents unreadable; writing would append instead "
                    "of replace and duplicate the playlist"
                ),
            )
            return result

        try:
            ok = client.update_playlist_songs(
                primary_id,
                song_ids,
                current_count=len(existing),
            )
            result["updated"] = bool(ok)
            result["success"] = bool(ok)
            if ok:
                # One line per genre playlist per scan meant a finalise pass
                # emitted ~45 of these. The section report's PLAYLIST
                # PROCESSING block carries the count; the per-playlist detail
                # is debug-only.
                logger.debug(
                    "[PLAYLISTS] Updated Navidrome playlist in place",
                    name=name,
                    playlist_id=primary_id,
                    songs=len(song_ids),
                    replaced=len(existing),
                )
            else:
                logger.warning(
                    "[PLAYLISTS] updatePlaylist returned failure",
                    name=name,
                    playlist_id=primary_id,
                    songs=len(song_ids),
                )
        except Exception as exc:
            logger.warning(
                "[PLAYLISTS] updatePlaylist raised",
                name=name,
                playlist_id=primary_id,
                error=str(exc),
            )
        return result

    # No existing regular playlist — create it.  Uses the client's
    # form-encoded POST (the song list can exceed the URL query limit for
    # 1000+ song playlists; the query-param path also mis-serialised the
    # ``songId`` list through ``_get_subsonic_response``).
    try:
        data = client.create_playlist(name, song_ids) or {}
        pid = str((data.get("playlist") or {}).get("id") or "")
        created = data.get("status") == "ok"
        result["created"] = bool(created)
        result["success"] = bool(created)
        result["playlist_id"] = pid
        if created:
            logger.info(
                "[PLAYLISTS] Created Navidrome playlist",
                name=name,
                playlist_id=pid,
                songs=len(song_ids),
            )
        else:
            logger.warning(
                "[PLAYLISTS] createPlaylist failed",
                name=name,
                response=data,
            )
    except Exception as exc:
        logger.warning(
            "[PLAYLISTS] createPlaylist raised",
            name=name,
            error=str(exc),
        )
    return result
