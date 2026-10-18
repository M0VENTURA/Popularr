"""Per-user favourites (heart) service — Navidrome star sync.

Each Navidrome user keeps their OWN heart state for tracks, albums and
artists.  The active user is resolved from the app session
(``session["username"]``) falling back to the first configured
``navidrome_users`` entry (single-user setups).

The heart state is persisted per-user in ``user_favourites`` and mirrored to
Navidrome via the Subsonic ``star`` / ``unstar`` endpoints (which accept
track, album and artist IDs).  Pulling ``getStarred2`` on demand re-syncs
Navidrome-originated hearts back into the DB.

Callers:
    - routes/favourites.py (API endpoints)
    - page render helpers (ui_routes) to pre-fill heart state
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from quart import session as _quart_session

from db.engine import db_session
from db.repositories.favourites_repository import (
    get_favourite_ids,
    get_favourite_navidrome_ids,
    is_favourite as repo_is_favourite,
    set_favourite,
)
from helpers.config_helpers import get_navidrome_users_normalized

logger = logging.getLogger(__name__)


def get_active_username() -> str:
    """Resolve the active Navidrome user (session first, then first config user)."""
    username = str(_quart_session.get("username") or "").strip()
    if username:
        return username
    users = get_navidrome_users_normalized()
    if users:
        return str(users[0].get("user") or "default_user")
    return "default_user"


def get_active_navidrome_user() -> Optional[dict[str, str]]:
    """The full Navidrome user config matching the active username, or None."""
    username = get_active_username()
    users = get_navidrome_users_normalized()
    for user in users:
        if user.get("user") == username:
            return user
    # Fall back to the first configured user when the session user isn't in
    # the config (e.g. legacy single-user setups).
    if users:
        return users[0]
    return None


def get_navidrome_client_for_active_user():
    """A Navidrome client for the active user, or None when not configured."""
    return get_navidrome_client_for_user(get_active_username())


def get_navidrome_user_by_name(username: str) -> Optional[dict[str, str]]:
    """The configured Navidrome user entry for *username*, or None.

    ⚠️ Exact match only. ``get_active_navidrome_user`` deliberately FALLS BACK
    to the first configured user, which is right for a single-user install but
    wrong here: the per-user sync must not silently push one account's
    playlists as another, so an unknown name is an error, not a fallback.
    """
    wanted = str(username or "").strip()
    if not wanted:
        return None
    for user in get_navidrome_users_normalized():
        if str(user.get("user") or "").strip() == wanted:
            return user
    return None


def get_navidrome_client_for_user(username: str):
    """A Navidrome client for a SPECIFIC configured user, or None.

    The client carries the username, and that is what decides whose account a
    playlist write lands in (Navidrome rejects a write to a playlist the
    caller does not own — Subsonic code 50).
    """
    user = get_navidrome_user_by_name(username)
    if not user:
        return None
    try:
        from api_clients.navidrome import NavidromeClient
        return NavidromeClient(
            base_url=user.get("base_url", ""),
            username=user.get("user", ""),
            password=user.get("pass", ""),
        )
    except Exception as exc:
        logger.debug("Could not build Navidrome client: %s", exc)
        return None


def is_favourite(entity_type: str, entity_id: str) -> bool:
    """True when the ACTIVE user has hearted this entity."""
    return repo_is_favourite(get_active_username(), entity_type, entity_id)


def favourite_ids(entity_type: str) -> list[str]:
    """Entity IDs hearted by the ACTIVE user for an entity type."""
    return get_favourite_ids(get_active_username(), entity_type)


def favourite_navidrome_ids(entity_type: str) -> list[str]:
    """Navidrome IDs hearted by the ACTIVE user (for star/unstar)."""
    return get_favourite_navidrome_ids(get_active_username(), entity_type)


def favourite_rating_floor() -> int:
    """Configured rating floor for hearted tracks (default 4).

    ``config.yaml → features.favourite_rating_floor`` — any track hearted by
    ANY configured Navidrome user never drops below this star rating.
    """
    try:
        from helpers.config_helpers import get_config
        value = int((get_config() or {}).get("features", {}).get("favourite_rating_floor", 4) or 4)
        return max(1, min(5, value))
    except Exception:
        return 4


def apply_favourite_rating_floor(artist: str, album: str) -> int:
    """Raise ``stars`` to the configured floor for hearted tracks in an album.

    A heart is personal taste: a track with low global popularity (3★
    algorithmically) that a user has hearted should not drop below the floor
    (e.g. 4★).  Applies across ALL configured Navidrome users (not just the
    active one) so any user's heart protects the track.

    Returns the number of tracks whose rating was raised.
    """
    floor = favourite_rating_floor()
    if floor <= 1:
        return 0
    try:
        from sqlalchemy import text
        from helpers.config_helpers import get_navidrome_users_normalized

        users = get_navidrome_users_normalized()
        if not users:
            return 0
        usernames = [str(u.get("user") or "") for u in users if u.get("user")]
        if not usernames:
            return 0

        with db_session() as session:
            placeholders = ", ".join(f"'{u}'" for u in usernames)
            result = session.execute(
                text(f"""
                    UPDATE tracks
                    SET stars = :floor, updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND COALESCE(stars, 0) < :floor
                      AND id IN (
                          SELECT entity_id FROM user_favourites
                          WHERE entity_type = 'track'
                            AND is_favourite = TRUE
                            AND username IN ({placeholders})
                      )
                """),
                {"floor": floor, "artist": artist, "album": album},
            )
            affected = int(result.rowcount or 0)
            if affected:
                logger.info("[FAVOURITES] Rating floor %d★ applied to %d hearted track(s) in '%s'", floor, affected, album)
            return affected
    except Exception as exc:
        logger.debug("[FAVOURITES] Rating floor apply failed: %s", exc)
        return 0


def toggle_favourite(
    entity_type: str,
    entity_id: str,
    liked: bool,
    navidrome_id: Optional[str] = None,
) -> dict[str, Any]:
    """Toggle a heart for the ACTIVE user and mirror it to Navidrome.

    ``entity_type``: ``track`` | ``album`` | ``artist``.
    ``entity_id``: the local (Popularr) entity identifier.
    ``navidrome_id``: the corresponding Navidrome/Subsonic ID used by the
    ``star``/``unstar`` endpoints.  When omitted, best-effort resolution is
    attempted (tracks use their local id; album/artist resolve via Navidrome
    if an ID is discoverable — callers should pass it when known).

    Returns ``{"success": bool, "is_favourite": bool, "navidrome_synced": bool,
    "error": str|None}``.
    """
    username = get_active_username()
    resolved_nav_id = navidrome_id or _resolve_navidrome_id(entity_type, entity_id)

    # Persist first (source of truth), then mirror to Navidrome (best-effort).
    stored = set_favourite(
        username, entity_type, entity_id,
        is_favourite=liked,
        navidrome_id=resolved_nav_id,
    )
    if stored is None:
        return {"success": False, "is_favourite": liked, "navidrome_synced": False,
                "error": "Could not persist favourite state"}

    nav_synced = False
    client = get_navidrome_client_for_active_user()
    if client and resolved_nav_id:
        try:
            # ⭐ ``star_entity``, not ``star_track``: Subsonic's ``star`` takes
            # ``id`` for a SONG but ``albumId`` / ``artistId`` for an album /
            # artist. The server ACCEPTS the wrong parameter and stars nothing,
            # so hearting an album or artist used to vanish silently — only
            # tracks (whose ``tracks.id`` IS the Navidrome song id, so the bare
            # ``id`` happened to be right) ever reached Navidrome.
            if liked:
                nav_synced = bool(client.star_entity(entity_type, resolved_nav_id))
            else:
                nav_synced = bool(client.unstar_entity(entity_type, resolved_nav_id))
        except Exception as exc:
            logger.debug("[FAVOURITES] Navidrome star sync failed: %s", exc)
            nav_synced = False

    logger.info(
        "[FAVOURITES] user=%s %s %s -> %s (navidrome_id=%s, synced=%s)",
        username, entity_type, entity_id, "liked" if liked else "unliked",
        resolved_nav_id, nav_synced,
    )
    return {
        "success": True,
        "is_favourite": bool(liked),
        "navidrome_synced": nav_synced,
        "username": username,
        "navidrome_id": resolved_nav_id,
    }


def _resolve_navidrome_id(entity_type: str, entity_id: str) -> Optional[str]:
    """Best-effort resolution of the Navidrome ID for a local entity.

    Tracks: the local track ``id`` IS the Navidrome song id (the library is
    imported from Navidrome, so ids line up) — no lookup needed.

    Albums/artists: resolved by NAME through the Navidrome ``search3`` API.

    ⚠️ This used to query ``albums.navidrome_album_id`` and
    ``artists.navidrome_artist_id``. **Neither column exists** — there is no
    ``albums`` table at all, and ``artists`` holds only ``id``/``name`` — so
    both queries raised (swallowed by the ``except``) and returned ``None``
    every time. The effect: hearting an album or artist in Popularr never
    mirrored a star to Navidrome, silently. The API is the only source of a
    Navidrome album/artist id.
    """
    etype = str(entity_type or "").strip().lower()
    if etype in ("track", "song"):
        return str(entity_id or "") or None

    term = str(entity_id or "").strip()
    if not term:
        return None

    client = get_navidrome_client_for_active_user()
    if not client:
        return None

    try:
        found = client.search(term, artist_count=5, album_count=5, song_count=0) or {}
    except Exception as exc:
        logger.debug("[FAVOURITES] navidrome search failed for %s/%s: %s", entity_type, entity_id, exc)
        return None

    def _exact(candidates):
        """An id whose name matches the local value, case/space-insensitively.

        ⚠️ Exact-first, never "take the first hit": a search for one artist's
        name routinely returns albums by OTHER artists, and starring the wrong
        id is worse than starring nothing.
        """
        wanted = " ".join(term.casefold().split())
        for cand in candidates or []:
            if not isinstance(cand, dict):
                continue
            name = " ".join(str(cand.get("name") or "").casefold().split())
            cid = str(cand.get("id") or "").strip()
            if cid and name == wanted:
                return cid
        return None

    if etype == "album":
        return _exact(found.get("albums"))
    if etype == "artist":
        return _exact(found.get("artists"))
    return None


def sync_favourites_from_navidrome(username: Optional[str] = None) -> dict[str, Any]:
    """Pull a user's starred items from Navidrome into the DB.

    Uses ``getStarred2`` (tracks/albums/artists).  ``username`` defaults to the
    ACTIVE user; ``sync_favourites_for_all_users`` passes each configured user
    in turn.  One-way Navidrome → Popularr refresh; the outbound direction
    (heart → star) happens in ``toggle_favourite``.

    ⚠️ Stored per-user: an artist's/album's ``entity_id`` here is its
    **Navidrome** id (that is what this returns), which is what
    ``favourite_playlist_service`` needs to expand the starred entities back
    into songs.

    Returns ``{"success": bool, "tracks": int, "albums": int, "artists": int,
    "error": str|None}``.
    """
    username = str(username or "").strip() or get_active_username()
    client = get_navidrome_client_for_user(username)
    if not client:
        return {"success": False, "tracks": 0, "albums": 0, "artists": 0,
                "error": f"Navidrome not configured for user '{username}'"}

    try:
        starred = client.get_starred_items() or {}
    except Exception as exc:
        return {"success": False, "tracks": 0, "albums": 0, "artists": 0, "error": str(exc)}

    counts = {"tracks": 0, "albums": 0, "artists": 0}

    # Tracks: entity_id is the Navidrome song id (same as local track id).
    for song in starred.get("tracks") or []:
        sid = str(song.get("id") or "")
        if not sid:
            continue
        set_favourite(username, "track", sid, True, navidrome_id=sid)
        counts["tracks"] += 1

    for album in starred.get("albums") or []:
        aid = str(album.get("id") or "")
        if not aid:
            continue
        set_favourite(username, "album", aid, True, navidrome_id=aid)
        counts["albums"] += 1

    for artist in starred.get("artists") or []:
        arid = str(artist.get("id") or "")
        if not arid:
            continue
        set_favourite(username, "artist", arid, True, navidrome_id=arid)
        counts["artists"] += 1

    logger.info("[FAVOURITES] user=%s synced from Navidrome: %s", username, counts)
    return {"success": True, **counts}


def sync_favourites_for_all_users() -> dict[str, Any]:
    """Pull EVERY configured Navidrome user's stars into the DB.

    Each user gets their OWN rows in ``user_favourites`` (the table's key is
    ``(username, entity_type, entity_id)``), so the per-user playlist builder
    can read one account's favourites without ever seeing another's.

    Returns ``{"success", "users": [...], "errors": [...]}``.
    """
    from helpers.config_helpers import get_navidrome_users_normalized

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for user in get_navidrome_users_normalized():
        name = str(user.get("user") or "").strip()
        if not name:
            continue
        outcome = sync_favourites_from_navidrome(username=name)
        results.append({"username": name, **outcome})
        if not outcome.get("success"):
            errors.append(f"{name}: {outcome.get('error')}")
    if not results:
        errors.append("No Navidrome users are configured")
    return {"success": not errors, "users": results, "errors": errors}
