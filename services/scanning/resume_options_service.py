"""Which artist should a resumed scan start from?

Backs the dashboard's "choose the artist to resume from" prompt, shown when a
scan is started with **Restart unchecked**.

Why this exists
---------------
A resumed scan already knows its resume point: ``resume_from`` names the artist
the last interrupted run stopped at, and the full-scan loop skips every artist
BEFORE it while still processing the named artist itself. That is exactly right
for an artist that was **interrupted half way** — it is re-scanned from the top.

It is wrong for an artist that **finished** before the scan stopped: re-scanning
it does the work twice, and the run should instead continue from the NEXT
artist. Distinguishing the two is this module's job.

It also surfaces the artists the user has recently touched — manually scanned,
or with albums scanned — so a resume can jump to any of them rather than only
wherever the last full scan happened to die.

How "completed" is decided
--------------------------
Per album, the scan writes a ``scan_history`` row and later updates that same
row: ``record_scan(..., "started", artist, album)`` then
``record_scan(..., "completed", artist, album)``. A scan killed mid-album leaves
the row at ``started``. So the status of an artist's **most recent** album-level
row tells us whether that artist finished:

* ``completed`` → the artist was finished → resume from the NEXT artist.
* ``started``   → the artist was interrupted → restart that artist.

Rows written at session level use the sentinel artist ``_SCAN_SESSION_`` and are
excluded — they carry no artist and would otherwise pollute the list.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.library import get_all_artists

logger = structlog.get_logger(__name__)

#: Sentinel ``scan_history.artist`` used for whole-session rows.
_SESSION_ARTIST = "_SCAN_SESSION_"

#: How many recently-touched artists to offer alongside the full-scan point.
_DEFAULT_MANUAL_LIMIT = 3

#: Upper bound on rows read when building the candidate list.
_ROW_SCAN_LIMIT = 400


def _artist_key(value: Any) -> str:
    """Comparison key: casefolded, punctuation collapsed.

    Mirrors ``services.popularity.stages.load_stage._artist_key`` so a resume
    name matched here is matched by the scan loop too.
    """
    text_value = str(value or "").casefold()
    return "".join(ch for ch in text_value if ch.isalnum() or ch.isspace()).strip()


def _recent_album_rows(limit: int = _ROW_SCAN_LIMIT) -> list[dict[str, Any]]:
    """Most recent album-level scan rows first.

    Ordered by ``id DESC`` rather than a timestamp: ``id`` is the table's
    serial, so it is monotonic and needs no NULL handling — legacy rows can have
    a NULL ``started_at``, which would reorder a timestamp sort unpredictably.
    """
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT artist, album, status
                    FROM scan_history
                    WHERE artist IS NOT NULL
                      AND TRIM(artist) <> ''
                      AND artist <> :session_artist
                      AND album IS NOT NULL
                      AND TRIM(album) <> ''
                    ORDER BY id DESC
                    LIMIT :limit
                """),
                {"session_artist": _SESSION_ARTIST, "limit": limit},
            ).fetchall()
    except Exception as exc:
        logger.warning("Resume options: scan_history read failed", error=str(exc))
        return []
    return [dict(row._mapping) for row in rows or []]


def _album_counts() -> dict[str, int]:
    """Distinct album count per artist, from the library."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) AS artist,
                           COUNT(DISTINCT album) AS albums
                    FROM tracks
                    WHERE COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) IS NOT NULL
                      AND COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) <> ''
                      AND album IS NOT NULL
                      AND TRIM(album) <> ''
                    GROUP BY 1
                """)
            ).fetchall()
    except Exception as exc:
        logger.warning("Resume options: album count read failed", error=str(exc))
        return {}
    return {
        str(dict(row._mapping).get("artist") or ""): int(dict(row._mapping).get("albums") or 0)
        for row in rows or []
    }


def _most_recent_status_by_artist() -> dict[str, str]:
    """artist -> status of its most recent album-level row."""
    statuses: dict[str, str] = {}
    for row in _recent_album_rows():
        artist = str(row.get("artist") or "").strip()
        if not artist:
            continue
        # Rows arrive newest-first; the first seen per artist wins.
        statuses.setdefault(artist, str(row.get("status") or "").strip().lower())
    return statuses


def _full_scan_checkpoint_artist() -> str | None:
    """The artist the interrupted full scan stopped at, if any."""
    try:
        from services.scanning.scan_state import (
            get_scan_progress_path,
            load_scan_checkpoint,
        )

        checkpoint = load_scan_checkpoint(get_scan_progress_path("full_scan")) or {}
        value = str(checkpoint.get("last_scanned_artist") or "").strip()
        return value or None
    except Exception as exc:
        logger.debug("Resume options: full-scan checkpoint read failed", error=str(exc))
        return None


def next_artist_after(artist: str, artists: list[str] | None = None) -> str | None:
    """The artist following ``artist`` in the scan's library order.

    The full scan iterates ``get_all_artists()`` as returned, so the "next"
    artist must come from the same list. Matching is punctuation/case tolerant
    (``_artist_key``) because a stored name can differ from the checkpoint's.
    """
    try:
        ordered = artists if artists is not None else get_all_artists()
    except Exception as exc:
        logger.debug("Resume options: artist list read failed", error=str(exc))
        return None

    wanted = _artist_key(artist)
    if not wanted:
        return None
    for index, name in enumerate(ordered):
        if _artist_key(name) == wanted:
            return ordered[index + 1] if index + 1 < len(ordered) else None
    return None


def artist_scan_state(artist: str, statuses: dict[str, str] | None = None) -> str:
    """``"completed"``, ``"interrupted"`` or ``"unknown"`` for one artist."""
    table = statuses if statuses is not None else _most_recent_status_by_artist()
    status = table.get(artist)
    if status == "completed":
        return "completed"
    if status in ("started", "running", "stop_requested"):
        return "interrupted"
    return "unknown"


def resolve_resume_target(artist: str) -> dict[str, Any]:
    """Where a resume that *chose* ``artist`` should actually begin.

    Returns ``{artist, resume_from, mode, reason}`` where ``mode`` is
    ``"next"`` (the chosen artist had finished, so continue after it) or
    ``"restart"`` (it was interrupted, so re-scan it from the top). A
    ``"next"`` with no following artist falls back to restarting the chosen one
    so the run still does something rather than silently no-opping.
    """
    chosen = str(artist or "").strip()
    if not chosen:
        return {"artist": "", "resume_from": None, "mode": "none",
                "reason": "No artist selected."}

    state = artist_scan_state(chosen)
    if state == "completed":
        following = next_artist_after(chosen)
        if following:
            return {
                "artist": chosen,
                "resume_from": following,
                "mode": "next",
                "reason": f"'{chosen}' was fully scanned — continuing from '{following}'.",
            }
        return {
            "artist": chosen,
            "resume_from": chosen,
            "mode": "restart",
            "reason": f"'{chosen}' was the last artist in the library — re-scanning it.",
        }

    if state == "interrupted":
        return {
            "artist": chosen,
            "resume_from": chosen,
            "mode": "restart",
            "reason": f"'{chosen}' was interrupted part way — restarting that artist.",
        }

    return {
        "artist": chosen,
        "resume_from": chosen,
        "mode": "restart",
        "reason": f"No completion record for '{chosen}' — starting at that artist.",
    }


def get_resume_options(manual_limit: int = _DEFAULT_MANUAL_LIMIT) -> dict[str, Any]:
    """Build the resume picker's option list.

    Ordering: the interrupted full scan's artist first, then the most recently
    touched artists (manually scanned, or with albums scanned), excluding any
    already listed.
    """
    try:
        statuses = _most_recent_status_by_artist()
        counts = _album_counts()
        artists = get_all_artists()
    except Exception as exc:
        logger.warning("Resume options unavailable", error=str(exc))
        return {"success": False, "error": str(exc), "has_options": False,
                "artists": [], "recommended": None}

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(name: str, source: str) -> None:
        clean = str(name or "").strip()
        if not clean:
            return
        key = _artist_key(clean)
        if not key or key in seen:
            return
        seen.add(key)

        state = artist_scan_state(clean, statuses)
        target = resolve_resume_target(clean)
        entries.append({
            "artist": clean,
            "source": source,
            "state": state,
            "album_count": int(counts.get(clean) or 0),
            "resume_from": target.get("resume_from"),
            "mode": target.get("mode"),
            "reason": target.get("reason"),
            "in_library": any(_artist_key(a) == key for a in artists),
        })

    checkpoint_artist = _full_scan_checkpoint_artist()
    if checkpoint_artist:
        _add(checkpoint_artist, "full_scan")

    for name in statuses:
        if len([e for e in entries if e["source"] == "recent"]) >= manual_limit:
            break
        if _artist_key(name) in seen:
            continue
        _add(name, "recent")

    # Only the resumed artist is offered from the checkpoint; the rest come from
    # recent activity. Nothing to choose between when the list is empty.
    recommended = entries[0]["artist"] if entries else None

    return {
        "success": True,
        "has_options": bool(entries),
        "recommended": recommended,
        "artists": entries,
        "full_scan_artist": checkpoint_artist,
    }
