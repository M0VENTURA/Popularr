"""Scan history query and recording service.

Provides read/write access to the ``scan_history`` table for WebUI dashboard
display and for recording scan events.

Key Functions:
    - record_scan(): Insert a scan event (start, complete, or failure).
    - get_recent_album_scans(): Fetch recent scan records with timestamps.

Architecture:
    Query and mutation layer for scan_history.  Pipeline code should call
    ``record_scan()`` at start and completion so the dashboard always has
    up-to-date data.
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy import text
import structlog

from db.engine import db_session

logger = structlog.get_logger(__name__)


def record_scan(
    scan_type: str,
    status: str,
    message: str = "",
    artist: str | None = None,
    album: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Record a scan event in the scan_history table.

    Args:
        scan_type: Type of scan (e.g. 'full', 'artist', 'popularity', 'navidrome').
        status: 'started', 'completed', or 'failed'.
        message: Human-readable description or error message.
        artist: Optional artist name this scan was for.
        album: Optional album name this scan was for.
        started_at: When the scan started.  If omitted, defaults to now.
    """
    try:
        with db_session() as session:
            if status == "started":
                session.execute(
                    text("""
                        INSERT INTO scan_history
                            (scan_type, status, message, artist, album, started_at)
                        VALUES (:scan_type, 'started', :message, :artist, :album, :started_at)
                    """),
                    {
                        "scan_type": scan_type,
                        "message": message,
                        "artist": artist,
                        "album": album,
                        "started_at": started_at or datetime.utcnow(),
                    },
                )
            else:
                # Update the most recent 'started' row for this scan_type/artist
                updated = session.execute(
                    text("""
                        UPDATE scan_history
                        SET status = :status,
                            message = :message,
                            completed_at = :completed_at,
                            duration_seconds = EXTRACT(EPOCH FROM (:completed_at - started_at))
                        WHERE id = (
                            SELECT id FROM scan_history
                            WHERE scan_type = :scan_type
                              AND (artist IS NOT DISTINCT FROM :artist)
                              AND status = 'started'
                            ORDER BY started_at DESC NULLS LAST, id DESC
                            LIMIT 1
                        )
                    """),
                    {
                        "scan_type": scan_type,
                        "status": status,
                        "message": message,
                        "artist": artist,
                        "completed_at": datetime.utcnow(),
                    },
                )
                # If NO matching 'started' row exists (e.g. the scan was
                # launched by a path that never recorded 'started', or the
                # started row was written with a different artist), INSERT the
                # completion as its own row so the scan history is never
                # silently lost.  The dashboard renders any non-failed
                # _SCAN_SESSION_ row as a completed session.
                if updated.rowcount == 0:
                    session.execute(
                        text("""
                            INSERT INTO scan_history
                                (scan_type, status, message, artist, album, started_at, completed_at)
                            VALUES (:scan_type, :status, :message, :artist, :album,
                                    :completed_at, :completed_at)
                        """),
                        {
                            "scan_type": scan_type,
                            "status": status,
                            "message": message,
                            "artist": artist,
                            "album": album,
                            "completed_at": datetime.utcnow(),
                        },
                    )
    except Exception as exc:
        logger.exception(
            "Failed to record scan history",
            scan_type=scan_type,
            status=status,
            error=str(exc),
        )


def get_recent_album_scans(limit: int = 50):
    with db_session() as session:
        # NULLS LAST: legacy rows (created under the old ``scan_timestamp``
        # schema) may have NULL ``started_at`` and would otherwise float to the
        # top of a DESC ordering and hide real entries.
        result = session.execute(text("""
            SELECT *
            FROM scan_history
            ORDER BY started_at DESC NULLS LAST, id DESC
            LIMIT :limit
        """), {"limit": limit})

        cols = list(result.keys())

        def _to_iso(value) -> str | None:
            if isinstance(value, datetime):
                return value.isoformat() + ("Z" if value.tzinfo is None else "")
            return value

        scans = []
        for row in result.fetchall() or []:
            record = dict(zip(cols, row))

            # Resolve the best-available timestamp: modern ``started_at``,
            # then legacy ``scan_timestamp`` (kept from old deployments), then
            # ``completed_at``. Exposed as BOTH ``scan_timestamp`` (legacy
            # frontend key) and ``started_at`` so the dashboard always has a
            # parseable value.
            resolved = (
                record.get("started_at")
                or record.get("scan_timestamp")
                or record.get("timestamp")
                or record.get("completed_at")
            )
            resolved_iso = _to_iso(resolved)
            record["scan_timestamp"] = resolved_iso
            if record.get("started_at") is None:
                record["started_at"] = resolved_iso

            # Serialize every datetime value as ISO-8601 so the dashboard JS
            # can parse them (avoids Quart's default RFC 1123 serialization).
            for col in ("started_at", "completed_at", "scan_timestamp", "timestamp"):
                if col in record:
                    record[col] = _to_iso(record.get(col))

            scans.append(record)
        return scans


def was_album_scanned(artist: str, album: str, scan_type: str, days: int = 7) -> bool:
    """Return True when an album was successfully scanned within ``days`` days.

    Mirrors the legacy ``was_album_scanned`` helper used by ``album_skip_days``:
    an album whose most recent scan of the given type completed within the
    window is treated as already scanned and can be skipped.

    ⚠️ TWO STATEMENTS, ON PURPOSE — this is the per-album hot spot.

    The tolerant one wraps ``artist``/``album`` in ``LOWER(COALESCE(...))``,
    and an EXPRESSION predicate cannot use ``idx_scan_history_scope
    (scan_type, artist, album, status, started_at DESC)``: the index can only
    serve the leading ``scan_type`` equality, so every row of that scan type
    has to be read and filtered (and for a type that is a large slice of the
    table the planner seq-scans the WHOLE table instead).  This runs three
    times per album — the skip check plus two popularity-due checks — so on a
    grown ``scan_history`` that was ~45s of dead time between an album's
    header line and its first track.

    The exact match runs FIRST because it constrains the whole index prefix
    plus the range on ``started_at``, so Postgres answers it from the index
    alone.  It almost always hits — the artist/album are the same values the
    ``record_scan`` writer was handed by the same loop iteration — and the
    tolerant statement stays as the fallback, so differing casing or an
    edition-marker rewrite still resolves.

    The cutoff is computed here rather than as ``NOW() - INTERVAL``: it matches
    the ``utcnow()`` timestamps ``record_scan`` stamps, and it parses on every
    dialect (the old form was PostgreSQL-only, so this helper could never be
    exercised by the SQLite test suite).
    """
    if not artist or not album or days <= 0:
        return False

    # Naive UTC — exactly what ``record_scan`` writes into ``started_at``.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(days))
    params = {
        "scan_type": scan_type,
        "artist": artist,
        "album": album,
        "cutoff": cutoff,
    }
    try:
        with db_session() as session:
            exact = session.execute(
                text("""
                    SELECT 1 FROM scan_history
                    WHERE scan_type = :scan_type
                      AND artist = :artist
                      AND album = :album
                      AND status = 'completed'
                      AND started_at > :cutoff
                    LIMIT 1
                """),
                params,
            ).fetchone()
            if exact is not None:
                return True

            tolerant = session.execute(
                text("""
                    SELECT 1 FROM scan_history
                    WHERE scan_type = :scan_type
                      AND LOWER(COALESCE(artist, '')) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                      AND status = 'completed'
                      AND started_at > :cutoff
                    LIMIT 1
                """),
                params,
            ).fetchone()
            return tolerant is not None
    except Exception as exc:
        logger.exception(
            "was_album_scanned query failed",
            artist=artist,
            album=album,
            scan_type=scan_type,
            error=str(exc),
        )
        return False