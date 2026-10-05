"""Slskd transfer watchdog: cancel stalled transfers.

Transfers that never start (peer offline at connect time) or die mid-flight
(speed drops to zero and never recovers) otherwise sit in ``transfers/downloads``
forever, while their queue items stay ``downloading``.  This reaper cancels
them and marks the owning queue item failed so the retry scheduler backs off
and re-attempts the download later.

Registered as an optional maintenance hook (``MAINTENANCE_CANDIDATES``) —
best-effort, never raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from db.repositories.queue import get_active_queue, mark_failed

logger = structlog.get_logger(__name__)

# A transfer at 0% after this many minutes is treated as never-started
# (offline peer / queue never picked up).  Transfers that were progressing
# but stalled get a longer window.
STALL_ZERO_PROGRESS_MINUTES = 15
STALL_MID_TRANSFER_MINUTES = 60


def _started_minutes_ago(started_at: Any) -> Optional[float]:
    """Convert a slskd ``startedAt`` ISO timestamp to minutes since start.

    Returns ``None`` when the timestamp is missing/unparseable so unknown-age
    transfers are never treated as stalled (conservative default).
    """
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def _normalise_path(path: Any) -> str:
    return str(path or "").replace("\\", "/").lower()

def _find_owning_item(transfer: dict, active_items: list) -> Optional[dict]:
    """Match a transfer to its queue item by path (slashes normalised)."""
    filename = _normalise_path(transfer.get("filename"))
    local_path = _normalise_path(transfer.get("localFilePath"))
    
    if not filename and not local_path:
        return None

    for item in active_items:
        for column in ("found_filename", "file_path", "music_file_path"):
            val = _normalise_path(item.get(column))
            if val and val in (filename, local_path):
                return item
    return None


def reap_stalled_transfers() -> dict[str, int]:
    """Cancel slskd transfers that are stalled and fail their queue items.

    Returns a stats dict; never raises (best-effort maintenance hook).
    """
    stats = {"checked": 0, "cancelled_transfers": 0, "requeued_items": 0}
    try:
        from api_clients.slskd_http import get_slskd_client
        from services.downloads.download_pipeline_service import _block_peer
        from services.downloads.slskd_service import SlskdService

        client = get_slskd_client()
        if client is None:
            return stats

        slskd = SlskdService(http_client=client)
        transfers = slskd.get_active_downloads()
        stats["checked"] = len(transfers)
        if not transfers:
            return stats

        active_items = get_active_queue(limit=300)

        for transfer in transfers:
            age_minutes = _started_minutes_ago(transfer.get("startedAt"))
            if age_minutes is None:
                continue

            progress = int(transfer.get("progress") or 0)
            speed = int(transfer.get("averageSpeed") or 0)
            stalled = (
                progress <= 0 and age_minutes >= STALL_ZERO_PROGRESS_MINUTES
            ) or (
                0 < progress < 100 and speed <= 0 and age_minutes >= STALL_MID_TRANSFER_MINUTES
            )
            if not stalled:
                continue

            username = str(transfer.get("username") or "")
            transfer_id = str(transfer.get("id") or "")
            filename = str(transfer.get("filename") or "")
            if username and transfer_id:
                if slskd.cancel_download(username, transfer_id, remove=True):
                    stats["cancelled_transfers"] += 1
                    logger.warning(
                        "Cancelled stalled transfer",
                        transfer_id=transfer_id,
                        username=username,
                        filename=transfer.get("filename"),
                        progress=progress,
                    )

            # EVERY other failure path already blocks the (peer, file) pair:
            # no free upload slots, ``download_file`` returning False, and a
            # mismatched download. The stall path did NOT — so the next search
            # re-selected the same dead peer and stalled again: ATEEZ
            # "HIGHER"/"Seeker" picked roqinghejing, sat at progress=0 for 15
            # minutes, was reaped, and repeated, every ~45 minutes, forever.
            #
            # Blocking here (same TTL and key as everywhere else) means the
            # next cycle either selects a different file or reports
            # ``no_results`` and backs off, instead of burning another stall.
            if username and filename:
                _block_peer(username, filename)
                logger.warning(
                    "Blocked peer after stalled transfer",
                    username=username,
                    filename=filename,
                    progress=progress,
                )

            item = _find_owning_item(transfer, active_items)
            if item:
                mark_failed(
                    int(item["id"]),
                    "stalled_transfer",
                )
                stats["requeued_items"] += 1
                logger.warning(
                    "Failed queue item for stalled transfer",
                    queue_id=item.get("id"),
                    artist=item.get("artist"),
                    title=item.get("title"),
                )

        return stats
    except Exception as exc:
        logger.error("Stalled transfer reaper failed", error=str(exc), exc_info=True)
        return stats
