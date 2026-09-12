"""Staged popularity scan runner with completeness checks."""

from __future__ import annotations

import json
import math
import time
import re
import socket
import threading
import concurrent.futures
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

import structlog
from sqlalchemy import text

socket.setdefaulttimeout(30.0)

from db.engine import db_session
from db.repositories.popularity_cache import upsert_track_popularity_bulk
from db.repositories.tracks import DeferredPersistSink, upsert_tracks_bulk

from helpers.config_helpers import (
    get_config, 
    get_feature, 
    get_prefetch_budget_seconds, 
    get_track_timeout_seconds
)
from helpers.logging_config import log_unified
from helpers.normalization_service import strip_featured_artist

from api_clients.discogs import DiscogsClient
from api_clients.musicbrainz_http import MusicBrainzHttpClient
from api_clients.listenbrainz import get_recording_tags_batch

from services.catalog.album_classification_service import (
    detect_live_album_type,
    is_bonus_track_title,
    is_instrumental_track_title,
    should_exclude_track_from_stats,
)
from services.metadata.album_name_update_service import apply_album_name_update, resolve_album_name
from services.metadata.album_tag_sync_service import sync_album_file_tags
from services.popularity.popularity_cache_policy import should_freeze_track
from services.popularity.popularity_cache_service import prefetch_artist_popularity
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_math import (
    ALBUM_RELATIVE_MIN_ALBUM_TRACKS,
    apply_album_relative_popularity,
    apply_track_artist_relative_popularity,
    reanchor_scores_to_album_relative,
)
from services.popularity.popularity_sources import (
    get_lastfm_artist_max_listeners,
    get_listenbrainz_album_tracklist_with_release,
)
from services.popularity.progress_tracker import finish, start, update
from services.popularity.release_cache_service import (
    get_artist_promo_titles,
    get_artist_single_titles,
    populate_missing_release_tracklists,
    prefetch_artist_releases,
    refresh_missing_releases_for_artist,
)
from services.popularity.scan_hooks import (
    apply_context_fields_to_track,
    get_stat_eligible_tracks,
    prepare_tracks_for_album,
)
from services.popularity.stages.album_stage import enrich_album, enrich_album_extras, ensure_album_type
from services.popularity.stages.finalise_stage import (
    _create_essential_m3u,
    _create_genre_top_track_playlists,
    _essential_playlists_enabled,
    _essential_strip_guest_credit,
    _fetch_essential_featured_rows,
    finalise_scan,
    post_album_star_ratings,
    prune_genre_playlists_for_deletion,
    refresh_genre_playlists_for_album,
)
from services.popularity.stages.load_stage import load_candidates
from services.popularity.stages.track_stage import process_track
from services.scanning.scan_history_service import record_scan, was_album_scanned
from services.scanning.scan_state import (
    get_scan_progress_path,
    is_stop_requested,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
)
from services.enrichment.single_detection_context_service import get_artist_lastfm_context
from services.enrichment.cover_detection_service import detect_covers_for_album

logger = structlog.get_logger(__name__)


def is_album_incomplete(tracks: list[dict[str, Any]]) -> tuple[bool, str]:
    """Check if an album needs a rerun due to missing fields or unpopulated genres."""
    if not tracks:
        return True, "no tracks found"

    required_single_fields = ("final_score", "musicbrainz_albumtype")
    
    for track in tracks:
        genres = str(track.get("genres") or "").strip()
        mb_genres = track.get("musicbrainz_genres")
        discogs_genres = track.get("discogs_genres")
        lastfm_tags = track.get("lastfm_tags")

        has_any_source_genre = any(
            g not in (None, "", [], {}, "null", "none") 
            for g in (mb_genres, discogs_genres, lastfm_tags)
        )
        
        if not genres or not has_any_source_genre:
            return True, f"track '{track.get('title')}' is missing genres/tags"

        for field in required_single_fields:
            val = track.get(field)
            if val is None or val == "" or (isinstance(val, (int, float)) and val <= 0):
                return True, f"track '{track.get('title')}' missing {field}"

        mbid = str(track.get("recording_mbid") or track.get("mbid") or "").strip()
        if not mbid:
            return True, f"track '{track.get('title')}' missing recording MBID"

    return False, ""


def _sanitize_release_name(album_name: str) -> str:
    if not album_name:
        return ""
    cleaned = re.sub(
        r'\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]', 
        '', 
        album_name, 
        flags=re.IGNORECASE
    ).strip()
    return cleaned if cleaned else album_name


def _is_comp_artist(artist_name: str) -> bool:
    if not artist_name:
        return False
    return artist_name.strip().lower() in (
        "various artists", "various artists -", "various", 
        "compilation", "soundtrack"
    )


def _resolve_scan_type(options: dict[str, Any]) -> str:
    if options.get("metadata_only"):
        return "metadata"
    if options.get("singles_only") or options.get("singles_with_missing_popularity"):
        return "singles"
    if options.get("popularity_only"):
        return "popularity"
    return "combined"


def _album_release_is_old(tracks: list[dict[str, Any]] | None, now=None) -> bool:
    try:
        age_months = int(get_feature("old_album_age_months", 48) or 48)
    except Exception:
        age_months = 48
    if age_months <= 0:
        return False
    try:
        years = []
        for _t in tracks or []:
            _y = _t.get("year") or _t.get("release_year")
            if _y:
                try:
                    years.append(int(str(_y)[:4]))
                except (TypeError, ValueError):
                    continue
        if not years:
            return False
        _min_year = min(years)
        _now = now or datetime.now()
        _age_months = (_now.year - _min_year) * 12 + _now.month
        return _age_months >= age_months
    except Exception:
        return False


def _load_mb_single_titles(artist: str) -> set[str]:
    if not artist:
        return set()
    try:
        titles = set()
        with db_session() as session:
            result = session.execute(
                text(
                    "SELECT title FROM missing_releases "
                    "WHERE LOWER(artist) = LOWER(:artist) AND LOWER(COALESCE(category, '')) = 'single'"
                ),
                {"artist": artist},
            )
            titles.update(str(row[0]).strip().lower() for row in result.fetchall() or [] if row[0])
        return titles
    except Exception:
        return set()


def _load_discogs_single_titles(artist: str) -> set[str]:
    if not artist:
        return set()
    try:
        return get_artist_single_titles(artist, source="discogs") or set()
    except Exception:
        return set()


def _load_discogs_promo_titles(artist: str) -> set[str]:
    if not artist:
        return set()
    try:
        return get_artist_promo_titles(artist, source="discogs") or set()
    except Exception:
        return set()


def _close_artist_essential_section(artist_name: str | None, options: dict[str, Any], done: set[str], featured_rows: list | None) -> tuple[set[str], list | None]:
    if not artist_name or options.get("metadata_only"):
        return done, featured_rows
    artist_name = _essential_strip_guest_credit(artist_name) or artist_name
    key = str(artist_name).strip().casefold()
    if not key or key in done or not _essential_playlists_enabled(options):
        return done, featured_rows
    try:
        if featured_rows is None:
            featured_rows = _fetch_essential_featured_rows()
        _create_essential_m3u(artist_name, featured_rows=featured_rows)
        done.add(key)
    except Exception:
        pass
    return done, featured_rows


def _run_album_cover_detection(artist: str, album: str, tracks: list[dict[str, Any]], options: dict[str, Any]) -> None:
    if not artist or not tracks:
        return
    if options.get("singles_only") or options.get("singles_with_missing_popularity") or options.get("popularity_only"):
        return
    try:
        if bool(get_feature("cover_detection_enabled", True)):
            detect_covers_for_album(album=album, artist=artist, tracks=tracks, conn=None, force=bool(options.get("force")))
    except Exception:
        pass


def _execute_track_jobs_safely(track_jobs, max_workers, artist, album) -> list[dict[str, Any] | None]:
    results = [None] * len(track_jobs)
    if not track_jobs:
        return results

    def _run_single(job_tuple):
        _prepared, _tc, _opts, _frozen = job_tuple
        try:
            return process_track(
                track=_prepared,
                track_context=_tc,
                album_context=_opts.get("album_context", {}),
                album_result=_opts.get("album_result", {}),
                options=_opts,
                album_lb_listens=_opts.get("album_lb_listens"),
                artist_max_lf_listeners=_opts.get("artist_max_lf_listeners", 0),
                artist_lf_context=_opts.get("artist_lf_context", {}),
                album_tracks=_opts.get("album_tracks", []),
                mb_cached_singles=_opts.get("mb_cached_singles", set()),
                discogs_cached_singles=_opts.get("discogs_cached_singles", set()),
                discogs_cached_promos=_opts.get("discogs_cached_promos", set()),
                prefetched_popularity=_opts.get("prefetched_popularity", {}),
            )
        except Exception as exc:
            logger.error(f"[TRACK-WORKER] Error processing track: {exc}")
            return None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, 
        thread_name_prefix="track_worker"
    ) as pool:
        future_to_idx = {pool.submit(_run_single, job): i for i, job in enumerate(track_jobs)}
        for future in concurrent.futures.as_completed(future_to_idx.keys()):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception:
                pass
    return results


def run_scan(
    *,
    verbose: bool = False,
    resume_from: str | None = None,
    artist_filter: str | None = None,
    album_filter: str | None = None,
    skip_header: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
    metadata_only: bool = False,
    clear_single_detection_sources: list | None = None,
    stop_progress_file: str | None = None,
    caller_scan_type: str | None = None,
    **extra_kwargs: Any,
) -> dict[str, Any]:

    options = {
        "verbose": verbose,
        "resume_from": resume_from,
        "artist_filter": artist_filter,
        "album_filter": album_filter,
        "skip_header": skip_header,
        "force": force,
        "filter_missing": filter_missing,
        "singles_only": singles_only,
        "singles_with_missing_popularity": singles_with_missing_popularity,
        "popularity_only": popularity_only,
        "metadata_only": metadata_only,
        "clear_single_detection_sources": clear_single_detection_sources,
        "stop_progress_file": stop_progress_file,
        "caller_scan_type": caller_scan_type,
        **extra_kwargs,
    }

    _deferred_persist = DeferredPersistSink()

    update(stage="loading", progress=3, message="Loading scan candidates...")
    albums = load_candidates(options)
    total_albums = len(albums)

    start(total_items=total_albums)

    if not albums:
        log_unified("Popularity Scan - No candidate tracks/albums were loaded from the library.")
        update(stage="complete", progress=100, message="No albums to scan.", processed=0, total_items=0)
        finish(success=True)
        return {"success": True, "albums_processed": 0, "tracks_processed": 0}

    log_unified("=" * 80)
    log_unified(f"🚀 LIBRARY SCAN ({total_albums} Album(s) Queued)")
    log_unified("=" * 80)

    albums_processed = 0
    tracks_processed = 0
    skipped_albums = 0
    results: list[dict[str, Any]] = []
    scan_type = _resolve_scan_type(options)
    _singles_pass = bool(options.get("singles_only") or options.get("singles_with_missing_popularity"))

    _scan_threads = 4
    try:
        _scan_threads = int(((get_config().get("popularity") or {}).get("scan_threads") or 4))
    except Exception:
        pass
    _scan_threads = max(1, min(_scan_threads, 8))

    artist_lf_context_cache: dict[str, dict[str, Any]] = {}
    artist_mb_singles_cache: dict[str, set[str]] = {}
    artist_discogs_singles_cache: dict[str, set[str]] = {}
    artist_discogs_promo_cache: dict[str, set[str]] = {}
    artist_all_tracks: dict[str, list[dict[str, Any]]] = {}

    for _cand in albums or []:
        _cand_artist = str(_cand.get("artist") or "")
        if _cand_artist:
            artist_all_tracks.setdefault(_cand_artist, []).extend(_cand.get("tracks") or [])

    last_prefetch_artist: str | None = None
    prefetched_popularity: dict[str, dict[str, Any]] = {}
    effective_stop_file = stop_progress_file or extra_kwargs.get("progress_file")
    _essential_featured_rows: list | None = None
    _essential_playlists_done: set[str] = set()
    _section_artist: str | None = None

    for album_index, album_row in enumerate(albums, start=1):
        if effective_stop_file and is_stop_requested(effective_stop_file):
            log_unified("Scan stopped by user request")
            finish(success=False)
            return False

        artist = album_row.get("artist") or ""
        album = album_row.get("album") or ""
        tracks = album_row.get("tracks") or []

        if artist and artist != _section_artist:
            _section_artist = artist

        _mode_meta = bool(options.get("metadata_only"))
        _mode_pop = bool(options.get("popularity_only"))
        _mode_singles = bool(options.get("singles_only") or options.get("singles_with_missing_popularity"))
        _album_is_old = _album_release_is_old(tracks)
        skip_album = False
        force_metadata_for_this_album = False

        if not force and not album_filter:
            try:
                skip_days = int(get_feature("album_skip_days", 7) or 0)
                if _album_is_old:
                    skip_days = int(get_feature("album_old_album_skip_days", 30) or 0)
            except Exception:
                skip_days = 7

            if skip_days > 0:
                if was_album_scanned(artist, album, scan_type, skip_days):
                    skip_album = True
                elif get_feature("skip_unchanged_albums", True) and tracks and not _mode_meta:
                    if all(float(t.get("final_score") or 0) > 0 for t in tracks):
                        skip_album = True

            # Override skip if tracks are missing core properties or genres
            if skip_album:
                incomplete, reason = is_album_incomplete(tracks)
                if incomplete:
                    skip_album = False
                    force_metadata_for_this_album = True
                    log_unified(f"Popularity Scan - Album recently scanned but incomplete — forcing rerun ({reason})")

        if skip_album:
            skipped_albums += 1
            continue

        progress = 5 + int((album_index / total_albums) * 90)
        current_item = f"{artist} - {album}"

        try:
            album_context, track_contexts = prepare_tracks_for_album(
                artist=artist,
                album=album,
                tracks=tracks,
                album_artist=album_row.get("album_artist"),
                spotify_album_type=album_row.get("spotify_album_type"),
                musicbrainz_album_type=album_row.get("musicbrainz_album_type"),
            )
            stat_eligible_tracks = get_stat_eligible_tracks(track_contexts)
        except Exception as exc:
            logger.warning("Album prep failed", artist=artist, album=album, error=str(exc))
            albums_processed += 1
            continue

        try:
            record_scan(scan_type, "started", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)

            album_result = enrich_album(
                album_row=album_row,
                album_context=album_context,
                stat_eligible_tracks=stat_eligible_tracks,
                options=options,
            ) or {}

            track_dicts = [tc["track"] for tc in track_contexts if tc.get("track")]

            if artist and artist not in artist_lf_context_cache and not _is_comp_artist(artist):
                try:
                    artist_lf_context_cache[artist] = get_artist_lastfm_context(artist, None, None)
                except Exception:
                    artist_lf_context_cache[artist] = {"mean": 0, "stdev": 0, "total": 0, "values": []}
            artist_lf_context = artist_lf_context_cache.get(artist) or {}

            _track_jobs = []
            for track_context in track_contexts:
                prepared_track = apply_context_fields_to_track(track_context)
                _frozen = False
                if not options.get("force") and should_freeze_track(prepared_track):
                    _frozen = True

                _track_options = dict(options)
                _track_options["_deferred_persist"] = _deferred_persist
                _track_options["album_context"] = album_context
                _track_options["album_result"] = album_result
                _track_options["artist_lf_context"] = artist_lf_context
                _track_options["album_tracks"] = track_dicts
                if force_metadata_for_this_album:
                    _track_options["force_metadata"] = True
                if _frozen:
                    _track_options["frozen_track"] = True

                _track_jobs.append((prepared_track, track_context, _track_options, _frozen))

            _track_results_ordered = _execute_track_jobs_safely(
                track_jobs=_track_jobs, 
                max_workers=_scan_threads, 
                artist=artist, 
                album=album
            )

            for track_result in _track_results_ordered:
                if track_result is not None:
                    results.append(track_result)
                tracks_processed += 1

            _run_album_cover_detection(artist=artist, album=album, tracks=tracks, options=options)
            record_scan(scan_type, "completed", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)

        except Exception as _album_exc:
            logger.warning("Album failed completely", artist=artist, album=album, error=str(_album_exc))
            record_scan(scan_type, "failed", message=f"Album failed: {_album_exc}", artist=artist, album=album)

        albums_processed += 1

    update(stage="finalising", progress=98, message="Finalising popularity scan...", processed=total_albums, total_items=total_albums)

    if not metadata_only:
        try:
            finalise_scan(results=results, options=options)
        except Exception as _finalise_exc:
            logger.warning("Finalise failed", error=str(_finalise_exc))

    update(stage="complete", progress=100, message="Popularity scan complete.", processed=total_albums, total_items=total_albums)
    finish(success=True)

    return {
        "success": True,
        "albums_processed": albums_processed,
        "albums_skipped": skipped_albums,
        "tracks_processed": tracks_processed,
    }
