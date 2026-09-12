"""
Per-track popularity/enrichment stage.

Connected to PostgreSQL JSONB native tracking and full completeness overrides.
"""

from __future__ import annotations

import json
import time
from difflib import SequenceMatcher
from typing import Any

import structlog

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import ListenBrainzClient
from api_clients.listenbrainz import get_recording_tags

from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    get_shared_mb_service,
)
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
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    get_aggregated_listenbrainz_popularity,
    get_search_aggregated_lastfm_popularity,
    get_work_level_listenbrainz_popularity,
)
from services.enrichment.single_detection_service import detect_single_for_track
from services.enrichment.cover_detection_service import detect_cover_song
from services.catalog.album_classification_service import (
    is_bonus_track_title,
    is_instrumental_track_title,
    is_live_or_alternate_track_title,
)
from db.repositories.tracks import insert_or_update_track
from helpers.normalization_service import (
    edition_annotations_compatible,
    safe_int,
    safe_str,
)
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
    import re
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
