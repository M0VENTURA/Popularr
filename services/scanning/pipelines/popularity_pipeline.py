"""
Popularity/metadata/singles scan pipeline helpers.

Uses scan_state as the single source of truth for:
- progress tracking
- stop requests

Routes should call this module rather than building scan kwargs inline.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from services.popularity.pipeline import run_popularity_scan
from helpers.logging_config import log_unified
from services.scanning.scan_history_service import record_scan

from services.scanning.scan_state import (
    get_scan_progress_path,
    write_progress_with_current_artist,
    is_stop_requested,
)

logger = structlog.get_logger(__name__)


def is_popularity_scan_active() -> bool:
    """Return True when a popularity-family scan is already running.

    Checks BOTH the in-process runtime registry and the shared DB scan state.
    The runtime registry is per-worker, so in a multi-worker (hypercorn)
    deployment a manual scan started in worker A is invisible to worker B —
    the shared ``ScanState`` row is the cross-process source of truth.  This
    prevents a scheduled popularity scan from overlapping a manual one (and
    vice versa): both write to the SAME ``popularity_scan`` progress row, so
    whichever finishes first marks the shared state complete while the other
    is still running, which makes a full scan look like it halted mid-letter.
    """
    from services.scanning.runtime_state import is_runtime_running

    if is_runtime_running("popularity"):
        return True

    try:
        from services.scanning.scan_state import read_progress_file

        # The dashboard "All" scan runs under the "full_scan" progress row;
        # targeted popularity scans use "popularity_scan".  Check both so a
        # scan running in another hypercorn worker is never double-started.
        for _scan_type in ("popularity_scan", "full_scan"):
            state = read_progress_file(get_scan_progress_path(_scan_type))
            if state.get("is_running"):
                return True
    except Exception:
        return False
    return False


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def run_popularity_mode(
    *,
    mode: str,
    progress_file: str | None = None,
    force_rescan: bool = False,
    resume_from: str | None = None,
) -> None:
    """
    Run a popularity-related scan mode.

    Supported modes:
        metadata
        popularity
        singles
        singles_detection
        all
    """

    if mode == "all":
        # Dashboard "All" = full scan aligned with the artist page: iterate
        # over every artist, running the full artist pipeline (Navidrome
        # import → metadata → combined → essentia) for each, and report
        # progress as % of total artists completed.
        _run_full_scan_as_artist_pipeline(
            force=force_rescan,
            resume_from=resume_from,
        )
        return

    progress_file = progress_file or get_scan_progress_path("popularity_scan")

    try:
        scan_type = "popularity_scan"

        kwargs: dict[str, Any] = {
            "verbose": False,
            "force": force_rescan,
        }

        if resume_from:
            kwargs["resume_from"] = resume_from

        # ---------------------------------------------------------------------
        # Mode mapping
        # ---------------------------------------------------------------------
        if mode == "metadata":
            scan_type = "metadata_lookup_scan"
            kwargs["metadata_only"] = True

        elif mode == "singles":
            scan_type = "singles_scan"
            kwargs["singles_only"] = True

        elif mode == "singles_detection":
            scan_type = "singles_scan"
            kwargs["singles_with_missing_popularity"] = True

        elif mode == "popularity":
            # True popularity-only scan: scores popularity and rates tracks on
            # popularity alone (5★ reserved for standout popularity tracks).
            # No singles detection, metadata or cover work.
            scan_type = "popularity_scan"
            kwargs["popularity_only"] = True

        else:
            logger.warning(
                "Unknown popularity scan mode — defaulting to full scan",
                mode=mode,
            )
            scan_type = "full_scan"

        # ---------------------------------------------------------------------
        # Mark scan start
        # ---------------------------------------------------------------------
        write_progress_with_current_artist(
            progress_file,
            scan_type,
            True,
            extra={
                "status": "starting",
                "mode": mode,
                "force": force_rescan,
            },
        )

        # ---------------------------------------------------------------------
        # Execute pipeline (correct entry point ✅)
        # ---------------------------------------------------------------------
        completed = run_popularity_scan(
            progress_file=progress_file,
            **kwargs,
        )

        # ---------------------------------------------------------------------
        # Determine final status
        # ---------------------------------------------------------------------
        stopped = is_stop_requested(progress_file)

        if stopped:
            status = "stopped"
        elif completed is False:
            status = "failed"
        else:
            status = "complete"

        # ---------------------------------------------------------------------
        # Mark scan completion
        # ---------------------------------------------------------------------
        write_progress_with_current_artist(
            progress_file,
            scan_type,
            False,
            extra={
                "status": status,
                "mode": mode,
                "exit_code": 0,
            },
        )

        log_unified(f"{scan_type} finished with status={status}")
        record_scan(mode, status, message=f"{mode} scan {status}", artist="_SCAN_SESSION_", album=mode)

    except Exception as exc:
        logger.exception("Popularity pipeline failed", mode=mode, error=str(exc))
        record_scan(mode, "failed", message=f"{mode} scan failed: {exc}", artist="_SCAN_SESSION_", album=mode)

        write_progress_with_current_artist(
            progress_file or get_scan_progress_path("popularity_scan"),
            "popularity_scan",
            False,
            extra={
                "status": "error",
                "error": str(exc),
                "exit_code": 1,
            },
        )

        raise


# =============================================================================
# FULL SCAN (dashboard "All") — aligned with the artist page pipeline
# =============================================================================

def _run_full_scan_as_artist_pipeline(
    *,
    force: bool = False,
    resume_from: str | None = None,
) -> None:
    """Dashboard 'All' scan: iterate over every artist, running the full
    artist pipeline (Navidrome import → metadata → combined → essentia) for
    each, and report progress as % of total artists completed.

    This aligns the dashboard's combined scan with the artist page's full
    scan — the same per-artist pipeline runs for every artist in the library,
    and the footer shows the percentage of total artists completed.
    """
    from db.repositories.library import get_all_artists
    from services.scanning.pipeline import run_artist_scan_pipeline
    from services.popularity.stages.load_stage import _artist_key

    progress_file = get_scan_progress_path("full_scan")

    # ── Clear STALE stop flags before starting ───────────────────────────
    # A previous "Stop" click leaves ``scan_states.stop_requested=True`` on
    # this scan type.  Nothing cleared it when a NEW "All" scan started, so
    # the loop's first ``is_stop_requested(full_scan)`` check immediately
    # halted — after doing exactly ONE artist (the resume point): the
    # reported "resume only does the current artist and stops", and "any
    # scan after Stop is immediately stopped again".
    try:
        from services.scanning.scan_state import clear_stop_request
        clear_stop_request(progress_file)
    except Exception as exc:
        logger.debug("[FULL_SCAN] Stop-flag clear failed", error=str(exc))

    try:
        artists = get_all_artists()
    except Exception as exc:
        log_unified(f"[FULL_SCAN] Failed to load artist list: {exc}")
        logger.exception("[FULL_SCAN] get_all_artists failed", error=str(exc))
        record_scan("all", "failed", message=f"full scan failed: {exc}", artist="_SCAN_SESSION_", album="all")
        # The full_scan progress row was NEVER marked running here (the
        # failure is before ``write_progress(... True)``), but a PREVIOUS
        # crashed scan may have left it running — or the row may carry a
        # stale running flag from an interrupted attempt.  Clear it so the
        # dashboard doesn't show "still running" and the next scan isn't
        # blocked by is_popularity_scan_active().
        try:
            from services.scanning import scan_state as _scan_state
            _scan_state.write_progress_with_current_artist(
                progress_file,
                "full_scan",
                False,
                extra={"status": "failed", "mode": "all", "exit_code": 1, "error": str(exc)},
            )
        except Exception as _clear_exc:
            logger.debug("[FULL_SCAN] Failed to clear full_scan progress row", error=str(_clear_exc))
        return
    total = len(artists)

    log_unified(
        f"[FULL_SCAN] Starting full scan — {total} artist(s) queued"
        f"{' (forced)' if force else ''}"
    )
    if not artists:
        log_unified(
            "[FULL_SCAN] No artists found in the library — nothing to scan. "
            "Check the library has been imported (Navidrome sync)."
        )

    # Honour resume_from (legacy parity): skip artists before the resume
    # point, tolerating case/punctuation variants.
    if resume_from:
        _resume_key = _artist_key(resume_from)
        _started = False
        _filtered: list[str] = []
        for _a in artists:
            if not _started:
                if (
                    _a.lower() == resume_from.lower()
                    or _artist_key(_a) == _resume_key
                    or (_resume_key and len(_resume_key) >= 3 and _resume_key in _artist_key(_a))
                ):
                    _started = True
            if _started:
                _filtered.append(_a)
        artists = _filtered
        total = len(artists)

    write_progress_with_current_artist(
        progress_file,
        "full_scan",
        True,
        extra={
            "status": "starting",
            "mode": "all",
            "force": force,
            "total_artists": total,
            "processed_artists": 0,
            "percent_complete": 0,
            # Mirror the artist counters onto the *_items keys the dashboard
            # actually renders, so the counter shows "0/80" from the very first
            # frame instead of "0/?".  `total` is known here (the artist list
            # has been loaded), so there is no reason to leave it unknown.
            "total_items": total,
            "processed_items": 0,
            "current_stage": "Metadata",
        },
    )

    status = "complete"
    # Per-artist failure / abandonment records, keyed by artist name, carried
    # on the full_scan progress row so the dashboard can show an investigation
    # banner (the reported need: when an artist is abandoned after the budget,
    # surface the reason instead of silently continuing).
    _abandoned_artists: dict[str, list[dict[str, Any]]] = {}
    # Stage bands for the overall percentage.  Each artist contributes an
    # equal share; within an artist the four stages (Metadata, Popularity,
    # Singles Detection, Essentia) each take a quarter of that share.  The
    # artist pipeline now runs ONE combined pass (the standalone metadata
    # pass was removed — it re-scraped every API the combined pass scrapes);
    # the pass's album loop is split so the dashboard shows "Metadata" for
    # the first quarter of albums, "Popularity" for the middle half and
    # "Singles Detection" for the last quarter (each album genuinely runs
    # metadata resolution → popularity → singles in that order).  With a
    # 4-album artist this gives 1 album into metadata = ~6%, metadata done
    # = 25%, popularity done = 75%, etc.
    _STAGE_IDX = {"metadata": 0, "popularity": 1, "singles": 2, "essentia": 3}
    _STAGE_LABEL = {
        "metadata": "Metadata",
        "popularity": "Popularity",
        "singles": "Singles Detection",
        "essentia": "Essentia",
    }
    try:
        for i, artist in enumerate(artists):
            if is_stop_requested(progress_file):
                status = "stopped"
                log_unified("[FULL_SCAN] Stop requested — halting artist loop")
                break

            artist_base = (i / total) * 100.0 if total else 100.0
            artist_share = (100.0 / total) if total else 100.0
            stage_width = artist_share / 4.0

            def _cb(stage, idx, t, item, track_fraction=None, _i=i, _base=artist_base, _sw=stage_width, _artist=artist):
                try:
                    si = _STAGE_IDX.get(stage, 0)
                    # Album-boundary callback: fraction is (idx+1)/total.  A
                    # per-track callback carries ``track_fraction`` (0..1) so
                    # the percentage advances WITHIN the album band instead of
                    # freezing until the next album starts.
                    if track_fraction is not None:
                        frac = min(1.0, max(0.0, float(track_fraction)))
                    else:
                        frac = min(1.0, (int(idx) + 1) / float(t)) if t else 1.0
                    overall = int(_base + si * _sw + frac * _sw)
                    # The final callback for the last artist's last stage must
                    # land exactly on 100% — float truncation (e.g. 99.97 → 99)
                    # would otherwise leave the footer at 99% after completion.
                    if (
                        _i == total - 1
                        and si == len(_STAGE_IDX) - 1
                        and frac >= 1.0
                    ):
                        overall = 100
                    overall = max(0, min(100, overall))

                    # ── Write throttle ─────────────────────────────────────
                    # The runner fires this callback per track now (live
                    # ``current_item`` + ``track_fraction``), so writing the
                    # DB row on EVERY per-track call would spam one
                    # session+commit per track (10k+ commits on a large
                    # library).  Album-boundary / stage-transition callbacks
                    # (no ``track_fraction``) always persist — they are rare
                    # and carry the authoritative % step.  Per-track
                    # callbacks persist at most once every 2s, and the final
                    # 100% always persists so the footer lands exactly on
                    # completion.
                    _is_final = _i == total - 1 and overall >= 100
                    _is_boundary = track_fraction is None
                    if _is_final or _is_boundary or time.monotonic() - _cb.last_write >= 2.0:
                        write_progress_with_current_artist(
                            progress_file,
                            "full_scan",
                            True,
                            current_artist=_artist,
                            extra={
                                "status": "running",
                                "mode": "all",
                                "percent_complete": overall,
                                "current_stage": _STAGE_LABEL.get(stage, stage),
                                "current_item": item or _artist,
                                "processed_artists": _i,
                                "total_artists": total,
                                # The dashboard renders `processed_items/total_items`
                                # (dashboard.js: `${scan.processed_items ?? 0}/${scan.total_items ?? "?"}`),
                                # NOT the *_artists keys. Writing only the artist
                                # counters is why the panel read "0/?" for the whole
                                # scan. Both spellings are written so the counter
                                # works and `processed_artists` stays available for
                                # the artist-oriented summaries and the abandoned
                                # -artist banner.
                                "processed_items": _i,
                                "total_items": total,
                            },
                        )
                        _cb.last_write = time.monotonic()
                except Exception:
                    pass

            _cb.last_write = 0.0

            log_unified(f"[FULL_SCAN] Artist {i + 1}/{total}: {artist}")
            # Bound each artist's pipeline with a hard wall-clock budget so
            # a HUNG artist (a stuck API/DNS/DB call that the per-track
            # timeouts don't catch) cannot freeze the whole scan.  The
            # reported freeze: the dashboard kept showing the scan running
            # with a live-but-stuck worker thread — the runtime self-heal
            # correctly refuses to clear a live owner, so the scan never
            # recovered.  Abandon the artist after the budget and move on.
            _artist_budget = 1800  # 30 min per artist default
            try:
                from helpers.config_helpers import get_feature
                _artist_budget = int(get_feature("full_scan_artist_timeout_seconds", 1800) or 1800)
            except Exception:
                pass

            def _run_one_artist() -> None:
                run_artist_scan_pipeline(artist, force=force, progress_callback=_cb)

            from services.popularity.scan_stage_runner import _bounded_call_report
            _artist_report = _bounded_call_report(
                _run_one_artist,
                seconds=_artist_budget,
                label=f"artist pipeline '{artist}'",
            )
            if _artist_report.get("ok"):
                log_unified(f"[FULL_SCAN] Artist {i + 1}/{total} done: {artist}")
            else:
                # Record the failure/abandonment reason so the dashboard can
                # show an investigation banner instead of silently moving on.
                _abandoned = bool(_artist_report.get("abandoned"))
                _reason = str(_artist_report.get("reason") or "unknown")
                log_unified(
                    f"[FULL_SCAN] Artist {i + 1}/{total} "
                    f"{'ABANDONED' if _abandoned else 'FAILED'}: {artist} — {_reason}"
                )
                logger.warning(
                    "[FULL_SCAN] Artist %s",
                    "abandoned (budget exceeded)" if _abandoned else "failed",
                    artist=artist,
                    reason=_reason,
                )
                # Persist to the full_scan progress row so /api/scan-progress
                # carries it; the dashboard banner reads scan.abandoned_artists.
                try:
                    _abandoned_list: list[dict[str, Any]] = list(
                        _abandoned_artists.get(artist) or []
                    )
                    _abandoned_artists.setdefault(artist, [])
                    _abandoned_artists[artist].append({
                        "abandoned": _abandoned,
                        "reason": _reason,
                        "budget_seconds": _artist_report.get("budget_seconds") if _abandoned else None,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    write_progress_with_current_artist(
                        progress_file,
                        "full_scan",
                        True,
                        current_artist=artist,
                        extra={
                            "status": "running",
                            "mode": "all",
                            "percent_complete": 5 + int((i / total) * 90),
                            "current_stage": "Metadata",
                            "current_item": f"{artist} (abandoned)",
                            "abandoned_artists": _abandoned_artists,
                        },
                    )
                except Exception as _rec_exc:
                    logger.debug("[FULL_SCAN] Abandoned-artist record failed", error=str(_rec_exc))

            # Persist the resume checkpoint so a stopped/failed "All" scan can
            # RESUME from this artist next time (unless restart was requested —
            # restart clears the checkpoint in the route before starting).
            try:
                from services.scanning.scan_state import save_artist_scan_checkpoint
                save_artist_scan_checkpoint(artist, progress_file)
            except Exception:
                pass
    except Exception as exc:
        status = "error"
        logger.exception("[FULL_SCAN] Artist loop failed", error=str(exc))
        raise
    finally:
        # A completed full scan clears the checkpoint so the NEXT scan starts
        # from the top (no stale resume point).  A stopped/failed scan keeps it
        # so the next run resumes where it left off.
        try:
            from services.scanning.scan_state import clear_scan_checkpoint
            if status == "complete":
                clear_scan_checkpoint(progress_file)
        except Exception:
            pass
        write_progress_with_current_artist(
            progress_file,
            "full_scan",
            False,
            extra={
                "status": status,
                "mode": "all",
                "exit_code": 0,
                "percent_complete": 100 if status == "complete" else 0,
                "processed_artists": total if status == "complete" else 0,
                "total_artists": total,
                # Keep the rendered *_items counters consistent with the final
                # outcome; otherwise the row is swapped out for the idle view
                # while still showing a stale mid-scan count.
                "processed_items": total if status == "complete" else 0,
                "total_items": total,
            },
        )
        log_unified(f"[FULL_SCAN] Finished with status={status}")
        record_scan("all", status, message=f"full scan {status}", artist="_SCAN_SESSION_", album="all")


# =============================================================================
# HELPERS
# =============================================================================

def _build_targeted_popularity_kwargs(
    *,
    artist: str | None = None,
    album: str | None = None,
    force: bool = False,
    scan_type: str = "popularity",
) -> dict[str, Any]:
    """Build kwargs for targeted artist/album scans."""

    kwargs: dict[str, Any] = {
        "verbose": True,
        "force": force,
        # Targeted scans honour dashboard stop requests: the runner checks
        # is_stop_requested(progress_file) per album, and the dashboard
        # stop-all button flags "popularity_scan".
        "progress_file": "popularity_scan",
    }

    if artist:
        kwargs["artist_filter"] = artist

    if album:
        kwargs["album_filter"] = album

    if scan_type == "metadata":
        kwargs["metadata_only"] = True

    elif scan_type == "singles":
        kwargs["singles_only"] = True

    elif scan_type == "singles_detection":
        kwargs["singles_with_missing_popularity"] = True

    elif scan_type == "popularity":
        # Popularity-only: score + rate on popularity alone, no singles
        # detection / metadata / cover work (matches the dashboard's
        # "Popularity" scan mode).
        kwargs["popularity_only"] = True

    return kwargs


# =============================================================================
# TARGETED SCANS
# =============================================================================

def run_popularity_artist_scan(
    artist: str,
    *,
    force: bool = False,
    scan_type: str = "popularity",
) -> Any:
    """Run a scan for a single artist."""

    kwargs = _build_targeted_popularity_kwargs(
        artist=artist,
        force=force,
        scan_type=scan_type,
    )

    return run_popularity_scan(**kwargs)


def run_popularity_album_scan(
    artist: str,
    album: str,
    *,
    force: bool = False,
    scan_type: str = "popularity",
) -> Any:
    """Run a scan for a single album."""

    kwargs = _build_targeted_popularity_kwargs(
        artist=artist,
        album=album,
        force=force,
        scan_type=scan_type,
    )

    return run_popularity_scan(**kwargs)