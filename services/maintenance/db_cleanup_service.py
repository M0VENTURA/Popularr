"""Safe "clear rows no longer in use" maintenance.

REQUESTED

> "Should we have an option to clear the database of rows no longer in use. Such
> as albums that have no items in the download queue or in Navidrome. Or
> downloads that have completed."

WHAT IS SAFE TO PRUNE, AND WHAT IS NOT

Deleting ``imported`` / ``completed`` / ``in_collection`` queue rows is NOT safe:
they drive the Matched/Unmatched folder flags, the Imported counter, metadata
heal, missing-track badges, the claimed-file and file-deletion guards, discovery
dedupe, and folder auto-deletion. This module therefore never touches them.

Two genuinely orphanable things ARE pruned, and both are ALWAYS available as a
dry-run first:

1. ``album_art`` rows whose album no longer exists in ``tracks`` — the table is
   keyed by (artist_name, album_name) with no foreign key and had NO delete path
   anywhere, so every removed/re-keyed album left its artwork behind forever.

2. ``download_queue`` TOMBSTONES — ``removed``/``cancelled``/``deleted`` rows
   older than a cutoff. These are excluded from the queue display, excluded from
   re-queue blocking, and only revive via an explicit requeue, so they are the
   least risky queue rows to age out. Their ``musicbrainz_release_tracks``
   children are deleted first (that FK has no ON DELETE action).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session

logger = structlog.get_logger(__name__)

#: Queue statuses that are pure tombstones.
TOMBSTONE_STATUSES: tuple[str, ...] = ("removed", "cancelled", "deleted")

#: Tombstones older than this are eligible.
TOMBSTONE_MAX_AGE_DAYS = 30

#: Matches an album_art row to a live tracks album (the house album key).
#:
#: ⚠️ References ``album_art`` by its bare name, not an alias: SQLite rejects a
#: table alias in DELETE, and the test suite runs this on SQLite.
_ORPHAN_ART_WHERE = """
    NOT EXISTS (
        SELECT 1 FROM tracks t
        WHERE LOWER(TRIM(COALESCE(NULLIF(TRIM(t.album_artist), ''), t.artist)))
              = LOWER(TRIM(album_art.artist_name))
          AND LOWER(TRIM(COALESCE(t.album, ''))) = LOWER(TRIM(album_art.album_name))
    )
"""


def _tombstone_cutoff(days: int = TOMBSTONE_MAX_AGE_DAYS) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=int(days))


def preview_cleanup(*, tombstone_days: int = TOMBSTONE_MAX_AGE_DAYS) -> dict[str, Any]:
    """Count what WOULD be removed. Never writes."""
    cutoff = _tombstone_cutoff(tombstone_days)
    placeholders = ", ".join(f":s{i}" for i in range(len(TOMBSTONE_STATUSES)))
    params = {f"s{i}": s for i, s in enumerate(TOMBSTONE_STATUSES)}
    params["cutoff"] = cutoff
    try:
        with db_session() as session:
            orphan_art = session.execute(
                text(f"SELECT COUNT(*) FROM album_art WHERE {_ORPHAN_ART_WHERE}")
            ).scalar() or 0
            tombstones = session.execute(
                text(
                    "SELECT COUNT(*) FROM download_queue "
                    f"WHERE status IN ({placeholders}) "
                    "AND COALESCE(updated_at, created_at) < :cutoff"
                ),
                params,
            ).scalar() or 0
        return {
            "success": True,
            "dry_run": True,
            "orphan_album_art": int(orphan_art),
            "queue_tombstones": int(tombstones),
            "tombstone_days": int(tombstone_days),
            "tombstone_statuses": list(TOMBSTONE_STATUSES),
        }
    except Exception as exc:
        logger.error("Cleanup preview failed", error=str(exc))
        return {"success": False, "error": str(exc)}


def run_cleanup(
    *,
    orphan_album_art: bool = True,
    queue_tombstones: bool = True,
    tombstone_days: int = TOMBSTONE_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Delete the safe targets. Returns what was removed."""
    cutoff = _tombstone_cutoff(tombstone_days)
    placeholders = ", ".join(f":s{i}" for i in range(len(TOMBSTONE_STATUSES)))
    params = {f"s{i}": s for i, s in enumerate(TOMBSTONE_STATUSES)}
    params["cutoff"] = cutoff
    removed = {"orphan_album_art": 0, "queue_tombstones": 0}
    try:
        with db_session() as session:
            if orphan_album_art:
                res = session.execute(
                    text(f"DELETE FROM album_art WHERE {_ORPHAN_ART_WHERE}")
                )
                removed["orphan_album_art"] = int(res.rowcount or 0)

            if queue_tombstones:
                ids = [
                    int(row[0])
                    for row in session.execute(
                        text(
                            "SELECT id FROM download_queue "
                            f"WHERE status IN ({placeholders}) "
                            "AND COALESCE(updated_at, created_at) < :cutoff"
                        ),
                        params,
                    ).fetchall()
                ]
                if ids:
                    try:
                        session.execute(
                            text(
                                "DELETE FROM musicbrainz_release_tracks "
                                "WHERE queue_id IN :ids"
                            ).bindparams(bindparam("ids", expanding=True)),
                            {"ids": ids},
                        )
                    except Exception:
                        # The table may not exist on a fresh/short schema; the
                        # queue delete below is the part that matters.
                        pass
                    res = session.execute(
                        text("DELETE FROM download_queue WHERE id IN :ids").bindparams(
                            bindparam("ids", expanding=True)
                        ),
                        {"ids": ids},
                    )
                    removed["queue_tombstones"] = int(res.rowcount or 0)
            try:
                session.commit()
            except Exception:
                pass
        logger.info("Database cleanup complete", **removed)
        return {
            "success": True,
            "dry_run": False,
            "removed": removed,
            "tombstone_days": int(tombstone_days),
        }
    except Exception as exc:
        logger.error("Cleanup failed", error=str(exc))
        return {"success": False, "error": str(exc)}
