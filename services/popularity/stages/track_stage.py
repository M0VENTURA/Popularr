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
from collections import Counter
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
    _is_alternate_performance_title,
    album_recording_batch_key,
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
    is_instrumental_track_title as is_instrumental_track,
    is_live_or_alternate_track_title,
)

# Genre aggregation
from services.enrichment.genre_aggregation_service import aggregate_genres, _parse_genre_input

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


_ALBUM_LIVE_TYPE_FIELDS: tuple[str, ...] = (
    "musicbrainz_albumtype",
    "musicbrainz_type",
    "detected_album_type",
    "musicbrainz_album_type",
    "album_type",
    "releasetype",
    # Raw, pre-corroboration MusicBrainz secondary type (e.g. "album+live"),
    # set by album_stage._resolve_album_type()/enrich_album() regardless of
    # whether the corroboration guard accepted it for the persisted/display
    # "detected_album_type". A release MusicBrainz confirms as live but whose
    # local title/track-title heuristics don't corroborate gets its
    # "detected_album_type" safely downgraded to a plain "album" -- so that
    # field alone is NOT reliable evidence of liveness. This field is: it is
    # never downgraded, only ever set from MusicBrainz's own classification.
    "musicbrainz_secondary_type_raw",
)


def _album_type_indicates_live(
    *sources: dict[str, Any] | None,
) -> bool:
    """True if any known album-type field across the given dicts reads as live.

    MusicBrainz secondary types compose as e.g. ``"album+live"``, so this is a
    substring match rather than an exact one. Checked across every dict the
    caller has on hand (track row, effective track, album_context,
    album_result) because which one actually carries the type varies by scan
    mode and by whether the album stage has already run this pass.

    ``album_context.get("live_album_type")`` is included as a direct boolean/
    string signal on top of the substring scan: it is set by the scan
    runner's ``_refresh_album_live_context`` specifically to record a live
    classification, separate from ``detected_album_type``.

    ``musicbrainz_secondary_type_raw`` (in ``_ALBUM_LIVE_TYPE_FIELDS`` above)
    is the important one for a release where MusicBrainz says live but local
    title heuristics disagree: ``detected_album_type``/``musicbrainz_albumtype``
    get safely downgraded to a plain "album" in that case (by design, to avoid
    destructively retitling studio tracks), so checking only those fields
    would miss exactly the release this whole check exists for. The raw
    field is never downgraded, so it still reads "album+live" even when every
    other field in ``sources`` says "album".
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        _lat = source.get("live_album_type")
        if _lat:
            return True
        for field in _ALBUM_LIVE_TYPE_FIELDS:
            raw = source.get(field)
            if raw and "live" in str(raw).strip().lower():
                return True
    return False


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

    album_source_map: dict[str, list[str]] = {}
    _source_cols = [
        ("musicbrainz", "musicbrainz_genres"),
        ("discogs", "discogs_genres"),
        ("lastfm", "lastfm_tags"),
        ("listenbrainz", "listenbrainz_genres"),
        ("spotify", "spotify_genres"),
        ("navidrome", "navidrome_genres"),
        ("audiodb", "audiodb_genres"),
        ("wikidata", "wikidata_genres"),
    ]

    for _at in album_tracks:
        if not isinstance(_at, dict):
            continue
        for _src, _col in _source_cols:
            raw = _at.get(_col)
            if not raw:
                continue
            _parsed_list = _parse_genre_input(raw)
            if not isinstance(_parsed_list, list):
                continue
            for _g in _parsed_list:
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
                           listenbrainz_genres, spotify_genres, navidrome_genres,
                           audiodb_genres, wikidata_genres
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND (
                        COALESCE(musicbrainz_genres::text, '') <> ''
                        OR COALESCE(discogs_genres::text, '') <> ''
                        OR COALESCE(lastfm_tags::text, '') <> ''
                        OR COALESCE(listenbrainz_genres::text, '') <> ''
                        OR COALESCE(spotify_genres::text, '') <> ''
                        OR COALESCE(navidrome_genres::text, '') <> ''
                        OR COALESCE(audiodb_genres::text, '') <> ''
                        OR COALESCE(wikidata_genres::text, '') <> ''
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


def _album_recording_batch_keys(
    *,
    primary_artist: str,
    primary_title: str,
    batch_artist: str = "",
    batch_title: str = "",
    disc: Any = None,
    track_number: Any = None,
) -> list[str]:
    """Candidate batch keys, MOST SPECIFIC FIRST.

    The row-specific form (``artist::title::disc::track``) names exactly one
    track, so it is tried before the legacy title-only form. Two rows of a
    single album can share a title and still be DIFFERENT recordings on the
    album's release — dArtagnan's "Helden X Hymnen" files the album's title track
    AND its "(Unplugged Version)" rendition both as "Helden X Hymnen" — and only
    the row-specific key keeps them apart.

    Both forms are built through ``album_recording_batch_key`` so this consumer
    and its producer (``scan_stage_runner._build_album_recording_batch``) cannot
    drift out of step.
    """
    keys: list[str] = []
    for _artist, _title in ((primary_artist, primary_title), (batch_artist, batch_title)):
        if not _artist or not _title:
            continue
        for _key in (
            album_recording_batch_key(_artist, _title, disc, track_number),
            album_recording_batch_key(_artist, _title),
        ):
            if _key and _key not in keys:
                keys.append(_key)
    return keys


def _album_edition_annotation(
    album_context: dict[str, Any] | None,
    album_tracks: list[dict[str, Any]] | None = None,
) -> str | None:
    """The version annotation the album being scanned agrees on, or None.

    WHY: a release routinely marks only SOME of its titles — on the unplugged
    edition of an album most read "Song (Unplugged Version)" while a few are
    tagged plainly as "Song". Every per-title check then fails for exactly
    those tracks, because ``edition_annotations_compatible("Song", "Song
    (Unplugged Version)")`` is False while ``("Song", "Song")`` is True — so
    the recording search skips the unplugged recording and takes the
    identically titled STUDIO one. The track then carries the studio
    recording's MBID and every popularity figure read through it is the studio
    version's, which is the reported "used the wrong version of the tracks
    when doing the popularity scoring".

    Resolved from the album NAME first, then by majority across the album's
    track titles. See ``helpers.normalization_service.album_version_annotation``.
    """
    context = album_context if isinstance(album_context, dict) else {}
    album_name = _as_str(context.get("album"))

    titles: list[str] = []
    for source in (album_tracks, context.get("tracks")):
        for entry in (source or []):
            if isinstance(entry, dict):
                titles.append(_as_str(entry.get("title")))

    try:
        from helpers.normalization_service import album_version_annotation
        return album_version_annotation(album_name, titles)
    except Exception as exc:
        logger.debug("Album version annotation lookup failed", error=str(exc))
        return None


_GENRE_SOURCE_COLUMNS = (
    "musicbrainz_genres",
    "discogs_genres",
    "listenbrainz_genres",
    "spotify_genres",
    "lastfm_tags",
    "audiodb_genres",
    "wikidata_genres",
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
_STALE_PROTECTED_COLUMNS = frozenset({"title", "album_artist"}) | _ALBUM_TYPE_COLUMNS | _ALBUM_MBID_COLUMNS

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


def _looks_like_json_fragment(value: str) -> bool:
    """True if a string still looks like it holds JSON syntax rather than a plain name."""
    stripped = value.strip()
    if not stripped:
        return False
    if stripped[0] in "[{" or stripped[-1] in "]}":
        return True
    if "''" in stripped:
        return True
    if '\\"' in stripped:
        return True
    return False


def _coerce_writer_list(raw: Any) -> list[str]:
    """Safely coerce a writer value into a clean list of plain strings."""
    value: Any = raw

    for _ in range(5):
        if isinstance(value, list):
            break
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.lower() in ("[]", "null", "none"):
                return []
            try:
                value = json.loads(stripped)
                continue
            except Exception:
                value = [stripped]
                break
        else:
            return []
    else:
        return []

    if not isinstance(value, list):
        return []

    flat_writers: list[str] = []

    def _add_candidate(candidate: Any) -> None:
        if isinstance(candidate, (list, tuple)):
            for sub in candidate:
                _add_candidate(sub)
            return
        text = str(candidate or "").strip().strip("'\"").strip()
        if not text:
            return
        if _looks_like_json_fragment(text):
            return
        if text not in flat_writers:
            flat_writers.append(text)

    for item in value:
        _add_candidate(item)

    return flat_writers


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

    _scored = calculate_combined_popularity_score(
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

    if isinstance(_scored, tuple):
        score_data = _scored[0] if _scored else {}
    else:
        score_data = _scored

    if not isinstance(score_data, dict):
        logger.warning(
            "Unexpected score payload type from calculate_combined_popularity_score",
            track_id=track_id,
            payload_type=type(score_data).__name__,
        )
        score_data = {}

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
    album_context: dict[str, Any] | None = None,
    album_result: dict[str, Any] | None = None,
    edition_annotation: str | None = None,
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
        # The album-release identity is looked up BEFORE the "already fully
        # resolved" short-circuit, so a stored MBID that is NOT the recording
        # the album's own MusicBrainz release puts at this track can be
        # corrected. Leaving it out of reach is what kept the unplugged tracks
        # of dArtagnan's "Helden X Hymnen" on the STUDIO recordings for good:
        # with a recording MBID and genres already present this function
        # returned early and never consulted the batch at all.
        _batch_mb = options.get("mb_batch_metadata") or {}
        for _batch_key in _album_recording_batch_keys(
            primary_artist=artist,
            primary_title=title,
            batch_artist=batch_artist,
            batch_title=batch_title,
            disc=track.get("disc_number"),
            track_number=track.get("track_number"),
        ):
            mb_data = _batch_mb.get(_batch_key)
            if mb_data:
                break

        _batch_mbid = _as_str((mb_data or {}).get("recording_mbid")).strip()
        _stored_mbid = _as_str(
            track.get("recording_mbid") or track.get("mbid") or track.get("musicbrainz_trackid")
        ).strip()
        _batch_corrects = bool(_batch_mbid) and _batch_mbid != _stored_mbid

        if frozen_track or (
            _has_mbid and _has_genres and not _force_meta and not _batch_corrects
        ):
            logger.debug("Skipping MB metadata lookup", track_id=track_id, reason="frozen or fully resolved")
            mb_data = None
        else:
            if _batch_corrects:
                logger.info(
                    "[MB] album-release recording identity applied",
                    track_id=track_id,
                    title=title,
                    stored_mbid=_stored_mbid or None,
                    release_mbid=_batch_mbid,
                )

            _from_batch = bool(mb_data)

            if not mb_data:
                _track_mbid = _as_str(track.get("recording_mbid") or track.get("mbid") or track.get("musicbrainz_trackid")).strip()

                # Only resolved for the search below (and for the composer
                # lookup it enables): a batch hit already carries the metadata a
                # search would have fetched for the album's own recording, so it
                # must not touch the MusicBrainz service at all.
                mb_service = get_shared_mb_service()
                # Pass the album being scanned so the recording is pinned to
                # THAT album's release. Without it, MusicBrainz's arbitrary
                # release ordering let a track adopt a live-tour album, a
                # compilation or a single as its album — splitting one folder
                # into many and titling it with the wrong release group.
                _album_anchor = _as_str(
                    album_context.get("album") if isinstance(album_context, dict) else ""
                ).strip()
                # Release-level liveness. A live album routinely ships PLAINLY
                # TITLED tracks ("Enter Sandman" on S&M is titled exactly as
                # its studio original), so the studio and live recordings score
                # an identical title match and the studio one wins by relevance
                # order. The track then carries the STUDIO recording's MBID,
                # and every popularity figure read from it (ListenBrainz
                # listens via that MBID, Last.fm via the release-scoped match)
                # is the studio recording's — which is why live albums were
                # scoring like studio albums. Passing the flag lets the search
                # prefer the live recording.
                _is_live_release = bool(
                    _album_type_indicates_live(track, album_context, album_result)
                    or is_live_or_alternate_track_title(title)
                )
                # The album's own version annotation, for a title that carries
                # none: without it a plainly tagged track on an unplugged (or
                # acoustic) album resolves to the STUDIO recording. See
                # ``_album_edition_annotation``.
                #
                # ``edition_annotation`` IS that value here: ``process_track``
                # computes it once and passes it in. Referring to the caller's
                # local (``_album_annotation``) from inside this function would
                # raise NameError — swallowed by the caller's debug-level
                # handler — and silently turn per-track MB resolution off.
                mb_data = mb_service.lookup_recording_metadata(
                    title,
                    artist,
                    album=_album_anchor or None,
                    is_live_release=_is_live_release,
                    edition_annotation=edition_annotation,
                    mbid=_track_mbid or None,
                )
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
                _raw_existing_writer = track.get("writer")
                _sanitized_existing_writer = _coerce_writer_list(_raw_existing_writer) if _raw_existing_writer else []
                _existing_writer_already_clean = False
                if _raw_existing_writer:
                    try:
                        _parsed_existing = json.loads(_raw_existing_writer) if isinstance(_raw_existing_writer, str) else _raw_existing_writer
                        _existing_writer_already_clean = isinstance(_parsed_existing, list) and _parsed_existing == _sanitized_existing_writer
                    except Exception:
                        _existing_writer_already_clean = False

                if _raw_existing_writer and not _existing_writer_already_clean:
                    payload["writer"] = json.dumps(_sanitized_existing_writer, ensure_ascii=False)

                if recording_mbid and not _sanitized_existing_writer:
                    _batch_writer = (mb_data or {}).get("writer") or []
                    if not _batch_writer and not _from_batch:
                        try:
                            _batch_writer = mb_service.get_composers_for_recording(recording_mbid) or []
                        except Exception as exc:
                            logger.debug("Composer fetch failed", track_id=track_id, error=str(exc))

                    flat_writers = _coerce_writer_list(_batch_writer)
                    if flat_writers:
                        payload["writer"] = json.dumps(flat_writers, ensure_ascii=False)

            if mb_data.get("title"):
                payload["musicbrainz_title"] = mb_data["title"]

            _artist_mbid = mb_data.get("artist_mbid")
            if _artist_mbid and not _as_str(track.get("musicbrainz_artistid") or track.get("musicbrainz_artist_id")):
                payload["musicbrainz_artistid"] = _artist_mbid

            _mb_isrc = _as_str(mb_data.get("isrc") or "").strip()
            if _mb_isrc and not _as_str(track.get("isrc") or "").strip():
                payload["isrc"] = _mb_isrc

            # STRICT ALBUM COHESION GUARD:
            # Never allow a track-level recording match to rename an existing album.
            # Recordings map to many releases (compilations, live bootlegs), which
            # shatters album grouping. Only fill if completely missing.
            _existing_album = _as_str(track.get("album") or "").strip()
            _mb_album = _as_str(mb_data.get("album") or "").strip()
            if _mb_album and not _existing_album:
                payload["album"] = _mb_album

            _existing_artist = _as_str(track.get("artist") or "").strip()
            _mb_artist = _as_str(mb_data.get("artist") or "").strip()
            if _mb_artist:
                from helpers.normalization_service import (
                    is_track_artist_placeholder,
                    normalize_artist,
                )

                # An album-level placeholder ("Various Artists") in the ARTIST
                # column is not a track artist, so replacing it with the
                # recording's own artist credit is a CORRECTION and needs no
                # forced metadata pass. Without this a compilation kept
                # "Various Artists" for every track the batch/MB resolved — the
                # reported "track artist is still not updating for a compilation
                # during a scan" — and those tracks were then reported as
                # COVERS, because the original artist (from the ISRC) can never
                # match a placeholder.
                if is_track_artist_placeholder(_existing_artist) and normalize_artist(
                    _mb_artist
                ) != normalize_artist(_existing_artist):
                    payload["artist"] = _mb_artist
                    logger.info(
                        "Compilation placeholder artist replaced from MusicBrainz",
                        track_id=track_id,
                        old=_existing_artist,
                        new=_mb_artist,
                    )
                elif not _existing_artist:
                    payload["artist"] = _mb_artist
                elif _force_meta and _mb_artist != _existing_artist:
                    if normalize_artist(_mb_artist) != normalize_artist(_existing_artist):
                        payload["artist"] = _mb_artist
                        logger.info(
                            "Artist corrected from MusicBrainz",
                            track_id=track_id,
                            old=_existing_artist,
                            new=_mb_artist,
                        )

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

            # Release (edition) identity.  ``album`` holds the release GROUP
            # name; the SPECIFIC edition's name and its own year are stored
            # separately so the album page can show an edition tagline
            # ("Experience: Expanded (Remixes and B-Sides)").
            #
            # ``year`` is the album's ORIGINAL year (handled above), so
            # ``release_year`` is the ONLY place the edition's year lives.
            # Both respect an existing value unless this is a forced metadata
            # pass, so a manual edit is never silently clobbered.
            _mb_release_title = _as_str(mb_data.get("release_title") or "").strip()
            if _mb_release_title and (
                _force_meta or not _as_str(track.get("release_title") or "").strip()
            ):
                payload["release_title"] = _mb_release_title

            _mb_release_year = mb_data.get("release_year")
            if _mb_release_year and (
                _force_meta or not _as_str(track.get("release_year") or "").strip()
            ):
                try:
                    payload["release_year"] = int(str(_mb_release_year)[:4])
                except (TypeError, ValueError):
                    logger.debug(
                        "Discarded unparsable release year",
                        track_id=track_id,
                        value=_mb_release_year,
                    )

    return {
        "mb_data": mb_data,
        "payload": payload,
        # Use the MusicBrainz-corrected identity downstream.
        "artist": _as_str(payload.get("artist") or artist),
        "title": _as_str(
            payload.get("musicbrainz_title")
            or payload.get("title")
            or title
        ),
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

    # Carry release-level/current-pass liveness through to finalisation.
    _resolved_is_live = bool(
        track.get("is_live")
        or track.get("album_context_live")
        or album_context.get("is_live_album")
        or _album_type_indicates_live(track, album_context, album_result)
        or is_live_or_alternate_track_title(track_title)
    )

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
        update_payload["_raw_combined"] = float(score_data["combined_score"])

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

    # The album's version annotation, for any track whose OWN title lacks it.
    # Computed once because BOTH recording lookups below need it — the MB
    # metadata pass and the ListenBrainz MBID fallback. See
    # ``_album_edition_annotation`` for the reported failure it prevents.
    _album_annotation = _album_edition_annotation(album_context, album_tracks)

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
                album_context=album_context,
                album_result=album_result,
                edition_annotation=_album_annotation,
            )
        except Exception as exc:
            logger.debug("MB pre-resolution failed", track_id=track_id, error=str(exc))

        if _mb_meta:
            _genre_lookup_artist = _mb_meta.get("artist")
            _genre_lookup_title = _mb_meta.get("title")
            update_payload.update(_mb_meta.get("payload") or {})
            
        # Inherit new external genres from the album-level fetch
        if album_context.get("audiodb_genres"):
            update_payload["audiodb_genres"] = album_context["audiodb_genres"]
        if album_context.get("wikidata_genres"):
            update_payload["wikidata_genres"] = album_context["wikidata_genres"]

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

            # Release-level liveness, resolved once up-front so it can gate
            # the Last.fm aggregation calls below. A live release frequently
            # ships plainly-titled tracks (e.g. every track on Metallica's
            # "S&M" is titled exactly as its studio original), so Last.fm's
            # title+artist matching would otherwise merge the STUDIO
            # recording's catalogue-wide listener count into the live
            # track's popularity data -- the count is real, it's just for
            # the wrong recording. ListenBrainz is unaffected because it
            # matches on recording MBID, not title.
            #
            # This mirrors the OR-of-conditions used later for
            # ``is_live_flag`` (title-marker regex + explicit flags), minus
            # the tag-based check, which depends on genre columns that are
            # not populated in ``update_payload`` until the metadata section
            # further down -- checking it here would never find anything,
            # same as it effectively never did in the original later-computed
            # flag before this fix.
            #
            # ``album_context.get("is_live_album")`` alone is NOT sufficient:
            # the album stage can reject a MusicBrainz "album+live" secondary
            # type when track titles don't corroborate it (e.g. every track
            # on Metallica's "S&M" is titled exactly as its studio original),
            # which leaves ``is_live_album`` False even though MusicBrainz
            # classified the release as live. ``_album_type_indicates_live``
            # checks the raw album-type fields directly -- across the track
            # row, the effective (post-metadata-merge) track, album_context,
            # and album_result -- so a "album+live" classification is honoured
            # here regardless of whether that title-corroboration heuristic
            # accepted or rejected it.
            is_live_release = bool(
                effective_track.get("is_live")
                or effective_track.get("album_context_live")
                or album_context.get("is_live_album")
                or _album_type_indicates_live(track, effective_track, album_context, album_result)
                or bool(re.search(r"[\(\[]\s*(live|acoustic|unplugged)[^)\]]*[\)\]]\s*$", str(raw_title or title).lower()))
            )

            # A version-marked RENDITION the local title does not declare.
            #
            # MusicBrainz's own release tracklist is authoritative here: the
            # album's release lists "Fur immer Dein (Unplugged Version)" while
            # the library file is titled plainly, and the identity resolved from
            # that release (``options["mb_batch_metadata"]``) carries the
            # version-marked title through as ``musicbrainz_title``. Providers
            # resolve by title+artist, so without this the plainly titled
            # unplugged track absorbs the STUDIO recording's listeners -- the
            # reported dArtagnan "Helden X Hymnen" inflation (7.5k Last.fm
            # listeners on a track whose album-mates sit at 300-500, enough to
            # lock it as an album 5-star top track).
            #
            # Deliberately NOT folded into ``is_live_release``: this is an
            # alternate RENDITION, not a live recording, so it must not pick up
            # the live weight penalty or the live star caps. Only the provider
            # "alternate performance" test consumes it.
            _alt_rendition = _is_alternate_performance_title(
                _as_str(effective_track.get("musicbrainz_title") or "")
            )

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
                _stored_score = float(effective_track.get("final_score") or effective_track.get("popularity") or 0)
                score_data = {
                    "combined_score": _stored_score,
                    "lastfm_score": float(effective_track.get("lastfm_score", 0)),
                    "listenbrainz_score": float(effective_track.get("listenbrainz_score", 0)),
                    "age_score": float(effective_track.get("age_score", 0)),
                }
                update_payload["_cached"] = True
                update_payload["_raw_combined"] = _stored_score
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
                    # FIXED: ``_prefetch_entry`` is populated once per ARTIST by
                    # prefetch_artist_popularity(), keyed only by normalised
                    # title -- it cannot distinguish a live recording from its
                    # studio namesake, so its ``lastfm_listeners`` is exactly
                    # the catalogue-wide contaminated count this whole
                    # is_live_release mechanism exists to avoid. The
                    # ``_album_tracklist`` flag below marks the entry as
                    # touched by the release-scoped ListenBrainz backfill, but
                    # that flag lives on the SAME dict as the unrelated,
                    # still-contaminated ``lastfm_listeners`` field, so it
                    # does not make the LF value release-scoped -- only the LB
                    # fields it was set for. On a live release, skip this
                    # cached value entirely and fall through to the direct,
                    # ``is_live_release``-aware aggregation call below instead.
                    if (
                        _prefetch_entry
                        and _prefetch_entry.get("lastfm_listeners")
                        and not is_live_release
                    ):
                        lastfm_listeners = _as_int(_prefetch_entry.get("lastfm_listeners") or 0)
                        lastfm_playcount = _as_int(_prefetch_entry.get("lastfm_playcount") or 0)
                        update_payload["lastfm_listeners"] = lastfm_listeners
                        update_payload["lastfm_playcount"] = lastfm_playcount
                        update_payload["lastfm_last_updated"] = now_ts
                        update_payload["_from_prefetch"] = True
                        if not effective_track.get("lastfm_tags") and _prefetch_entry.get("lastfm_tags"):
                            update_payload["lastfm_tags"] = json.dumps(_prefetch_entry["lastfm_tags"], ensure_ascii=False)
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
                                    is_live_release=is_live_release,
                                    target_is_alt_rendition=_alt_rendition,
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
                                            update_payload["lastfm_tags"] = json.dumps(_agg_tags, ensure_ascii=False)
                                elif is_live_release:
                                    # A live release with no aggregated match must NOT
                                    # fall through to the bare title+artist lookup below
                                    # -- that lookup cannot distinguish the live
                                    # performance from its studio namesake and would
                                    # silently reintroduce the contamination this fix
                                    # exists to prevent.
                                    lastfm_listeners = 0
                                    lastfm_playcount = 0
                                else:
                                    lf_result = lf.get_track_info(artist, title)
                                    lastfm_listeners = _as_int(lf_result.get("listeners") if isinstance(lf_result, dict) else 0)
                                    lastfm_playcount = _as_int(lf_result.get("track_play") if isinstance(lf_result, dict) else 0)

                                    toptags = lf_result.get("toptags", {}) if isinstance(lf_result, dict) else {}
                                    tag_list = toptags.get("tag", []) if isinstance(toptags, dict) else []
                                    if tag_list:
                                        update_payload["lastfm_tags"] = json.dumps(
                                            [t.get("name", "") for t in tag_list if isinstance(t, dict) and t.get("name")],
                                            ensure_ascii=False
                                        )

                                update_payload["lastfm_listeners"] = lastfm_listeners
                                update_payload["lastfm_playcount"] = lastfm_playcount
                                update_payload["lastfm_last_updated"] = now_ts
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
                                is_live_release=is_live_release,
                                target_is_alt_rendition=_alt_rendition,
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
                                    _isrc_rec = resolve_isrc_recording(
                                        isrc, title=raw_title or title, artist=artist,
                                        is_live_release=is_live_release,
                                    )
                                    if _isrc_rec and _isrc_rec.get("recording_mbid"):
                                        recording_mbid = _isrc_rec["recording_mbid"]
                                        _lb_source = "isrc_resolved"

                                if not recording_mbid:
                                    _batch_mb = options.get("mb_batch_metadata") or {}
                                    _mb_entry = _batch_mb.get(f"{artist.lower()}::{str(raw_title or title).lower()}")
                                    if _mb_entry and _mb_entry.get("recording_mbid"):
                                        recording_mbid = _mb_entry["recording_mbid"]
                                    else:
                                        # ``is_live_release`` matters here for the
                                        # same reason it does in the metadata
                                        # lookup: a plainly titled live track must
                                        # not resolve to its studio namesake, or
                                        # the ListenBrainz count fetched for that
                                        # MBID is the STUDIO recording's — which
                                        # is how a live album scored like a studio
                                        # album. ``edition_annotation`` extends the
                                        # same protection to every other version
                                        # marker (unplugged, acoustic, ...).
                                        recording_mbid, _conf = get_shared_mb_service().get_suggested_mbid(
                                            raw_title or title,
                                            artist,
                                            is_live_release=is_live_release,
                                            edition_annotation=_album_annotation,
                                        )

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

                # ``is_live_flag`` folds in the tag-based check (which needs
                # ``update_payload`` as populated by the LF/LB fetches above)
                # on top of the release-level ``is_live_release`` resolved
                # up-front for the Last.fm calls. Recomputing the title/flag
                # portion here would be redundant with ``is_live_release``,
                # so it is reused directly.
                is_live_flag = bool(
                    is_live_release
                    or _has_safe_live_recording_tag(update_payload)
                )
                _resolved_is_live = bool(_resolved_is_live or is_live_flag)
                is_instrumental_flag = is_instrumental_track(raw_title or title)
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

    if _sd_fresh:
        update_payload["is_single"] = bool(track.get("is_single", False))
        update_payload["single_confidence"] = str(track.get("single_confidence") or "low")
        update_payload["single_status"] = str(track.get("single_status") or "none")
        update_payload["single_sources"] = track.get("single_sources") or "[]"
        _single_summary = f"Single: {str(update_payload['single_confidence']).upper()} (cached)"

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
                    _sd_album_artist = _as_str(
                        effective_track.get("album_artist")
                        or album_context.get("album_artist")
                        or ""
                    ).strip().casefold()
                    _is_comp_album = bool(
                        album_context.get("is_va_compilation")
                        or _sd_album_artist in {
                            "various artists", "various", "va", "v/a",
                            "compilation", "soundtrack", "soundtracks",
                        }
                        or "various artists" in str(sd_album or "").casefold()
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

            # Persist only source-backed single evidence. Popularity is not
            # proof of a single release, and generic code must not contain
            # artist-specific title safeguards. Curated exceptions belong in
            # single_manual_override or an ISRC/MBID-backed override store.
            if sd_result:
                update_payload["is_single"] = bool(sd_result.get("is_single", False))
                update_payload["single_confidence"] = sd_result.get("confidence", "low")
                update_payload["single_confidence_score"] = sd_result.get("confidence_score", 0.0)
                update_payload["single_status"] = sd_result.get("single_status", "none")
                update_payload["single_sources"] = json.dumps(
                    sd_result.get("sources", []),
                    ensure_ascii=False,
                )
                update_payload["single_detection_last_updated"] = sd_now
                _sd_conf_str = str(
                    update_payload.get("single_confidence", "low") or "low"
                ).upper()
                _sd_chips = _single_chips(update_payload.get("single_sources"))
                _single_summary = f"Single: {_sd_conf_str} {_sd_chips}".strip()
            else:
                if _sd_manual_override:
                    _single_summary = "Single: SKIPPED (manual override)"
                else:
                    update_payload["is_single"] = False
                    update_payload["single_confidence"] = "low"
                    update_payload["single_confidence_score"] = 0.0
                    update_payload["single_status"] = "none"
                    update_payload["single_sources"] = json.dumps([], ensure_ascii=False)
                    update_payload["single_detection_last_updated"] = sd_now
                    _single_summary = (
                        "Single: LOW (no source-backed single evidence)"
                        if _sd_eligible
                        else "Single: LOW (below top-50% album popularity)"
                    )
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
                        _mb_names = [g.get("name") for g in mb_genres if isinstance(g, dict) and g.get("name")]
                        update_payload["musicbrainz_genres"] = json.dumps(_mb_names, ensure_ascii=False)
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
                                update_payload["discogs_genres"] = json.dumps(list(set(genres + styles)), ensure_ascii=False)
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
                            update_payload["listenbrainz_genres"] = json.dumps([n for n in names if n], ensure_ascii=False)
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
                    "writer": effective_track.get("writer"),
                    "work_mbid": effective_track.get("work_mbid"),
                }
                force_cover = bool(options.get("force_cover_detection"))

                # A title that carried a "(X Cover)" ATTRIBUTION had that
                # wording removed by the scan's identity pass — and the wording
                # was what made this detector fire in the first place, storing a
                # cover verdict for a track that is not one ("falsely created as
                # a cover"). Two consequences:
                #
                #   * the verdict must be RE-EVALUATED, not read from the cache:
                #     ``detect_cover_song`` short-circuits on an existing
                #     "already confirmed" verdict, so ``force`` is set when the
                #     wording was removed; and
                #   * a negative verdict must be WRITTEN. This block only ever
                #     wrote a positive result, because writing nothing leaves a
                #     confirmed verdict alone — which means clearing a false one
                #     is impossible without an explicit False.
                #
                # ``cover_manual_override`` still wins: that flag records a
                # decision the USER made, and nothing here may undo it.
                _cover_wording_removed = bool(track.get("title_had_cover_wording"))
                _cover_manual_override = bool(cover_data.get("cover_manual_override"))
                if _cover_wording_removed and not _cover_manual_override:
                    force_cover = True

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
                    _cover_list = ["Cover"] + [g for g in _mbg if g != "Cover"]
                    update_payload["musicbrainz_genres"] = json.dumps(_cover_list, ensure_ascii=False)
                elif _cover_wording_removed and not _cover_manual_override:
                    update_payload["is_cover"] = False
                    update_payload["is_cover_reason"] = "cover attribution removed from title"
                    # Drop the "Cover" genre the false verdict had added, or the
                    # genre playlists would keep filing the track as a cover.
                    _mbg = update_payload.get("musicbrainz_genres")
                    if _mbg is None:
                        _mbg = track.get("musicbrainz_genres")
                    if isinstance(_mbg, str):
                        try:
                            _mbg = json.loads(_mbg)
                        except Exception:
                            _mbg = []
                    if isinstance(_mbg, list):
                        _mbg = [g for g in _mbg if str(g).strip().lower() != "cover"]
                        update_payload["musicbrainz_genres"] = json.dumps(_mbg, ensure_ascii=False)
                    logger.info(
                        "[TRACK] false cover flag cleared",
                        track_id=track_id,
                        title=title,
                        detector_verdict=reason,
                    )
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
                ("audiodb_genres", "audiodb"),
                ("wikidata_genres", "wikidata"),
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
    #
    # BOTH year columns are pinned to ONE album-level verdict.  They used to
    # be written per track, and because MusicBrainz resolves each recording to
    # whichever release lists it first, a multi-edition album picked up a
    # different ``release_year`` on different tracks of the SAME folder.
    #
    # That split the album, because the UI groups albums on (name, year) and
    # falls back to ``release_year`` when ``year`` is empty:
    #   routes/ui_routes.py   album_key = f"{album.lower()}::{track_year}"
    #   dashboard SQL         GROUP BY ..., COALESCE(year, release_year)
    #
    # ``year`` is the album's ORIGINAL year (the release group); ``release_year``
    # is THIS edition's year and is a property of the release, not of the
    # individual recording — so every track of the folder must agree on it.
    #
    # The scan runner resolves the authoritative pair once per album and
    # supplies it via ``album_context``; the album-wide scan below is the
    # fallback for direct callers (and for tests) that pass no album_context.
    # -------------------------------------------------------------------------
    if not popularity_only and not singles_detection_only:
        try:
            _auth_year = album_context.get("authoritative_year")
            _auth_edition_year = album_context.get("authoritative_release_year")

            _album_years = []
            _album_edition_years = []
            for _at in (album_tracks or []):
                _y = _as_str(_at.get("year")).strip()
                if _y:
                    try:
                        _album_years.append(int(str(_y)[:4]))
                    except ValueError:
                        pass
                _ey = _as_str(_at.get("release_year")).strip()
                if _ey:
                    try:
                        _album_edition_years.append(int(str(_ey)[:4]))
                    except ValueError:
                        pass

            _pl_year = _as_str(update_payload.get("year")).strip()
            if _pl_year:
                try:
                    _album_years.append(int(str(_pl_year)[:4]))
                except ValueError:
                    pass

            # ── Original year ────────────────────────────────────────────
            if _auth_year is not None:
                _year_target = int(str(_auth_year)[:4])
            else:
                _year_target = min(_album_years) if _album_years else None

            if _year_target is not None:
                _curr_year = _as_str(update_payload.get("year") or track.get("year")).strip()

                _update_needed = False
                if not _curr_year:
                    _update_needed = True
                else:
                    try:
                        if int(str(_curr_year)[:4]) != _year_target:
                            _update_needed = True
                    except ValueError:
                        _update_needed = True

                if _update_needed:
                    update_payload["year"] = str(_year_target)

            # ── Edition year ─────────────────────────────────────────────
            # Majority verdict: the edition year belongs to the RELEASE, so the
            # value most tracks carry is the release's.  Ties go to the earlier
            # year so the outcome is deterministic.  Only written when known —
            # never clobbered to NULL, and never invented from the original.
            if _auth_edition_year is not None:
                _edition_target = int(str(_auth_edition_year)[:4])
            elif _album_edition_years:
                _edition_counts = Counter(_album_edition_years)
                _best_edition_count = max(_edition_counts.values())
                _edition_target = min(
                    year for year, count in _edition_counts.items()
                    if count == _best_edition_count
                )
            else:
                _edition_target = None

            if _edition_target is not None:
                _curr_edition = _as_str(
                    update_payload.get("release_year") or track.get("release_year")
                ).strip()
                _edition_needs_update = False
                if not _curr_edition:
                    _edition_needs_update = True
                else:
                    try:
                        if int(str(_curr_edition)[:4]) != _edition_target:
                            _edition_needs_update = True
                    except ValueError:
                        _edition_needs_update = True

                if _edition_needs_update:
                    update_payload["release_year"] = _edition_target
        except Exception as e:
            logger.debug("Album year unification failed", track_id=track_id, error=str(e))

    # -------------------------------------------------------------------------
    # 6. PERSISTENCE
    # -------------------------------------------------------------------------
    # ``album_artist`` sits in ``_STALE_PROTECTED_COLUMNS``, so a value the
    # scan's identity pass decided is DROPPED here unless it is declared in the
    # payload. The flag is set by ``scan_hooks.prepare_track_context`` only when
    # it actually filled an empty album artist, so a populated one is still
    # never overwritten by a pass that knows less than the user does.
    if track.get("album_artist_from_scan_identity"):
        _scan_album_artist = _as_str(track.get("album_artist")).strip()
        if _scan_album_artist:
            update_payload.setdefault("album_artist", _scan_album_artist)

    effective_track = _strip_album_type_columns(track, update_payload)

    _jsonb_fields = [
        "musicbrainz_genres", "discogs_genres", "lastfm_tags",
        "listenbrainz_genres", "spotify_genres", "essentia_genres",
        "manual_genres", "navidrome_genres", "single_sources", "writer",
        "audiodb_genres", "wikidata_genres"
    ]
    for _j_field in _jsonb_fields:
        _val = effective_track.get(_j_field)
        if isinstance(_val, (list, dict)):
            effective_track[_j_field] = json.dumps(_val, ensure_ascii=False)
        elif _val is None:
            effective_track[_j_field] = "[]"

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

    _ret_raw_combined = float(
        update_payload.get("_raw_combined")
        or track.get("_raw_combined")
        or _result_final_score
        or 0.0
    )
    _ret_is_single = bool(update_payload.get("is_single", track.get("is_single", False)))
    _ret_single_conf = str(update_payload.get("single_confidence") or track.get("single_confidence") or "low")
    _ret_single_srcs = update_payload.get("single_sources") or track.get("single_sources") or "[]"

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
        "_raw_combined": _ret_raw_combined,
        "lastfm_score": float(score_data.get("lastfm_score", 0)),
        "listenbrainz_score": float(score_data.get("listenbrainz_score", 0)),
        "is_single": _ret_is_single,
        "single_confidence": _ret_single_conf,
        "single_sources": _ret_single_srcs,
        "popularity_marked": bool(track.get("popularity_marked", False)),
        "is_live": bool(_resolved_is_live),
        "exclude_from_stats": bool(track.get("exclude_from_stats")),
    }
