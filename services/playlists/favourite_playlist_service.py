"""Per-user favourite playlists built from Navidrome stars.

What it does
------------
For EACH configured Navidrome user:

1. Their starred **albums** and **artists** are read from ``user_favourites``
   (``get_favourite_navidrome_ids``), which ``sync_favourites_from_navidrome``
   fills from Navidrome's ``getStarred2``.
2. Each starred entity is expanded through the Navidrome API into **song ids**:
   an album via ``getAlbum``, an artist via ``getArtist`` → its albums →
   ``getAlbum`` each.
3. ``tracks.id`` IS the Navidrome song id, so the song ids address local rows
   directly. Tracks below ``favourite_min_stars`` are DROPPED.
4. The playlist is written to **that user's OWN** Navidrome account
   (``sync_playlist_by_name`` takes the client, and the client carries the
   username). Navidrome refuses a write to a playlist the calling user does not
   own (Subsonic code 50), so per-user clients are what make this work.

⚠️ WHY THE API AND NOT A LOCAL JOIN: there is no local album→Navidrome-id or
artist→Navidrome-id mapping anywhere (no ``albums`` table; ``artists`` holds only
``id``/``name``). ``tracks.id`` is the ONLY stable local↔Navidrome key, so the
album/artist side MUST be expanded server-side.

Layout: one pair per user — ``Favourite Artists`` and ``Favourite Albums``
(names from config). Navidrome playlists are per-account, so the names need no
user prefix.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import bindparam, text

from db.engine import db_session
from db.repositories.favourites_repository import get_favourite_navidrome_ids

logger = logging.getLogger(__name__)


def _config() -> dict[str, Any]:
    from helpers.config_helpers import get_favourite_playlists_config

    return get_favourite_playlists_config()


def _dedupe(values: Iterable[str]) -> list[str]:
    """Ordered de-duplication — a repeated id would stop a playlist settling."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or ():
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _song_ids_from_album(client: Any, album_id: str) -> list[str]:
    payload = client.fetch_album_tracks(album_id) or {}
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    return [
        str(t.get("id") or "")
        for t in (tracks or [])
        if isinstance(t, dict) and t.get("id")
    ]


def build_favourite_album_song_ids(client: Any, album_ids: list[str]) -> list[str]:
    """Every song id on the given Navidrome albums (ordered, de-duplicated)."""
    song_ids: list[str] = []
    for album_id in album_ids:
        song_ids.extend(_song_ids_from_album(client, album_id))
    return _dedupe(song_ids)


def build_favourite_artist_song_ids(client: Any, artist_ids: list[str]) -> list[str]:
    """Every song id on every Navidrome album of the given artists."""
    song_ids: list[str] = []
    for artist_id in artist_ids:
        for album in client.fetch_artist_albums(artist_id) or []:
            album_id = str((album or {}).get("id") or "")
            if album_id:
                song_ids.extend(_song_ids_from_album(client, album_id))
    return _dedupe(song_ids)


def _qualifying_tracks(song_ids: list[str], min_stars: int) -> list[str]:
    """Keep only the song ids whose LOCAL ``tracks.stars`` meets the floor.

    Order follows *song_ids* (the playlist's intended order), and ids with no
    local row are dropped — a starred album can hold tracks this library never
    imported, and such an id would make the pushed playlist differ from the
    requested one forever.

    ⚠️ An expanding bindparam, never a JSON array deconstructor inside the IN
    list: ``json_each`` is SQLite's, and PostgreSQL's same-named function
    expands a JSON **object** with a ``json``-typed ``value``, so it can never
    compare to a TEXT column (see
    ``playlist_service.create_or_update_loved_tracks_playlist``).
    """
    if not song_ids:
        return []
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT CAST(id AS TEXT) AS id, COALESCE(stars, 0) AS stars
                    FROM tracks
                    WHERE CAST(id AS TEXT) IN :ids
                """).bindparams(bindparam("ids", expanding=True)),
                {"ids": [str(s) for s in song_ids]},
            ).fetchall() or []
    except Exception as exc:
        logger.warning("[FAVOURITES] track star filter failed: %s", exc)
        return []
    star_by_id = {str(r[0]): int(r[1] or 0) for r in rows}
    return [sid for sid in song_ids if star_by_id.get(str(sid), 0) >= int(min_stars)]


def sync_favourite_playlists_for_user(username: str, *, force: bool = False) -> dict[str, Any]:
    """Build and push this user's Favourite Artists / Favourite Albums playlists.

    Returns ``{"success", "username", "min_stars", "artists": {...},
    "albums": {...}, "error"}``. Never raises.
    """
    from services.favourites_service import get_navidrome_user_by_name
    from services.playlists.playlist_navidrome_service import sync_playlist_by_name

    result: dict[str, Any] = {
        "success": False, "username": username, "min_stars": 0,
        "artists": {}, "albums": {}, "error": None,
    }
    cfg = _config()
    min_stars = int(cfg["favourite_min_stars"])
    result["min_stars"] = min_stars

    user = get_navidrome_user_by_name(username)
    if not user:
        result["error"] = f"Navidrome user '{username}' is not configured"
        return result

    from api_clients.navidrome import NavidromeClient

    try:
        client = NavidromeClient(
            base_url=user.get("base_url", ""),
            username=user.get("user", ""),
            password=user.get("pass", ""),
        )
    except Exception as exc:
        result["error"] = f"Could not build a Navidrome client for '{username}': {exc}"
        return result

    artist_ids = get_favourite_navidrome_ids(username, "artist")
    album_ids = get_favourite_navidrome_ids(username, "album")

    try:
        artist_songs = build_favourite_artist_song_ids(client, artist_ids)
        album_songs = build_favourite_album_song_ids(client, album_ids)
    except Exception as exc:
        result["error"] = f"Navidrome expansion failed: {exc}"
        return result

    artist_kept = _qualifying_tracks(artist_songs, min_stars)
    album_kept = _qualifying_tracks(album_songs, min_stars)

    # ⚠️ An EMPTY push is skipped, never written: ``sync_playlist_by_name``
    # would otherwise replace a populated playlist with nothing every time the
    # user un-stars something, and the re-star would then have to rebuild it
    # from scratch. A playlist with no qualifying tracks is left as it is.
    if artist_kept:
        result["artists"] = sync_playlist_by_name(
            client, cfg["favourite_artists_name"], artist_kept, force=force,
        )
    else:
        result["artists"] = {"skipped": True, "reason": "no qualifying tracks"}

    if album_kept:
        result["albums"] = sync_playlist_by_name(
            client, cfg["favourite_albums_name"], album_kept, force=force,
        )
    else:
        result["albums"] = {"skipped": True, "reason": "no qualifying tracks"}

    result["success"] = True
    logger.info(
        "[FAVOURITES] playlists synced user=%s artists=%d/%d albums=%d/%d min_stars=%d",
        username, len(artist_kept), len(artist_songs),
        len(album_kept), len(album_songs), min_stars,
    )
    return result


def sync_favourite_playlists(*, force: bool = False) -> dict[str, Any]:
    """Run the per-user favourite playlist sync for EVERY configured user.

    Returns ``{"success", "users": [...], "errors": [...]}``.
    """
    from helpers.config_helpers import get_navidrome_users_normalized

    cfg = _config()
    if not cfg["favourite_playlists_enabled"]:
        return {"success": True, "users": [], "errors": [],
                "note": "favourite playlists disabled"}

    users = get_navidrome_users_normalized()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for user in users:
        name = str(user.get("user") or "").strip()
        if not name:
            continue
        outcome = sync_favourite_playlists_for_user(name, force=force)
        results.append(outcome)
        if not outcome.get("success"):
            errors.append(f"{name}: {outcome.get('error')}")
    return {"success": not errors, "users": results, "errors": errors}
