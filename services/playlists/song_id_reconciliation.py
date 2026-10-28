"""Persistent playlist song-ID rejections, scoped to one Navidrome server.

Navidrome silently drops a song ID it cannot resolve. Re-sending the same ID
on every scan rewrites the whole playlist for no effect and repeats the same
warning forever.

A rejection is therefore recorded by ``(server, playlist, song_id)`` and
filtered out of later writes. It is cleared only after a verified import /
ID adoption, so the retry remains possible once the mapping can have changed.
"""

from __future__ import annotations

from typing import Any, Iterable

import structlog

from db.engine import db_session

logger = structlog.get_logger(__name__)


def server_key(client: Any) -> str:
    """Stable identity for a Navidrome server (never mix two servers' IDs).

    A client without a base URL (legacy/test double) has no reliable server
    identity, so it gets ``""``: callers still report unresolved IDs but must
    not persist a suppression that could leak across servers.
    """
    base = str(getattr(client, "base_url", "") or "").strip().rstrip("/").casefold()
    if not base:
        return ""
    user = str(getattr(client, "username", "") or "").strip().casefold()
    return f"{base}|{user}"


def get_rejected_song_ids(server_key_value: str, playlist_name: str) -> dict[str, str]:
    """Return ``{song_id: track_hint}`` for this playlist on this server."""
    if not server_key_value or not playlist_name:
        return {}
    try:
        from sqlalchemy import text

        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT song_id, track_hint FROM playlist_song_rejections "
                    "WHERE server_key = :server AND playlist_name = :playlist"
                ),
                {"server": server_key_value, "playlist": playlist_name},
            ).fetchall() or []
        return {
            str(row[0]): str(row[1] or "")
            for row in rows
            if row and str(row[0] or "").strip()
        }
    except Exception as exc:
        logger.debug("Playlist rejection read failed", error=str(exc))
        return {}


def record_rejected_song_ids(
    server_key_value: str,
    playlist_name: str,
    song_ids: Iterable[str],
    hints: dict[str, str] | None = None,
) -> int:
    """Upsert rejections and return how many were NEW.

    Newly-recorded IDs are the ones worth a warning; a repeated rejection is a
    debug-level known state.
    """
    values = [str(value).strip() for value in song_ids or [] if str(value or "").strip()]
    if not values:
        return 0
    if not server_key_value or not playlist_name:
        # A test/legacy client without server identity cannot persist a
        # rejection; still report them as new so the caller warns truthfully.
        return len(values)
    hints = hints or {}
    existing = get_rejected_song_ids(server_key_value, playlist_name)
    new_ids = [value for value in values if value not in existing]
    try:
        from sqlalchemy import text

        with db_session() as session:
            for song_id in values:
                session.execute(
                    text(
                        "INSERT INTO playlist_song_rejections "
                        "(server_key, playlist_name, song_id, track_hint, first_seen, last_seen) "
                        "VALUES (:server, :playlist, :song, :hint, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (server_key, playlist_name, song_id) DO UPDATE SET "
                        "last_seen = CURRENT_TIMESTAMP, "
                        "track_hint = COALESCE(EXCLUDED.track_hint, playlist_song_rejections.track_hint)"
                    ),
                    {
                        "server": server_key_value,
                        "playlist": playlist_name,
                        "song": song_id,
                        "hint": hints.get(song_id) or None,
                    },
                )
    except Exception as exc:
        # Without the table we lose the suppression optimisation, but the
        # caller's result is still truthful (partial reconciliation).
        logger.debug("Playlist rejection write failed", error=str(exc))
        return len(new_ids)
    return len(new_ids)


def clear_playlist_song_rejections(
    server_key_value: str | None = None,
    *,
    song_ids: Iterable[str] | None = None,
) -> int:
    """Forget rejections after a verified import / ID adoption.

    ``server_key_value=None`` clears every server (used by an explicit
    administrative repair); normal callers pass the server they just imported.
    """
    values = [
        str(value).strip()
        for value in (song_ids or [])
        if str(value or "").strip()
    ]
    if server_key_value is not None and not str(server_key_value).strip():
        # No server identity: do not touch another server's rejections.
        return 0
    try:
        from sqlalchemy import text

        clauses = []
        params: dict[str, Any] = {}
        if server_key_value:
            clauses.append("server_key = :server")
            params["server"] = server_key_value
        if values:
            placeholders = ", ".join(f":s{i}" for i in range(len(values)))
            clauses.append(f"song_id IN ({placeholders})")
            params.update({f"s{i}": value for i, value in enumerate(values)})
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with db_session() as session:
            result = session.execute(
                text(f"DELETE FROM playlist_song_rejections{where}"), params
            )
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.debug("Playlist rejection clear failed", error=str(exc))
        return 0
