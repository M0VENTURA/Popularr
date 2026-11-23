"""Clear the retry dates that an OUTAGE — not a genuine miss — stamped on queue rows.

Why this needs a separate, explicit action
------------------------------------------
Reported: Soulseek was disconnected for ~8 hours on 19 Nov, 1,432 searches were
skipped, and on 20 Nov 34 searches returned 0 results between 05:00 and 06:59
for tracks that are trivial to find. Every one of those was then logged as
"persistent miss, retrying Thursday 27 Nov" — a whole week lost per track, and a
``retry_count`` that pushed it past every backoff tier.

The forward fix lives in ``download_pipeline_service`` (a zero-result STREAK now
pauses searching instead of recording a miss). This module repairs the rows that
were already stamped, which a forward fix cannot undo.

Why "outage windows" and not a date range
-----------------------------------------
Bluntly clearing every miss in a window would also reset GENUINE misses, which
cost a week each for a reason and would simply be re-stamped. So the window has
to be EVIDENCE-BASED: a run of consecutive zero-result / disconnected searches
long enough that no library could plausibly contain none of those tracks.

Deliberately manual
-------------------
Exposed as an action, never an automatic maintenance hook. Re-running a retry
sweep automatically would refire the same backlog on every upgrade and on every
quiet patch of searching.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session

logger = structlog.get_logger(__name__)

#: Consecutive zero-result searches that constitute an outage rather than bad
#: luck. Matches ``_EMPTY_STREAK_LIMIT`` in the pipeline by intent, and is
#: deliberately conservative: a false positive would re-download a genuine miss.
OUTAGE_STREAK_MINIMUM = 5

#: A gap longer than this between two searches ends a candidate window — it is
#: the worker having nothing to do, not the service being down.
OUTAGE_MAX_GAP_MINUTES = 30

#: Only rows touched within this many days of the window are considered.
OUTAGE_MATCH_TOLERANCE_MINUTES = 15


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_outage_windows(
    *,
    lookback_days: int = 7,
    streak_minimum: int = OUTAGE_STREAK_MINIMUM,
) -> list[tuple[datetime, datetime]]:
    """Time windows in which the search service returned nothing, repeatedly.

    Returns ``[(start, end)]``, oldest first. Empty when there was no outage —
    which is the common case, and the caller must then touch nothing.
    """
    since = _utc_naive() - timedelta(days=max(1, lookback_days))
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT created_at, result_count, notes
                    FROM slskd_search_logs
                    WHERE created_at >= :since
                    ORDER BY created_at ASC
                """),
                {"since": since},
            ).fetchall() or []
    except Exception as exc:
        logger.warning("Could not read search logs for outage detection", error=str(exc))
        return []

    windows: list[tuple[datetime, datetime]] = []
    run_start: datetime | None = None
    run_last: datetime | None = None
    run_length = 0

    def _close() -> None:
        nonlocal run_start, run_last, run_length
        if run_start is not None and run_last is not None and run_length >= streak_minimum:
            windows.append((run_start, run_last))
        run_start = run_last = None
        run_length = 0

    for row in rows:
        mapping = row._mapping
        created = mapping.get("created_at")
        if not isinstance(created, datetime):
            continue
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)

        notes = str(mapping.get("notes") or "")
        # A disconnected search is logged too; count it as part of the outage.
        interrupted = (
            int(mapping.get("result_count") or 0) == 0
            or "disconnected" in notes.casefold()
            or "no_results" in notes.casefold()
        )

        if not interrupted:
            _close()
            continue

        if run_last is not None and (
            created - run_last > timedelta(minutes=OUTAGE_MAX_GAP_MINUTES)
        ):
            # Too long a gap: the previous run was not one continuous outage.
            _close()

        if run_start is None:
            run_start = created
        run_last = created
        run_length += 1

    _close()
    return windows


def _miss_rows_within(
    windows: list[tuple[datetime, datetime]],
) -> list[dict[str, Any]]:
    """Queue rows whose retry date was stamped INSIDE one of *windows*."""
    if not windows:
        return []

    tolerance = timedelta(minutes=OUTAGE_MATCH_TOLERANCE_MINUTES)
    found: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, artist, title, updated_at, failure_reason, next_retry_at
                    FROM download_queue
                    WHERE status IN ('backed_off', 'pending_release', 'failed', 'queued')
                      AND failure_reason IS NOT NULL
                      AND (
                            failure_reason ILIKE '%persistent miss%'
                         OR failure_reason ILIKE '%no_results%'
                         OR failure_reason ILIKE '%disconnected%'
                      )
                """)
            ).fetchall() or []
            candidates = [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.warning("Could not read miss rows for repair", error=str(exc))
        return []

    for row in candidates:
        updated = row.get("updated_at")
        if not isinstance(updated, datetime):
            continue
        if updated.tzinfo is not None:
            updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
        if any(start - tolerance <= updated <= end + tolerance for start, end in windows):
            found.append(row)
    return found


def clear_outage_misses(
    *,
    lookback_days: int = 7,
    streak_minimum: int = OUTAGE_STREAK_MINIMUM,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Release the misses an outage stamped, so they retry normally again.

    ``dry_run`` is the default — this is destructive to retry state and the
    caller must see which rows it would touch first.
    """
    windows = find_outage_windows(
        lookback_days=lookback_days, streak_minimum=streak_minimum
    )
    if not windows:
        return {
            "success": True,
            "outage_windows": 0,
            "candidates": 0,
            "cleared": 0,
            "dry_run": dry_run,
            "message": "No outage window found — nothing to clear.",
        }

    rows = _miss_rows_within(windows)
    if not rows:
        return {
            "success": True,
            "outage_windows": len(windows),
            "candidates": 0,
            "cleared": 0,
            "dry_run": dry_run,
            "windows": [[s.isoformat(), e.isoformat()] for s, e in windows],
            "message": "Outage window(s) found, but no miss was stamped inside them.",
        }

    if dry_run:
        return {
            "success": True,
            "outage_windows": len(windows),
            "candidates": len(rows),
            "cleared": 0,
            "dry_run": True,
            "windows": [[s.isoformat(), e.isoformat()] for s, e in windows],
            "items": [
                {
                    "id": r.get("id"),
                    "artist": r.get("artist"),
                    "title": r.get("title"),
                    "failure_reason": r.get("failure_reason"),
                    "next_retry_at": str(r.get("next_retry_at") or ""),
                }
                for r in rows[:100]
            ],
        }

    ids = [int(r["id"]) for r in rows if r.get("id") is not None]
    try:
        with db_session() as session:
            session.execute(
                text(
                    "UPDATE download_queue "
                    "SET status = 'queued', "
                    "    retry_count = 0, "
                    "    next_retry_at = NULL, "
                    "    failure_reason = 'outage miss cleared — retrying normally', "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE CAST(id AS TEXT) IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [str(i) for i in ids]},
            )
    except Exception as exc:
        logger.error("Clearing outage misses failed", error=str(exc))
        return {"success": False, "error": str(exc), "cleared": 0}

    logger.warning(
        "Cleared queue misses that were recorded during a search outage",
        cleared=len(ids),
        outage_windows=len(windows),
    )
    return {
        "success": True,
        "outage_windows": len(windows),
        "candidates": len(rows),
        "cleared": len(ids),
        "dry_run": False,
        "windows": [[s.isoformat(), e.isoformat()] for s, e in windows],
    }
