"""
Per-track popularity/enrichment stage.

Connected to PostgreSQL JSONB native tracking, completeness overrides, 
and strict back-end live tag suffix detection.
"""

from __future__ import annotations

import json
import re
import time
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


def _has_real_genres(track: dict[str, Any]) -> bool:
    _genre_source_columns = (
        "musicbrainz_genres", "discogs_genres", "listenbrainz_genres",
        "spotify_genres", "lastfm_tags",
    )
    for column in _genre_source_columns:
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

    score_data = calculate_combined_popularity_score(
        lastfm_listeners=lastfm_listeners,
        lastfm_artist_max_listeners=artist_max_lf_listeners,
        listenbrainz_listens=listenbrainz_listens,
        album_lb_listens=album_lb_listens,
        album_lf_listeners=None,
        age_source_value=listenbrainz_listens,
        release_date=release_date,
        is_single=is_single,
        has_metadata=has_mb_meta,
        is_featured_track=is_featured_track,
        is_live_track=is_live_track,
        is_instrumental_track=is_instrumental_track,
        lastfm_weight_override=lastfm_weight_override,
        single_boost=cfg_single_boost,
        metadata_score_floor=cfg_floor,
        live_weight_penalty=cfg_live_penalty,
        instrumental_weight_penalty=cfg_instrumental_penalty,
    )

    try:
        lb_percentile = calculate_listenbrainz_percentile(listenbrainz_listens, album_lb_listens) if album_lb_listens else 0.0
    except Exception:
        lb_percentile = 0.0

    return score_data, lb_percentile


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
    _force_meta = bool(force_meta) or _track_needs_metadata_enrichment(track)

    mb_data = None
    if title and artist:
        if frozen_track or (_has_mbid and _has_genres and not _force_meta):
            logger.debug("Skipping MB metadata lookup", track_id=track_id)
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
            if recording_mbid:
                payload["recording_mbid"] = recording_mbid
                payload["mbid"] = recording_mbid
            
            _mb_year = _as_str(mb_data.get("year") or "").strip()
            _existing_year = _as_str(track.get("year") or "").strip()
            if _mb_year:
                if not _existing_year or _force_meta or int(_mb_year[:4]) < int(_existing_year[:4] or "9999"):
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
    _isrc_found: str = ""
    _pop_summary: str = ""
    _single_summary: str = ""

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

    # 1. POPULARITY
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
            
            lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
            listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)

            # Strict backend-only live detection for suffix evaluation
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
                is_single=bool(effective_track.get("is_single")),
                has_mb_meta=bool(recording_mbid),
                is_featured_track=is_featured_flag,
                is_live_track=is_live_flag,
                is_instrumental_track=is_instrumental_flag,
                artist_lf_context=artist_lf_context,
                track_duration=_safe_duration(effective_track.get("duration")),
            )

            update_payload.update(score_data)
            combined = score_data.get("combined_score", 0.0)
            update_payload["final_score"] = combined
            update_payload["popularity"] = combined
        except Exception as e:
            logger.warning("Scoring failed", track_id=track_id, error=str(e))

    # 2. METADATA FETCH (MusicBrainz, Discogs, ListenBrainz)
    if not popularity_only and not singles_detection_only:
        try:
            title = _genre_lookup_title or track_title
            artist = _genre_lookup_artist or track_artist
            mb_data = (_mb_meta or {}).get("mb_data")
            _force_meta = bool((_mb_meta or {}).get("force_meta"))

            # MusicBrainz Genres (Stored natively as list/JSONB)
            if title and artist and (not _has_real_genres(track) or _force_meta):
                try:
                    mb_raw = get_shared_mb_client()
                    mb_genres: list[Any] = []
                    _rec_mbid = str(
                        (mb_data or {}).get("recording_mbid")
                        or track.get("recording_mbid")
                        or track.get("mbid")
                        or ""
                    ).strip()
                    if _rec_mbid:
                        _rec = mb_raw.get_recording(_rec_mbid, inc="genres+tags") or {}
                        mb_genres = _rec.get("genres") or []
                    if mb_genres:
                        update_payload["musicbrainz_genres"] = [g.get("name") for g in mb_genres if isinstance(g, dict) and g.get("name")]
                except Exception as e:
                    logger.debug("MB genre fetch failed", error=str(e))

            # Discogs Genres
            if title and artist:
                try:
                    from api_clients.discogs_http import DiscogsHttpClient
                    from helpers.config_helpers import get_config as _get_disc_cfg
                    _tok = str((_get_disc_cfg().get("api_integrations", {}).get("discogs", {}) or {}).get("token") or "")
                    if _tok and _tok.lower() not in ("your_discogs_token", "placeholder"):
                        discogs = DiscogsHttpClient(token=_tok)
                        results = discogs.search_database({"q": f"{artist} {title}", "type": "release", "per_page": 1}) or []
                        if results:
                            genres = results[0].get("genre", []) or []
                            styles = results[0].get("style", []) or []
                            if genres or styles:
                                update_payload["discogs_genres"] = list(set(genres + styles))
                except Exception as e:
                    logger.debug("Discogs genre fetch failed", error=str(e))

            # ListenBrainz Genres
            if title and artist:
                try:
                    _lb_mbid = (mb_data or {}).get("recording_mbid") or track.get("recording_mbid")
                    if _lb_mbid:
                        lb_tags = get_recording_tags(_lb_mbid) or []
                        names = [str(t.get("tag") or t.get("name") or "").strip() for t in lb_tags if isinstance(t, dict)]
                        if names:
                            update_payload["listenbrainz_genres"] = [n for n in names if n]
                except Exception as e:
                    logger.debug("LB genre fetch failed", error=str(e))
        except Exception as e:
            logger.debug("Metadata fetch failed", error=str(e))

    # 3. GENRE AGGREGATION
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
            logger.debug("Genre aggregation failed", error=str(e))

    # 4. PERSISTENCE
    effective_track = _strip_album_type_columns(track, update_payload)
    try:
        insert_or_update_track(track_id, effective_track)
    except Exception as e:
        logger.warning("DB Persist failed", track_id=track_id, error=str(e))

    return {
        "track_id": track_id,
        "artist": track_artist,
        "album": track.get("album") or effective_track.get("album", ""),
        "title": track.get("title") or effective_track.get("title") or "",
        "popularity_score": float(update_payload.get("final_score") or 0),
        "final_score": float(update_payload.get("final_score") or 0),
    }
