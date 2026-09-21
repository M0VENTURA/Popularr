"""Formatted scan section report for the unified scan log.

WHY THIS EXISTS
---------------
The unified scan log used to be a stream of low-level instrumentation:
``[MB] call started`` / ``[MB] call completed`` for every MusicBrainz request,
``[ENRICH] section started`` / ``section completed`` for ~25 enrichment
sections per album, ``[SCAN] section started``, plus a ``[TRACK] ▶ Processing``
line per track. A single album produced several hundred lines, so the shape of
the scan — which album, which stage, what it found — was impossible to see
without scrolling past the request tracing.

This module emits a READABLE SECTION REPORT instead:

    ══════════════════════════════════════════════════════════════════════
    🎵 ARTIST SCAN STARTED
    ══════════════════════════════════════════════════════════════════════

    Artist: Silent Civilian
    Albums Found: 2
    Mode: Forced Scan

    ──────────────────────────────────────────────────────────────────────
    ALBUM 1 OF 2
    Ghost Stories
    ──────────────────────────────────────────────────────────────────────

    📈 COMMENCING POPULARITY SCAN
    ...
    ✅ POPULARITY SCAN COMPLETE

The detailed instrumentation is NOT deleted — it is gated behind debug logging
via ``helpers.logging_config.log_scan_detail`` / ``debug_enabled``, so
``logging.level: debug`` in config.yaml restores every line inline for
troubleshooting a stalled or mis-scoring scan.

DESIGN NOTE
-----------
The report is emitted through the SAME ``log_unified`` channel as before, so
the dashboard scanning panel, ``UnifiedLogFilter`` and the
``services/log_service._scan_activity_filter`` allow-list all keep working
unchanged. The banner characters are plain box-drawing glyphs; no filter
matches on them, so a report line is kept by the ``[POPULARITY]``/scan
keywords the sections carry.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: The heavy horizontal rule that frames a scan and its album blocks.
RULE = "═" * 70
#: The lighter rule used inside an album block.
SUB_RULE = "─" * 70


def _emit(line: str) -> None:
    """Write one report line to the unified scan log."""
    try:
        from helpers.logging_config import log_unified

        log_unified(line)
    except Exception as exc:  # never let reporting break a scan
        logger.debug("Scan report line failed", error=str(exc))


def _emit_many(lines: list[str]) -> None:
    for line in lines:
        _emit(line)


def blank() -> None:
    """Emit a single blank line (visual separation between sections)."""
    _emit("")


def scan_started(*, artist: str, albums: int, mode: str) -> None:
    """Frame the start of an artist/album scan run.

    ``artist`` may be empty for a full-library scan, in which case the banner
    reads LIBRARY SCAN and no Artist line is emitted.
    """
    artist = str(artist or "").strip()
    title = f"🎵 ARTIST SCAN STARTED" if artist else "🎵 LIBRARY SCAN STARTED"
    lines = [RULE, title, RULE, ""]
    if artist:
        lines.append(f"Artist: {artist}")
    lines.append(f"Albums Found: {int(albums or 0)}")
    lines.append(f"Mode: {mode}")
    lines.append("")
    _emit_many(lines)


def album_started(*, index: int, total: int, album: str) -> None:
    """Frame one album inside a scan run."""
    _emit_many([
        SUB_RULE,
        f"ALBUM {int(index or 0)} OF {int(total or 0)}",
        str(album or ""),
        SUB_RULE,
        "",
    ])


def album_summary(
    *,
    album: str,
    tracks_processed: int,
    metadata_corrections: int = 0,
    genres_added: int = 0,
    genres_removed: int = 0,
    singles_detected: int = 0,
    star_counts: dict[int, int] | None = None,
    playlists_updated: int = 0,
    duration_s: float | None = None,
) -> None:
    """Emit the closing ALBUM SUMMARY block."""
    counts = star_counts or {}
    lines = [
        SUB_RULE,
        "",
        "📊 ALBUM SUMMARY",
        str(album or ""),
        "",
        f"Tracks Processed: {int(tracks_processed or 0)}",
        f"Metadata Corrections: {int(metadata_corrections or 0)}",
        "",
        f"Genres Added: {int(genres_added or 0)}",
        f"Genres Removed: {int(genres_removed or 0)}",
        "",
        f"Singles Detected: {int(singles_detected or 0)}",
        "",
        "Ratings",
    ]
    for stars in (5, 4, 3, 2, 1):
        lines.append(f"{stars}★: {int(counts.get(stars, 0) or 0)}")
    lines.append("")
    lines.append(f"Playlists Updated: {int(playlists_updated or 0)}")
    if duration_s is not None:
        lines.extend(["", f"Duration: {format_duration(duration_s)}"])
    lines.append("")
    lines.append(RULE)
    _emit_many(lines)


def scan_complete(*, artist: str, albums: int, duration_s: float | None = None) -> None:
    """Close an artist/album scan run."""
    lines = ["", RULE, "✅ SCAN COMPLETE", RULE, ""]
    if artist:
        lines.insert(1, f"Artist: {artist}")
    lines.insert(2, f"Albums Processed: {int(albums or 0)}")
    if duration_s is not None:
        lines.insert(3, f"Duration: {format_duration(duration_s)}")
    lines.append("")
    _emit_many(lines)


def stage_started(*, emoji: str, name: str) -> None:
    """Announce a scan stage (POPULARITY SCAN, SINGLE DETECTION, …)."""
    _emit_many([f"{emoji} COMMENCING {name}", ""])


def stage_complete(*, name: str, stats: dict[str, Any] | None = None) -> None:
    """Close a scan stage, with optional ``Label: value`` statistics."""
    _emit("✅ " + name)
    _emit("")
    for label, value in (stats or {}).items():
        _emit(f"{label}: {value}")
    _emit_many(["", SUB_RULE, ""])


def list_section(*, heading: str, items: list[str] | None = None) -> None:
    """Emit a ``heading`` followed by a ``• item`` list (or ``• None``)."""
    _emit(heading)
    for item in (items or ["None"]):
        _emit(f"• {item}")
    _emit("")


def star_breakdown(star_counts: dict[int, int] | None) -> None:
    """Emit the ★-prefixed per-track rating list, grouped highest first."""
    counts = star_counts or {}
    for stars in (5, 4, 3, 2, 1):
        for title in counts.get(stars, []) if isinstance(counts.get(stars), list) else []:
            _emit(f"{'★' * stars} {'☆' * (5 - stars)} {title}")
        _emit("")


def format_duration(seconds: float) -> str:
    """Format a duration as ``1m 23s`` (or ``45s`` under a minute)."""
    try:
        total = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        return "0s"
    minutes, secs = divmod(max(0, total), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"
