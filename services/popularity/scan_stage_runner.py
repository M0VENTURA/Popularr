"""
Popularity pipeline entrypoints.

Stable WebUI/scheduler entrypoint for popularity scans.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable

import structlog

from services.popularity.progress_tracker import update
from services.scanning.scan_state import (
    write_progress_with_current_artist,
    clear_stop_request,
)

logger = structlog.get_logger(__name__)

DEFAULT_SCANNER_MODULE = "services.popularity.scan_stage_runner"


# =============================================================================
# Errors
# =============================================================================

class PopularityPipelineError(RuntimeError):
    pass


# =============================================================================
# Scanner resolution
# =============================================================================

def _load_scanner_module():
    module_name = os.environ.get(
        "POPULARITY_STAGE_RUNNER_MODULE",
        DEFAULT_SCANNER_MODULE,
    )

    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        import traceback
        try:
            from helpers.logging_config import log_unified
            err_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            for line in err_str.splitlines():
                if line.strip():
                    log_unified(f"[POPULARITY] FATAL IMPORT ERROR: {line}")
        except Exception:
            pass
            
        raise PopularityPipelineError(
            f"Could not import scanner module '{module_name}'"
        ) from exc


def _resolve_scanner_callable(scanner_module) -> Callable[..., Any]:
    """Return the scan entry point from a loaded scanner module."""
    staged = getattr(scanner_module, "run_scan", None)
    if staged:
        return staged

    scan_fn = getattr(scanner_module, "popularity_scan", None)
    if scan_fn:
        return scan_fn

    raise PopularityPipelineError(
        f"{scanner_module.__name__} has no valid scan entry point."
    )


# =============================================================================
# Core entrypoint
# =============================================================================

def _reload_config_before_scan() -> None:
    """Drop the process-level config cache so this scan re-reads config.yaml.

    Hand-edited ``config.yaml`` (or UI changes saved outside this process)
    now take effect on the NEXT scan instead of requiring a container
    restart.  Cheap and idempotent — the next ``get_config()`` re-reads.
    """
    try:
        from helpers.config_helpers import clear_config_cache
        clear_config_cache()
    except Exception as exc:
        logger.debug("Config reload skipped", error=str(exc))


def _log_scan_config() -> None:
    """Emit every applied scan setting to the unified log at scan start."""
    try:
        from helpers.logging_config import log_unified
        from helpers.config_helpers import get_config, get_genre_weights

        cfg = get_config() or {}
        sd = cfg.get("single_detection") or {}
        if not isinstance(sd, dict):
            sd = {}

        def _src(section, key=None) -> str:
            if not isinstance(section, dict):
                return "defaults"
            if key is None:
                return "config" if section else "defaults"
            return "config" if key in section else "defaults"

        # ── single_detection (z bands, marking, floors) ────────────────
        from services.popularity.popularity_config import (
            resolve_weights,
            get_zscore_thresholds,
            get_single_boost,
            get_metadata_score_floor,
            get_live_weight_penalty,
            get_instrumental_weight_penalty,
            get_single_organic_floor,
        )
        z = get_zscore_thresholds(cfg)
        org_score, org_listeners = get_single_organic_floor(cfg)
        lf, lb, age = resolve_weights(cfg)
        log_unified(
            f"📋 SCAN CONFIG — single_detection ({_src(sd)}): "
            f"z_hi={z['high']:g} z_med={z['medium']:g} gap={z['standout_gap_z']:g} | "
            f"artist_top={float(sd.get('artist_top_percentile') or 0.10):g} "
            f"large={float(sd.get('artist_top_percentile_large') or 0.25):g}(>{int(sd.get('artist_catalog_large_threshold') or 30)}) "
            f"med_bump={float(sd.get('artist_medium_bump_percentile') or 0.20):g} | "
            f"organic={org_score:g}/{org_listeners:g} | "
            f"listener_z={float(sd.get('listener_5star_z_threshold') or 1.0):g} | "
            f"eps={float(sd.get('star_epsilon_score_points') or 0.5):g} "
            f"boost={get_single_boost(cfg):g} floor={get_metadata_score_floor(cfg):g} "
            f"live_pen={get_live_weight_penalty(cfg):g} "
            f"inst_pen={get_instrumental_weight_penalty(cfg):g}"
        )

        # ── star tiers + album-scaling era rules ────────────────────────
        from services.popularity.stages.finalise_stage import (
            _live_star_thresholds,
            _live_album_scaling,
        )
        th = _live_star_thresholds()
        era_rules, _, _ = _live_album_scaling()
        _era_fmt = " ".join(
            f"{era}(top={int(float(r['catalog_top_pct']) * 100)}%, n={r['album_top_n']}, "
            f"max{r['max_5star_slots']})"
            for era, r in era_rules.items()
        )
        log_unified(
            f"📋 SCAN CONFIG — star tiers: 5★(album_z={th['star5_album_z']:g}, "
            f"artist_z={th['star5_artist_z']:g}) 4★(album_z={th['star4_album_z']:g}) "
            f"3★(album_z={th['star3_album_z']:g}) 2★(album_z={th['star2_album_z']:g}) | "
            f"era: {_era_fmt}"
        )

        # ── popularity + genre weights ──────────────────────────────────
        _pop_w = cfg.get("popularity") or {}
        _pop_weights = _pop_w.get("weights") if isinstance(_pop_w, dict) else None
        _pop_src = "config" if isinstance(_pop_weights, dict) and _pop_weights else "defaults"
        _g_w = get_genre_weights() or {}
        _genres_cfg = (cfg.get("genres") or {}).get("weights") if isinstance(cfg.get("genres"), dict) else None
        _g_src = "config" if isinstance(_genres_cfg, dict) and _genres_cfg else "defaults"
        _g_fmt = " ".join(f"{k}={float(v):g}" for k, v in _g_w.items())
        log_unified(
            f"📋 SCAN CONFIG — weights ({_pop_src}): LF={lf:g} LB={lb:g} Age={age:g} | "
            f"genres ({_g_src}): {_g_fmt}"
        )

        # ── single-detection source confidence + playlists ──────────────
        try:
            from services.enrichment.single_detection_service import _source_confidence_levels
            _levels = _source_confidence_levels()
            _lv_fmt = " ".join(f"{k}={v}" for k, v in _levels.items())
        except Exception:
            _lv_fmt = "unavailable"
        _pl = cfg.get("playlists") or {}
        _pl_cfg = _pl if isinstance(_pl, dict) else {}
        log_unified(
            f"📋 SCAN CONFIG — sources: {_lv_fmt} | playlists ({_src(_pl_cfg)}): "
            f"essential={'on' if _pl_cfg.get('essential_playlists_enabled', True) else 'off'} "
            f"featured={'on' if _pl_cfg.get('essential_include_featured', True) else 'off'} "
            f"genre={'create' if _pl_cfg.get('genre_playlists_enabled', True) else 'off'}"
            f"/{'delete' if _pl_cfg.get('genre_playlists_delete_enabled', True) else 'off'} "
            f"(min={int(_pl_cfg.get('genre_playlists_min_stars', 4))}★, "
            f"per={int(_pl_cfg.get('genre_playlists_max_genres', 3))}, "
            f"create>{int(_pl_cfg.get('genre_playlists_create_threshold', 100))}, "
            f"delete<{int(_pl_cfg.get('genre_playlists_delete_threshold', 80))})"
        )
    except Exception as exc:
        logger.debug("Scan config dump skipped", error=str(exc))


def run_popularity_scan(
    *,
    verbose: bool = False,
    resume_from: str | None = None,
    artist_filter: str | None = None,
    album_filter: str | None = None,
    force: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
    metadata_only: bool = False,
    progress_file: str | None = None,
    caller_scan_type: str | None = None,
    **extra_kwargs: Any,
):
    """Run the popularity scan pipeline. Entry point for CLI, WebUI, and scheduler."""
    from helpers.logging_config import log_unified
    _reload_config_before_scan()
    log_unified(f"[POPULARITY_PIPELINE] Starting scan (artist={artist_filter or 'ALL'}, verbose={verbose}, force={force}) — config.yaml reloaded")
    _log_scan_config()

    # ✅ CLEAR STALE STOP FLAGS
    if progress_file:
        try:
            clear_stop_request(progress_file)
        except Exception as e:
            logger.warning("Failed to clear stop request flag", error=str(e))

    _scan_type_label = _derive_progress_scan_type(
        metadata_only=metadata_only,
        singles_only=singles_only,
        singles_with_missing_popularity=singles_with_missing_popularity,
        popularity_only=popularity_only,
    )
    
    if progress_file:
        try:
            write_progress_with_current_artist(
                progress_file,
                _scan_type_label,
                True,
                current_artist=artist_filter,
                extra={
                    "status": "running",
                    "mode": _scan_type_label,
                    "force": force,
                },
            )
        except Exception as e:
            logger.debug("Failed to mark scan running in DB state", error=str(e))

    update(stage="initialising", progress=1, message="Starting popularity scan...")

    scanner_module = _load_scanner_module()
    scanner = _resolve_scanner_callable(scanner_module)

    kwargs = {
        "verbose": verbose,
        "resume_from": resume_from,
        "artist_filter": artist_filter,
        "album_filter": album_filter,
        "force": force,
        "singles_only": singles_only,
        "singles_with_missing_popularity": singles_with_missing_popularity,
        "popularity_only": popularity_only,
        "metadata_only": metadata_only,
        "progress_file": progress_file,
        "caller_scan_type": caller_scan_type,
    }

    kwargs.update(extra_kwargs)

    log_unified(f"Running popularity scan via {scanner_module.__name__}")

    try:
        result = scanner(**kwargs)

        if result is False or (isinstance(result, dict) and result.get("status") == "stopped"):
            update(stage="stopped", message="Scan stopped by user")
            log_unified("[POPULARITY] Scan stopped by user request")
            from services.popularity.progress_tracker import finish as _tracker_finish
            _tracker_finish(success=False)
        else:
            update(stage="complete", progress=100, message="Scan complete")
            log_unified("[POPULARITY] Scan complete")

        if progress_file:
            try:
                write_progress_with_current_artist(
                    progress_file,
                    _scan_type_label,
                    False,
                    current_artist=artist_filter,
                    extra={
                        "status": "complete" if result is not False else "stopped",
                        "mode": _scan_type_label,
                        "exit_code": 0,
                    },
                )
            except Exception as e:
                logger.debug("Failed to mark scan complete in DB state", error=str(e))

        return result
    except Exception:
        update(stage="failed", message="Scan failed")
        log_unified("[POPULARITY] Scan failed")
        from services.popularity.progress_tracker import finish as _tracker_finish
        _tracker_finish(success=False)

        if progress_file:
            try:
                write_progress_with_current_artist(
                    progress_file,
                    _scan_type_label,
                    False,
                    current_artist=artist_filter,
                    extra={
                        "status": "error",
                        "mode": _scan_type_label,
                        "error": "Scan failed",
                        "exit_code": 1,
                    },
                )
            except Exception as e:
                logger.debug("Failed to mark scan failed in DB state", error=str(e))

        raise


def _derive_progress_scan_type(
    *,
    metadata_only: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
) -> str:
    """Map runner options to the progress-file scan-type label."""
    if metadata_only:
        return "metadata_lookup_scan"
    if singles_only or singles_with_missing_popularity:
        return "singles_scan"
    return "popularity_scan"


# =============================================================================
# Artist entrypoint
# =============================================================================

def run_popularity_from_artist(
    *,
    artist: str,
    force_rescan: bool = False,
    progress_file: str | None = None,
    verbose: bool = False,
):
    _reload_config_before_scan()
    logger.info("Starting popularity scan from artist", artist=artist)
    
    from helpers.logging_config import log_unified
    log_unified(f"Starting popularity scan from artist '{artist}'")
    _log_scan_config()

    if progress_file:
        write_progress_with_current_artist(
            progress_file,
            "popularity_scan",
            True,
            current_artist=artist,
            extra={
                "status": "running",
                "stop_requested": False,  # ✅ Overwrite any lingering stop state
                "resume_from": artist,
                "processed_artists": 0,
                "total_artists": 0,
                "percent_complete": 0,
            },
        )

    try:
        completed = run_popularity_scan(
            verbose=verbose,
            force=force_rescan,
            resume_from=artist,
            progress_file=progress_file,
            caller_scan_type="popularity",
        )

        if progress_file:
            payload = {
                "resume_from": artist,
            }

            if completed is False or (isinstance(completed, dict) and completed.get("status") == "stopped"):
                payload["status"] = "stopped"
                payload["exit_code"] = 1
                log_unified(f"Scan stopped for '{artist}'")
            else:
                payload["status"] = "complete"
                payload["exit_code"] = 0
                payload["percent_complete"] = 100
                log_unified(f"Scan complete for '{artist}'")

            write_progress_with_current_artist(
                progress_file,
                "popularity_scan",
                False,
                current_artist=artist,
                extra=payload,
            )

        return completed

    except Exception as exc:
        logger.error("Scan failed for artist", artist=artist, error=str(exc), exc_info=True)

        if progress_file:
            write_progress_with_current_artist(
                progress_file,
                "popularity_scan",
                False,
                current_artist=artist,
                extra={
                    "status": "error",
                    "resume_from": artist,
                    "error": str(exc),
                    "exit_code": 1,
                },
            )

        raise


# =============================================================================
# Convenience wrappers
# =============================================================================

def run_metadata_only_scan(**kwargs: Any):
    kwargs["metadata_only"] = True
    return run_popularity_scan(**kwargs)


def run_popularity_only_scan(**kwargs: Any):
    kwargs["popularity_only"] = True
    return run_popularity_scan(**kwargs)
