"""Popularity scan finalisation stage.

Handles:
- Star rating assignment (1–5★) using album/artist z-scores + z-score bands
- Navidrome rating sync via Subsonic API
- Essential Collection API sync (deduplicated 4★/5★ artist best-of)
- Summary logging

NOTE: All playlist creation/maintenance (Essential Collections, genre
playlists, New Music) is done entirely through the Subsonic/Navidrome API.
No .m3u or .nsp files are written to disk anywhere in this module.

Write-amplification controls added in this revision:
- Playlist pushes are skipped when the resolved track list is byte-identical
  to the list already pushed for that playlist, so a repeated finalise pass
  no longer rewrites every playlist.
- Genre playlist and New Music rebuilds are global operations. They are now
  coalesced so a scan that finalises per artist/album runs them once rather
  than once per batch.
- The genre-playlist track fetch (a full-library query) is cached for a short
  TTL and shared between the full rebuild and the per-album refresh.
- Navidrome clients and the featured-track sweep are resolved once instead of
  once per artist.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from statistics import mean, median, stdev
from typing import Any

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from db.utils import row_get
from services.popularity.popularity_math import (
    age_skew_multiplier,
    apply_album_relative_popularity,
    calculate_robust_zscore,
    effective_album_ratio,
    calculate_percentile_star_rating,
    fmt_count as _fmt_count,
)
from services.popularity.popularity_zscore import composite_listener_z
from services.catalog.album_classification_service import (
    is_instrumental_track_title,
    is_live_or_alternate_track_title,
)

from helpers.config_helpers import get_standout_config
from helpers.logging_config import log_unified

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Live star-rating / era-scaling config
# ---------------------------------------------------------------------------

_DEFAULT_ERA_RULES: dict[str, dict[str, float | int]] = {
    "peak": {"catalog_top_pct": 0.20, "album_top_n": 3, "max_5star_slots": 4},
    "solid": {"catalog_top_pct": 0.15, "album_top_n": 3, "max_5star_slots": 3},
    "minor": {"catalog_top_pct": 0.10, "album_top_n": 3, "max_5star_slots": 2},
}


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _live_star_thresholds() -> dict[str, float]:
    """Star-tier z boundaries + epsilon buffer, read live from config."""
    cfg = get_standout_config() or {}

    def _tier(key: str, field: str, default: float) -> float:
        block = cfg.get(key) or {}
        return _safe_float(block.get(field), default)

    return {
        "star5_album_z": _tier("star_5", "album_z", 1.0),
        "star5_artist_z": _tier("star_5", "artist_z", 1.2),
        "star4_album_z": _tier("star_4", "album_z", 0.5),
        "star4_artist_z": _tier("star_4", "artist_z", 1.0),
        "star3_album_z": _tier("star_3", "album_z", -0.5),
        "star2_album_z": _tier("star_2", "album_z", -1.2),
        "epsilon": _safe_float(cfg.get("star_epsilon_score_points"), 0.5),
        "listener_5star_z": _safe_float(cfg.get("listener_5star_z_threshold"), 1.0),
    }


def _live_album_scaling() -> tuple[dict[str, dict[str, float | int]], float, float]:
    """Era rules + era-boundary ratios, read live from ``album_scaling``."""
    cfg = get_standout_config() or {}
    scaling = cfg.get("album_scaling") or {}
    if not isinstance(scaling, dict):
        scaling = {}
    rules: dict[str, dict[str, float | int]] = {}
    for era, defaults in _DEFAULT_ERA_RULES.items():
        rules[era] = {
            "catalog_top_pct": _safe_float(
                scaling.get(f"{era}_catalog_top_pct"), float(defaults["catalog_top_pct"])
            ),
            "album_top_n": int(_safe_float(
                scaling.get(f"{era}_album_top_n"), float(defaults["album_top_n"])
            )),
            "max_5star_slots": int(_safe_float(
                scaling.get(f"{era}_max_5star_slots"), float(defaults["max_5star_slots"])
            )),
        }
    peak_min = _safe_float(scaling.get("peak_era_min_ratio"), 0.75)
    solid_min = _safe_float(scaling.get("solid_era_min_ratio"), 0.40)
    return rules, peak_min, solid_min


# ---------------------------------------------------------------------------
# Standout detection helpers
# ---------------------------------------------------------------------------

def _compute_album_z(score: float, scores: list[float]) -> tuple[float, float]:
    """Robust album z (median + scaled-MAD) — and the spread used."""
    return calculate_robust_zscore(score, scores, min_count=3)


def _compute_artist_z(score: float, artist_scores: list[float]) -> tuple[float, float]:
    """Robust artist-catalogue z (median + scaled-MAD) — and the spread used."""
    return calculate_robust_zscore(score, artist_scores, min_count=5)


def _star_epsilon_z(spread: float, epsilon: float | None = None) -> float:
    """Convert the score-point epsilon buffer into z-units for a spread."""
    if not spread or spread <= 0:
        return 0.0
    if epsilon is None:
        epsilon = _live_star_thresholds()["epsilon"]
    return epsilon / spread


def _resolve_navidrome_artist_id(artist: str) -> str | None:
    """Return the real Navidrome artist id for ``artist``, or None."""
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT artist_id FROM artist_stats "
                    "WHERE LOWER(artist_name) = LOWER(:artist) "
                    "  AND LOWER(artist_id) <> LOWER(:artist) "
                    "LIMIT 1"
                ),
                {"artist": artist},
            ).fetchone()
            found = row_get(row, "artist_id") if row else None
            if found and str(found).strip() and str(found).casefold() != str(artist).casefold():
                return str(found).strip()
    except Exception as exc:
        logger.debug("artist_stats id lookup failed", artist=artist, error=str(exc))
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT artist_id FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "  AND artist_id IS NOT NULL AND artist_id <> '' "
                    "  AND LOWER(artist_id) <> LOWER(:artist) "
                    "LIMIT 1"
                ),
                {"artist": artist},
            ).fetchone()
            found = row_get(row, "artist_id") if row else None
            if found and str(found).strip() and str(found).casefold() != str(artist).casefold():
                return str(found).strip()
    except Exception as exc:
        logger.debug("tracks id lookup failed", artist=artist, error=str(exc))
    return None


# ---------------------------------------------------------------------------
# Star rating assignment
# ---------------------------------------------------------------------------

def _has_z_standout_source(track: dict[str, Any]) -> bool:
    """Return True when the track's single_sources carry ``popularity_z_standout``."""
    if is_instrumental_track_title(str(track.get("title") or "")):
        return False
    try:
        raw = track.get("single_sources") or ""
        if isinstance(raw, str):
            sources = json.loads(raw) if raw.strip() else []
        else:
            sources = raw
        return any(
            isinstance(s, dict) and str(s.get("source") or "") == "popularity_z_standout"
            for s in (sources or [])
        )
    except Exception:
        return False


def _album_z_band_star(
    score: float,
    album_scores: list[float],
    reference_scores: list[float] | None = None,
    artist_scores: list[float] | None = None,
    is_live: bool = False,
    single_confidence: str = "low",
) -> int:
    """Album-relative 1-4★ rating from the z-score bands."""
    if score <= 0:
        return 1
    valid = [float(s) for s in (reference_scores if reference_scores is not None else album_scores or []) if float(s or 0) > 0]
    if len(valid) < 3:
        return 3
    album_z, spread = _compute_album_z(score, valid)
    th = _live_star_thresholds()
    epsilon = _star_epsilon_z(spread, th["epsilon"])

    artist_eligible_4star = True
    if artist_scores:
        valid_artist = [float(s) for s in artist_scores if float(s or 0) > 0]
        if len(valid_artist) >= 5 and len(valid_artist) > len(valid):
            artist_z, artist_spread = _compute_artist_z(score, valid_artist)
            artist_eligible_4star = artist_z >= th["star4_artist_z"] - _star_epsilon_z(artist_spread, th["epsilon"])

    if is_live and album_z >= th["star4_album_z"] - epsilon:
        try:
            _requires_single = bool(
                get_standout_config().get("live_4star_requires_single", True)
            )
        except Exception:
            _requires_single = True
        if _requires_single and str(single_confidence or "low").strip().casefold() not in ("high", "medium", "user"):
            album_z = float("-inf")

    if album_z >= th["star4_album_z"] - epsilon and artist_eligible_4star:
        return 4
    if album_z >= th["star3_album_z"] - epsilon:
        return 3
    if album_z >= th["star2_album_z"] - epsilon:
        return 2
    return 1


def _assign_stars(
    track: dict[str, Any],
    album_scores: list[float],
    artist_scores: list[float],
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
    popularity_only: bool = False,
    album_model: dict[str, Any] | None = None,
    is_compilation: bool = False,
    artist_listen_distribution: list[float] | None = None,
    generic_compilation_artist: bool = False,
) -> int:
    """Assign 1–5 star rating to a single track."""
    score = float(track.get("popularity_score") or track.get("final_score") or 0)
    single_confidence = str(track.get("single_confidence") or "low").strip().casefold()

    track.pop("_era_5star", None)
    track.pop("_force_floor", None)

    is_live = (
        bool(track.get("is_live"))
        or bool(track.get("album_context_live"))
        or is_live_or_alternate_track_title(track.get("title"))
    )

    if single_confidence == "user":
        track["_global_5star_locked"] = True
        return 5

    th = _live_star_thresholds()

    try:
        from services.popularity.popularity_config import get_single_organic_floor
        _org_score, _org_listeners = get_single_organic_floor()
    except Exception:
        _org_score, _org_listeners = 45.0, 1000.0

    organic = score >= _org_score or int(track.get("lastfm_listeners") or 0) >= _org_listeners
    ref_scores = artist_scores if is_compilation else album_scores

    album_z, album_spread = _compute_album_z(score, ref_scores)
    artist_z, artist_spread = _compute_artist_z(score, artist_scores)
    popularity_marked = bool(track.get("popularity_marked"))

    if is_instrumental_track_title(str(track.get("title") or "")):
        popularity_marked = False

    if track.get("_global_5star_locked") and not popularity_only:
        if not is_live and organic:
            track["_global_5star_locked"] = True
            return 5

    z_standout_source = _has_z_standout_source(track)
    if z_standout_source:
        _verify_z = 0.0
        if album_lf_listeners is not None and album_lb_listens is not None:
            _verify_z = composite_listener_z(
                float(track.get("lastfm_listeners") or 0),
                float(track.get("listenbrainz_listens") or 0),
                artist=track.get("artist"),
                album=track.get("album"),
                album_lf_listeners=album_lf_listeners,
                album_lb_listens=album_lb_listens,
            )
        if _verify_z and _verify_z < th["listener_5star_z"]:
            z_standout_source = False

    is_standout = (
        album_z >= th["star5_album_z"] - _star_epsilon_z(album_spread, th["epsilon"])
        and artist_z >= th["star5_artist_z"] - _star_epsilon_z(artist_spread, th["epsilon"])
        and (popularity_marked or z_standout_source)
    )

    _force_stars: int | None = None
    if (
        not is_live
        and not is_compilation
        and not is_instrumental_track_title(str(track.get("title") or ""))
        and artist_listen_distribution
        and not popularity_only
    ):
        try:
            from services.popularity.popularity_config import get_artist_force_star_percentiles
            _force5_pct, _force4_pct = get_artist_force_star_percentiles()
            _track_listens = float(track.get("lastfm_listeners") or 0)
            if _track_listens > 0 and (_force5_pct > 0 or _force4_pct > 0):
                _valid = [float(v) for v in artist_listen_distribution if float(v or 0) > 0]
                if len(_valid) >= 5:
                    _valid.sort(reverse=True)
                    _rank = sum(1 for v in _valid if v > _track_listens) + 1
                    _pct = _rank / len(_valid)
                    if _force5_pct > 0 and _pct <= _force5_pct:
                        _force_stars = 5
                    elif _force4_pct > 0 and _pct <= _force4_pct:
                        _force_stars = 4
        except Exception:
            _force_stars = None

    if _force_stars is not None:
        track["_force_floor"] = _force_stars
        return _force_stars

    if is_compilation:
        raw_lf = float(track.get("lastfm_listeners") or 0)
        is_verified_single = single_confidence in ("high", "medium")

        if is_verified_single and not popularity_only:
            if score >= 55.0 or raw_lf >= 1_000_000:
                comp_stars = 5
            elif score >= 45.0 or raw_lf >= 250_000:
                comp_stars = 4
            elif score >= 30.0 or raw_lf >= 50_000:
                comp_stars = 3
            elif score >= 15.0 or raw_lf >= 10_000:
                comp_stars = 2
            else:
                comp_stars = 1
        else:
            has_deep_catalog = (
                not generic_compilation_artist
                and len([s for s in artist_scores if s > 0]) >= max(len(album_scores) + 15, 30)
            )

            if has_deep_catalog:
                if artist_z >= th["star5_artist_z"]:
                    comp_stars = 5
                elif artist_z >= th["star4_artist_z"]:
                    comp_stars = 4
                elif artist_z >= 0.0:
                    comp_stars = 3
                elif artist_z >= th["star2_album_z"]:
                    comp_stars = 2
                else:
                    comp_stars = 1
            else:
                if score >= 95.0 or raw_lf >= 1_500_000:
                    comp_stars = 5
                elif score >= 85.0 or raw_lf >= 500_000:
                    comp_stars = 4
                elif score >= 70.0 or raw_lf >= 75_000:
                    comp_stars = 3
                elif score >= 35.0 or raw_lf >= 10_000:
                    comp_stars = 2
                else:
                    comp_stars = 1

        if is_live:
            comp_stars = max(1, comp_stars - 1)

        if comp_stars == 5:
            track["_era_5star"] = True

        return comp_stars

    base_stars = _album_z_band_star(
        score=score,
        album_scores=album_scores,
        reference_scores=ref_scores,
        artist_scores=artist_scores,
        is_live=is_live,
        single_confidence=single_confidence,
    )

    if (
        is_standout
        and organic
        and not is_live
        and not popularity_only
    ):
        five_star_eligible = True

        if album_model and album_model.get("has_benchmark"):
            catalog_cutoff = album_model.get("catalog_cutoff")
            album_top_n = max(1, int(album_model.get("album_top_n") or 1))

            if catalog_cutoff is not None and score < float(catalog_cutoff):
                five_star_eligible = False

            if _album_rank(score, album_scores) > album_top_n:
                five_star_eligible = False

        if five_star_eligible:
            track["_era_5star"] = True
            return 5

    return base_stars


# ---------------------------------------------------------------------------
# 3-step scaling model helpers
# ---------------------------------------------------------------------------

def _percentile_cutoff(scores: list[float], top_pct: float) -> float | None:
    valid = sorted((float(s) for s in (scores or []) if float(s or 0) > 0), reverse=True)
    if not valid:
        return None
    n = max(1, math.ceil(len(valid) * top_pct))
    return valid[min(n - 1, len(valid) - 1)]


def _album_rank(score: float, album_scores: list[float]) -> int:
    if score <= 0:
        return len([s for s in (album_scores or []) if float(s or 0) > 0]) + 1
    higher = sum(1 for s in (album_scores or []) if float(s or 0) > score)
    return higher + 1


def _album_era_for_ratio(reff: float) -> str:
    _rules, peak_min, solid_min = _live_album_scaling()
    if reff >= peak_min:
        return "peak"
    if reff >= solid_min:
        return "solid"
    return "minor"


def _build_album_model(
    artist: str,
    album_results: list[dict[str, Any]],
    artist_scores: list[float],
) -> dict[str, Any]:
    """3-step scaling model context for ONE album."""
    try:
        from services.popularity.popularity_math import (
            album_prominence_score,
            album_prominence_median,
        )
        current_album = str(album_results[0].get("album") or "")
        scanned_keys = {
            (
                str(r.get("album") or "").strip().casefold(),
                _normalise_essential_title(str(r.get("title") or "")),
            )
            for r in album_results
        }

        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT title, album, final_score, year, "
                    "COALESCE(lastfm_listeners, 0) AS lastfm_listeners, "
                    "COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens "
                    "FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND final_score > 0"
                ),
                {"artist": artist},
            ).fetchall() or []

        by_album: dict[str, list[float]] = {}
        album_years: dict[str, int] = {}
        prominence_by_album: dict[str, list[float]] = {}

        for row in rows:
            _t_title = str(row_get(row, "title") or "").strip()
            _t_album = str(row_get(row, "album") or "")
            r_key = (_t_album.strip().casefold(), _normalise_essential_title(_t_title))
            if r_key in scanned_keys:
                continue
            _score = float(row_get(row, "final_score") or 0)
            if _score > 0:
                by_album.setdefault(_t_album, []).append(_score)
            _lf = int(row_get(row, "lastfm_listeners") or 0)
            _lb = int(row_get(row, "listenbrainz_listens") or 0)
            if _lf > 0 or _lb > 0:
                prominence_by_album.setdefault(_t_album, []).append(
                    album_prominence_score(_lf, _lb)
                )
            _year = int(row_get(row, "year") or 0)
            if _year > 0:
                album_years.setdefault(_t_album, _year)

        for r in album_results:
            _score = float(r.get("popularity_score") or r.get("final_score") or 0)
            if _score > 0 and not bool(r.get("exclude_from_stats")):
                by_album.setdefault(current_album, []).append(_score)
            _lf = int(r.get("lastfm_listeners") or 0)
            _lb = int(r.get("listenbrainz_listens") or 0)
            if _lf > 0 or _lb > 0:
                prominence_by_album.setdefault(current_album, []).append(
                    album_prominence_score(_lf, _lb)
                )
            _year = int(r.get("year") or r.get("release_year") or 0)
            if _year > 0:
                album_years.setdefault(current_album, _year)

        prominence_medians: dict[str, float] = {}
        for _album, _scores in prominence_by_album.items():
            prominence_value = album_prominence_median(_scores)
            if prominence_value > 0:
                prominence_medians[_album] = prominence_value

        use_prominence = len(prominence_medians) >= 2
        prominence_power = 2.0

        if use_prominence:
            m_peak = max(prominence_medians.values())
            current_median = prominence_medians.get(current_album) or m_peak
            peak_album = max(prominence_medians, key=prominence_medians.get)
        else:
            medians: dict[str, float] = {}
            for _album, _scores in by_album.items():
                if not _scores:
                    continue
                _reanchored = [
                    apply_album_relative_popularity(s, _scores) for s in _scores
                ]
                medians[_album] = median(_reanchored)
            if not medians:
                return {}
            m_peak = max(medians.values())
            if m_peak <= 0:
                return {}
            current_median = medians.get(current_album) or m_peak
            peak_album = max(medians, key=medians.get)

        peak_year = album_years.get(peak_album, 0)
        album_year = album_years.get(current_album, 0)

        a_skew = age_skew_multiplier(album_year, datetime.now().year, peak_year)
        if use_prominence:
            raw_ratio = min(1.0, max(0.0, float(current_median) / float(m_peak)))
            reff = min(1.0, max(0.0, raw_ratio ** float(prominence_power)))
        else:
            reff = effective_album_ratio(current_median * a_skew, m_peak)

        era = _album_era_for_ratio(reff)
        _rules, _, _ = _live_album_scaling()
        rules = _rules.get(era, _rules["peak"])

        return {
            "has_benchmark": True,
            "m_peak": m_peak,
            "album_median": current_median,
            "a_skew": a_skew,
            "reff": reff,
            "era": era,
            "benchmark_source": "prominence" if use_prominence else "scores",
            "catalog_cutoff": _percentile_cutoff(
                artist_scores, float(rules["catalog_top_pct"])
            ),
            "album_top_n": int(rules["album_top_n"]),
            "max_5star_slots": int(rules["max_5star_slots"]),
        }
    except Exception as exc:
        logger.debug("Album benchmark failed", artist=artist, error=str(exc))
        return {}


# ---------------------------------------------------------------------------
# Navidrome sync
# ---------------------------------------------------------------------------

def _sync_rating_to_navidrome(track_id: str, stars: int, clients: list[Any] | None = None) -> bool:
    try:
        from services.navidrome.rating_sync_service import (
            sync_track_rating_to_navidrome,
            sync_track_rating_with_clients,
        )
        if clients is None:
            return sync_track_rating_to_navidrome(track_id, stars)
        return sync_track_rating_with_clients(clients, track_id, stars)
    except Exception as exc:
        logger.debug("Navidrome rating sync call failed", track_id=track_id, error=str(exc))
        return False


def _build_rating_sync_clients() -> list[Any]:
    try:
        from services.navidrome.rating_sync_service import get_rating_sync_clients
        return list(get_rating_sync_clients() or [])
    except Exception as exc:
        logger.warning("Navidrome rating client creation failed", error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Essential Collection playlist helpers (Subsonic/Navidrome API only)
# ---------------------------------------------------------------------------

_ESSENTIAL_EXCLUDED_ARTISTS = frozenset({
    "various artists", "various", "va", "v/a",
    "soundtrack", "soundtracks", "unknown artist", "unknown",
})

_ESSENTIAL_MIN_TRACKS = 12

_ESSENTIAL_TITLE_NOISE_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_ESSENTIAL_FEAT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
_ESSENTIAL_FEAT_SPLIT_RE = re.compile(r"\s*(?:&|,|/|\+|\bx\b|\bvs\.?\b)\s*", re.IGNORECASE)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so artist names containing % or _ match literally."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _essential_playlist_name(artist: str) -> str:
    try:
        from helpers.config_helpers import get_config
        template = str(
            (get_config().get("playlists") or {}).get("essential_name_template")
            or "{artist} - Essential Collection"
        )
    except Exception:
        template = "{artist} - Essential Collection"
    name = str(template).replace("{artist}", artist).strip()
    return name or f"{artist} - Essential Collection"


def _normalise_essential_title(title: str) -> str:
    t = str(title or "").strip().lower()
    t = _ESSENTIAL_TITLE_NOISE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_excluded_essential_artist(artist: str) -> bool:
    return str(artist or "").strip().casefold() in _ESSENTIAL_EXCLUDED_ARTISTS


def _essential_strip_guest_credit(artist: str) -> str:
    match = _ESSENTIAL_FEAT_RE.search(str(artist or ""))
    if not match:
        return str(artist or "").strip()
    return str(artist or "")[: match.start()].strip()


def _refresh_all_essential_collections() -> int:
    artists: list[str] = []
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT DISTINCT TRIM(COALESCE(NULLIF(album_artist, ''), artist)) AS artist
                FROM tracks
                WHERE COALESCE(stars, star_rating) >= 4
                  AND TRIM(COALESCE(NULLIF(album_artist, ''), artist)) <> ''
            """))
            artists = [str(r[0]) for r in result.fetchall() or [] if r and r[0]]
    except Exception as exc:
        logger.debug("Essential collection artist scan failed", error=str(exc))
        return 0

    seen: set[str] = set()
    unique_artists: list[str] = []
    for artist in artists:
        primary = _essential_strip_guest_credit(artist) or artist
        key = primary.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_artists.append(primary)

    featured_rows: list[dict[str, Any]] = []
    if _essential_include_featured_enabled():
        featured_rows = _fetch_essential_featured_rows()

    refreshed = 0
    for artist in unique_artists:
        if _is_excluded_essential_artist(artist):
            continue
        try:
            if _sync_essential_playlist(artist, featured_rows=featured_rows):
                refreshed += 1
        except Exception as exc:
            logger.debug("Essential collection refresh failed", artist=artist, error=str(exc))
    return refreshed


def _essential_playlists_enabled(options: dict[str, Any]) -> bool:
    flag = options.get("create_playlists")
    if flag is not None:
        return bool(flag)
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("essential_playlists_enabled", True))
    except Exception:
        return True


_NEW_MUSIC_MIN_TRACKS = 100
_NEW_MUSIC_MAX_TRACKS = 100


def _new_music_playlist_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("new_music_playlist_enabled", True))
    except Exception:
        return True


def _playlist_sync_succeeded(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    return bool(
        result.get("created")
        or result.get("updated")
        or result.get("unchanged")
        or result.get("skipped")
    )


def _create_new_music_playlist() -> int:
    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, duration,
                           COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                           COALESCE(stars, star_rating) AS stars,
                           updated_at
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND file_path IS NOT NULL AND TRIM(file_path) <> ''
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT :max_rows
                """),
                {"max_rows": _NEW_MUSIC_MAX_TRACKS * 5},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("New Music fetch failed", error=str(exc))
        return 0

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("artist") or "").strip().casefold(),
            _normalise_essential_title(str(row.get("title") or "")),
        )
        if not key[1]:
            continue
        grouped.setdefault(key, []).append(row)
    winners = [group[0] for group in grouped.values()]

    if len(winners) < _NEW_MUSIC_MIN_TRACKS:
        _delete_playlist_from_navidrome("New Music")
        return 0

    winners = winners[:_NEW_MUSIC_MAX_TRACKS]

    _song_ids = [str(r.get("id") or "").strip() for r in winners if str(r.get("id") or "").strip()]
    if not _song_ids:
        return 0

    try:
        sync_res = _sync_playlist_to_navidrome("New Music", _song_ids)
        if not _playlist_sync_succeeded(sync_res):
            return 0
        if not sync_res.get("skipped"):
            log_unified(f"📄 Playlist: Synced 'New Music' to Navidrome ({len(_song_ids)} tracks)")
        return len(_song_ids)
    except Exception as exc:
        logger.warning("New Music playlist sync failed", error=str(exc))
        return 0


def _essential_include_featured_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("essential_include_featured", True))
    except Exception:
        return True


def _track_has_featured_artist(artist_field: str, target_artist: str) -> bool:
    if not target_artist:
        return False
    match = _ESSENTIAL_FEAT_RE.search(artist_field or "")
    if not match:
        return False
    target_key = re.sub(r"\s+", " ", target_artist).strip().casefold()
    guests = _ESSENTIAL_FEAT_SPLIT_RE.split(artist_field[match.end():])
    return any(
        re.sub(r"\s+", " ", g).strip().casefold() == target_key
        for g in guests
        if g and g.strip()
    )


def _fetch_essential_featured_rows() -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, duration,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           year, release_year, artist
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND (
                          artist ILIKE '% feat %' OR artist ILIKE '% feat.%'
                          OR artist ILIKE '%feat.%' OR artist ILIKE '%featuring%'
                          OR artist ILIKE '% ft %' OR artist ILIKE '% ft.%'
                      )
                """),
            )
            return [dict(r._mapping) for r in (result.fetchall() or [])]
    except Exception as exc:
        logger.debug("Featured-track fetch failed", error=str(exc))
        return []


def _sync_essential_playlist(
    artist: str,
    artist_scores: list[float] | None = None,
    featured_rows: list[dict[str, Any]] | None = None
) -> bool:
    """Build ONE artist's Essential Collection and sync it to Navidrome via the API."""
    if _is_excluded_essential_artist(artist):
        return False

    artist = _essential_strip_guest_credit(artist) or artist
    if _is_excluded_essential_artist(artist):
        return False

    playlist_name = _essential_playlist_name(artist)

    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        max_tracks = max(1, int(cfg.get("essential_max_tracks", 50) or 50))
    except Exception:
        max_tracks = 50

    if artist_scores is None:
        try:
            with db_session() as session:
                res = session.execute(
                    text("SELECT final_score FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND final_score > 0"),
                    {"artist": artist},
                )
                artist_scores = [float(r[0]) for r in res.fetchall() or [] if r and r[0]]
        except Exception:
            artist_scores = []

    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, duration,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           year, release_year, artist, album_artist
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND (
                          LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) = LOWER(TRIM(:artist))
                          OR LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) LIKE :artist_prefix ESCAPE '\\'
                      )
                """),
                {
                    "artist": artist,
                    "artist_prefix": _escape_like(str(artist).strip().lower()) + " %",
                },
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("Essential collection fetch failed", artist=artist, error=str(exc))

    _artist_lower = str(artist or "").strip().casefold()
    _kept: list[dict[str, Any]] = []
    for row in rows:
        stored = str(row.get("album_artist") or row.get("artist") or "").strip()
        if not stored:
            _kept.append(row)
            continue
        stored_key = stored.casefold()
        if stored_key == _artist_lower:
            _kept.append(row)
            continue
        if (
            _ESSENTIAL_FEAT_RE.search(stored)
            and _essential_strip_guest_credit(stored).casefold() == _artist_lower
        ):
            _kept.append(row)
    rows = _kept

    if _essential_include_featured_enabled():
        try:
            _feat_rows = featured_rows if featured_rows is not None else _fetch_essential_featured_rows()
            for row in _feat_rows:
                if _track_has_featured_artist(row.get("artist") or "", artist):
                    rows.append(row)
        except Exception as exc:
            logger.debug("Featured-track merge failed", artist=artist, error=str(exc))

    def _track_year(row: dict[str, Any]) -> int:
        raw = row.get("release_year") or row.get("year") or 0
        try:
            val = int(float(raw)) if str(raw).strip() else 0
            return val if val > 0 else 9999
        except (TypeError, ValueError):
            return 9999

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _normalise_essential_title(row.get("title") or "")
        if key:
            grouped.setdefault(key, []).append(row)

    winners: list[dict[str, Any]] = []
    for group in grouped.values():
        group.sort(
            key=lambda r: (
                int(r.get("is_live") or 0),              # 1. Penalize live tracks
                -int(r.get("stars") or 0),               # 2. Highest stars wins (5★ beats 4★)
                -float(r.get("popularity_score") or 0),  # 3. Highest popularity breaks ties
                int(r.get("is_compilation") or 0),       # 4. Compilation status moved down to a tie-breaker
                _track_year(r),                          # 5. Earliest release year
                str(r.get("title") or "").casefold(),
            ),
        )
        winners.append(group[0])

    if len(winners) < _ESSENTIAL_MIN_TRACKS:
        _delete_playlist_from_navidrome(playlist_name)
        return False

    for w in winners:
        _, pct = calculate_percentile_star_rating(float(w.get("popularity_score") or 0), artist_scores)
        w["percentile"] = pct

    winners.sort(
        key=lambda r: (
            -int(r.get("stars") or 0),               # 1. All 5★ tracks first, then 4★
            -float(r.get("popularity_score") or 0),  # 2. Ordered by popularity score within star tiers
            str(r.get("title") or "").casefold(),    # 3. Alphabetical tie-breaker
        ),
    )

    winners = winners[:max_tracks]

    _song_ids = [str(r.get("id") or "").strip() for r in winners if str(r.get("id") or "").strip()]
    if not _song_ids:
        return False

    try:
        sync_res = _sync_playlist_to_navidrome(playlist_name, _song_ids)
        if not _playlist_sync_succeeded(sync_res):
            return False

        if sync_res.get("skipped"):
            return True

        log_unified(f"📄 Playlist: Synced '{playlist_name}' to Navidrome ({len(_song_ids)} tracks)")

        try:
            from services.playlists.playlist_service import attach_playlist_cover
            attach_playlist_cover(playlist_name, artist)
        except Exception as cover_exc:
            pass
        return True
    except Exception as exc:
        return False


# ---------------------------------------------------------------------------
# Genre top-tracks playlists
# ---------------------------------------------------------------------------

def _genre_playlists_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("genre_playlists_enabled", True))
    except Exception:
        return True


def _genre_playlists_delete_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("genre_playlists_delete_enabled", True))
    except Exception:
        return True


def _genre_playlists_active() -> bool:
    return _genre_playlists_enabled() or _genre_playlists_delete_enabled()


def _genre_playlist_name(genre: str) -> str:
    try:
        from helpers.config_helpers import get_config
        template = str(
            (get_config().get("playlists") or {}).get("genre_playlists_name_template")
            or "{genre} - Top Tracks"
        )
    except Exception:
        template = "{genre} - Top Tracks"
    return str(template).replace("{genre}", genre).strip() or f"{genre} - Top Tracks"


def _display_genre(genre: str) -> str:
    return " ".join(
        w.capitalize() if not (w and w[0].isdigit()) else w
        for w in str(genre or "").split()
    )


# ---------------------------------------------------------------------------
# Navidrome client + playlist push caches
# ---------------------------------------------------------------------------

_CLIENTS_LOCK = threading.Lock()
_CLIENTS_CACHE: list[Any] | None = None
_CLIENTS_CACHED_AT = 0.0
_CLIENTS_TTL_SECONDS = 300.0

_PLAYLIST_SIG_LOCK = threading.Lock()
_PLAYLIST_SIGNATURES: "OrderedDict[str, str]" = OrderedDict()
_PLAYLIST_SIG_MAX = 2000


def _navidrome_clients() -> list[Any]:
    global _CLIENTS_CACHE, _CLIENTS_CACHED_AT
    now = time.monotonic()
    with _CLIENTS_LOCK:
        if _CLIENTS_CACHE is not None and (now - _CLIENTS_CACHED_AT) < _CLIENTS_TTL_SECONDS:
            return list(_CLIENTS_CACHE)

    clients: list[Any] = []
    try:
        from helpers.config_helpers import get_navidrome_users_normalized
        from api_clients.navidrome import NavidromeClient
        for user in get_navidrome_users_normalized():
            base_url = (user.get("base_url") or "").strip()
            username = (user.get("user") or "").strip()
            password = (user.get("pass") or "").strip()
            if base_url and username and password:
                clients.append(NavidromeClient(base_url, username, password))
    except Exception as exc:
        pass

    with _CLIENTS_LOCK:
        _CLIENTS_CACHE = list(clients)
        _CLIENTS_CACHED_AT = now
    return clients


def _playlist_signature(song_ids: list[str]) -> str:
    joined = "\n".join(str(s) for s in song_ids)
    return hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()


def _playlist_already_pushed(playlist_name: str, signature: str) -> bool:
    key = str(playlist_name or "").strip().casefold()
    with _PLAYLIST_SIG_LOCK:
        existing = _PLAYLIST_SIGNATURES.get(key)
        if existing == signature:
            _PLAYLIST_SIGNATURES.move_to_end(key)
            return True
    return False


def _record_playlist_push(playlist_name: str, signature: str) -> None:
    key = str(playlist_name or "").strip().casefold()
    with _PLAYLIST_SIG_LOCK:
        _PLAYLIST_SIGNATURES[key] = signature
        _PLAYLIST_SIGNATURES.move_to_end(key)
        while len(_PLAYLIST_SIGNATURES) > _PLAYLIST_SIG_MAX:
            _PLAYLIST_SIGNATURES.popitem(last=False)


def _forget_playlist_push(playlist_name: str) -> None:
    key = str(playlist_name or "").strip().casefold()
    with _PLAYLIST_SIG_LOCK:
        _PLAYLIST_SIGNATURES.pop(key, None)


def reset_playlist_push_cache() -> None:
    with _PLAYLIST_SIG_LOCK:
        _PLAYLIST_SIGNATURES.clear()
    global _CLIENTS_CACHE
    with _CLIENTS_LOCK:
        _CLIENTS_CACHE = None


def _delete_playlist_from_navidrome(playlist_name: str) -> bool:
    if not playlist_name:
        return False
    deleted = False
    try:
        for client in _navidrome_clients():
            playlist = client.find_playlist_by_name(playlist_name)
            if playlist and playlist.get("id") and client.delete_playlist(str(playlist["id"])):
                deleted = True
    except Exception as exc:
        pass
    if deleted:
        _forget_playlist_push(playlist_name)
    return deleted


def _sweep_orphaned_genre_playlists_from_navidrome(keep_playlist_names: set[str]) -> None:
    try:
        if not _genre_playlists_delete_enabled():
            return
        suffix = _genre_playlist_name("GENRE").replace("GENRE", "", 1).strip()
        if not suffix:
            return
        keep_keys = {str(name).strip().casefold() for name in keep_playlist_names if str(name).strip()}
        for client in _navidrome_clients():
            for playlist in client.fetch_all_playlists() or []:
                name = str(playlist.get("name") or "").strip()
                if suffix.casefold() not in name.casefold():
                    continue
                if name.casefold() in keep_keys:
                    continue
                playlist_id = str(playlist.get("id") or "").strip()
                if playlist_id and client.delete_playlist(playlist_id):
                    _forget_playlist_push(name)
    except Exception as exc:
        pass


def _sync_playlist_to_navidrome(
    playlist_name: str,
    song_ids: list[str],
) -> dict[str, int]:
    synced = {
        "updated": 0,
        "created": 0,
        "unchanged": 0,
        "deduped": 0,
        "failed": 0,
        "clients": 0,
        "skipped": 0,
    }

    song_ids = list(song_ids or [])
    signature = _playlist_signature(song_ids)

    if _playlist_already_pushed(playlist_name, signature):
        synced["skipped"] = 1
        return synced

    try:
        from services.playlists.playlist_navidrome_service import sync_playlist_by_name
    except Exception as exc:
        synced["failed"] = 1
        return synced

    clients = _navidrome_clients()

    if not clients:
        return synced

    for client in clients:
        synced["clients"] += 1
        try:
            result = (
                sync_playlist_by_name(
                    client,
                    playlist_name,
                    list(song_ids),
                )
                or {}
            )

            if result.get("updated"):
                synced["updated"] += 1

            if result.get("created"):
                synced["created"] += 1

            if result.get("unchanged"):
                synced["unchanged"] += 1

            synced["deduped"] += int(
                result.get("deduped") or 0
            )

            if (
                result.get("success")
                and not result.get("created")
                and not result.get("updated")
                and not result.get("unchanged")
            ):
                synced["unchanged"] += 1

        except Exception as exc:
            synced["failed"] += 1

    if _playlist_sync_succeeded(synced):
        if not synced["failed"]:
            _record_playlist_push(playlist_name, signature)

    return synced


def _genre_playlist_track_genres(
    row: dict[str, Any],
    *,
    max_genres: int = 3,
    json_sources: dict[str, str] | None = None,
    delimited_sources: dict[str, str] | None = None,
) -> list[str]:
    from services.enrichment.genre_aggregation_service import aggregate_genres
    from services.enrichment.genre_tag_aggregator import parse_json_tags, parse_delimited_tags

    _json_sources = json_sources or {
        "lastfm_tags": "lastfm",
        "listenbrainz_genres": "listenbrainz",
        "discogs_genres": "discogs",
        "musicbrainz_genres": "musicbrainz",
        "spotify_genres": "spotify",
    }
    _delimited_sources = delimited_sources or {
        "essentia_genres": "essentia",
        "manual_genres": "manual",
        "navidrome_genres": "navidrome",
    }

    source_map: dict[str, list[str]] = {}
    for column, source in _json_sources.items():
        raw = row.get(column)
        if raw:
            names = [t.get("name") for t in (parse_json_tags(raw) or []) if t.get("name")]
            if names:
                source_map[source] = names
    for column, source in _delimited_sources.items():
        raw = row.get(column)
        if raw:
            names = [t.get("name") for t in (parse_delimited_tags(raw) or []) if t.get("name")]
            if names:
                source_map[source] = names
    if not source_map:
        return []
    return aggregate_genres(source_map, max_genres=max_genres)


# ---------------------------------------------------------------------------
# Genre-playlist source rows (full-library query, cached briefly)
# ---------------------------------------------------------------------------

_GENRE_ROWS_LOCK = threading.Lock()
_GENRE_ROWS_CACHE: list[dict[str, Any]] | None = None
_GENRE_ROWS_MIN_STARS: int | None = None
_GENRE_ROWS_AT = 0.0
_GENRE_ROWS_TTL_SECONDS = 120.0

_GENRE_ROWS_SQL = """
    SELECT id, title, file_path, duration, artist, album_artist,
           COALESCE(stars, star_rating) AS stars,
           COALESCE(popularity, final_score, 0) AS popularity_score,
           COALESCE(is_live, 0) AS is_live,
           COALESCE(is_compilation, 0) AS is_compilation,
           lastfm_tags, listenbrainz_genres, discogs_genres,
           musicbrainz_genres, spotify_genres,
           essentia_genres, manual_genres, navidrome_genres
    FROM tracks
    WHERE COALESCE(stars, star_rating) >= :min_stars
"""


def _fetch_genre_playlist_rows(min_stars: int) -> list[dict[str, Any]]:
    global _GENRE_ROWS_CACHE, _GENRE_ROWS_MIN_STARS, _GENRE_ROWS_AT
    now = time.monotonic()
    with _GENRE_ROWS_LOCK:
        if (
            _GENRE_ROWS_CACHE is not None
            and _GENRE_ROWS_MIN_STARS == min_stars
            and (now - _GENRE_ROWS_AT) < _GENRE_ROWS_TTL_SECONDS
        ):
            return _GENRE_ROWS_CACHE

    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(text(_GENRE_ROWS_SQL), {"min_stars": min_stars})
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        return []

    with _GENRE_ROWS_LOCK:
        _GENRE_ROWS_CACHE = rows
        _GENRE_ROWS_MIN_STARS = min_stars
        _GENRE_ROWS_AT = now
    return rows


def invalidate_genre_playlist_rows() -> None:
    global _GENRE_ROWS_CACHE, _GENRE_ROWS_MIN_STARS
    with _GENRE_ROWS_LOCK:
        _GENRE_ROWS_CACHE = None
        _GENRE_ROWS_MIN_STARS = None


def refresh_genre_playlists_for_album(artist: str, album: str) -> int:
    if not _genre_playlists_active():
        return 0
    try:
        try:
            from helpers.config_helpers import get_config
            _cfg = (get_config() or {}).get("playlists") or {}
        except Exception:
            _cfg = {}
        min_stars = max(1, int(_cfg.get("genre_playlists_min_stars", 4) or 4))

        rows: list[dict[str, Any]] = []
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, duration, artist, album_artist,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           lastfm_tags, listenbrainz_genres, discogs_genres,
                           musicbrainz_genres, spotify_genres,
                           essentia_genres, manual_genres, navidrome_genres
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND COALESCE(stars, star_rating) >= :min_stars
                """),
                {"artist": artist, "album": album, "min_stars": min_stars},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
        if not rows:
            return 0

        max_genres = max(1, int(_cfg.get("genre_playlists_max_genres", 3) or 3))
        affected: set[str] = set()
        for row in rows:
            for genre in _genre_playlist_track_genres(row, max_genres=max_genres):
                affected.add(genre)
        if not affected:
            return 0
        return _create_genre_top_track_playlists(only_genres=affected)
    except Exception as exc:
        return 0


def _create_genre_top_track_playlists(
    prune_only: bool = False,
    only_genres: set[str] | None = None,
) -> int:
    from collections import defaultdict

    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
    except Exception:
        cfg = {}

    min_stars = max(1, int(cfg.get("genre_playlists_min_stars", 4) or 4))
    max_genres = max(1, int(cfg.get("genre_playlists_max_genres", 3) or 3))
    create_threshold = max(1, int(cfg.get("genre_playlists_create_threshold", 100) or 100))
    delete_threshold = max(1, int(cfg.get("genre_playlists_delete_threshold", 80) or 80))
    max_tracks = max(1, int(cfg.get("genre_playlists_max_tracks", 100) or 100))
    create_enabled = _genre_playlists_enabled()
    delete_enabled = _genre_playlists_delete_enabled()

    rows = _fetch_genre_playlist_rows(min_stars)

    if not rows:
        return 0

    _json_sources = {
        "lastfm_tags": "lastfm",
        "listenbrainz_genres": "listenbrainz",
        "discogs_genres": "discogs",
        "musicbrainz_genres": "musicbrainz",
        "spotify_genres": "spotify",
    }
    _delimited_sources = {
        "essentia_genres": "essentia",
        "manual_genres": "manual",
        "navidrome_genres": "navidrome",
    }

    def _track_genres(row: dict[str, Any]) -> list[str]:
        return _genre_playlist_track_genres(
            row,
            max_genres=max_genres,
            json_sources=_json_sources,
            delimited_sources=_delimited_sources,
        )

    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    genre_freq: dict[str, int] = defaultdict(int)
    genre_display: dict[str, str] = {}

    for row in rows:
        for genre in _track_genres(row):
            norm_key = re.sub(r"[^\w\s-]", "", genre.lower()).strip()
            if not norm_key:
                continue

            pools[norm_key].append({
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Unknown"),
                "file_path": str(row.get("file_path") or ""),
                "duration": row.get("duration"),
                "stars": int(row.get("stars") or 0),
                "score": float(row.get("popularity_score") or row.get("final_score") or 0),
                "artist": str(row.get("artist") or row.get("album_artist") or ""),
                "is_live": int(row.get("is_live") or 0),
                "is_compilation": int(row.get("is_compilation") or 0),
            })

            genre_freq[genre] += 1
            current_top = genre_display.get(norm_key)
            if not current_top or genre_freq[genre] > genre_freq[current_top]:
                genre_display[norm_key] = genre

    def _tiebreak(item: dict[str, Any]) -> tuple:
        return (
            item["is_live"],
            item["is_compilation"],
            -item["stars"],
            -item["score"],
            item["title"].casefold(),
        )

    def _popularity_order(item: dict[str, Any]) -> tuple:
        return (
            -item["score"],
            -item["stars"],
            item["title"].casefold(),
        )

    written = 0
    skipped_unchanged = 0
    keep_playlist_names: set[str] = set()

    norm_only_genres = None
    if only_genres is not None:
        norm_only_genres = {re.sub(r"[^\w\s-]", "", g.lower()).strip() for g in only_genres}

    for norm_key, tracks in pools.items():
        if norm_only_genres is not None and norm_key not in norm_only_genres:
            continue

        genre = genre_display[norm_key]

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for t in tracks:
            key = (
                re.sub(r"\s+", " ", t["artist"]).strip().casefold(),
                _normalise_essential_title(t["title"]),
            )
            grouped[key].append(t)

        winners = [min(group, key=_tiebreak) for group in grouped.values()]
        qualifying_count = len(winners)

        playlist_name = _genre_playlist_name(_display_genre(genre))

        if qualifying_count >= delete_threshold:
            keep_playlist_names.add(playlist_name)

        if prune_only:
            continue

        if not create_enabled or qualifying_count < create_threshold:
            continue

        winners.sort(key=_popularity_order)
        winners = winners[:max_tracks]

        _song_ids = [str(t.get("id") or "").strip() for t in winners if str(t.get("id") or "").strip()]
        if not _song_ids:
            continue

        try:
            sync_res = _sync_playlist_to_navidrome(playlist_name, _song_ids)
            if not _playlist_sync_succeeded(sync_res):
                continue

            keep_playlist_names.add(playlist_name)

            if sync_res.get("skipped"):
                skipped_unchanged += 1
                continue

            log_unified(f"📄 Playlist: Synced '{playlist_name}' to Navidrome ({len(_song_ids)} tracks)")
            written += 1

        except Exception as exc:
            logger.warning("Genre playlist API sync failed", genre=genre, error=str(exc))

    if delete_enabled and only_genres is None:
        if pools:
            try:
                _sweep_orphaned_genre_playlists_from_navidrome(keep_playlist_names)
            except Exception:
                pass

    return written


def prune_genre_playlists_for_deletion() -> None:
    try:
        if not _genre_playlists_delete_enabled():
            return
        _create_genre_top_track_playlists(prune_only=True)
    except Exception as exc:
        pass


# ---------------------------------------------------------------------------
# Per-album star rating posting
# ---------------------------------------------------------------------------

def compute_artist_scores(
    artist: str,
    scan_scores: list[float],
    scanned_keys: set[tuple[str, str]] | None = None,
) -> list[float]:
    scanned_keys = scanned_keys or set()
    from services.catalog.album_classification_service import is_bonus_track_title
    artist_scores = [float(s) for s in scan_scores if float(s or 0) > 0]
    db_rows: list[tuple[str, float]] = []

    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "SELECT title, album, final_score FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND final_score > 0"
                ),
                {"artist": artist},
            )
            for row in result.fetchall() or []:
                score_val = float(row_get(row, "final_score") or 0)
                if not score_val:
                    continue
                t_title = str(row_get(row, "title") or "").strip()
                t_album = str(row_get(row, "album") or "").strip()
                if is_bonus_track_title(t_title):
                    continue
                r_key = (t_album.casefold(), _normalise_essential_title(t_title))
                if r_key in scanned_keys:
                    continue
                db_rows.append((t_album, score_val))
    except Exception as exc:
        pass

    try:
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        db_scores = list(reanchor_scores_to_album_relative(db_rows))
    except Exception as exc:
        db_scores = [float(s) for _alb, s in db_rows]

    return list(artist_scores) + db_scores


def _album_scaling_configured() -> bool:
    try:
        cfg = get_standout_config() or {}
        return isinstance(cfg.get("album_scaling"), dict) and bool(cfg.get("album_scaling"))
    except Exception:
        return False


def _log_scan_weights(artist: str, album: str, album_model: dict[str, Any]) -> None:
    try:
        from services.popularity.popularity_config import resolve_weights
        lf, lb, age = resolve_weights()
        rules, peak_min, solid_min = _live_album_scaling()
        era = str(album_model.get("era") or "?")
        reff = float(album_model.get("reff") or 0)
        era_rules = rules.get(era) or {}
        log_unified(
            f"⚖️ WEIGHTS: {str(artist or '').strip()} — {str(album or '').strip()} "
            f"| blend: LF={lf:.2f} LB={lb:.2f} Age={age:.2f} "
            f"| era={era} (R_eff={reff:.2f}, peak≥{peak_min:.2f}, solid≥{solid_min:.2f}) "
            f"| caps: catalog_top={float(era_rules.get('catalog_top_pct') or 0) * 100:.0f}%, "
            f"album_top_n={era_rules.get('album_top_n') or '—'}, "
            f"max_5★={era_rules.get('max_5star_slots') or '—'} "
            f"| source: {'config' if _album_scaling_configured() else 'defaults'}"
        )
    except Exception as exc:
        pass


def post_album_star_ratings(
    *,
    album_results: list[dict[str, Any]],
    artist: str,
    artist_scores: list[float],
    options: dict[str, Any],
) -> dict[str, int]:
    album = (
        str(
            album_results[0].get("album")
            or "Unknown"
        )
        if album_results
        else "Unknown"
    )

    if not album_results:
        return {"star_ratings": 0, "navidrome_synced": 0}

    total_star_ratings = 0
    navidrome_synced = 0

    try:
        try:
            from services.enrichment.single_detection_service import is_compilation_album
            _album_type = str(
                album_results[0].get("album_type")
                or album_results[0].get("detected_album_type")
                or ""
            )
            is_compilation = bool(is_compilation_album(_album_type, album))
        except Exception as exc:
            is_compilation = False

        _GENERIC_COMPILATION_ARTISTS = frozenset({
            "various artists", "various", "va", "v/a",
            "compilation", "soundtrack", "soundtracks",
        })
        generic_compilation_artist = str(artist or "").strip().casefold() in _GENERIC_COMPILATION_ARTISTS

        album_scores = [
            float(r.get("popularity_score") or r.get("final_score") or 0)
            for r in album_results
            if float(r.get("popularity_score") or r.get("final_score") or 0) > 0
        ]

        if len(album_scores) >= 3:
            _eligible = [
                float(r.get("popularity_score") or r.get("final_score") or 0)
                for r in album_results
                if float(r.get("popularity_score") or r.get("final_score") or 0) > 0
                and not bool(r.get("exclude_from_stats"))
            ]
            if len(_eligible) >= 3:
                album_scores = _eligible

        album_lf_listeners = [float(r.get("lastfm_listeners") or 0) for r in album_results]
        album_lb_listens = [float(r.get("listenbrainz_listens") or 0) for r in album_results]

        if len(album_lf_listeners) >= 3 and len(album_lb_listens) >= 3:
            _elf = [float(r.get("lastfm_listeners") or 0) for r in album_results if not bool(r.get("exclude_from_stats"))]
            _elb = [float(r.get("listenbrainz_listens") or 0) for r in album_results if not bool(r.get("exclude_from_stats"))]
            if len(_elf) >= 3 and len(_elb) >= 3:
                album_lf_listeners = _elf
                album_lb_listens = _elb

        try:
            from services.catalog.album_classification_service import is_bonus_track_title
            scanned_titles = {str(r.get("title") or "").strip().lower() for r in album_results}
            with db_session() as session:
                rows = session.execute(
                    text("SELECT title, final_score FROM tracks "
                         "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album AND final_score > 0"),
                    {"artist": artist, "album": album},
                ).fetchall() or []

            db_album_scores = [
                float(row_get(row, "final_score") or 0)
                for row in rows
                if row_get(row, "final_score")
                and str(row_get(row, "title") or "").strip().lower() not in scanned_titles
                and not is_bonus_track_title(str(row_get(row, "title") or ""))
            ]
            album_scores = list(album_scores) + list(db_album_scores)
        except Exception as exc:
            pass

        album_model: dict[str, Any] = {}
        try:
            album_model = _build_album_model(artist, album_results, artist_scores)
        except Exception as exc:
            pass

        _log_scan_weights(artist, album, album_model)

        _stored_stars: dict[str, int] = {}
        _stored_paths: dict[str, str] = {}
        try:
            _album_ids = [
                str(t.get("track_id") or "").strip()
                for t in album_results
                if str(t.get("track_id") or "").strip()
            ]
            if _album_ids:
                with db_session() as _sess:
                    _rows = _sess.execute(
                        text(
                            "SELECT id, COALESCE(stars, star_rating, 0) AS stars, file_path FROM tracks "
                            "WHERE CAST(id AS TEXT) IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": _album_ids},
                    ).fetchall() or []
                for _r in _rows:
                    _m = _r._mapping
                    _stored_stars[str(_m.get("id"))] = int(_m.get("stars") or 0)
                    _p = str(_m.get("file_path") or "").strip()
                    if _p:
                        _stored_paths[str(_m.get("id"))] = _p
        except Exception as exc:
            pass

        try:
            from helpers.config_helpers import get_tagging_config
            _skip_unchanged = bool(get_tagging_config().get("skip_unchanged_ratings", True))
        except Exception:
            _skip_unchanged = True

        _artist_listen_distribution = [
            float(result.get("lastfm_listeners") or 0)
            for result in album_results
            if float(result.get("lastfm_listeners") or 0) > 0
        ]
        try:
            _scanned_listener_keys = {
                (
                    str(result.get("album") or "").strip().casefold(),
                    _normalise_essential_title(str(result.get("title") or "")),
                )
                for result in album_results
            }
            with db_session() as _sess:
                _artist_rows = _sess.execute(
                    text(
                        "SELECT album, title, lastfm_listeners FROM tracks "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "AND COALESCE(lastfm_listeners, 0) > 0"
                    ),
                    {"artist": artist},
                ).fetchall() or []
            for _r in _artist_rows:
                mapping = _r._mapping
                row_key = (
                    str(mapping.get("album") or "").strip().casefold(),
                    _normalise_essential_title(str(mapping.get("title") or "")),
                )
                if row_key in _scanned_listener_keys:
                    continue
                listeners = float(mapping.get("lastfm_listeners") or 0)
                if listeners > 0:
                    _artist_listen_distribution.append(listeners)
        except Exception as exc:
            pass

        # 1. Assign star ratings in memory
        for track in album_results:
            try:
                stars = _assign_stars(
                    track,
                    album_scores,
                    artist_scores,
                    album_lf_listeners,
                    album_lb_listens,
                    popularity_only=bool(options.get("popularity_only")),
                    album_model=album_model,
                    is_compilation=is_compilation,
                    artist_listen_distribution=_artist_listen_distribution,
                    generic_compilation_artist=generic_compilation_artist,
                )
            except Exception as exc:
                track_id = str(track.get("track_id") or "").strip()
                fallback_stars = int(_stored_stars.get(track_id, 0))
                if fallback_stars < 1:
                    fallback_stars = 1
                stars = fallback_stars

            track["stars"] = stars
            total_star_ratings += 1

        # 2. Enforce 5-star era cap before persistence
        if album_model.get("has_benchmark") and not is_compilation:
            _rules, _, _ = _live_album_scaling()
            configured_slots = album_model.get("max_5star_slots")
            if configured_slots is None:
                configured_slots = _rules["peak"]["max_5star_slots"]
            max_slots = max(0, int(configured_slots))

            protected_five_star_tracks = [
                track for track in album_results
                if (
                    int(track.get("stars") or 0) == 5
                    and (
                        bool(track.get("_global_5star_locked"))
                        or str(track.get("single_confidence") or "").strip().casefold() == "user"
                        or int(track.get("_force_floor") or 0) == 5
                    )
                )
            ]

            protected_track_ids = {id(track) for track in protected_five_star_tracks}

            automatic_five_star_tracks = [
                track for track in album_results
                if (
                    int(track.get("stars") or 0) == 5
                    and bool(track.get("_era_5star"))
                    and id(track) not in protected_track_ids
                )
            ]

            automatic_five_star_tracks.sort(
                key=lambda track: (
                    _compute_album_z(
                        float(track.get("popularity_score") or track.get("final_score") or 0),
                        album_scores,
                    )[0],
                    float(track.get("popularity_score") or track.get("final_score") or 0),
                ),
                reverse=True,
            )

            automatic_slots = max(0, max_slots - len(protected_five_star_tracks))

            for track in automatic_five_star_tracks[automatic_slots:]:
                track["stars"] = 4
                track["_era_5star"] = False
                logger.info(
                    "Era slot cap applied: 5★ → 4★",
                    artist=artist, album=album,
                    title=track.get("title"), cap=max_slots,
                    protected=len(protected_five_star_tracks),
                )

        # 2.5 Enforce 4-star percentage cap before persistence
        if album_model.get("has_benchmark") and not is_compilation:
            baseline_tracks = int(album_results[0].get("original_track_count") or 0)
            if baseline_tracks <= 0:
                local_count = len(album_results)
                baseline_tracks = local_count if local_count < 14 else 12

            era = str(album_model.get("era") or "peak")
            era_caps = {"peak": 0.30, "solid": 0.20, "minor": 0.15}
            cap_pct = era_caps.get(era, 0.20)
            
            max_4star_slots = max(1, int(baseline_tracks * cap_pct))

            automatic_4star_tracks = [
                track for track in album_results
                if int(track.get("stars") or 0) == 4
                and not bool(track.get("_global_5star_locked"))
                and int(track.get("_force_floor") or 0) < 4
            ]
            
            automatic_4star_tracks.sort(
                key=lambda track: float(track.get("popularity_score") or track.get("final_score") or 0),
                reverse=True
            )

            if len(automatic_4star_tracks) > max_4star_slots:
                for track in automatic_4star_tracks[max_4star_slots:]:
                    track["stars"] = 3
                    logger.info(
                        "Era slot cap applied: 4★ → 3★",
                        artist=artist, album=album,
                        title=track.get("title"), cap=max_4star_slots,
                        baseline_tracks=baseline_tracks
                    )

        # 3. Persist calculated and capped ratings first
        _ratings_changed = False
        with db_session() as session:
            for track in album_results:
                track_id = str(track.get("track_id") or "").strip()
                stars = int(track.get("stars") or 0)
                if not track_id or stars < 1:
                    continue
                if stars != _stored_stars.get(track_id, 0):
                    _ratings_changed = True
                nested = session.begin_nested()
                try:
                    session.execute(
                        text("UPDATE tracks SET stars = :stars WHERE id = :tid"),
                        {"stars": stars, "tid": track_id},
                    )
                    if nested.is_active:
                        nested.commit()
                except Exception as exc:
                    try:
                        nested.rollback()
                    except Exception:
                        pass

        if _ratings_changed:
            invalidate_genre_playlist_rows()

        # 4. Apply the favourite floor to the newly persisted ratings
        try:
            from services.favourites_service import apply_favourite_rating_floor
            _floored = apply_favourite_rating_floor(artist, album)
            if _floored:
                invalidate_genre_playlist_rows()
                log_unified(f"♥ {_floored} hearted track(s) raised to the favourite rating floor")
        except Exception as _floor_err:
            pass

        # 5. Reload authoritative final ratings into memory in one query batch
        _track_by_id = {
            str(track.get("track_id") or "").strip(): track
            for track in album_results
            if str(track.get("track_id") or "").strip()
        }
        if _track_by_id:
            try:
                with db_session() as session:
                    _final_rows = session.execute(
                        text(
                            "SELECT id, COALESCE(stars, star_rating, 0) AS stars FROM tracks "
                            "WHERE CAST(id AS TEXT) IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": list(_track_by_id)},
                    ).fetchall() or []
                for row in _final_rows:
                    mapping = row._mapping
                    tid = str(mapping.get("id") or "").strip()
                    final_stars = max(1, min(int(mapping.get("stars") or 1), 5))
                    if tid in _track_by_id:
                        _track_by_id[tid]["stars"] = final_stars
            except Exception as _reload_err:
                pass

        # 6. Write file tags and log progress using final values (without opening an unused DB session)
        for track in album_results:
            stars = int(track.get("stars") or 0)
            if stars < 1:
                continue
            track_id = str(track.get("track_id") or "").strip()

            _track_score = float(track.get("popularity_score") or track.get("final_score") or 0)
            _final_score = float(track.get("final_score") or _track_score or 0)
            _album_z = _compute_album_z(_track_score, album_scores)[0]
            _artist_z = _compute_artist_z(_track_score, artist_scores)[0]

            _src_names: list[str] = []
            try:
                _src_raw = track.get("single_sources") or ""
                if isinstance(_src_raw, str):
                    _src_parsed = json.loads(_src_raw) if _src_raw.strip() else []
                else:
                    _src_parsed = _src_raw
                _src_names = [
                    str(s.get("source") or "").replace("_", " ")
                    for s in (_src_parsed or [])
                    if isinstance(s, dict) and bool(s.get("matched"))
                ]
            except Exception as source_exc:
                pass

            _src_part = f", matched=[{', '.join(_src_names)}]" if _src_names else ""

            log_unified(
                f"[TRACK_RESULT] {artist} - {track.get('title')} → {stars}★ "
                f"(final_score={_final_score:.1f}, album_z={_album_z:.2f}, artist_z={_artist_z:.2f}, "
                f"single={track.get('is_single')}/{track.get('single_confidence')}"
                + (f", era={album_model.get('era')}/R={float(album_model.get('reff') or 0):.2f}" if album_model.get("has_benchmark") else "")
                + f"{_src_part})"
            )

            if not track_id:
                continue

            _rating_changed = stars != _stored_stars.get(track_id, 0)
            if not (_skip_unchanged and not _rating_changed):
                try:
                    from services.metadata.tag_file_service import (
                        _resolve_music_file_path,
                        write_rating_to_file,
                    )
                    _path = _stored_paths.get(track_id) or ""
                    _absolute_path = _resolve_music_file_path(_path) if _path else None
                    if _absolute_path:
                        write_rating_to_file(_absolute_path, stars)
                except Exception as tag_exc:
                    pass

        try:
            star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for t in album_results:
                s = int(t.get("stars") or 0)
                if 1 <= s <= 5:
                    star_counts[s] += 1

            _pop_only = bool(options.get("popularity_only"))
            singles_detected = [] if _pop_only else [t for t in album_results if t.get("is_single")]
            if singles_detected:
                log_unified(f"Singles Detection - Detected {len(singles_detected)} single(s) in '{album}'")

            _ref_scores = artist_scores if is_compilation else album_scores

            rows: list[dict[str, Any]] = []
            for t in album_results:
                t_stars = int(t.get("stars") or 0)
                t_conf = str(t.get("single_confidence") or "low").lower()
                t_title = str(t.get("title") or "Unknown").strip()
                t_score = float(t.get("popularity_score") or t.get("final_score") or 0)
                album_z, _ = _compute_album_z(t_score, _ref_scores)

                note = ""
                try:
                    sources = t.get("single_sources") or ""
                    if isinstance(sources, str):
                        parsed = json.loads(sources) if sources.strip() else []
                    else:
                        parsed = sources
                    if isinstance(parsed, list) and parsed:
                        matched_sources = [
                            str(s.get("source") or "") for s in parsed
                            if isinstance(s, dict) and bool(s.get("matched"))
                        ]
                        if matched_sources:
                            note = " (" + ", ".join(s.replace("_", " ").title() for s in matched_sources[:2]) + ")"
                except Exception:
                    pass
                if not note and t_stars == 5 and t_conf == "low":
                    note = " (Standout)"

                rows.append({
                    "stars": max(1, min(t_stars, 5)),
                    "title": t_title[:34],
                    "z": album_z,
                    "score": t_score,
                    "lf": _fmt_count(t.get("lastfm_listeners")),
                    "conf": t_conf.upper() if t_conf in ("high", "medium") else "LOW",
                    "note": note,
                })
            rows.sort(key=lambda r: (-r["stars"], -r["score"]))

            log_unified("=" * 80)
            log_unified(f"📊 SCAN RESULTS: {str(artist or '').strip()} — {str(album or '').strip()} ({len(album_results)} Tracks)")
            log_unified("=" * 80)
            log_unified(f"{'RATING':<7} {'TRACK TITLE':<34} {'Z-SCORE':>7} {'SCORE':>6} {'LF LISTENS':>10}  SINGLE CONF")
            log_unified("-" * 80)
            for r in rows:
                star_str = "★" * r["stars"] + "☆" * (5 - r["stars"])
                log_unified(f"{star_str:<7} {r['title']:<34} {r['z']:>+7.2f} {r['score']:>6.1f} {r['lf']:>10}  {r['conf']}{r['note']}")
            log_unified("-" * 80)
            log_unified(f"⭐ Distribution: 5★: {star_counts[5]} | 4★: {star_counts[4]} | 3★: {star_counts[3]} | 2★: {star_counts[2]} | 1★: {star_counts[1]}")
            log_unified("=" * 80)
        except Exception as log_exc:
            pass

        if options.get("sync_navidrome", True):
            _attempted = 0
            _synced = 0
            _skipped = 0
            _sync_clients: list[Any] | None = None
            _consecutive_failures = 0

            for track in album_results:
                stars = track.get("stars", 0)
                if stars < 1:
                    continue
                track_id = str(track.get("track_id") or "")
                if track_id:
                    if _skip_unchanged and stars == _stored_stars.get(track_id, 0):
                        _skipped += 1
                        continue
                    if _sync_clients is None:
                        _sync_clients = _build_rating_sync_clients()
                        if not _sync_clients:
                            break

                    _attempted += 1
                    if _sync_rating_to_navidrome(track_id, stars, clients=_sync_clients):
                        navidrome_synced += 1
                        _synced += 1
                        _consecutive_failures = 0
                    else:
                        _consecutive_failures += 1
                        if _consecutive_failures >= 3:
                            break

            _failed = _attempted - _synced
            if _skipped > 0:
                log_unified(f"🔗 Navidrome: skipped {_skipped} unchanged rating(s) for '{artist}'")
            if _synced > 0:
                log_unified(f"🔗 Navidrome: synced {_synced} rating(s) for '{artist}'")

    except Exception as exc:
        pass

    return {"star_ratings": total_star_ratings, "navidrome_synced": navidrome_synced}


# ---------------------------------------------------------------------------
# Global playlist rebuild coalescing
# ---------------------------------------------------------------------------

_GLOBAL_PLAYLIST_LOCK = threading.Lock()


def _run_global_playlist_rebuild(options: dict[str, Any]) -> None:
    marker = "_global_playlists_done"
    if options.get(marker):
        return

    with _GLOBAL_PLAYLIST_LOCK:
        if options.get(marker):
            return
        try:
            if _genre_playlists_active():
                written = _create_genre_top_track_playlists()
                if written:
                    log_unified(f"[FINALISE_STAGE] Genre playlists: {written} playlist(s) synced")
        except Exception as exc:
            pass

        try:
            if _new_music_playlist_enabled():
                _create_new_music_playlist()
        except Exception as exc:
            pass

        try:
            options[marker] = True
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def _sync_isrc_popularity() -> int:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks AS target
                    SET popularity = source.max_score,
                        final_score = source.max_score,
                        is_single = source.max_single_status
                    FROM (
                        SELECT
                            isrc,
                            MAX(COALESCE(popularity, final_score, 0)) AS max_score,
                            BOOL_OR(COALESCE(is_single, FALSE)) AS max_single_status
                        FROM tracks
                        WHERE isrc IS NOT NULL
                          AND TRIM(isrc) <> ''
                        GROUP BY isrc
                    ) AS source
                    WHERE target.isrc = source.isrc
                      AND (
                          COALESCE(target.popularity, target.final_score, 0)
                              IS DISTINCT FROM source.max_score
                          OR COALESCE(target.is_single, FALSE)
                              IS DISTINCT FROM source.max_single_status
                      )
                """)
            )
            return max(0, int(result.rowcount or 0))
    except Exception as exc:
        return 0


def finalise_scan(*, results: list[dict[str, Any]], options: dict[str, Any]) -> None:
    track_count = len(results) if results else 0
    log_unified(f"[FINALISE_STAGE] Finalising scan — {track_count} tracks processed")

    try:
        from services.popularity.popularity_config import get_single_organic_floor
        from helpers.config_helpers import get_config
        _floor_score, _floor_listeners = get_single_organic_floor()
        _standout_cfg = get_standout_config() or {}
        _lz = _safe_float(_standout_cfg.get("listener_5star_z_threshold"), 1.0)
        _lz_source = "config" if "listener_5star_z_threshold" in _standout_cfg else "defaults"
        _sd_cfg = (get_config() or {}).get("single_detection") or {}
        _floor_source = "config" if isinstance(_sd_cfg, dict) and "single_organic_floor_score" in _sd_cfg else "defaults"
        log_unified(
            f"🧪 STAR CONFIG: organic_floor={_floor_score:g} (listeners {_floor_listeners:g}, {_floor_source}) | "
            f"listener_5star_z={_lz:g} ({_lz_source})"
        )
    except Exception:
        pass

    if not results:
        try:
            if _essential_playlists_enabled(options):
                if options.get("_essential_playlists_done"):
                    log_unified(
                        f"[FINALISE_STAGE] Essential collections refreshed during scan: "
                        f"{len(options['_essential_playlists_done'])} artist(s)"
                    )
                else:
                    _essential_refreshed = _refresh_all_essential_collections()
                    if _essential_refreshed:
                        log_unified(f"[FINALISE_STAGE] Essential collections refreshed: {_essential_refreshed} artist(s)")
            _run_global_playlist_rebuild(options)
        except Exception as exc:
            pass
        return

    from collections import defaultdict
    by_artist: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        artist = str(r.get("album_artist") or r.get("artist") or r.get("canonical_artist") or "Unknown")
        by_artist[artist].append(r)

    total_star_ratings = 0
    navidrome_synced = 0

    per_album_posted = bool(options.get("_per_album_posted"))

    raw_posted_keys = options.get("_per_album_posted_keys") or set()
    posted_keys = {
        (
            str(key[0]).strip().casefold(),
            str(key[1]).strip().casefold(),
        )
        for key in raw_posted_keys
        if isinstance(key, (tuple, list)) and len(key) >= 2
    }

    _featured_rows = options.get("_essential_featured_rows")
    if _featured_rows is None and _essential_playlists_enabled(options) and _essential_include_featured_enabled():
        _featured_rows = _fetch_essential_featured_rows()

    try:
        for artist, artist_results in by_artist.items():
            scanned_keys = {
                (
                    str(r.get("album") or "").strip().casefold(),
                    _normalise_essential_title(str(r.get("title") or "")),
                )
                for r in artist_results
            }
            artist_scores = compute_artist_scores(
                artist,
                [
                    float(r.get("popularity_score") or r.get("final_score") or 0)
                    for r in artist_results
                    if float(r.get("popularity_score") or r.get("final_score") or 0) > 0
                    and not bool(r.get("exclude_from_stats"))
                ],
                scanned_keys=scanned_keys,
            )

            try:
                _valid_scores = [float(s) for s in artist_scores if float(s or 0) > 0]
                if _valid_scores:
                    _med = median(_valid_scores)
                    _mads = [abs(s - _med) for s in _valid_scores]
                    _mad = median(_mads) if _mads else 0.0
                    _album_count = len({str(r.get("album") or "") for r in artist_results if r.get("album")})
                    _artist_id = _resolve_navidrome_artist_id(artist) or artist

                    with db_session() as session:
                        session.execute(
                            text("""
                                INSERT INTO artist_stats
                                    (artist_id, artist_name, album_count, track_count, last_updated,
                                     mean_popularity, median_popularity, popularity_stddev, popularity_mad)
                                VALUES (:artist_id, :artist_name, :album_count, :track_count, :last_updated,
                                        :mean, :median, :stddev, :mad)
                                ON CONFLICT (artist_id) DO UPDATE SET
                                    artist_name = EXCLUDED.artist_name,
                                    album_count = EXCLUDED.album_count,
                                    track_count = EXCLUDED.track_count,
                                    last_updated = EXCLUDED.last_updated,
                                    mean_popularity = EXCLUDED.mean_popularity,
                                    median_popularity = EXCLUDED.median_popularity,
                                    popularity_stddev = EXCLUDED.popularity_stddev,
                                    popularity_mad = EXCLUDED.popularity_mad
                            """),
                            {
                                "artist_id": _artist_id,
                                "artist_name": artist,
                                "album_count": _album_count,
                                "track_count": len(_valid_scores),
                                "last_updated": datetime.now().isoformat(),
                                "mean": mean(_valid_scores),
                                "median": _med,
                                "stddev": stdev(_valid_scores) if len(_valid_scores) > 1 else 0.0,
                                "mad": _mad,
                            },
                        )
            except Exception as exc:
                pass

            by_album: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in artist_results:
                album = str(r.get("album") or "Unknown")
                by_album[album].append(r)

            for album, album_results in by_album.items():
                if (artist.strip().casefold(), album.strip().casefold()) in posted_keys:
                    continue
                _posted = post_album_star_ratings(
                    album_results=album_results,
                    artist=artist,
                    artist_scores=artist_scores,
                    options=options,
                )
                total_star_ratings += _posted.get("star_ratings", 0)
                navidrome_synced += _posted.get("navidrome_synced", 0)

            if _essential_playlists_enabled(options):
                _done_artists = {
                    str(value).strip().casefold()
                    for value in (options.get("_essential_playlists_done") or set())
                }
                if artist.strip().casefold() not in _done_artists:
                    _sync_essential_playlist(
                        artist,
                        artist_scores=artist_scores,
                        featured_rows=_featured_rows,
                    )

        try:
            _isrc_updated = _sync_isrc_popularity()
            if _isrc_updated:
                log_unified(
                    f"[FINALISE_STAGE] ISRC sync: {_isrc_updated} track(s) updated; "
                    f"affected star ratings will be recalculated during the next rating pass"
                )
        except Exception as exc:
            pass

        _run_global_playlist_rebuild(options)

    except Exception as exc:
        pass

    if per_album_posted:
        total_star_ratings = sum(1 for r in results if (r.get("stars") or 0) > 0)

    log_unified(f"[FINALISE_STAGE] Star ratings assigned: {total_star_ratings}")
    log_unified(f"[FINALISE_STAGE] Navidrome syncs: {navidrome_synced}")

    star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in results:
        s = r.get("stars", 0) or 0
        if 1 <= s <= 5:
            star_counts[s] += 1

    log_unified(
        f"[FINALISE_STAGE] Star distribution — 5★: {star_counts[5]}, 4★: {star_counts[4]}, 3★: {star_counts[3]}, 2★: {star_counts[2]}, 1★: {star_counts[1]}",
    )

    return None
