"""Add newly-imported tracks to the playlist they were queued for.

DESIGN

"Download Missing" on the Exportify panel queues each missing track with a
``metadata.playlist_target`` (and ``playlist_user``) intent on its queue row. A
track cannot be added to a Navidrome playlist the moment it is queued — it has no
library row yet — and even at import time the ``tracks.id`` is a placeholder
(``dlimport-…``) until the Navidrome import ADOPTS it to the real Subsonic song id

    (services/downloads/download_completion_service.py::_new_import_track_id)

So the addition is applied HERE, later, by a maintenance sweep:

  1. only when no Navidrome import is running (a playlist write is deferred while
     one is — it would silently no-op);
  2. only for queue rows that have landed (imported/completed/in_collection) and
     whose file now resolves to a REAL ``tracks.id``;
  3. idempotently — the playlist's current ids are read and the new one appended.

The intent is cleared only after the write succeeds, so a deferred or failed
attempt is retried on the next cycle.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

_LANDED_STATUSES = ("imported", "completed", "in_collection")


def _meta_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _pending_rows(limit: int) -> list[dict[str, Any]]:
    """Queue rows that landed and still carry a playlist intent."""
    statuses = ", ".join(f"'{s}'" for s in _LANDED_STATUSES)
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, metadata, "
                    "       COALESCE(music_file_path, file_path, '') AS path "
                    "FROM download_queue "
                    "WHERE COALESCE(CAST(metadata AS TEXT), '') LIKE '%playlist_target%' "
                    f"  AND status IN ({statuses}) "
                    "  AND COALESCE(music_file_path, file_path, '') <> '' "
                    "ORDER BY COALESCE(updated_at, created_at) ASC "
                    "LIMIT :limit"
                ),
                {"limit": int(limit)},
            ).fetchall()
    except Exception as exc:
        logger.debug("Pending playlist additions query failed", error=str(exc))
        return []
    return [dict(row._mapping) for row in rows]


def _resolve_track_id(path: str) -> str | None:
    """The REAL tracks.id for a landed file, or None if it is not indexed yet."""
    wanted = str(path or "").strip()
    if not wanted:
        return None
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT CAST(id AS TEXT) AS id FROM tracks WHERE file_path = :p LIMIT 1"),
                {"p": wanted},
            ).fetchone()
    except Exception as exc:
        logger.debug("Track id lookup failed", path=wanted, error=str(exc))
        return None
    if not row:
        return None
    track_id = str(row[0] or "").strip()
    if not track_id or track_id.startswith("dlimport-"):
        # Not yet adopted by the Navidrome import — try again later.
        return None
    return track_id


def _client_for(user: str):
    try:
        from services.favourites_service import (
            get_navidrome_client_for_active_user,
            get_navidrome_client_for_user,
        )

        if user:
            return get_navidrome_client_for_user(user) or get_navidrome_client_for_active_user()
        return get_navidrome_client_for_active_user()
    except Exception as exc:
        logger.debug("Navidrome client for playlist intent failed", error=str(exc))
        return None


def _clear_intent(queue_id: Any, meta: dict[str, Any]) -> None:
    from db.repositories.queue import update_queue_item

    remaining = dict(meta or {})
    remaining.pop("playlist_target", None)
    remaining.pop("playlist_user", None)
    try:
        update_queue_item(int(queue_id), metadata=remaining or {})
    except Exception as exc:
        logger.debug("Could not clear playlist intent", queue_id=queue_id, error=str(exc))


def apply_pending_playlist_additions(limit: int = 200) -> dict[str, Any]:
    """Maintenance hook: add landed tracks to their target playlists."""
    try:
        from services.navidrome.import_gate import is_import_running

        if is_import_running():
            return {
                "success": True,
                "skipped": True,
                "reason": "navidrome_import_running",
                "added": 0,
                "pending": 0,
            }
    except Exception:
        pass

    rows = _pending_rows(limit)
    if not rows:
        return {"success": True, "added": 0, "pending": 0, "processed": 0}

    from services.playlists.playlist_navidrome_service import (
        add_track_to_playlist_by_name,
    )

    added = 0
    pending = 0
    for row in rows:
        meta = _meta_dict(row.get("metadata"))
        target = str(meta.get("playlist_target") or "").strip()
        if not target:
            continue
        user = str(meta.get("playlist_user") or "").strip()
        track_id = _resolve_track_id(str(row.get("path") or ""))
        if not track_id:
            pending += 1
            continue
        client = _client_for(user)
        if client is None:
            pending += 1
            continue
        result = add_track_to_playlist_by_name(client, target, track_id)
        if result.get("success"):
            if result.get("changed"):
                added += 1
                logger.info(
                    "Added imported track to its playlist",
                    queue_id=row.get("id"),
                    playlist=target,
                    track_id=track_id,
                )
            _clear_intent(row.get("id"), meta)
        else:
            pending += 1
            logger.debug(
                "Playlist addition deferred",
                queue_id=row.get("id"),
                playlist=target,
                reason=result.get("reason"),
            )
    return {
        "success": True,
        "added": added,
        "pending": pending,
        "processed": len(rows),
    }
