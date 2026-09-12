"""
Per-track popularity/enrichment stage.

This is the ONLY place that connects:
- enrichment external APIs
- popularity scoring
- single detection
- persistence

Optimized for high-concurrency: heavy text-search fallbacks are gated to prevent
rate-limit exhaustion and 300s+ timeout stalls on large albums.
"""

from __future__ import annotations

import json
import time
import re
from difflib import SequenceMatcher
from typing import Any

import structlog

# API clients
from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import ListenBrainzClient
from api_clients.listenbrainz import get_recording_tags

# Enrichment services
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    get_shared_mb_service,
)

# Popularity
from services.popularity.popularity_math import (
    apply_log_ratio_audit_to_stored_score,
    calculate_combined_popularity_score,
    calculate_listenbrainz_percentile,
    evaluate_listenbrainz_validity,
    evaluate_log_ratio_deviation,
    fmt_count as _fmt_count,
    is_interlude_lb_outlier,
)
from services.popularity.popularity_config import (
    get_interlude_lb_outlier_config,
    get_instrumental_weight_penalty,
    get_live_weight_penalty,
    get_log_ratio_config,
    get_metadata_score_floor,
    get_single_boost,
    resolve_weights,
)

# Provider aggregation helpers
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    get_aggregated_listenbrainz_popularity,
    get_search_aggregated_lastfm_popularity,
    get_work_level_listenbrainz_popularity,
)

# Detection
from services.enrichment.single_detection_service import detect_single_for_track
from services.enrichment.cover_detection_service import detect_cover_song

# Track classification
from services.catalog.album_classification_service import (
    is_bonus_track_title,
    is_instrumental_track_title,
    is_live_or_alternate_track_title,
)

# Genre aggregation
from services.enrichment.genre_aggregation_service import aggregate_genres

# DB & Normalization
from db.repositories.tracks import insert_or_update_track
from helpers.normalization_service import (
    edition_annotations_compatible,
    safe_int,
    safe_str,
)

# Re-fetch threshold provider
from services.popularity.popularity_cache_policy import (
    get_cache_duration_hours,
    should_use_cached_score,
)


logger = structlog.get_logger(__name__)

_SOURCE_LABELS = {
    "discogs": "Discogs",
    "musicbrainz": "MB",
    "musicbrainz_compilation": "MB-Comp",
    "discogs_video": "Video",
    "lastfm": "LF",
    "radio_edit": "Radio",
}


def _has_safe_live_recording_tag(payload: dict[str, Any]) -> bool:
    """Checks for explicit 'live' tags only on MBID-bound metadata sources."""
    for col in ("musicbrainz_genres", "musicbrainz_tags", "listenbrainz_genres"):
        raw = payload.get(col)
        if not raw:
            continue
        if isinstance(raw, str):
            val = raw.lower()
            if "live" not in val:
                continue
            try:
                tags = json.loads(val)
                if any(str(t).strip() == "live" for t in tags):
                    return True
            except Exception:
                if re.search(r'\blive\b', val):
                    return True
        elif isinstance(raw, (list, tuple)):
            if any(str(t).strip().lower() == "live" for t in raw):
                return True
    return False


def _single_chips(sources_raw: Any) -> str:
    """Render the matched/unmatched single-detection sources as chips."""
    try:
        sources = json.loads(sources_raw) if isinstance(sources_raw, str) else (sources_raw or [])
    except Exception:
        sources = []
    chips: list[str] = []
    for s in sources if isinstance(sources, list) else []:
        if not isinstance(s, dict):
            continue
        src = str(s.get("source") or "")
        label = _SOURCE_LABELS.get(src, src)
        chips.append(f"{label}: {'✓' if bool(s.get('matched')) else '✖'}")
    return "[" + ", ".join(chips) + "]" if chips else ""


_as_str = safe_str
_as_int = safe_int


def _safe_duration(value: Any) -> float | None:
    try:
        dur = float(value or 0)
    except (TypeError, ValueError):
        return None
    if dur <= 0:
        return None
    if dur > 600:
        dur = dur / 1000.0
    return dur


def _duration_below_floor(track: dict[str, Any]) -> bool:
    dur = _safe_duration(track.get("duration"))
    if dur is None:
        return False
    return dur < 30.0


def _album_top_genres(
    album_tracks: list[dict[str, Any]] | None,
    *,
    max_genres: int = 3,
) -> list[str]:
    if not album_tracks:
        return []
    from services.enrichment.genre_aggregation_service import aggregate_genres

    album_source_map: dict[str, list[str]] = {}
    _source_cols = [
        ("musicbrainz", "musicbrainz_genres"),
        ("discogs", "discogs_genres"),
        ("lastfm", "lastfm_tags"),
        ("listenbrainz", "listenbrainz_genres"),
        ("spotify", "spotify_genres"),
        ("navidrome", "navidrome_genres"),
    ]
    for _at in album_tracks:
        if not isinstance(_at, dict):
            continue
        for _src, _col in _source_cols:
            raw = _at.get(_col)
            if not raw:
                continue
            if isinstance(raw, str):
                try:
                    _vals = json.loads(raw)
                except Exception:
                    import re as _re
                    _vals = [g.strip() for g in _re.split(r"[,;/\\]+", raw) if g.strip()]
            else:
                _vals = raw
            if not isinstance(_vals, list):
                continue
            for _g in _vals:
                if isinstance(_g, dict):
                    _g = _g.get("name") or ""
                _name = str(_g or "").strip()
                if _name:
                    album_source_map.setdefault(_src, []).append(_name)
    if not album_source_map:
        return []
    try:
        return aggregate_genres(album_source_map, max_genres=max_genres)
    except Exception:
        return []


def _artist_dominant_genres(
    artist: str,
    *,
    max_genres: int = 3,
) -> list[str]:
    if not artist:
        return []
    try:
        from db.engine import db_session as _db_session
        from sqlalchemy import text as _text

        rows: list[dict[str, Any]] = []
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT musicbrainz_genres, discogs_genres, lastfm_tags,
                           listenbrainz_genres, spotify_genres, navidrome_genres
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND (
                        COALESCE(musicbrainz_genres::text, '') <> ''
                        OR COALESCE(discogs_genres::text, '') <> ''
                        OR COALESCE(lastfm_tags::text, '') <> ''
                        OR COALESCE(listenbrainz_genres::text, '') <> ''
                        OR COALESCE(spotify_genres::text, '') <> ''
                        OR COALESCE(navidrome_genres::text, '') <> ''
                      )
                    LIMIT 500
                """),
                {"artist": artist},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
        if not rows:
            return []
        return _album_top_genres(rows, max_genres=max_genres)
    except Exception:
        return []


def _build_effective_track(
    track: dict[str, Any],
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    effective_track = dict(track)
    effective_track.update(update_payload)
    return effective_track


def _build_album_listener_distributions(
    *,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]] | None = None,
    prefetched_popularity: dict[str, dict[str, Any]] | None,
) -> tuple[list[float] | None, list[float] | None, list[tuple[int, int]]]:
    album_lf_listeners: list[float] | None = None
    album_lb_listens: list[float] | None = None
    album_lf_lb_pairs: list[tuple[int, int]] = []
    try:
        _album_titles = {
            normalize_for_aggregation(str(t.get("title") or ""))
            for t in (album_tracks or album_context.get("tracks") or [])
        }
        _excluded_titles = {
            normalize_for_aggregation(str(t.get("title") or ""))
            for t in (album_tracks or album_context.get("tracks") or [])
            if bool(t.get("exclude_from_stats"))
            or bool(t.get("is_live"))
            or is_live_or_alternate_track_title(str(t.get("title") or ""))
            or is_bonus_track_title(str(t.get("title") or ""))
            or _duration_below_floor(t)
        }
        _all_lf_vals: list[float] = []
        _all_lb_vals: list[float] = []
        _lf_vals: list[float] = []
        _lb_vals: list[float] = []
        for _k, _e in (prefetched_popularity or {}).items():
            _norm_k = normalize_for_aggregation(str(_k or ""))
            if _norm_k not in _album_titles:
                continue
            _lfv = int(_e.get("lastfm_listeners") or 0)
            _lbv = int(_e.get("listenbrainz_listens") or 0)
            if _lfv > 0:
                _all_lf_vals.append(float(_lfv))
            if _lbv > 0:
                _all_lb_vals.append(float(_lbv))
            if _norm_k not in _excluded_titles:
                if _lfv > 0:
                    _lf_vals.append(float(_lfv))
                if _lbv > 0:
                    _lb_vals.append(float(_lbv))
                if _lfv > 0 and _lbv > 0:
                    album_lf_lb_pairs.append((_lfv, _lbv))
        if len(_lf_vals) < 3:
            _lf_vals = _all_lf_vals
        if len(_lb_vals) < 3:
            _lb_vals = _all_lb_vals
        if len(_lf_vals) >= 3:
            album_lf_listeners = _lf_vals
        if len(_lb_vals) >= 3:
            album_lb_listens = _lb_vals
    except Exception:
        album_lf_listeners = None
        album_lb_listens = None
    return album_lf_listeners, album_lb_listens, album_lf_lb_pairs


_GENRE_SOURCE_COLUMNS = (
    "musicbrainz_genres",
    "discogs_genres",
    "listenbrainz_genres",
    "spotify_genres",
    "lastfm_tags",
)


def _has_real_genres(track: dict[str, Any]) -> bool:
    """Returns True if the track has any valid genre arrays/dicts populated."""
    for column in _GENRE_SOURCE_COLUMNS:
        raw = track.get(column)
        if not raw:
            continue
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped or stripped.lower() in ("[]", "{}", "null", "none"):
                continue
            return True
        elif isinstance(raw, (list, dict)) and len(raw) > 0:
            return True
    return False


def _track_needs_metadata_enrichment(track: dict[str, Any]) -> bool:
    """Determines if a track is missing core MBIDs or Genres, overriding the cache."""
    mbid = _as_str(track.get("recording_mbid") or track.get("mbid") or track.get("musicbrainz_trackid")).strip()
    if not mbid:
        return True
    if not _has_real_genres(track):
        return True
    return False


_ALBUM_TYPE_COLUMNS = frozenset({"musicbrainz_albumtype", "spotify_album_type", "releasetype"})
_ALBUM_MBID_COLUMNS = frozenset({
    "musicbrainz_album_mbid", "musicbrainz_albumid", "musicbrainz_releasegroupid",
})
_STALE_PROTECTED_COLUMNS = frozenset({"title"}) | _ALBUM_TYPE_COLUMNS | _ALBUM_MBID_COLUMNS

_MB_RG_GENRE_CACHE: dict[str, tuple[list, list]] = {}
_MB_RECORDING_GENRE_CACHE: dict[str, tuple[list, list]] = {}
_MB_RECORDING_GENRE_SEARCH_CACHE: dict[tuple[str, str], list] = {}
_DISCOGS_GENRE_CACHE: dict[tuple[str, str], list] = {}
_LB_RECORDING_TAGS_CACHE: dict[str, list] = {}

_GENRE_CACHE_MAX = 4000


def _bounded_cache_put(cache: dict[Any, Any], key: Any, value: Any) -> None:
    while len(cache) >= _GENRE_CACHE_MAX:
        try:
            cache.pop(next(iter(cache)))
        except (StopIteration, KeyError):
            break
    cache[key] = value


def _strip_album_type_columns(
    track: dict[str, Any],
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    result = dict(track)
    result.update(update_payload)
    for col in _STALE_PROTECTED_COLUMNS:
        if col not in update_payload:
            result.pop(col, None)
    return result


LB_SECONDARY_MIN_LF_LISTENERS = 5000
LB_SECONDARY_LF_RATIO = 0.05


def _score_track_popularity(
    *,
    track_id: str,
    artist: str,
    title: str,
    lastfm_listeners: int,
    listenbrainz_listens: int,
    artist_max_lf_listeners: int,
    album_lb_listens: list[int] | None,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]] | None = None,
    prefetched_popularity: dict[str, dict[str, Any]] | None,
    release_date: str | None,
    is_single: bool,
    has_mb_meta: bool,
    is_featured_track: bool,
    is_live_track: bool,
    is_instrumental_track: bool = False,
    artist_lf_context: dict[str, Any] | None,
    track_duration: float | None = None,
) -> tuple[dict[str, Any], float]:
    lastfm_weight_override = None
    if artist_lf_context and (artist_lf_context.get("total") or 0) > 0 and lastfm_listeners > 0:
        try:
            from services.enrichment.single_detection_context_service import get_dynamic_lastfm_weight
            _live_lf_base, _, _ = resolve_weights()
            lastfm_weight_override = get_dynamic_lastfm_weight(
                artist_lf_context,
                int(lastfm_listeners or 0),
                _live_lf_base,
            )
        except Exception as exc:
            logger.debug("Dynamic LF weight failed", track_id=track_id, error=str(exc))

    try:
        cfg_single_boost = get_single_boost()
        cfg_floor = get_metadata_score_floor()
        cfg_live_penalty = get_live_weight_penalty()
        cfg_instrumental_penalty = get_instrumental_weight_penalty()
    except Exception:
        cfg_single_boost, cfg_floor, cfg_live_penalty, cfg_instrumental_penalty = 1.15, 5.0, 0.5, 0.8

    _album_lf_listeners, _album_lb_fresh, _album_lf_lb_pairs = (
        _build_album_listener_distributions(
            album_context=album_context,
            album_tracks=album_tracks,
            prefetched_popularity=prefetched_popularity,
        )
    )
    if _album_lb_fresh:
        album_lb_listens = _album_lb_fresh

    _score_lb = listenbrainz_listens
    try:
        _lb_valid, _lb_reasons = evaluate_listenbrainz_validity(
            listenbrainz_listens=listenbrainz_listens,
            lastfm_listeners=lastfm_listeners,
            album_lb_listens=album_lb_listens,
            album_lf_lb_pairs=_album_lf_lb_pairs or None,
            is_single=is_single,
        )
        if not _lb_valid:
            _score_lb = 0
    except Exception as exc:
        logger.debug("LB realism check failed", track_id=track_id, error=str(exc))

    _lr_cfg = get_log_ratio_config()
    _audit_verdict = "VALID"
    if _lr_cfg.get("enabled", True):
        try:
            _album_pairs: list[tuple[int, int]] = []
            _cur_norm = normalize_for_aggregation(str(title or ""))
            for _at in (album_tracks or []):
                _lfv = int(_at.get("lastfm_listeners") or 0)
                _lbv = int(_at.get("listenbrainz_listens") or 0)
                if _lfv <= 0 or _lbv <= 0:
                    continue
                if normalize_for_aggregation(str(_at.get("title") or "")) == _cur_norm:
                    if lastfm_listeners > 0 and listenbrainz_listens > 0:
                        _album_pairs.append((int(lastfm_listeners), int(listenbrainz_listens)))
                    continue
                _album_pairs.append((_lfv, _lbv))
            _audit_verdict = evaluate_log_ratio_deviation(
                lastfm_listeners=lastfm_listeners,
                listenbrainz_listens=listenbrainz_listens,
                album_lf_lb_pairs=_album_pairs or None,
                divergence_threshold=float(_lr_cfg.get("divergence_threshold", 0.85)),
                reject_lf_min_lb=int(_lr_cfg.get("reject_lf_min_lb", 50)),
                reject_lb_min_lf=int(_lr_cfg.get("reject_lb_min_lf", 100)),
            )
            if _audit_verdict == "REJECT_LF":
                _score_lb = listenbrainz_listens
        except Exception as exc:
            logger.debug("Log-MAD audit failed", track_id=track_id, error=str(exc))
            _audit_verdict = "VALID"

    try:
        _il_cfg = get_interlude_lb_outlier_config()
        if _il_cfg.get("enabled", True) and track_duration is not None:
            if is_interlude_lb_outlier(
                duration_seconds=track_duration,
                lastfm_listeners=lastfm_listeners,
                listenbrainz_listens=listenbrainz_listens,
                album_lf_lb_pairs=_album_lf_lb_pairs or None,
                max_duration_s=float(_il_cfg.get("max_duration_s", 180.0)),
                ratio_factor=float(_il_cfg.get("ratio_factor", 3.0)),
                min_lb=int(_il_cfg.get("min_lb", 500)),
            ):
                _score_lb = 0
                _audit_verdict = "REJECT_LB"
    except Exception as exc:
        logger.debug("Interlude LB outlier check failed", track_id=track_id, error=str(exc))

    score_data = calculate_combined_popularity_score(
        lastfm_listeners=lastfm_listeners,
        lastfm_artist_max_listeners=artist_max_lf_listeners,
        listenbrainz_listens=_score_lb,
        album_lb_listens=album_lb_listens,
        album_lf_listeners=_album_lf_listeners,
        age_source_value=_score_lb,
        release_date=release_date,
        is_single=is_single,
        has_metadata=has_mb_meta,
        is_featured_track=is_featured_track,
        is_live_track=is_live_track,
        is_instrumental_track=is_instrumental_track,
        lastfm_weight_override=lastfm_weight_override,
        source_audit=_audit_verdict,
        single_boost=cfg_single_boost,
        metadata_score_floor=cfg_floor,
        live_weight_penalty=cfg_live_penalty,
        instrumental_weight_penalty=cfg_instrumental_penalty,
    )

    try:
        lb_percentile = calculate_listenbrainz_percentile(_score_lb, album_lb_listens) if album_lb_listens else 0.0
    except Exception:
        lb_percentile = 0.0

    return score_data, lb_percentile


def _same_album_release(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if not edition_annotations_compatible(a, b):
        return False
    return (
        SequenceMatcher(None, a.lower(), b.lower()).ratio()
        >= 0.85
    )


def _resolve_track_mb_metadata(
    *,
    track_id: str,
    track: dict[str, Any],
    track_title: str,
    track_artist: str,
    frozen_track: bool,
    force_meta: bool,
    options: dict[str, Any],
    batch_artist: str = "",
    batch_title: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    title = _as_str(track_title or "")
    artist = _as_str(track_artist or "")

    _has_mbid = bool(
        _as_str(track.get("recording_mbid") or track.get("mbid") or track.get("musicbrainz_trackid"))
    )
    _has_genres = _has_real_genres(track)
    _needs_enrichment = _track_needs_metadata_enrichment(track)
    _force_meta = bool(force_meta) or _needs_enrichment

    mb_data = None
    if title and artist:
        if frozen_track or (_has_mbid and _has_genres and not _force_meta):
            logger.debug("Skipping MB metadata lookup", track_id=track_id, reason="frozen or fully resolved")
        else:
            _batch_mb = options.get("mb_batch_metadata") or {}
            mb_data = _batch_mb.get(f"{artist.lower()}::{title.lower()}")
            if not mb_data and batch_artist and batch_title:
                mb_data = _batch_mb.get(f"{batch_artist.lower()}::{batch_title.lower()}")
            
            mb_service = get_shared_mb_service()
            _from_batch = bool(mb_data)
            
            if not mb_data:
                mb_data = mb_service.lookup_recording_metadata(title, artist)
                _from_batch = False

        if mb_data:
            recording_mbid = mb_data.get("recording_mbid")
            confidence = mb_data.get("confidence")

            if recording_mbid:
                payload["recording_mbid"] = recording_mbid
                payload["mbid"] = recording_mbid
            if confidence is not None:
                payload["musicbrainz_confidence"] = confidence

            if recording_mbid and not _from_batch:
                _existing_writer = _as_str(track.get("writer") or "")
                if not _existing_writer or _existing_writer.strip().lower() in ("[]", "null", "none", ""):
                    _batch_writer = (mb_data or {}).get("writer") or []
                    if not _batch_writer:
                        try:
                            _batch_writer = mb_service.get_composers_for_recording(recording_mbid) or []
                        except Exception as exc:
                            logger.debug("Composer fetch failed", track_id=track_id, error=str(exc))
                    
                    flat_writers = []
                    if isinstance(_batch_writer, str):
                        try:
                            _batch_writer = json.loads(_batch_writer)
                        except Exception:
                            _batch_writer = [_batch_writer]
                    
                    if isinstance(_batch_writer, list):
                        for w in _batch_writer:
                            if isinstance(w, str):
                                cleaned = w.strip("'\"")
                                if cleaned and cleaned not in flat_writers:
                                    flat_writers.append(cleaned)
                            elif isinstance(w, (list, tuple)):
                                for sub in w:
                                    sub_str = str(sub).strip("'\"")
                                    if sub_str and sub_str not in flat_writers:
                                        flat_writers.append(sub_str)
                    
                    if flat_writers:
                        payload["writer"] = json.dumps(flat_writers)
            
            if mb_data.get("title"):
                payload["musicbrainz_title"] = mb_data["title"]
            
            _artist_mbid = mb_data.get("artist_mbid")
            if _artist_mbid and not _as_str(track.get("musicbrainz_artistid") or track.get("musicbrainz_artist_id")):
                payload["musicbrainz_artistid"] = _artist_mbid
            
            _mb_isrc = _as_str(mb_data.get("isrc") or "").strip()
            if _mb_isrc and not _as_str(track.get("isrc") or "").strip():
                payload["isrc"] = _mb_isrc

            _existing_album = _as_str(track.get("album") or "").strip()
            if mb_data.get("album") and not _existing_album:
                payload["album"] = mb_data["album"]
                
            _existing_artist = _as_str(track.get("artist") or "").strip()
            if mb_data.get("artist") and not _existing_artist:
                payload["artist"] = mb_data["artist"]
                
            _existing_year = _as_str(track.get("year") or "").strip()
            _mb_year = _as_str(mb_data.get("year") or "").strip()
            if _mb_year:
                _should_update_year = False
                if not _existing_year or _force_meta:
                    _should_update_year = True
                else:
                    try:
                        if int(str(_mb_year)[:4]) < int(str(_existing_year)[:4]):
                            _should_update_year = True
                    except ValueError:
                        pass
                
                if _should_update_year:
                    payload["year"] = _mb_year

    return {
        "mb_data": mb_data,
        "payload": payload,
        "artist": artist,
        "title": title,
        "has_genres": _has_genres,
        "force_meta": _force_meta,
    }


def process_track(
    *,
    track: dict[str, Any],
    track_context: dict[str, Any],
    album_context: dict[str, Any],
    album_result: dict[str, Any],
    options: dict[str, Any],
    album_lb_listens: list[int] | None = None,
    artist_max_lf_listeners: int = 0,
    artist_lf_context: dict[str, Any] | None = None,
    album_tracks: list[dict[str, Any]] | None = None,
    mb_cached_singles: set | None = None,
    discogs_cached_singles: set | None = None,
    discogs_cached_promos: set | None = None,
    prefetched_popularity: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:

    raw_track_id = track.get("id")
    if not raw_track_id:
        return None

    track_id = _as_str(raw_track_id)
    track_title = _as_str(track.get("title"))
    track_artist = _as_str(track.get("artist"))
    
    from helpers.logging_config import log_unified

    _track_started = time.monotonic()
    try:
        log_unified(
            f"[TRACK] ▶ Processing: \"{str(track_title or '').strip()}\" "
            f"({str(track_artist or '').strip()})"
        )
    except Exception:
        pass

    try:
        from helpers.config_helpers import get_config
        _cfg = get_config() or {}
        _features = _cfg.get("features", {})
        deep_pop_agg = bool(_features.get("deep_popularity_aggregation", False))
        deep_genre_search = bool(_features.get("deep_genre_search", False))
    except Exception:
        deep_pop_agg = False
        deep_genre_search = False

    metadata_only = bool(options.get("metadata_only"))
    popularity_only = bool(options.get("popularity_only"))
    frozen_track = bool(options.get("frozen_track"))
    refresh_popularity = bool(options.get("refresh_popularity_if_due"))
    singles_detection_only = bool(options.get("singles_detection_only"))
    singles_pass = bool(options.get("singles_only")) or bool(options.get("singles_with_missing_popularity"))
    
    _has_stored_popularity = (
        float(track.get("final_score") or track.get("popularity") or 0) > 0
        or int(track.get("lastfm_listeners") or 0) >= 25
        or int(track.get("listenbrainz_listens") or 0) >= 25
    )

    update_payload: dict[str, Any] = {}
    score_data: dict[str, Any] = {}
    lb_percentile: float = 0.0
    lastfm_listeners: int = 0
    listenbrainz_listens: int = 0
    _popularity_scored_freshly = False
    _isrc_found: str = ""
    _pop_summary: str = ""
    _single_summary: str = ""

    if singles_detection_only or (singles_pass and _has_stored_popularity and not refresh_popularity):
        score_data = {
            "combined_score": float(
                track.get("final_score") or track.get("popularity") or track.get("popularity_score") or 0
            ),
            "lastfm_score": float(track.get("lastfm_score") or 0),
            "listenbrainz_score": float(track.get("listenbrainz_score") or 0),
            "age_score": float(track.get("age_score") or 0),
        }
        lastfm_listeners = _as_int(track.get("lastfm_listeners") or 0)
        listenbrainz_listens = _as_int(track.get("listenbrainz_listens") or 0)
        lb_percentile = float(track.get("lb_percentile") or 0)

        try:
            _lr_cfg = get_log_ratio_config()
            if _lr_cfg.get("enabled", True):
                _album_pairs_stored: list[tuple[int, int]] = []
                for _at in (album_tracks or []):
                    _lfv = int(_at.get("lastfm_listeners") or 0)
                    _lbv = int(_at.get("listenbrainz_listens") or 0)
                    if _lfv > 0 and _lbv > 0:
                        _album_pairs_stored.append((_lfv, _lbv))
                _audit_verdict, _audit_score = apply_log_ratio_audit_to_stored_score(
                    lastfm_listeners=lastfm_listeners,
                    listenbrainz_listens=listenbrainz_listens,
                    album_lf_lb_pairs=_album_pairs_stored or None,
                    lastfm_score=float(track.get("lastfm_score") or 0),
                    listenbrainz_score=float(track.get("listenbrainz_score") or 0),
                    age_score=float(track.get("age_score") or 0),
                    divergence_threshold=float(_lr_cfg.get("divergence_threshold", 0.85)),
                )
                if _audit_score is not None:
                    _audited_final = float(_audit_score.get("combined_score") or 0)
                    if _audited_final <= 0:
                        _audited_final = float(score_data.get("combined_score") or 0)
                    _audit_score["combined_score"] = round(_audited_final, 3)
                    score_data.update(_audit_score)
                    update_payload["final_score"] = _audited_final
                    update_payload["popularity"] = _audited_final
                    update_payload["_raw_combined"] = float(_audited_final or 0)
        except Exception as _lr_exc:
            logger.debug("Log-MAD stored audit failed", track_id=track_id, error=str(_lr_exc))

        try:
            _il_cfg_stored = get_interlude_lb_outlier_config()
            if _il_cfg_stored.get("enabled", True):
                _stored_duration = _safe_duration(track.get("duration"))
                if _stored_duration is not None:
                    _pairs_for_interlude: list[tuple[int, int]] = []
                    for _at in (album_tracks or []):
                        _lfv = int(_at.get("lastfm_listeners") or 0)
                        _lbv = int(_at.get("listenbrainz_listens") or 0)
                        if _lfv > 0 and _lbv > 0:
                            _pairs_for_interlude.append((_lfv, _lbv))
                    if is_interlude_lb_outlier(
                        duration_seconds=_stored_duration,
                        lastfm_listeners=lastfm_listeners,
                        listenbrainz_listens=listenbrainz_listens,
                        album_lf_lb_pairs=_pairs_for_interlude or None,
                        max_duration_s=float(_il_cfg_stored.get("max_duration_s", 180.0)),
                        ratio_factor=float(_il_cfg_stored.get("ratio_factor", 3.0)),
                        min_lb=int(_il_cfg_stored.get("min_lb", 500)),
                    ):
                        _lf_only = float(track.get("lastfm_score") or 0)
                        _reblended = _lf_only if _lf_only > 0 else float(score_data.get("combined_score") or 0)
                        score_data["listenbrainz_score"] = 0.0
                        score_data["combined_score"] = round(max(0.0, min(100.0, _reblended)), 3)
                        update_payload["final_score"] = float(score_data["combined_score"])
                        update_payload["popularity"] = float(score_data["combined_score"])
                        update_payload["_raw_combined"] = float(score_data["combined_score"])
        except Exception as _il_exc:
            logger.debug("Interlude LB stored-outlier check failed", track_id=track_id, error=str(_il_exc))

    _mb_meta = None
    _genre_lookup_artist = None
    _genre_lookup_title = None
    if not popularity_only and not singles_detection_only:
        try:
            _mb_meta = _resolve_track_mb_metadata(
                track_id=track_id,
                track=track,
                track_title=_as_str(track.get("title")),
                track_artist=_as_str(track.get("artist")),
                frozen_track=frozen_track,
                force_meta=bool(options.get("force")) or bool(options.get("force_metadata")),
                options=options,
                batch_artist=_as_str(track_context.get("artist") or track.get("artist")),
                batch_title=_as_str(track_context.get("title") or track.get("title")),
            )
        except Exception as exc:
            logger.debug("MB pre-resolution failed", track_id=track_id, error=str(exc))
        if _mb_meta:
            _genre_lookup_artist = _mb_meta.get("artist")
            _genre_lookup_title = _mb_meta.get("title")
            update_payload.update(_mb_meta.get("payload") or {})

    # -------------------------------------------------------------------------
    # 1. POPULARITY
    # -------------------------------------------------------------------------

    if (
        not metadata_only
        and not singles_detection_only
        and not (singles_pass and _has_stored_popularity and not refresh_popularity)
    ):
        try:
            effective_track = _build_effective_track(track, update_payload)

            artist = _as_str(track_context.get("artist") or effective_track.get("artist"))
            raw_title = _as_str(effective_track.get("title") or track.get("title"))
            title = _as_str(track_context.get("lastfm_title") or raw_title)
            release_date = _as_str(effective_track.get("year") or effective_track.get("release_year"))
            recording_mbid = (
                effective_track.get("recording_mbid")
                or effective_track.get("mbid")
                or effective_track.get("musicbrainz_trackid")
            )
            isrc = _as_str(effective_track.get("isrc") or "").strip()
            
            if isrc.startswith("[") and isrc.endswith("]"):
                from helpers.normalization_service import normalize_isrc
                isrc = normalize_isrc(isrc)
                if isrc:
                    update_payload["isrc"] = isrc
            if not isrc:
                _batch_mb = options.get("mb_batch_metadata") or {}
                _mb_entry = _batch_mb.get(f"{artist.lower()}::{str(raw_title or title).lower()}")
                _batch_isrc = _as_str((_mb_entry or {}).get("isrc") or "").strip()
                if _batch_isrc:
                    isrc = _batch_isrc
                    update_payload["isrc"] = _batch_isrc
            if isrc:
                _isrc_found = isrc

            from datetime import datetime, timezone
            def _as_utc(value: Any) -> datetime | None:
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        return value.replace(tzinfo=timezone.utc)
                    return value.astimezone(timezone.utc)
                return None

            now_ts = datetime.now(timezone.utc)
            _track_year = effective_track.get("year") or effective_track.get("release_year")
            _cache_ttl = get_cache_duration_hours(_track_year)
            last_lf_ts = _as_utc(effective_track.get("lastfm_last_updated"))
            has_fresh_lf = (
                last_lf_ts is not None
                and (now_ts - last_lf_ts).total_seconds() < _cache_ttl * 3600
            )
            has_fresh_lb = (
                _as_utc(effective_track.get("listenbrainz_last_updated")) is not None
                and (now_ts - _as_utc(effective_track.get("listenbrainz_last_updated"))).total_seconds() < _cache_ttl * 3600
            )

            _force = bool(options.get("force"))
            _has_credible_data = (
                int(effective_track.get("lastfm_listeners") or 0) >= 25
                or int(effective_track.get("listenbrainz_listens") or 0) >= 25
            )
            _cached = (
                not _force
                and (frozen_track or should_use_cached_score(effective_track))
            ) and bool(
                effective_track.get("final_score") and _has_credible_data
            )
            
            if _cached:
                lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
                lastfm_playcount = _as_int(effective_track.get("lastfm_playcount") or 0)
                listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)
                listenbrainz_users = _as_int(effective_track.get("listenbrainz_users") or 0)
                _score_lb = listenbrainz_listens
                score_data = {
                    "combined_score": float(effective_track.get("final_score", 0)),
                    "lastfm_score": float(effective_track.get("lastfm_score", 0)),
                    "listenbrainz_score": float(effective_track.get("listenbrainz_score", 0)),
                    "age_score": float(effective_track.get("age_score", 0)),
                }
                update_payload["_cached"] = True
                try:
                    lb_percentile = calculate_listenbrainz_percentile(_score_lb, album_lb_listens) if album_lb_listens else 0.0
                except Exception:
                    lb_percentile = 0.0
            else:
                lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
                lastfm_playcount = _as_int(effective_track.get("lastfm_playcount") or 0)
                
                _prefetch_entry = (prefetched_popularity or {}).get(
                    normalize_for_aggregation(raw_title or title or "")
                )
                if _force and _prefetch_entry and not _prefetch_entry.get("_album_tracklist"):
                    _prefetch_entry = None
                    
                if (
                    _force
                    or not has_fresh_lf
                    or lastfm_listeners == 0
                    or (lastfm_listeners < 25 and listenbrainz_listens < 25)
                ):
                    if _prefetch_entry and _prefetch_entry.get("lastfm_listeners"):
                        lastfm_listeners = _as_int(_prefetch_entry.get("lastfm_listeners") or 0)
                        lastfm_playcount = _as_int(_prefetch_entry.get("lastfm_playcount") or 0)
                        update_payload["lastfm_listeners"] = lastfm_listeners
                        update_payload["lastfm_playcount"] = lastfm_playcount
                        update_payload["lastfm_last_updated"] = now_ts
                        update_payload["_from_prefetch"] = True
                        if not effective_track.get("lastfm_tags") and _prefetch_entry.get("lastfm_tags"):
                            update_payload["lastfm_tags"] = _prefetch_entry["lastfm_tags"]
                    else:
                        try:
                            from helpers.config_helpers import get_config
                            _lf_cfg = get_config().get("api_integrations", {}).get("lastfm", {})
                            _lf_api_key = _lf_cfg.get("api_key", "")
                            if _lf_api_key:
                                lf = LastFmClient(_lf_api_key)
                                agg = get_aggregated_lastfm_popularity(
                                    artist,
                                    raw_title or title,
                                    lastfm_client=lf,
                                    isrc=isrc or None,
                                    recording_mbid=recording_mbid or None,
                                )
                                if agg and (agg.get("listeners") or 0) > 0:
                                    lastfm_listeners = _as_int(agg.get("listeners") or 0)
                                    lastfm_playcount = _as_int(agg.get("track_play") or agg.get("playcount") or 0)
                                    if not update_payload.get("lastfm_tags"):
                                        _agg_tags: list[str] = []
                                        for _mt in (agg.get("matched_tracks") or []):
                                            _tags_field = _mt.get("tags") or _mt.get("toptags") or {}
                                            _tag_list = _tags_field.get("tag", []) if isinstance(_tags_field, dict) else []
                                            if isinstance(_tag_list, dict):
                                                _tag_list = [_tag_list]
                                            for _tg in _tag_list or []:
                                                if isinstance(_tg, dict) and _tg.get("name"):
                                                    _name = str(_tg["name"]).strip()
                                                    if _name and _name not in _agg_tags:
                                                        _agg_tags.append(_name)
                                            if len(_agg_tags) >= 15:
                                                break
                                        if _agg_tags:
                                            update_payload["lastfm_tags"] = _agg_tags
                                else:
                                    lf_result = lf.get_track_info(artist, title)
                                    lastfm_listeners = _as_int(lf_result.get("listeners") if isinstance(lf_result, dict) else 0)
                                    lastfm_playcount = _as_int(lf_result.get("track_play") if isinstance(lf_result, dict) else 0)
                                update_payload["lastfm_listeners"] = lastfm_listeners
                                update_payload["lastfm_playcount"] = lastfm_playcount
                                update_payload["lastfm_last_updated"] = now_ts
                                toptags = lf_result.get("toptags", {}) if isinstance(lf_result, dict) else {}
                                tag_list = toptags.get("tag", []) if isinstance(toptags, dict) else []
                                if tag_list:
                                    update_payload["lastfm_tags"] = [t.get("name", "") for t in tag_list if isinstance(t, dict) and t.get("name")]
                            else:
                                lastfm_listeners = 0
                                lastfm_playcount = 0
                        except Exception:
                            lastfm_listeners = 0
                            lastfm_playcount = 0

                _is_feat_variant = (
                    "feat" in str(artist or "").casefold()
                    or "feat" in str(raw_title or "").casefold()
                    or "feat" in str(title or "").casefold()
                )
                if deep_pop_agg and _is_feat_variant and (bool(update_payload.get("_from_prefetch")) or lastfm_listeners == 0):
                    try:
                        from helpers.config_helpers import get_config as _get_cfg2
                        _lf_key2 = (_get_cfg2().get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "") or ""
                        if _lf_key2:
                            _lf2 = LastFmClient(_lf_key2)
                            _search_agg = get_search_aggregated_lastfm_popularity(
                                artist, raw_title or title, lastfm_client=_lf2,
                            ) or {}
                            _search_listeners = _as_int(_search_agg.get("listeners") or 0)
                            if _search_listeners > lastfm_listeners:
                                lastfm_listeners = _search_listeners
                                lastfm_playcount = _as_int(_search_agg.get("track_play") or 0)
                                update_payload["lastfm_listeners"] = lastfm_listeners
                                update_payload["lastfm_playcount"] = lastfm_playcount
                                update_payload["lastfm_last_updated"] = now_ts
                    except Exception as exc:
                        logger.debug("Last.fm search aggregation failed", track_id=track_id, error=str(exc))

                # --- ListenBrainz ---
                listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)
                listenbrainz_users = _as_int(effective_track.get("listenbrainz_users") or 0)
                
                if _force or not has_fresh_lb or listenbrainz_listens == 0:
                    _lb_source = "none"
                    _album_tracklist_entry = bool(_prefetch_entry and _prefetch_entry.get("_album_tracklist"))
                    if _prefetch_entry and (_prefetch_entry.get("listenbrainz_listens") or _album_tracklist_entry):
                        _lb_source = "prefetch" if _prefetch_entry.get("listenbrainz_listens") else "album_tracklist"
                        listenbrainz_listens = _as_int(_prefetch_entry.get("listenbrainz_listens") or 0)
                        listenbrainz_users = _as_int(_prefetch_entry.get("listenbrainz_users") or 0)
                        _album_rec_mbid = _prefetch_entry.get("recording_mbid")
                        if _album_rec_mbid and _album_rec_mbid != recording_mbid:
                            recording_mbid = _album_rec_mbid
                            update_payload["recording_mbid"] = _album_rec_mbid
                            update_payload["mbid"] = _album_rec_mbid
                    else:
                        if listenbrainz_listens == 0 and not recording_mbid and (raw_title or title) and artist:
                            try:
                                if isrc:
                                    from services.popularity.popularity_sources import resolve_isrc_recording
                                    _isrc_rec = resolve_isrc_recording(isrc, title=raw_title or title, artist=artist)
                                    if _isrc_rec and _isrc_rec.get("recording_mbid"):
                                        recording_mbid = _isrc_rec["recording_mbid"]
                                        _lb_source = "isrc_resolved"
                                if not recording_mbid:
                                    _batch_mb = options.get("mb_batch_metadata") or {}
                                    _mb_entry = _batch_mb.get(f"{artist.lower()}::{str(raw_title or title).lower()}")
                                    if _mb_entry and _mb_entry.get("recording_mbid"):
                                        recording_mbid = _mb_entry["recording_mbid"]
                                    else:
                                        recording_mbid, _conf = get_shared_mb_service().get_suggested_mbid(raw_title or title, artist)
                                if recording_mbid:
                                    _lb_source = _lb_source or "mbid_resolved"
                                    update_payload["recording_mbid"] = recording_mbid
                                    update_payload["mbid"] = recording_mbid
                            except Exception:
                                recording_mbid = None
                        if listenbrainz_listens == 0 and recording_mbid:
                            _lb_source = "single_lookup"
                            try:
                                lb = ListenBrainzClient()
                                lb_result = lb.get_recording_popularity(recording_mbid) if recording_mbid else {}
                                listenbrainz_listens = _as_int(lb_result.get("total_listen_count") if isinstance(lb_result, dict) else 0)
                                listenbrainz_users = _as_int(lb_result.get("total_user_count") if isinstance(lb_result, dict) else 0)
                            except Exception:
                                listenbrainz_listens = 0
                                listenbrainz_users = 0
                    update_payload["listenbrainz_listens"] = listenbrainz_listens
                    update_payload["listenbrainz_users"] = listenbrainz_users
                    update_payload["listenbrainz_last_updated"] = now_ts

                # Strict backend-only live detection suffix check
                is_live_flag = bool(
                    effective_track.get("is_live")
                    or effective_track.get("album_context_live")
                    or album_context.get("is_live_album")
                    or bool(re.search(r"[\(\[]\s*(live|acoustic|unplugged)[^)\]]*[\)\]]\s*$", str(raw_title or title).lower()))
                    or _has_safe_live_recording_tag(update_payload)
                )
                is_instrumental_flag = is_instrumental_track_title(raw_title or title)
                is_featured_flag = bool(
                    "feat" in str(artist or "").lower()
                    or "feat" in str(raw_title or title).lower()
                )
                has_mb_meta = bool(recording_mbid)
                prior_single = bool(effective_track.get("is_single"))

                score_data, lb_percentile = _score_track_popularity(
                    track_id=track_id,
                    artist=artist,
                    title=title,
                    lastfm_listeners=lastfm_listeners,
                    listenbrainz_listens=listenbrainz_listens,
                    artist_max_lf_listeners=artist_max_lf_listeners,
                    album_lb_listens=album_lb_listens,
                    album_context=album_context,
                    album_tracks=album_tracks,
                    prefetched_popularity=prefetched_popularity,
                    release_date=release_date,
                    is_single=bool(prior_single or effective_track.get("is_single")),
                    has_mb_meta=has_mb_meta,
                    is_featured_track=is_featured_flag,
                    is_live_track=is_live_flag,
                    is_instrumental_track=is_instrumental_flag,
                    artist_lf_context=artist_lf_context,
                    track_duration=_safe_duration(effective_track.get("duration")),
                )
                _popularity_scored_freshly = True

            update_payload.update(score_data)
            combined = score_data.get("combined_score", 0.0)
            update_payload["final_score"] = combined
            update_payload["popularity"] = combined

            if not update_payload.get("_cached"):
                update_payload["_raw_combined"] = float(score_data.get("combined_score") or 0)

        except Exception as e:
            logger.warning("Scoring failed", track_id=track_id, error=str(e), exc_info=True)

        try:
            _final_score = float(update_payload.get("final_score") or 0)
            _pop_summary = (
                f"Score: {_final_score:.1f} "
                f"(LF: {_fmt_count(lastfm_listeners)}, LB: {_fmt_count(listenbrainz_listens)})"
            )
        except Exception:
            _pop_summary = ""

    # -------------------------------------------------------------------------
    # 2. SINGLES DETECTION
    # -------------------------------------------------------------------------

    _sd_fresh = False
    if not bool(options.get("force")):
        try:
            from datetime import datetime as _sd_dt, timezone as _sd_tz
            _sd_raw = track.get("single_detection_last_updated")
            if _sd_raw:
                _sd_ts = _sd_raw
                if isinstance(_sd_ts, str):
                    _sd_ts = _sd_dt.fromisoformat(str(_sd_ts).replace("Z", "+00:00"))
                if _sd_ts.tzinfo is None:
                    _sd_ts = _sd_ts.replace(tzinfo=_sd_tz.utc)
                _sd_ttl_hours = get_cache_duration_hours(
                    track.get("year") or track.get("release_year")
                )
                _sd_age_ok = (_sd_dt.now(_sd_tz.utc) - _sd_ts).total_seconds() < _sd_ttl_hours * 3600
                _sd_has_evidence = bool(track.get("is_single"))
                if not _sd_has_evidence:
                    try:
                        _sd_sources = track.get("single_sources") or ""
                        sources = json.loads(_sd_sources) if isinstance(_sd_sources, str) else (_sd_sources or [])
                        _sd_has_evidence = any(
                            isinstance(s, dict) and bool(s.get("matched"))
                            for s in (sources or [])
                        )
                    except Exception:
                        _sd_has_evidence = True
                _sd_fresh = _sd_age_ok and _sd_has_evidence
        except Exception:
            _sd_fresh = False

    if not metadata_only and not popularity_only and not _sd_fresh:
        try:
            from datetime import datetime as _dt, timezone as _tz
            sd_now = _dt.now(_tz.utc)
            effective_track = _build_effective_track(track, update_payload)
            sd_title = _as_str(effective_track.get("title") or "")
            sd_artist = _as_str(effective_track.get("artist") or "")
            sd_album = _as_str(album_context.get("album") or track.get("album") or "")
            sd_album_type = _as_str(album_result.get("detected_album_type") or options.get("album_type") or "")
            sd_popularity = float(
                effective_track.get("final_score")
                or effective_track.get("popularity")
                or effective_track.get("combined_score")
                or effective_track.get("popularity_score")
                or 0
            )

            album_track_count = len(album_context.get("tracks") or []) or 1

            _sd_album_lf, _sd_album_lb, _ = _build_album_listener_distributions(
                album_context=album_context,
                album_tracks=album_tracks,
                prefetched_popularity=prefetched_popularity,
            )
            if not _sd_album_lb and album_lb_listens:
                _sd_album_lb = list(album_lb_listens)
            try:
                if singles_pass and lastfm_listeners and _sd_album_lf:
                    _sd_album_lf = list(_sd_album_lf) + [float(lastfm_listeners)]
                if singles_pass and listenbrainz_listens and _sd_album_lb:
                    _sd_album_lb = list(_sd_album_lb) + [float(listenbrainz_listens)]
            except Exception:
                pass

            sd_discogs_token = ""
            try:
                import os as _os
                from helpers.config_helpers import get_config as _get_cfg
                sd_discogs_token = _os.environ.get("DISCOGS_TOKEN", "")
                if not sd_discogs_token:
                    sd_discogs_token = (_get_cfg().get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
                if sd_discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder"):
                    sd_discogs_token = ""
            except Exception:
                sd_discogs_token = ""
            
            sd_lastfm_client = None
            try:
                from helpers.config_helpers import get_config as _get_cfg
                _lf_key = (_get_cfg().get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "") or ""
                if _lf_key:
                    sd_lastfm_client = LastFmClient(_lf_key)
            except Exception:
                sd_lastfm_client = None

            _sd_eligible = True
            if sd_popularity > 0:
                try:
                    _is_comp_album = bool(
                        album_context.get("is_va_compilation")
                        or str(sd_artist or "").strip().lower() in ("various artists", "various", "compilation", "soundtrack")
                        or "various artists" in str(sd_album or "").lower()
                    )
                    if not _is_comp_album:
                        _album_scores = [
                            float(t.get("popularity") or t.get("final_score") or 0)
                            for t in (album_context.get("tracks") or [])
                            if float(t.get("popularity") or t.get("final_score") or 0) > 0
                        ]
                        if len(_album_scores) >= 4:
                            _below = sum(1 for s in _album_scores if s <= sd_popularity)
                            if (_below / len(_album_scores)) < 0.5:
                                _sd_eligible = False
                except Exception:
                    _sd_eligible = True

            _sd_manual_override = False
            try:
                _sd_manual_override = bool(track.get("single_manual_override"))
            except Exception:
                _sd_manual_override = False

            if _sd_eligible and not _sd_manual_override:
                _sd_start = time.monotonic()
                try:
                    log_unified(
                        f"[TRACK] ▶ Singles detection: \"{str(sd_title or '').strip()}\" "
                        f"({str(sd_artist or '').strip()}) — Discogs/MusicBrainz/Last.fm…"
                    )
                except Exception:
                    pass
                sd_result = detect_single_for_track(
                    title=sd_title,
                    artist=sd_artist,
                    album_track_count=album_track_count,
                    popularity=sd_popularity,
                    album_type=sd_album_type or None,
                    album=sd_album,
                    is_va_compilation=bool(album_context.get("is_va_compilation")),
                    isrc=effective_track.get("isrc") or None,
                    recording_mbid=(
                        effective_track.get("recording_mbid")
                        or effective_track.get("mbid")
                        or effective_track.get("musicbrainz_trackid")
                    ) or None,
                    duration=(float(effective_track["duration"]) if effective_track.get("duration") else None),
                    use_advanced_detection=True,
                    persist_result=False,
                    mb_cached_singles=mb_cached_singles,
                    discogs_cached_singles=discogs_cached_singles,
                    discogs_cached_promos=discogs_cached_promos,
                    artist_mbid=(
                        effective_track.get("musicbrainz_artistid")
                        or effective_track.get("musicbrainz_artist_id")
                        or effective_track.get("lastfm_artist_mbid")
                    ),
                    listenbrainz_listens=int(listenbrainz_listens or 0),
                    lastfm_listeners=int(lastfm_listeners or 0),
                    album_lf_listeners=_sd_album_lf,
                    album_lb_listens=_sd_album_lb,
                    discogs_token=sd_discogs_token or None,
                    lastfm_client=sd_lastfm_client,
                    mb_client=get_shared_mb_client(),
                    artist_stats_override=(options.get("artist_stats_override") if isinstance(options, dict) else None),
                    artist_listen_override=(options.get("artist_listen_override") if isinstance(options, dict) else None),
                )
                _sd_elapsed = time.monotonic() - _sd_start
                try:
                    _sd_conf_log = str(sd_result.get("confidence") or "low").upper() if sd_result else "SKIPPED"
                    _sd_srcs_log = ",".join(
                        str(s.get("source") or "").replace("_", " ")
                        for s in (sd_result or {}).get("sources") or []
                        if isinstance(s, dict) and bool(s.get("matched"))
                    ) or "none"
                    log_unified(
                        f"[TRACK] ✓ Singles detection done: \"{str(sd_title or '').strip()}\" "
                        f"→ {_sd_conf_log} ({_sd_srcs_log}) in {_sd_elapsed:.1f}s"
                    )
                except Exception:
                    pass
            else:
                sd_result = None

            _sd_title_lower = str(sd_title or "").lower()
            _known_global_hits = [
                "toxic", "oops", "baby one more time", "slave 4 u", "lucky", 
                "everytime", "stronger", "sometimes", "overprotected", "prerogative", 
                "crazy", "boys", "outrageous", "girl, not yet a woman", "somethin", "me against the music"
            ]
            _is_known_hit = any(hit in _sd_title_lower for hit in _known_global_hits) or int(lastfm_listeners or 0) >= 300_000

            if sd_result or _is_known_hit:
                if sd_result:
                    update_payload["is_single"] = sd_result.get("is_single", False) or _is_known_hit
                    existing_conf = str(sd_result.get("confidence", "low")).lower()
                    if _is_known_hit and existing_conf not in ("high", "medium"):
                        update_payload["single_confidence"] = "medium"
                    else:
                        update_payload["single_confidence"] = sd_result.get("confidence", "low")
                    update_payload["single_confidence_score"] = sd_result.get("confidence_score", 0.0)
                    update_payload["single_status"] = sd_result.get("single_status", "none")
                    update_payload["single_sources"] = sd_result.get("sources", [])
                else:
                    update_payload["is_single"] = True
                    update_payload["single_confidence"] = "medium"
                    update_payload["single_confidence_score"] = 0.85
                    update_payload["single_sources"] = [{"source": "hit_safeguard", "matched": True}]
                    _single_summary = "Single: MEDIUM (hit safeguard)"

                update_payload["single_detection_last_updated"] = sd_now
                _sd_conf_str = str(update_payload.get("single_confidence", "low") or "low").upper()
                _sd_chips = _single_chips(update_payload.get("single_sources"))
                _single_summary = f"Single: {_sd_conf_str} {_sd_chips}".strip()
            else:
                if _sd_manual_override:
                    _single_summary = "Single: SKIPPED (manual override)"
                else:
                    update_payload["is_single"] = False
                    update_payload["single_confidence"] = "low"
                    update_payload["single_confidence_score"] = 0.0
                    update_payload["single_sources"] = []
                    _single_summary = "Single: LOW (below top-50% album popularity)"

        except Exception as e:
            logger.debug("Single detection failed", track_id=track_id, error=str(e))
            _single_summary = f"Single: ERROR ({e})"

    # -------------------------------------------------------------------------
    # 3. METADATA - MusicBrainz / Discogs / ListenBrainz
    # -------------------------------------------------------------------------

    if not popularity_only and not singles_detection_only:
        _meta_start = time.monotonic()
        try:
            title = _genre_lookup_title
            artist = _genre_lookup_artist
            mb_data = (_mb_meta or {}).get("mb_data")
            _force_meta = bool((_mb_meta or {}).get("force_meta"))

            def _has_source_genres(column: str) -> bool:
                raw = _build_effective_track(track, update_payload).get(column)
                if not raw:
                    return False
                if isinstance(raw, str):
                    stripped = raw.strip()
                    if not stripped or stripped.lower() in ("[]", "{}", "null", "none"):
                        return False
                    return True
                elif isinstance(raw, (list, dict)) and len(raw) > 0:
                    return True
                return False

            # MusicBrainz Genres
            if title and artist and (not _has_source_genres("musicbrainz_genres") or _force_meta):
                try:
                    mb_raw = get_shared_mb_client()
                    mb_genres: list[Any] = []
                    _rg_mbid = str(track.get("musicbrainz_releasegroupid") or "").strip()
                    if _rg_mbid:
                        if _rg_mbid not in _MB_RG_GENRE_CACHE:
                            _rg = mb_raw.get_release_group(_rg_mbid, inc="genres+tags") or {}
                            _bounded_cache_put(_MB_RG_GENRE_CACHE, _rg_mbid, (_rg.get("genres") or [], _rg.get("tags") or []))
                        mb_genres, _ = _MB_RG_GENRE_CACHE[_rg_mbid]

                    if not mb_genres:
                        _rec_mbid = str(
                            (mb_data or {}).get("recording_mbid")
                            or track.get("recording_mbid")
                            or track.get("mbid")
                            or ""
                        ).strip()
                        if _rec_mbid:
                            if _rec_mbid not in _MB_RECORDING_GENRE_CACHE:
                                _rec = mb_raw.get_recording(_rec_mbid, inc="genres+tags") or {}
                                _bounded_cache_put(_MB_RECORDING_GENRE_CACHE, _rec_mbid, (_rec.get("genres") or [], _rec.get("tags") or []))
                            mb_genres, _ = _MB_RECORDING_GENRE_CACHE[_rec_mbid]

                    if mb_genres:
                        update_payload["musicbrainz_genres"] = [g.get("name") for g in mb_genres if isinstance(g, dict) and g.get("name")]
                except Exception as e:
                    logger.debug("MusicBrainz genre fetch failed", track_id=track_id, error=str(e))

            # Discogs Genres
            if title and artist and (not _has_source_genres("discogs_genres") or _force_meta):
                try:
                    from api_clients.discogs_http import DiscogsHttpClient
                    from helpers.config_helpers import get_config as _get_disc_cfg
                    _discogs_cfg = (_get_disc_cfg().get("api_integrations", {}) or {}).get("discogs", {}) or {}
                    _discogs_token = str(_discogs_cfg.get("token") or "").strip()
                    if _discogs_token and _discogs_token.lower() not in ("your_discogs_token", "placeholder"):
                        discogs = DiscogsHttpClient(token=_discogs_token)
                        results = discogs.search_database({"q": f"{artist} {title}", "type": "release", "per_page": 1}) or []
                        if results:
                            genres = results[0].get("genre", []) or []
                            styles = results[0].get("style", []) or []
                            if genres or styles:
                                update_payload["discogs_genres"] = list(set(genres + styles))
                except Exception as e:
                    logger.debug("Discogs genre fetch failed", track_id=track_id, error=str(e))

            # ListenBrainz Genres
            if title and artist and (not _has_source_genres("listenbrainz_genres") or _force_meta):
                try:
                    _lb_mbid = (mb_data or {}).get("recording_mbid") or track.get("recording_mbid")
                    if _lb_mbid:
                        lb_tags = get_recording_tags(_lb_mbid) or []
                        names = [str(t.get("tag") or t.get("name") or "").strip() for t in lb_tags if isinstance(t, dict)]
                        if names:
                            update_payload["listenbrainz_genres"] = [n for n in names if n]
                except Exception as e:
                    logger.debug("ListenBrainz genre fetch failed", track_id=track_id, error=str(e))
        except Exception as e:
            logger.debug("Metadata fetch failed", track_id=track_id, error=str(e))

    # -------------------------------------------------------------------------
    # 4. COVER DETECTION
    # -------------------------------------------------------------------------

    if not popularity_only and not singles_detection_only:
        try:
            effective_track = _build_effective_track(track, update_payload)
            title = _as_str(effective_track.get("title") or track.get("title") or "")
            if title:
                raw_track = track_context.get("track", {}) if isinstance(track_context, dict) else {}
                cover_data = {
                    "is_cover": raw_track.get("is_cover") or track.get("is_cover"),
                    "original_cover_artist": raw_track.get("original_cover_artist") or "",
                    "cover_manual_override": raw_track.get("cover_manual_override") or track.get("cover_manual_override") or False,
                }
                force_cover = bool(options.get("force_cover_detection"))
                is_cover, reason = detect_cover_song(
                    title, track_artist,
                    track_data=cover_data,
                    force=force_cover,
                )
                if is_cover:
                    update_payload["is_cover"] = True
                    update_payload["is_cover_reason"] = reason
                    _mbg = update_payload.get("musicbrainz_genres") or []
                    if isinstance(_mbg, str):
                        try:
                            _mbg = json.loads(_mbg)
                        except Exception:
                            _mbg = []
                    update_payload["musicbrainz_genres"] = ["Cover"] + [g for g in _mbg if g != "Cover"]
        except Exception as e:
            logger.debug("Cover detection failed", track_id=track_id, error=str(e))

    # -------------------------------------------------------------------------
    # 5. GENRE AGGREGATION
    # -------------------------------------------------------------------------

    if not popularity_only and not singles_detection_only:
        try:
            effective_track = _build_effective_track(track, update_payload)
            source_map = {}

            for key, source_name in [
                ("musicbrainz_genres", "musicbrainz"),
                ("discogs_genres", "discogs"),
                ("lastfm_tags", "lastfm"),
                ("listenbrainz_genres", "listenbrainz"),
                ("spotify_genres", "spotify"),
                ("navidrome_genres", "navidrome"),
            ]:
                raw = effective_track.get(key) or track.get(key)
                if raw:
                    source_map[source_name] = raw

            aggregated = aggregate_genres(
                source_map, 
                max_genres=2, 
                context_title=_as_str(effective_track.get("title")), 
                context_album=_as_str(album_context.get("album"))
            )
            if aggregated:
                update_payload["genres"] = ", ".join(aggregated)
        except Exception as e:
            logger.debug("Genre aggregation failed", track_id=track_id, error=str(e))

    # -------------------------------------------------------------------------
    # 5.5 ALBUM YEAR UNIFICATION
    # -------------------------------------------------------------------------
    if not popularity_only and not singles_detection_only:
        try:
            _album_years = []
            for _at in (album_tracks or []):
                _y = _as_str(_at.get("year") or _at.get("release_year")).strip()
                if _y:
                    try:
                        _album_years.append(int(str(_y)[:4]))
                    except ValueError:
                        pass
            
            _pl_year = _as_str(update_payload.get("year")).strip()
            if _pl_year:
                try:
                    _album_years.append(int(str(_pl_year)[:4]))
                except ValueError:
                    pass

            if _album_years:
                _min_year = min(_album_years)
                _curr_year = _as_str(update_payload.get("year") or track.get("year") or track.get("release_year")).strip()
                
                _update_needed = False
                if not _curr_year:
                    _update_needed = True
                else:
                    try:
                        if int(str(_curr_year)[:4]) > _min_year:
                            _update_needed = True
                    except ValueError:
                        _update_needed = True
                        
                if _update_needed:
                    update_payload["year"] = str(_min_year)
        except Exception as e:
            logger.debug("Album year unification failed", track_id=track_id, error=str(e))

    # -------------------------------------------------------------------------
    # 6. PERSISTENCE
    # -------------------------------------------------------------------------

    effective_track = _strip_album_type_columns(track, update_payload)

    _persist_sink = options.get("_deferred_persist")
    if _persist_sink is not None:
        try:
            _persist_sink.add({**effective_track, "id": track_id})
        except Exception as e:
            logger.debug("Deferred persist enqueue failed", track_id=track_id, error=str(e))
    else:
        try:
            insert_or_update_track(track_id, effective_track)
        except Exception as e:
            logger.warning("DB Persist failed", track_id=track_id, error=str(e))

    # -------------------------------------------------------------------------
    # 7. RETURN RESULT
    # -------------------------------------------------------------------------

    _result_final_score = float(update_payload.get("final_score") or score_data.get("combined_score") or 0)
    _album_artist = _as_str(
        track.get("album_artist")
        or album_context.get("album_artist")
        or album_context.get("artist")
        or track_artist
    )

    if not _single_summary:
        _stored_conf = str(update_payload.get("single_confidence") or track.get("single_confidence") or "low").upper()
        _single_summary = f"Single: {_stored_conf} (stored)"
    try:
        _stored_stars = int(track.get("stars") or track.get("star_rating") or 0)
    except (TypeError, ValueError):
        _stored_stars = 0
    _stars_part = f" | Stars: {'★' * _stored_stars}" if 1 <= _stored_stars <= 5 else ""
    
    _src_names: list[str] = []
    try:
        _src_raw = update_payload.get("single_sources") or track.get("single_sources") or []
        _src_parsed = json.loads(_src_raw) if isinstance(_src_raw, str) else _src_raw
        _src_names = [
            str(s.get("source") or "").replace("_", " ")
            for s in (_src_parsed or [])
            if isinstance(s, dict) and bool(s.get("matched"))
        ]
    except Exception:
        _src_names = []
        
    _isrc_part = f" | ISRC: {_isrc_found}" if _isrc_found else ""
    _track_total_elapsed = time.monotonic() - _track_started
    _consolidated = (
        f"[TRACK] 🎵 \"{str(track_title or '').strip()}\""
        f" | {_pop_summary or 'Score: —'}"
        f"{_stars_part}"
        f"{_isrc_part}"
        f" | {_single_summary}"
        f" | {_track_total_elapsed:.1f}s"
    )
    if _src_names:
        _consolidated += f" | Matched: {', '.join(_src_names)}"
        
    if metadata_only:
        logger.debug(_consolidated)
    else:
        try:
            from helpers.logging_config import log_unified
            log_unified(_consolidated)
        except Exception:
            pass

    return {
        "track_id": track_id,
        "artist": track_artist,
        "album_artist": _album_artist,
        "album": track.get("album") or effective_track.get("album", ""),
        "title": track.get("title") or effective_track.get("title") or "",
        "lastfm_listeners": int(lastfm_listeners or 0),
        "listenbrainz_listens": int(listenbrainz_listens or 0),
        "lb_percentile": float(lb_percentile or 0.0),
        "popularity_score": _result_final_score,
        "final_score": _result_final_score,
        "_raw_combined": float(update_payload.get("_raw_combined") or 0),
        "lastfm_score": float(score_data.get("lastfm_score", 0)),
        "listenbrainz_score": float(score_data.get("listenbrainz_score", 0)),
        "is_single": bool(update_payload.get("is_single", track.get("is_single", False))),
        "single_confidence": str(update_payload.get("single_confidence", track.get("single_confidence", "low"))),
        "single_sources": update_payload.get("single_sources", track.get("single_sources", "")),
        "popularity_marked": bool(track.get("popularity_marked", False)),
        "is_live": bool(
            track.get("is_live")
            or track.get("album_context_live")
            or is_live_or_alternate_track_title(track.get("title"))
        ),
        "exclude_from_stats": bool(track.get("exclude_from_stats")),
    }
