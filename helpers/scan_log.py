"""Unified scan-log events, heartbeat, warning aggregation and result blocks.

This is the presentation layer for the scan log. It deliberately does NOT change
any matching/scoring/genre/playlist rule; it only gives the pipeline one place
to emit a stable event name, structured context and readable wording.

Normal level (INFO) answers: what is running, what completed, what was saved and
which playlists changed. ``log_scan_detail`` keeps the decision evidence in
DEBUG, next to the progress line it belongs to.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

#: Context keys whose VALUES are never written to the log.
_SENSITIVE_KEY_RE = re.compile(
    r"token|password|secret|api[_-]?key|authorization|cookie|session", re.I
)
_URL_CREDENTIALS_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*\S+"
)


def _redact_inline(text: str) -> str:
    text = _URL_CREDENTIALS_RE.sub("://***@", text)
    return _INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)

_INFO = "info"
_WARN = "warn"
_ERROR = "error"
_DEBUG = "debug"


def _safe_value(value: Any) -> str:
    text = repr(value) if isinstance(value, str) else str(value)
    if len(text) > 300:
        text = text[:297] + "..."
    text = _redact_inline(text)
    return text.replace("\n", " ").replace("\r", " ")


def _context_suffix(event: str, context: dict[str, Any]) -> str:
    parts: list[str] = []
    if event:
        parts.append(f"event={event}")
    for key, value in context.items():
        if value is None or value == "":
            continue
        name = str(key)
        if _SENSITIVE_KEY_RE.search(name):
            value = "***"
        parts.append(f"{name}={_safe_value(value)}")
    return " (" + ", ".join(parts) + ")" if parts else ""


def log_event(
    event: str,
    message: str,
    *,
    level: str = _INFO,
    debug: bool = False,
    **context: Any,
) -> None:
    """Emit one stable scan event, fail-safe.

    ``debug=True`` routes to ``log_scan_detail`` (DEBUG only). INFO/WARN/ERROR
    lines go to the unified log with a bracketed severity prefix; the event name
    and context ride at the end so the file stays greppable and readable.
    """
    try:
        from helpers.logging_config import (
            log_scan_detail,
            log_unified,
            log_unified_level,
        )

        prefix = "[SCAN]"
        if debug:
            prefix += " [DEBUG]"
        elif level == _WARN:
            prefix += " [WARN]"
        elif level == _ERROR:
            prefix += " [ERROR]"
        line = f"{prefix} {message}{_context_suffix(event, context)}"
        if debug:
            log_scan_detail(line)
        elif level == _INFO:
            log_unified(line)
        else:
            log_unified_level(line, level=level)
    except Exception as exc:  # logging must never break a scan
        logger.debug("Scan event emit failed", event=event, error=str(exc))


def log_evidence(event: str, message: str, **context: Any) -> None:
    """Shorthand for a DEBUG decision-evidence line."""
    log_event(event, message, debug=True, **context)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

def scan_started(
    *,
    scope: str,
    mode: str,
    albums: int,
    tracks: int,
    scan_id: str,
    force: bool = False,
) -> None:
    log_event(
        "scan.started",
        f"Starting scan: {scope or 'library'}",
        mode=mode,
        albums=int(albums or 0),
        tracks=int(tracks or 0),
        scan_id=scan_id,
        force=bool(force),
    )


def album_started(
    *,
    index: int,
    total: int,
    album: str,
    tracks: int,
    artist: str = "",
) -> None:
    log_event(
        "album.started",
        f"[{int(index or 0)}/{int(total or 0)}] {album} · {int(tracks or 0)} tracks",
        artist=artist or None,
    )


def track_started(
    title: str,
    *,
    artist: str = "",
    album: str = "",
    track_id: str = "",
    config: dict[str, Any] | None = None,
) -> None:
    cfg = config or _logging_config()
    if not cfg["progress"]["show_track_starts"]:
        return
    log_event(
        "track.started",
        f"Checking: {title}",
        artist=artist or None,
        album=album or None,
        track_id=track_id or None,
    )


def track_checked(
    *,
    index: int,
    total: int,
    title: str,
    preserved: bool = False,
    score: float | None = None,
    lastfm_score: float | None = None,
    listenbrainz_score: float | None = None,
    track_id: str = "",
    config: dict[str, Any] | None = None,
) -> None:
    cfg = config or _logging_config()
    if cfg["progress"]["show_track_completions"]:
        verb = "Preserved" if preserved else "Checked"
        suffix = " · popularity frozen" if preserved else ""
        log_event(
            "track.preserved" if preserved else "track.checked",
            f"{verb} {int(index or 0)}/{int(total or 0)}: {title}{suffix}",
            track_id=track_id or None,
        )
    # Provisional values are DEBUG only — they are pre-reconciliation.
    log_evidence(
        "track.provisional_score",
        f"Provisional score: {title}",
        score=score,
        lastfm_score=lastfm_score,
        listenbrainz_score=listenbrainz_score,
        track_id=track_id or None,
    )


def scan_completed(
    *,
    scope: str,
    outcome: str,
    albums: int,
    tracks: int,
    preserved: int = 0,
    failed: int = 0,
    duration_s: float | None = None,
    playlists: dict[str, int] | None = None,
) -> None:
    from helpers.scan_report import format_duration

    outcome = str(outcome or "completed")
    duration = (
        f" in {format_duration(duration_s)}" if duration_s is not None else ""
    )
    lines = [f"Finished: {scope or 'library'}{duration} — {outcome}"]
    lines.append(
        f"  {int(albums or 0)} albums · {int(tracks or 0)} tracks processed · "
        f"{int(preserved or 0)} preserved · {int(failed or 0)} failed"
    )
    if playlists:
        lines.append(
            "  Playlists: "
            f"{int(playlists.get('created') or 0)} created · "
            f"{int(playlists.get('updated') or 0)} updated · "
            f"{int(playlists.get('unchanged') or 0)} unchanged · "
            f"{int(playlists.get('skipped') or 0)} skipped · "
            f"{int(playlists.get('partial') or 0)} partial · "
            f"{int(playlists.get('failed') or 0)} failed"
        )
    for index, line in enumerate(lines):
        log_event(
            "scan.completed",
            line,
            outcome=outcome if index == 0 else None,
        )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class ScanHeartbeat:
    """One coordinator heartbeat for quiet, long-running work.

    Emits after ``heartbeat_seconds`` without ``touch()``, repeating while the
    operation remains active. It never decides that work is "stalled" — it only
    proves activity monitoring is alive. ``stop()`` is safe from any thread and
    from the completion/cancellation/failure paths.

    ``quiet`` is the time since the last visible progress; ``total`` is the
    lifetime of this heartbeat. They are reported separately because the old
    line labelled the quiet interval as "elapsed", so a scan that had run for
    minutes appeared to reset to 15s on every heartbeat.
    """

    def __init__(
        self,
        *,
        interval: float | None = None,
        activity: Callable[[], str | None] | str | None = None,
        config: dict[str, Any] | None = None,
    ):
        cfg = config or _logging_config()
        self._interval = max(
            1.0, float(interval or cfg["progress"]["heartbeat_seconds"] or 15)
        )
        self._activity = activity
        self._started_at = time.monotonic()
        self._last_progress = self._started_at
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_activity(self, activity: Callable[[], str | None] | str | None) -> None:
        with self._lock:
            self._activity = activity

    def touch(self) -> None:
        self._last_progress = time.monotonic()

    def start(self) -> "ScanHeartbeat":
        if self._thread and self._thread.is_alive():
            return self
        self.touch()
        self._thread = threading.Thread(
            target=self._run, name="scan-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        wait_for = max(0.2, self._interval / 2.0)
        while not self._stop.wait(wait_for):
            quiet = time.monotonic() - self._last_progress
            if quiet < self._interval:
                continue
            with self._lock:
                activity = self._activity
            try:
                label = activity() if callable(activity) else activity
            except Exception:
                label = None
            label = str(label or "working")
            if not label.lower().startswith(("waiting", "checking", "still")):
                label = f"checking {label}"
            total = time.monotonic() - self._started_at
            log_event(
                "scan.heartbeat",
                f"Still {label}... · no new result for {int(quiet)}s "
                f"· total elapsed {_format_elapsed(total)}",
                quiet_seconds=round(quiet, 1),
                elapsed_seconds=round(total, 1),
                activity=label,
            )
            self._last_progress = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.2, min(2.0, self._interval)))


# ---------------------------------------------------------------------------
# Warning aggregation
# ---------------------------------------------------------------------------

class WarningAggregator:
    """Keep the first detailed warning, aggregate repeats, report recovery."""

    def __init__(self, *, every: int = 20):
        self._every = max(1, int(every or 20))
        self._counts: dict[str, int] = {}
        self._messages: dict[str, str] = {}
        self._lock = threading.Lock()

    def warn(self, key: str, message: str, **context: Any) -> None:
        with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            self._messages.setdefault(key, message)
        if count == 1:
            log_event(key, message, level=_WARN, **context)
        elif count % self._every == 0:
            log_event(
                key,
                f"{message} · repeated {count} times (intermediate repeats suppressed)",
                level=_WARN,
                **context,
            )

    def recovered(self, key: str, message: str, **context: Any) -> None:
        with self._lock:
            count = self._counts.pop(key, 0)
            self._messages.pop(key, None)
        if count:
            log_event(key, f"{message} · recovered after {count} warning(s)", **context)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._messages.clear()

    def summary(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def emit_summary(self) -> dict[str, int]:
        with self._lock:
            pending = list(self._counts.items())
            messages = dict(self._messages)
        for key, count in pending:
            log_event(
                key,
                f"{messages.get(key, key)} · {count} occurrence(s) unresolved",
                level=_WARN,
                occurrences=count,
            )
        return dict(pending)


#: Scan-wide warning aggregator: repeated database/provider failures keep the
#: first detail, report periodic affected counts and a final unresolved total.
SCAN_WARNINGS = WarningAggregator()


# ---------------------------------------------------------------------------
# Playlist outcomes
# ---------------------------------------------------------------------------

_PLAYLIST_TALLY: dict[str, int] = {
    "created": 0,
    "updated": 0,
    "unchanged": 0,
    "skipped": 0,
    "partial": 0,
    "failed": 0,
}
_PLAYLIST_LOCK = threading.Lock()
_PLAYLIST_BATCH = threading.local()


def set_playlist_batch(enabled: bool) -> None:
    """Mark the current thread as a batch refresh (summary, not per-name)."""
    _PLAYLIST_BATCH.enabled = bool(enabled)


def in_playlist_batch() -> bool:
    return bool(getattr(_PLAYLIST_BATCH, "enabled", False))


def reset_playlist_outcomes() -> None:
    with _PLAYLIST_LOCK:
        for key in _PLAYLIST_TALLY:
            _PLAYLIST_TALLY[key] = 0


def playlist_outcome_summary() -> dict[str, int]:
    with _PLAYLIST_LOCK:
        return dict(_PLAYLIST_TALLY)


def emit_playlist_summary(*, config: dict[str, Any] | None = None) -> dict[str, int]:
    counts = playlist_outcome_summary()
    if any(int(v or 0) for v in counts.values()):
        log_event(
            "playlist.summary",
            "Playlist refresh complete: "
            f"{counts['created']} created · {counts['updated']} updated · "
            f"{counts['unchanged']} unchanged · {counts['skipped']} skipped · "
            f"{counts['partial']} partial · {counts['failed']} failed",
            **counts,
        )
    return counts


def playlist_outcome(
    name: str,
    sync_res: dict[str, Any] | None,
    *,
    track_count: int = 0,
    config: dict[str, Any] | None = None,
) -> str:
    """Record and report one playlist sync result. Returns the outcome name."""
    cfg = config or _logging_config()
    res = sync_res or {}
    created = int(res.get("created") or 0)
    updated = int(res.get("updated") or 0)
    skipped = int(res.get("skipped") or 0)
    failed = int(res.get("failed") or 0)
    partial = int(res.get("partial") or 0)
    unresolved = len(res.get("unresolved_ids") or [])

    if failed:
        outcome = "failed"
    elif created:
        outcome = "created"
    elif updated:
        outcome = "updated"
    elif skipped:
        outcome = "skipped"
    elif partial:
        # In sync with its RESOLVED subset, but known-unresolved IDs are
        # awaiting a Navidrome import. This is NOT a server rejection: the
        # old mapping reported every such playlist as `failed` on every scan.
        outcome = "partial"
    else:
        outcome = "unchanged"

    with _PLAYLIST_LOCK:
        _PLAYLIST_TALLY[outcome] = _PLAYLIST_TALLY.get(outcome, 0) + 1

    count = f" · {int(track_count or 0)} tracks" if track_count else ""
    if outcome == "created":
        if cfg["playlists"]["show_created"]:
            log_event(
                "playlist.created",
                f'Created playlist: "{name}"{count}',
                track_count=int(track_count or 0),
            )
        else:
            log_evidence(
                "playlist.created", f'Created playlist: "{name}"{count}'
            )
    elif outcome == "failed":
        reason = f"{failed} client failure(s)"
        if cfg["playlists"]["show_failed"]:
            log_event(
                "playlist.failed",
                f'Could not update playlist: "{name}" · Reason: {reason}',
                level=_WARN,
                failed=failed,
            )
        else:
            log_evidence(
                "playlist.failed",
                f'Could not update playlist: "{name}" · Reason: {reason}',
            )
    elif outcome == "partial":
        # Debug evidence, not a WARN: the playlist matches its resolvable
        # subset and is waiting for Navidrome to index the missing track(s).
        detail = (
            f" · {unresolved} track(s) awaiting Navidrome import"
            if unresolved
            else ""
        )
        log_evidence(
            "playlist.partial",
            f'Playlist partially synced: "{name}"{count}{detail}',
            unresolved=unresolved,
        )
    elif outcome == "updated":
        # A batch refresh (scan/global rebuild) reports one summary; a single
        # manual Regenerate names the playlist it changed.
        if cfg["playlists"]["show_updated"] and not in_playlist_batch():
            log_event(
                "playlist.updated",
                f'Updated playlist: "{name}"{count}',
                track_count=int(track_count or 0),
            )
        else:
            log_evidence(
                "playlist.updated", f'Updated playlist: "{name}"{count}'
            )
    elif outcome == "skipped":
        log_evidence("playlist.skipped", f'Skipped playlist: "{name}"{count}')
    else:
        if cfg["playlists"]["show_unchanged_individually"]:
            log_event("playlist.unchanged", f'Playlist unchanged: "{name}"')
        else:
            log_evidence("playlist.unchanged", f'Playlist unchanged: "{name}"')
    return outcome


# ---------------------------------------------------------------------------
# Final results / genres
# ---------------------------------------------------------------------------

def _row_genres(row: dict[str, Any]) -> list[str]:
    raw = row.get("genres")
    if isinstance(raw, (list, tuple, set)):
        values = [str(v).strip() for v in raw]
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                import json

                parsed = json.loads(text)
                values = (
                    [str(v).strip() for v in parsed]
                    if isinstance(parsed, list)
                    else [text]
                )
            except Exception:
                values = [part.strip() for part in text.split(",")]
        else:
            values = [part.strip() for part in text.split(",")]
    out: list[str] = []
    for value in values:
        if value and value.casefold() not in {v.casefold() for v in out}:
            out.append(value)
    return out


def _genre_text(genres: list[str]) -> str:
    return ", ".join(genres)


def album_final_results(
    album: str,
    rows: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    show_genres: bool | None = None,
) -> None:
    """Emit the one authoritative per-album result block.

    Ratings are finalised values. Genre display collapses to a shared list when
    every row agrees, otherwise an album default plus accurate per-track
    exceptions; unresolved rows are called out rather than implied to inherit.
    """
    try:
        from helpers.logging_config import log_unified

        cfg = (config or _logging_config())["results"]
        if not rows:
            return
        ordered = sorted(
            rows,
            key=lambda r: (
                -int(r.get("stars") or 0),
                -float(r.get("final_score") or r.get("popularity_score") or 0),
                str(r.get("title") or "").casefold(),
            ),
        )

        def _emit_lines(lines: list[str]) -> None:
            for line in lines:
                # The Scan panel's filter keys off bracketed scan prefixes; keep
                # every presentation line visible there without changing it.
                log_unified(f"[SCAN] {line}" if line else "[SCAN]")

        lines: list[str] = []
        if cfg["show_final_ratings"]:
            lines = ["", f"Final results: {album}", ""]
            for row in ordered:
                title = str(row.get("title") or "?")
                stars = int(row.get("stars") or 0)
                score = float(
                    row.get("final_score") or row.get("popularity_score") or 0
                )
                flags: list[str] = []
                if row.get("popularity_frozen") or row.get("_frozen"):
                    flags.append("preserved")
                suffix = f" · {', '.join(flags)}" if flags else ""
                lines.append(f"  {stars}★  {score:5.1f}  {title}{suffix}")
                if (
                    cfg["genre_display"] == "per_track"
                    and cfg["show_final_genres"]
                ):
                    lines.append(
                        "          Genres: "
                        f"{_genre_text(_row_genres(row)) or 'none resolved'}"
                    )
            _emit_lines(lines)
            log_event(
                "album.final_results",
                f"Final results emitted for {album} ({len(ordered)} tracks)",
                tracks=len(ordered),
            )

        if (
            show_genres is False
            or not cfg["show_final_genres"]
            or cfg["genre_display"] == "none"
            or cfg["genre_display"] == "per_track"
        ):
            return
        genre_lines: list[str] = []
        genre_sets = [_row_genres(row) for row in ordered]
        non_empty = [g for g in genre_sets if g]
        if not non_empty:
            genre_lines.append("Final genres: none resolved")
        else:
            texts = [_genre_text(g) for g in non_empty]
            if len(set(texts)) == 1 and all(
                _genre_text(g) == texts[0] for g in genre_sets if g
            ):
                genre_lines.append(
                    f"Final genres for all {len(ordered)} tracks: {texts[0]}"
                )
            else:
                counts: dict[str, int] = {}
                for text in texts:
                    counts[text] = counts.get(text, 0) + 1
                default = max(counts, key=counts.get)
                genre_lines.append(f"Album genre defaults: {default}")
                exceptions: list[str] = []
                for row, genres in zip(ordered, genre_sets):
                    text = _genre_text(genres)
                    if text == default:
                        continue
                    exceptions.append(
                        f"  {row.get('title') or '?'}: "
                        f"{text or 'no genres resolved'}"
                    )
                if exceptions:
                    genre_lines.append("Track genre differences:")
                    genre_lines.extend(exceptions)
        _emit_lines(genre_lines)
    except Exception as exc:
        logger.debug("Final results block failed", album=album, error=str(exc))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _logging_config() -> dict[str, Any]:
    try:
        from helpers.config_helpers import get_scan_logging_config

        return get_scan_logging_config()
    except Exception:
        return {
            "progress": {
                "show_track_starts": True,
                "show_track_completions": True,
                "heartbeat_seconds": 15,
            },
            "results": {
                "show_final_ratings": True,
                "show_final_genres": True,
                "genre_display": "defaults_with_exceptions",
                "show_provisional_scores": False,
            },
            "playlists": {
                "show_created": True,
                "show_updated": True,
                "show_failed": True,
                "show_unchanged_individually": False,
            },
            "diagnostics": {"provider_payloads": False},
        }
