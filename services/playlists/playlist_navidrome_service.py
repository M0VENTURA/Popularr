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
from services.playlists.song_id_reconciliation import (
    get_rejected_song_ids,
    record_rejected_song_ids,
    server_key,
)

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
    existing_playlist_id: str | None = None,
    all_playlists: list[dict[str, Any]] | None = None,
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
        "skipped": False,
        "partial": False,
        "reconciliation_complete": False,
        "needs_navidrome_import": False,
        "deduped": 0,
        "playlist_id": "",
        "song_count": 0,
        "requested_song_count": len(song_ids or []),
    }
    if not client or not name:
        return result

    # ── Deferred while a Navidrome import runs ────────────────────────────
    #
    # ⚠️ THIS MUST SKIP, NOT PROCEED. A deferred ``getPlaylists`` returns an
    # EMPTY list, and an empty list is indistinguishable from "this playlist
    # does not exist yet" — so the normal path would CREATE a second copy of a
    # playlist that is sitting right there on the server. The dedupe pass would
    # then delete one of them on the next run, and the two could ping-pong
    # exactly as the multi-account bug did.
    #
    # "skipped" (rather than an error) tells the caller the truth: nothing was
    # attempted, and the playlist keeps its current contents.
    if not force:
        try:
            from services.navidrome.import_gate import is_import_running

            if is_import_running():
                result["success"] = True
                result["skipped"] = True
                logger.debug(
                    "[PLAYLISTS] playlist sync deferred — Navidrome import running",
                    name=name,
                )
                return result
        except Exception:
            pass  # fail open: never block a sync on a broken gate

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

    # ── Known rejected IDs ────────────────────────────────────────────────
    # Filter IDs this exact server/playlist already rejected. A forced sync
    # retries them (an explicit repair), otherwise the playlist is written
    # with the RESOLVED subset and reported as partial. The rejection is
    # cleared by a verified import / ID adoption, so retry remains possible.
    _server = server_key(client)
    _rejected = {} if force else get_rejected_song_ids(_server, name)
    if _rejected:
        _unresolved = [value for value in song_ids if value in _rejected]
        if _unresolved:
            song_ids = [value for value in song_ids if value not in _rejected]
            result["unresolved_ids"] = _unresolved
            result["partial"] = True
            result["needs_navidrome_import"] = True
            result["song_count"] = len(song_ids)
            logger.debug(
                "[PLAYLISTS] unresolved track mapping retained",
                playlist=name,
                count=len(_unresolved),
                ids=_unresolved[:8],
            )
            if not song_ids:
                # Never clear/replace a populated playlist with an empty list
                # merely because every requested ID is currently unresolved.
                result["skipped"] = True
                return result

    matches: list[dict[str, Any]] = []
    if existing_playlist_id and all_playlists is None:
        matches = [{"id": str(existing_playlist_id).strip(), "name": name}]
    else:
        try:
            playlist_list = (
                all_playlists
                if all_playlists is not None
                else (client.fetch_all_playlists() or [])
            )
            wanted = str(name or "").strip().lower()
            matches = [
                p for p in playlist_list
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
            result["success"] = not result["partial"]
            result["reconciliation_complete"] = not result["partial"]
            logger.info(
                "[PLAYLISTS] Navidrome playlist already up to date",
                name=name,
                playlist_id=primary_id,
                songs=len(song_ids),
                unresolved=len(result.get("unresolved_ids") or []),
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
                # The match above already read these off the live playlist —
                # they are what lets a code 50 say WHICH cause it was.
                owner=str(primary.get("owner") or ""),
                playlist_name=name,
            )
            result["updated"] = bool(ok)
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
                # ── Verify what Navidrome actually STORED ────────────────
                # A write can be ACCEPTED yet keep fewer tracks than asked:
                # after Navidrome's 0.64 "canonical 128-bit IDs" migration,
                # song IDs cached from an older server no longer resolve, so
                # the log would claim success while the playlist shows stale
                # or missing tracks. One extra getPlaylist per CHANGED
                # playlist converts that silent drift into a warning.
                stored = _current_song_ids(client, primary_id)
                if stored is None:
                    result["partial"] = True
                    result["reconciliation_complete"] = False
                    result["needs_navidrome_import"] = True
                    logger.warning(
                        "[PLAYLISTS] could not verify playlist contents after update",
                        name=name,
                        playlist_id=primary_id,
                    )
                elif stored != song_ids:
                    # WHICH ids were dropped. A count alone cannot separate the
                    # two very different causes: a STALE id (a Navidrome import
                    # refreshes it) from a song Navidrome has not INDEXED yet
                    # (only a Navidrome scan can — the import is powerless,
                    # which is why "re-run the import" can be followed and the
                    # warning still return). The ids are also what the caller
                    # needs to name the affected tracks.
                    _requested = set(song_ids)
                    _stored = set(stored)
                    dropped = [i for i in song_ids if i not in _stored]
                    extra = [i for i in stored if i not in _requested]
                    result["dropped_ids"] = dropped
                    result["extra_ids"] = extra
                    result["stored_song_ids"] = stored
                    result["partial"] = True
                    result["reconciliation_complete"] = False
                    result["needs_navidrome_import"] = True
                    _new_rejections = record_rejected_song_ids(
                        _server, name, dropped,
                    )
                    result["new_rejections"] = _new_rejections
                    if _new_rejections:
                        logger.warning(
                            "[PLAYLISTS] Navidrome stored a different number of tracks "
                            "than requested — stale song IDs are the usual cause "
                            "(Navidrome re-encoded all IDs in 0.64); re-run the "
                            "Navidrome import scan to refresh them. If they survive "
                            "an import, Navidrome has not indexed those files yet — "
                            "rescan Navidrome first",
                            name=name,
                            playlist_id=primary_id,
                            requested=len(song_ids),
                            stored=len(stored),
                            dropped=len(dropped),
                            dropped_ids=dropped[:8],
                            extra=len(extra),
                            extra_ids=extra[:8],
                        )
                    else:
                        logger.debug(
                            "[PLAYLISTS] known rejected song IDs still unresolved",
                            name=name,
                            playlist_id=primary_id,
                            dropped_ids=dropped[:8],
                        )
                else:
                    result["stored_song_ids"] = stored
                    result["reconciliation_complete"] = not result["partial"]
                    result["success"] = not result["partial"]
            else:
                result["reconciliation_complete"] = False
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
        _playlist = data.get("playlist") or {}
        pid = str(_playlist.get("id") or "")
        created = data.get("status") == "ok"
        result["created"] = bool(created)
        result["playlist_id"] = pid
        if created:
            logger.info(
                "[PLAYLISTS] Created Navidrome playlist",
                name=name,
                playlist_id=pid,
                songs=len(song_ids),
            )
            # ── Verify creation, exactly like update ────────────────────
            # ``stored_count == 0`` is a valid value, so never use truthiness.
            stored_ids = _current_song_ids(client, pid) if pid else None
            _stored_count_raw = _playlist.get("songCount")
            if stored_ids is not None and _stored_count_raw is not None:
                try:
                    if int(_stored_count_raw) != len(stored_ids):
                        # The create response's own count is authoritative
                        # when the follow-up fetch cannot see the new playlist.
                        stored_ids = None
                except (TypeError, ValueError):
                    pass
            if stored_ids is not None:
                result["stored_song_ids"] = stored_ids
                if stored_ids != song_ids:
                    _stored = set(stored_ids)
                    dropped = [i for i in song_ids if i not in _stored]
                    result["dropped_ids"] = dropped
                    result["partial"] = True
                    result["needs_navidrome_import"] = True
                    result["new_rejections"] = record_rejected_song_ids(
                        _server, name, dropped,
                    )
                    if result["new_rejections"]:
                        logger.warning(
                            "[PLAYLISTS] Navidrome stored a different number of tracks "
                            "than requested on create — stale song IDs are the usual cause",
                            name=name,
                            playlist_id=pid,
                            requested=len(song_ids),
                            stored=len(stored_ids),
                            dropped_ids=dropped[:8],
                        )
            elif _stored_count_raw is not None:
                try:
                    _stored_count = int(_stored_count_raw)
                except (TypeError, ValueError):
                    _stored_count = -1
                if _stored_count != len(song_ids):
                    result["partial"] = True
                    result["needs_navidrome_import"] = True
                    logger.warning(
                        "[PLAYLISTS] Navidrome stored a different number of tracks "
                        "than requested on create",
                        name=name,
                        playlist_id=pid,
                        requested=len(song_ids),
                        stored=_stored_count,
                    )
            else:
                # Created but the result cannot be verified: never call that
                # fully synchronised.
                result["partial"] = True
                result["needs_navidrome_import"] = True
            result["reconciliation_complete"] = not result["partial"]
            result["success"] = bool(created and not result["partial"])
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
