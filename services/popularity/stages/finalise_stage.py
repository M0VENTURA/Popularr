"""Popularity scan finalisation stage.

Handles:
- Star rating assignment (1-5★) using album/artist z-scores + z-score bands
- Live-album rating driven by ARTIST-z rather than album-z
- Navidrome rating sync via Subsonic API
- Essential Collection API sync (deduplicated 4★/5★ artist best-of)
- Summary logging

NOTE: All playlist creation/maintenance (Essential Collections, genre
playlists, New Music) is done entirely through the Subsonic/Navidrome API.
No .m3u or .nsp files are written to disk anywhere in this module.

Write-amplification controls:
- Playlist pushes are skipped when the resolved track list is byte-identical
  to the list already pushed for that playlist, so a repeated finalise pass
  no longer rewrites every playlist.
- Genre playlist and New Music rebuilds are global operations. They are
  coalesced so a scan that finalises per artist/album runs them once rather
  than once per batch.
- The genre-playlist track fetch (a full-library query) is cached for a short
  TTL and shared between the full rebuild and the per-album refresh.
- Navidrome clients and the featured-track sweep are resolved once instead of
  once per artist.

Live-album policy:
- Live releases are rated against the ARTIST's catalogue (artist-z), not
  against their own tracks. Album-relative z is self-referential and lets the
  best cut on a mediocre live record score like the best cut on a landmark
  studio album. Most live tracks sit at negative artist-z and fall naturally
  into the 1-3 bands; genuine singles rise on their own merit.
- Album-z acts only as a gate (blocking 4-5★ when a weak-catalogue artist's
  live album merely beats their own low median) and as a promotion path (a
  decisive standout within its own release that is also respectable
  catalogue-wide). It never lifts a rating on its own.
- Per-release slot caps (1x 5★, 2x 4★ by default) apply after banding.

Global 5-star catalogue-top pre-pass (scan_runner):
- Before an artist's albums are finalised, scan_runner flags a top-percentage
  of that artist's catalogue-top tracks as ``_global_5star_locked``.
- FIXED: ``_assign_stars`` previously treated this flag as sufficient on its
  own to award 5 stars, bypassing the album/artist z-score bounds entirely.
  That let a track with an album_z well below the configured 5★ threshold
  (e.g. +0.37 against a 5★ floor of ~0.44) still receive 5 stars purely from
  catalogue-top membership, and it starved the 4★ tier because every track
  the pre-pass selected got pulled straight to 5★ instead of landing on the
  band ladder. The lock is now a gate, not a verdict: it only grants 5★ when
  the track ALSO clears the normal album-z/artist-z 5★ bounds (matching the
  documented rule "album z AND artist z above these bounds AND a top-X%
  catalogue flag"). When it doesn't clear those bounds, the track is floored
  to 4★ instead, so a confirmed catalogue-top single still gets recognised
  without displacing genuine standouts from the 5★ tier or starving the 4★
  tier.

COMPILATION RATING FIX (this revision):
- A compilation ("Various Artists"-style) album groups many different
  CREDITED artists under one shared album/album_artist folder. The star
  rating for each compilation track must be computed against the CREDITED
  TRACK ARTIST's own catalogue -- not against the album/album_artist's
  catalogue, and not against the generic "Various Artists" pool.
- ``post_album_star_ratings`` now builds a per-credited-artist score
  distribution (``track_artist_score_map``) for compilation albums, keyed by
  the track's own ``artist`` field (the recording credit), and passes that
  distribution into ``_assign_stars`` as the catalogue reference for that
  track instead of the whole-album/whole-compilation ``artist_scores`` list.
- ``_assign_stars`` compilation branch is rewritten to rate a track primarily
  against its credited artist's catalogue z-score/percentile. Verified single
  status is corroborating evidence that can raise a track to a floor, not the
  primary ranking ladder. Absolute score/listener thresholds are now used only
  as a fallback when the credited artist's catalogue is too thin (<5 usable
  scores) to compute a robust z-score.
- The catalogue-top 5-star lock is no longer evaluated against the
  compilation's shared distribution; for compilation tracks it defers
  entirely to the compilation branch's own catalogue-relative decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from statistics import mean, median, stdev
from typing import Any
from urllib.parse import urlsplit

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from db.utils import row_get
from helpers.config_helpers import get_standout_config
from helpers.logging_config import log_unified
from helpers.normalization_service import normalize_unicode_punctuation
from services.catalog.album_classification_service import (
    METAL_GENRE_FAMILY,
    is_christmas_genre as is_christmas_genre_token,
    is_christmas_track,
    is_instrumental_track_title,
    is_live_or_alternate_track_title,
    is_metal_genre,
    iter_genre_tokens,
)
from services.popularity.popularity_math import (
    age_skew_multiplier,
    album_prominence_score,
    album_ratio_standout,
    apply_album_relative_popularity,
    calculate_percentile_star_rating,
    calculate_robust_zscore,
    effective_album_ratio,
    fmt_count as _fmt_count,
    is_live_album_context,
    live_album_star_from_artist_z,
    playlist_popularity_score,
)
from services.popularity.popularity_zscore import composite_listener_z
from services.enrichment.genre_aggregation_service import (
    is_playlist_pool_genre,
    playlist_pool_genres,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Live star-rating / era-scaling config
# ---------------------------------------------------------------------------

_DEFAULT_ERA_RULES: dict[str, dict[str, float | int]] = {
    "peak": {"catalog_top_pct": 0.20, "album_top_n": 3, "max_5star_slots": 4},
    "solid": {"catalog_top_pct": 0.15, "album_top_n": 3, "max_5star_slots": 3},
    "minor": {"catalog_top_pct": 0.10, "album_top_n": 3, "max_5star_slots": 2},
}

#: Fallbacks for the album ratio-standout gate, mirroring
#: helpers/config_helpers._DEFAULT_ALBUM_RATIO_STANDOUT. Kept here as well so
#: the gate has sane values when it is reached without a config layer (tests,
#: direct calls).
_DEFAULT_ALBUM_RATIO_RULES: dict[str, float] = {
    "enabled": 1.0,
    "runner_up_min": 1.5,
    "median_min": 3.0,
    "floor_min": 10.0,
    "min_passed": 3.0,
    "live_runner_up_min": 1.5,
    "live_median_min": 2.0,
    "live_floor_min": 5.0,
    "live_min_passed": 2.0,
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
        "live_4star_requires_single": 1.0 if bool(cfg.get("live_4star_requires_single", True)) else 0.0,
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
# Live-album rating config
# ---------------------------------------------------------------------------

_DEFAULT_LIVE_ALBUM_RULES: dict[str, Any] = {
    "enabled": 1,
    "mode": "artist_and_album",      # or "artist_z"
    "max_stars": 3,
    "artist_z_star5": 1.50,
    "artist_z_star4": 1.00,
    "artist_z_star3": -0.25,
    "artist_z_star2": -1.20,
    "album_z_gate": 2.00,
    "promote_artist_z_min": 0.50,
    "min_catalogue": 8,
    "max_5star_slots": 1,
    "max_4star_slots": 2,
    "track_fraction": 0.60,
}


def _live_album_rules() -> dict[str, Any]:
    """Live-album rules, read live from ``standout.live_album_scaling``."""
    cfg = get_standout_config() or {}
    block = cfg.get("live_album_scaling") or {}
    if not isinstance(block, dict):
        block = {}

    rules: dict[str, Any] = {}
    for key, default in _DEFAULT_LIVE_ALBUM_RULES.items():
        if key == "mode":
            rules[key] = str(block.get(key) or default).strip().casefold()
        else:
            rules[key] = _safe_float(block.get(key), float(default))
    return rules


def _live_album_scaling_configured() -> bool:
    try:
        cfg = get_standout_config() or {}
        block = cfg.get("live_album_scaling")
        return isinstance(block, dict) and bool(block)
    except Exception:
        return False


def _album_ratio_rules(*, live: bool = False) -> dict[str, float]:
    """Album ratio-standout thresholds, read live from config."""
    cfg = get_standout_config() or {}
    block = cfg.get("album_ratio_standout") or {}
    if not isinstance(block, dict):
        block = {}

    defaults = _DEFAULT_ALBUM_RATIO_RULES
    prefix = "live_" if live else ""
    return {
        "enabled": _safe_float(block.get("enabled"), float(defaults["enabled"])),
        "runner_up_min": _safe_float(
            block.get(f"{prefix}runner_up_min"), float(defaults[f"{prefix}runner_up_min"])
        ),
        "median_min": _safe_float(
            block.get(f"{prefix}median_min"), float(defaults[f"{prefix}median_min"])
        ),
        "floor_min": _safe_float(
            block.get(f"{prefix}floor_min"), float(defaults[f"{prefix}floor_min"])
        ),
        "min_passed": _safe_float(
            block.get(f"{prefix}min_passed"), float(defaults[f"{prefix}min_passed"])
        ),
    }


def _album_ratio_standout_ok(
    track: dict[str, Any],
    *,
    album_lf_listeners: list[float] | None,
    album_lb_listens: list[float] | None,
    live: bool = False,
) -> tuple[bool, str]:
    """Apply the ratio ("shape of popularity") test to a 5★ candidate.

    ONE PROVIDER, RAW COUNTS — both points are load-bearing:

    * **One provider.** The series must measure one thing. Last.fm listeners
      and ListenBrainz listens differ by orders of magnitude for the same
      recording, so pairing them per track (or concatenating them) would make
      the multipliers meaningless. Last.fm is preferred; ListenBrainz is the
      fallback when Last.fm has no usable series.

    * **Raw counts, never log-transformed.** A log compression is monotonic
      but massively shrinks large ratios. Taking the ratio AFTER compressing
      therefore destroys exactly the dynamic range this test exists to measure.

    Returns ``(ok, reason)``. ``ok`` is True when the test is DISABLED or the
    data is too thin to judge, so this gate only ever blocks a 5★ it can
    actually justify.
    """
    rules = _album_ratio_rules(live=live)
    if not bool(rules.get("enabled", 1)):
        return True, "ratio_gate_disabled"

    def _series(values: list[float] | None) -> list[float]:
        return [float(v or 0) for v in (values or []) if float(v or 0) > 0]

    lf_series = _series(album_lf_listeners)
    lb_series = _series(album_lb_listens)

    track_lf = float(track.get("lastfm_listeners") or 0)
    track_lb = float(track.get("listenbrainz_listens") or 0)

    if len(lf_series) >= 3 and track_lf > 0:
        counts, candidate, source = lf_series, track_lf, "lastfm"
    elif len(lb_series) >= 3 and track_lb > 0:
        counts, candidate, source = lb_series, track_lb, "listenbrainz"
    else:
        return True, "ratio_inconclusive(no_usable_series)"

    ok, reason, _ratios = album_ratio_standout(
        candidate,
        counts,
        runner_up_min=float(rules["runner_up_min"]),
        median_min=float(rules["median_min"]),
        floor_min=float(rules["floor_min"]),
        min_passed=int(rules["min_passed"]),
    )
    return ok, f"{reason}[{source}]"


def _detect_live_album(album_results: list[dict[str, Any]]) -> bool:
    """True when the RELEASE is a live album, not just a live track."""
    rules = _live_album_rules()
    if not bool(rules.get("enabled", 1)):
        return False

    album_type = ""
    if album_results:
        album_type = str(
            album_results[0].get("album_type")
            or album_results[0].get("detected_album_type")
            or album_results[0].get("live_album_type")
            or ""
        )
    return is_live_album_context(
        album_results,
        album_type=album_type,
        fraction=float(rules["track_fraction"]),
    )


# ---------------------------------------------------------------------------
# Standout detection helpers
# ---------------------------------------------------------------------------

def _compute_album_z(score: float, scores: list[float]) -> tuple[float, float]:
    """Robust album z (median + scaled-MAD) -- and the spread used."""
    return calculate_robust_zscore(score, scores, min_count=3)


#: Smallest same-fate cohort that may replace the album pool. Equals
#: ``_compute_album_z``'s ``min_count``: a smaller pool returns 0.0 anyway, so
#: falling back to the album is both safe and honest.
_LB_COHORT_MIN_TRACKS = 3


def _lb_unusable_cohort(
    album_rows: list[dict[str, Any]],
) -> tuple[set[str], list[float]]:
    """Track ids whose ListenBrainz source is unusable, and their scores.

    "Unusable" means zero listens, or the Log-MAD audit REJECTS them against
    this album's own LF/LB pairs — exactly the test ``track_stage`` applies
    when it zeroes the count, so a track classified here is treated the same
    way it was scored.
    """
    unusable: set[str] = set()
    scores: list[float] = []
    if not album_rows:
        return unusable, scores

    try:
        from services.popularity.popularity_config import get_log_ratio_config
        from services.popularity.popularity_math import evaluate_log_ratio_deviation

        cfg = get_log_ratio_config()
        threshold = float(cfg.get("divergence_threshold", 0.85))
        reject_lb_min_lf = int(cfg.get("reject_lb_min_lf", 100))
    except Exception as exc:
        logger.debug("LB cohort config unavailable", error=str(exc))
        threshold, reject_lb_min_lf = None, None

    pairs = [
        (int(r.get("lastfm_listeners") or 0), int(r.get("listenbrainz_listens") or 0))
        for r in album_rows
        if int(r.get("lastfm_listeners") or 0) > 0
        and int(r.get("listenbrainz_listens") or 0) > 0
    ]

    for row in album_rows:
        lb = int(row.get("listenbrainz_listens") or 0)
        if lb <= 0:
            bad = True
        elif pairs and threshold is not None:
            try:
                bad = evaluate_log_ratio_deviation(
                    lastfm_listeners=int(row.get("lastfm_listeners") or 0),
                    listenbrainz_listens=lb,
                    album_lf_lb_pairs=pairs,
                    divergence_threshold=threshold,
                    reject_lb_min_lf=reject_lb_min_lf,
                ) == "REJECT_LB"
            except Exception:
                bad = False
        else:
            bad = False

        if not bad:
            continue
        key = str(row.get("track_id") or row.get("id") or "").strip()
        if key:
            unusable.add(key)
        score = float(row.get("popularity_score") or row.get("final_score") or 0)
        if score > 0:
            scores.append(score)
    return unusable, scores


def _compute_artist_z(score: float, artist_scores: list[float]) -> tuple[float, float]:
    """Robust artist-catalogue z (median + scaled-MAD) -- and the spread used."""
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
    except Exception as exc:
        logger.debug("single_sources parse failed", title=track.get("title"), error=str(exc))
        return False


def _album_z_band_star(
    score: float,
    album_scores: list[float],
    reference_scores: list[float] | None = None,
    artist_scores: list[float] | None = None,
    is_live: bool = False,
    single_confidence: str = "low",
    max_stars: int = 5,
    thresholds: dict[str, float] | None = None,
    precomputed_album_z: tuple[float, float] | None = None,
    precomputed_artist_z: tuple[float, float] | None = None,
) -> int:
    """Album-relative 1-4★ rating from the z-score bands."""
    if score <= 0:
        return 1

    th = thresholds if thresholds is not None else _live_star_thresholds()
    valid = [
        float(s) for s in
        (reference_scores if reference_scores is not None else album_scores or [])
        if float(s or 0) > 0
    ]
    if len(valid) < 3:
        # A SINGLE/EP RELEASE (1-2 track album) has no album distribution; the
        # album z is 0.0 by construction. Rate the base band against the
        # artist's catalogue when it is large enough to be one.
        if artist_scores:
            valid_artist = [
                float(s) for s in artist_scores if float(s or 0) > 0
            ]
            if len(valid_artist) >= 5:
                artist_z, artist_spread = (
                    precomputed_artist_z
                    if precomputed_artist_z is not None
                    else _compute_artist_z(score, valid_artist)
                )
                epsilon = _star_epsilon_z(artist_spread, th["epsilon"])
                if artist_z >= th["star4_artist_z"] - epsilon:
                    return min(4, int(max_stars))
                if artist_z >= th["star3_album_z"] - epsilon:
                    return min(3, int(max_stars))
                if artist_z >= th["star2_album_z"] - epsilon:
                    return min(2, int(max_stars))
                return 1
        return min(3, int(max_stars))

    album_z, spread = (
        precomputed_album_z
        if precomputed_album_z is not None
        else _compute_album_z(score, valid)
    )
    epsilon = _star_epsilon_z(spread, th["epsilon"])

    artist_eligible_4star = True
    if artist_scores:
        valid_artist = [float(s) for s in artist_scores if float(s or 0) > 0]
        if len(valid_artist) >= 5 and len(valid_artist) > len(valid):
            artist_z, artist_spread = (
                precomputed_artist_z
                if precomputed_artist_z is not None
                else _compute_artist_z(score, valid_artist)
            )
            artist_eligible_4star = artist_z >= th["star4_artist_z"] - _star_epsilon_z(artist_spread, th["epsilon"])

    if is_live and album_z >= th["star4_album_z"] - epsilon:
        _requires_single = bool(th.get("live_4star_requires_single", 1.0))
        if _requires_single and str(single_confidence or "low").strip().casefold() not in ("high", "medium", "user"):
            return min(3, int(max_stars))

    if album_z >= th["star4_album_z"] - epsilon and artist_eligible_4star:
        return min(4, int(max_stars))
    if album_z >= th["star3_album_z"] - epsilon:
        return min(3, int(max_stars))
    if album_z >= th["star2_album_z"] - epsilon:
        return min(2, int(max_stars))
    return 1


def _live_album_stars(
    track: dict[str, Any],
    score: float,
    album_scores: list[float],
    artist_scores: list[float],
    single_confidence: str,
    organic: bool,
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
    thresholds: dict[str, float] | None = None,
    precomputed_album_z: tuple[float, float] | None = None,
    precomputed_artist_z: tuple[float, float] | None = None,
) -> tuple[int, str]:
    """Star rating for a track on a LIVE album, driven by artist-z."""
    rules = _live_album_rules()
    max_stars = int(rules["max_stars"])
    th = thresholds if thresholds is not None else _live_star_thresholds()

    valid_artist = [float(s) for s in (artist_scores or []) if float(s or 0) > 0]
    if len(valid_artist) < int(rules["min_catalogue"]):
        band = _album_z_band_star(
            score,
            album_scores,
            reference_scores=album_scores,
            artist_scores=artist_scores,
            is_live=True,
            single_confidence=single_confidence,
            max_stars=max_stars,
            thresholds=th,
            precomputed_album_z=precomputed_album_z,
            precomputed_artist_z=precomputed_artist_z,
        )
        return min(band, max_stars), f"thin_catalogue(n={len(valid_artist)})"

    artist_z, artist_spread = (
        precomputed_artist_z
        if precomputed_artist_z is not None
        else _compute_artist_z(score, valid_artist)
    )
    album_z, _album_spread = (
        precomputed_album_z
        if precomputed_album_z is not None
        else _compute_album_z(score, album_scores)
    )

    epsilon_z = _star_epsilon_z(artist_spread, th["epsilon"])

    stars, reason = live_album_star_from_artist_z(
        artist_z,
        album_z,
        mode=str(rules["mode"]),
        single_confidence=single_confidence,
        organic=organic,
        star5_z=float(rules["artist_z_star5"]),
        star4_z=float(rules["artist_z_star4"]),
        star3_z=float(rules["artist_z_star3"]),
        star2_z=float(rules["artist_z_star2"]),
        album_z_gate=float(rules["album_z_gate"]),
        promote_artist_z_min=float(rules["promote_artist_z_min"]),
        default_max=max_stars,
        epsilon_z=epsilon_z,
    )

    if stars >= 5 and str(single_confidence or "").strip().casefold() != "user":
        _ratio_ok, _ratio_reason = _album_ratio_standout_ok(
            track,
            album_lf_listeners=album_lf_listeners,
            album_lb_listens=album_lb_listens,
            live=True,
        )
        track["_ratio_5star_reason"] = _ratio_reason
        if not _ratio_ok:
            stars = 4
            reason = f"{reason}+ratio_gate_failed"

    return stars, reason


def _album_top_genres(
    album_tracks: list[dict[str, Any]] | None,
    *,
    max_genres: int = 3,
) -> list[str]:
    """Aggregates all genres for an album, prioritizing accurate tags."""
    if not album_tracks:
        return []

    from services.enrichment.genre_aggregation_service import (
        _parse_genre_input,
        aggregate_genres,
        is_propagatable_genre,
    )

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
        top = aggregate_genres(album_source_map, max_genres=max_genres)
        return [g for g in top if is_propagatable_genre(g)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Compilation track-artist catalogue helpers
# ---------------------------------------------------------------------------

def _compilation_track_artist(track: dict[str, Any]) -> str:
    """Return the CREDITED recording artist for a compilation track."""
    return str(track.get("artist") or track.get("album_artist") or "").strip()


#: Album-artist values that mean "this folder is a compilation", independent
#: of any type tag or title keyword.
_VA_ALBUM_ARTIST_NAMES = frozenset({
    "various artists", "various", "va", "v/a",
    "compilation", "soundtrack", "soundtracks",
})

#: Credited "artists" that name no real performer and therefore cannot have
#: an online chart: the VA placeholders above plus generic unknowns. Bracket
#: credits like ``[dialogue]`` are handled by shape, not by name.
_UNRATABLE_TRACK_ARTISTS = _VA_ALBUM_ARTIST_NAMES | frozenset({
    "unknown artist", "unknown", "none", "n/a", "not available",
})

_MULTI_ARTIST_CREDIT_RE = re.compile(
    r"[,&/]|\s+(?:and|with|feat\.?|ft\.?|featuring)\s+",
    re.IGNORECASE,
)


def _is_online_rankable_artist(name: str) -> bool:
    """True when ``name`` could plausibly be a real, chartable artist."""
    text_val = str(name or "").strip()
    if not text_val:
        return False
    if text_val.startswith("[") and text_val.endswith("]"):
        return False
    return text_val.casefold() not in _UNRATABLE_TRACK_ARTISTS


def _online_catalogue_stars(
    *,
    credited_artist: str,
    track_title: str,
    rules: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Rate a compilation track by its RANK in the artist's ONLINE catalogue."""
    detail: dict[str, Any] = {"source": "none"}
    if not bool(rules.get("enabled", 1)):
        return 0, detail

    if not _is_online_rankable_artist(credited_artist):
        return 0, {"source": "pseudo_artist"}

    candidates: list[str] = []
    _seen_candidates: set[str] = set()

    def _add_candidate(value: str) -> None:
        text_value = str(value or "").strip()
        key = text_value.casefold()
        if text_value and key not in _seen_candidates:
            _seen_candidates.add(key)
            candidates.append(text_value)

    _add_candidate(credited_artist)
    try:
        if _MULTI_ARTIST_CREDIT_RE.search(credited_artist):
            from services.popularity.popularity_matching import (
                get_artist_lookup_candidates,
                get_primary_artist_preserve_case,
            )

            for candidate in get_artist_lookup_candidates(credited_artist):
                _add_candidate(candidate)
            _add_candidate(get_primary_artist_preserve_case(credited_artist))
            for part in _MULTI_ARTIST_CREDIT_RE.split(credited_artist):
                _add_candidate(part)
    except Exception as exc:
        logger.debug("Artist candidate expansion failed", artist=credited_artist, error=str(exc))

    try:
        from services.popularity.popularity_sources import get_online_artist_track_rank

        rank_info: dict[str, Any] = {}
        matched_candidate = ""
        _min_catalogue = int(rules.get("min_catalogue_size", 5))
        for candidate in candidates:
            try:
                _info = get_online_artist_track_rank(candidate, track_title) or {}
            except Exception as exc:
                logger.debug(
                    "Online catalogue rank lookup failed",
                    artist=candidate, title=track_title, error=str(exc),
                )
                continue
            if int(_info.get("rank") or 0) > 0 and int(_info.get("total") or 0) >= _min_catalogue:
                rank_info = _info
                matched_candidate = candidate
                break
            if not rank_info:
                rank_info = _info
        if not rank_info:
            rank_info = {"source": "none"}
        if matched_candidate and matched_candidate != credited_artist:
            logger.info(
                "Online catalogue resolved via a credited artist within the credit",
                credited_artist=credited_artist,
                matched_artist=matched_candidate,
                title=track_title,
            )
    except Exception as exc:
        logger.debug(
            "Online catalogue rank lookup failed",
            artist=credited_artist, title=track_title, error=str(exc),
        )
        return 0, detail

    total = int(rank_info.get("total") or 0)
    rank = int(rank_info.get("rank") or 0)
    detail = dict(rank_info)

    if rank <= 0 or total < int(rules.get("min_catalogue_size", 5)):
        return 0, detail

    percentile = float(rank_info.get("percentile") or 1.0)

    if rank == 1 or percentile <= float(rules.get("rank_percentile_5star", 0.02)):
        stars = 5
    elif percentile <= float(rules.get("rank_percentile_4star", 0.10)):
        stars = 4
    elif percentile <= float(rules.get("rank_percentile_3star", 0.35)):
        stars = 3
    elif percentile <= float(rules.get("rank_percentile_2star", 0.65)):
        stars = 2
    else:
        stars = 1

    detail["stars"] = stars
    return stars, detail


def _resolve_compilation_flags(
    *,
    album_results: list[dict[str, Any]],
    artist: str,
    album: str,
) -> tuple[bool, bool]:
    """Best-effort ``(is_compilation, is_va_compilation)`` from local data."""
    row = album_results[0] if album_results else {}
    from services.catalog.album_classification_service import (
        classify_compilation_category,
        detect_greatest_hits_album,
        joined_album_type,
    )

    type_text = joined_album_type(row)
    album_artist = str(row.get("album_artist") or artist or "").strip()
    generic_artist = album_artist.casefold() in _VA_ALBUM_ARTIST_NAMES

    try:
        category = classify_compilation_category(
            artist=artist,
            album=album,
            tracks=album_results,
            album_artist=album_artist,
            type_text=type_text,
        ) or ""
    except Exception as exc:
        logger.debug(
            "Compilation classification failed",
            artist=artist, album=album, error=str(exc),
        )
        category = ""

    if category:
        return True, category == "va"
    if generic_artist:
        return True, True

    if not type_text and detect_greatest_hits_album(album=album, artist=artist):
        return True, False

    return False, False


def compute_track_artist_scores(
    track_artist: str,
    scan_results: list[dict[str, Any]],
    *,
    exclude_album: str | None = None,
) -> list[float]:
    """Build the credited track artist's catalogue score distribution."""
    artist_key = str(track_artist or "").strip().casefold()
    if not artist_key:
        return []

    exclude_key = str(exclude_album or "").strip().casefold()

    scanned_keys: set[tuple[str, str]] = set()
    scores: list[float] = []

    for result in scan_results:
        result_artist = str(result.get("artist") or "").strip().casefold()
        if result_artist != artist_key:
            continue

        score = float(result.get("popularity_score") or result.get("final_score") or 0)
        if score <= 0 or bool(result.get("exclude_from_stats")):
            continue

        result_album = str(result.get("album") or "").strip().casefold()
        scanned_keys.add(
            (
                result_album,
                _normalise_essential_title(str(result.get("title") or "")),
            )
        )
        if exclude_key and result_album == exclude_key:
            continue

        scores.append(score)

    db_rows: list[tuple[str, float]] = []
    try:
        from services.catalog.album_classification_service import is_bonus_track_title

        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT title, album, COALESCE(NULLIF(raw_score, 0), final_score) AS final_score FROM tracks "
                    "WHERE LOWER(TRIM(artist)) = LOWER(TRIM(:artist)) "
                    "AND COALESCE(NULLIF(raw_score, 0), final_score) > 0"
                ),
                {"artist": track_artist},
            ).fetchall() or []

        for row in rows:
            title = str(row_get(row, "title") or "").strip()
            album = str(row_get(row, "album") or "").strip()
            score = float(row_get(row, "raw_score") or row_get(row, "final_score") or 0)

            if score <= 0 or is_bonus_track_title(title):
                continue

            if exclude_key and album.casefold() == exclude_key:
                continue

            key = (album.casefold(), _normalise_essential_title(title))
            if key in scanned_keys:
                continue

            db_rows.append((album, score))
    except Exception as exc:
        logger.debug("Track-artist catalogue fetch failed", artist=track_artist, error=str(exc))

    try:
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        scores.extend(reanchor_scores_to_album_relative(db_rows))
    except Exception as exc:
        logger.debug("Track-artist catalogue re-anchor failed", artist=track_artist, error=str(exc))
        scores.extend(score for _album, score in db_rows)

    return [float(s) for s in scores if float(s or 0) > 0]


def _build_compilation_track_artist_scores(
    album_results: list[dict[str, Any]],
    *,
    exclude_album: str | None = None,
) -> dict[str, list[float]]:
    """Build one score distribution per credited artist on a compilation."""
    credited_artists = {
        _compilation_track_artist(track)
        for track in album_results
        if _compilation_track_artist(track)
    }

    score_map: dict[str, list[float]] = {}
    for credited_artist in credited_artists:
        score_map[credited_artist.casefold()] = compute_track_artist_scores(
            credited_artist,
            album_results,
            exclude_album=exclude_album,
        )
    return score_map


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
    is_va_compilation: bool = False,
    is_live_album: bool = False,
    source_unusable: bool = False,
    cohort_scores: list[float] | None = None,
    compilation_raw_scores: list[float] | None = None,
    *,
    thresholds: dict[str, float] | None = None,
    organic_floor: tuple[float, float] | None = None,
    force_star_percentiles: tuple[float, float] | None = None,
    compilation_online_rules: dict[str, Any] | None = None,
) -> int:
    """Assign 1-5 star rating to a single track."""
    score = float(track.get("popularity_score") or track.get("final_score") or 0)
    single_confidence = str(track.get("single_confidence") or "low").strip().casefold()

    track.pop("_era_5star", None)
    track.pop("_force_floor", None)
    track.pop("_live_reason", None)
    track.pop("_ratio_5star_reason", None)
    track.pop("_computed_album_z", None)
    track.pop("_computed_artist_z", None)
    # The pre-pass flag ``_global_5star_locked`` is INPUT and is never
    # mutated here, so a re-rate (provisional -> reconciled) sees the same
    # lock. Whether the lock was actually HONOURED is recorded separately in
    # ``_lock_honoured``, which is what the slot/short-track caps read.
    track.pop("_lock_honoured", None)
    for _stale_key in [k for k in track if str(k).startswith("_compilation_")]:
        track.pop(_stale_key, None)

    is_live = (
        bool(track.get("is_live"))
        or bool(track.get("album_context_live"))
        or is_live_or_alternate_track_title(track.get("title"))
    )

    th = thresholds if thresholds is not None else _live_star_thresholds()

    if organic_floor is not None:
        _org_score, _org_listeners = organic_floor
    else:
        try:
            from services.popularity.popularity_config import get_single_organic_floor
            _org_score, _org_listeners = get_single_organic_floor()
        except Exception as exc:
            logger.debug("Organic floor config read failed, using defaults", error=str(exc))
            _org_score, _org_listeners = 45.0, 1000.0

    organic = score >= _org_score or int(track.get("lastfm_listeners") or 0) >= _org_listeners

    _cohort_pool = list(cohort_scores or [])
    _use_lb_cohort = (
        bool(source_unusable)
        and not is_compilation
        and len(_cohort_pool) >= _LB_COHORT_MIN_TRACKS
    )
    _album_pool = _cohort_pool if _use_lb_cohort else album_scores

    _small_album_pool = (
        len([s for s in _album_pool if float(s or 0) > 0])
        < _LB_COHORT_MIN_TRACKS
    )
    if is_compilation or _small_album_pool:
        album_z, album_spread = _compute_artist_z(score, artist_scores)
        artist_z, artist_spread = album_z, album_spread
    else:
        album_z, album_spread = _compute_album_z(score, _album_pool)
        artist_z, artist_spread = _compute_artist_z(score, artist_scores)

    # Cache the exact z-scores that rated this track so downstream sorting
    # and logging in post_album_star_ratings do not recompute or drift.
    track["_computed_album_z"] = album_z
    track["_computed_artist_z"] = artist_z

    if single_confidence == "user":
        track["_lock_honoured"] = True
        return 5

    popularity_marked = bool(track.get("popularity_marked"))
    if is_instrumental_track_title(str(track.get("title") or "")):
        popularity_marked = False

    if is_live_album and not is_compilation:
        live_stars, live_reason = _live_album_stars(
            track,
            score,
            _album_pool,
            artist_scores,
            single_confidence,
            organic,
            album_lf_listeners=album_lf_listeners,
            album_lb_listens=album_lb_listens,
            thresholds=th,
            precomputed_album_z=(album_z, album_spread),
            precomputed_artist_z=(artist_z, artist_spread),
        )
        track["_live_reason"] = live_reason
        if live_stars >= 5:
            track["_era_5star"] = True
        return live_stars

    if track.get("_global_5star_locked") and not popularity_only and not is_compilation:
        if not is_live and organic:
            _epsilon_alb = _star_epsilon_z(album_spread, th["epsilon"])
            _epsilon_art = _star_epsilon_z(artist_spread, th["epsilon"])
            _clears_5star_bounds = (
                album_z >= th["star5_album_z"] - _epsilon_alb
                and artist_z >= th["star5_artist_z"] - _epsilon_art
            )
            if _clears_5star_bounds:
                track["_lock_honoured"] = True
                track["_ratio_5star_reason"] = "lock_bypasses_album_shape_gate"
                return 5
            track["_force_floor"] = 4
            return 4

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
            if force_star_percentiles is not None:
                _force5_pct, _force4_pct = force_star_percentiles
            else:
                from services.popularity.popularity_config import get_artist_force_star_percentiles
                _force5_pct, _force4_pct = get_artist_force_star_percentiles()
            _track_listens = float(track.get("lastfm_listeners") or 0)
            if _track_listens > 0 and (_force5_pct > 0 or _force4_pct > 0):
                _valid = [float(v) for v in artist_listen_distribution if float(v or 0) > 0]
                if len(_valid) >= 5:
                    _rank = sum(1 for v in _valid if v > _track_listens) + 1
                    _pct = _rank / len(_valid)
                    if _force5_pct > 0 and _pct <= _force5_pct:
                        _force_stars = 5
                    elif _force4_pct > 0 and _pct <= _force4_pct:
                        _force_stars = 4
        except Exception as exc:
            logger.debug("Force-star percentile check failed", title=track.get("title"), error=str(exc))
            _force_stars = None

    if _force_stars is not None:
        track["_force_floor"] = _force_stars
        return _force_stars

    if is_compilation:
        raw_lf = float(track.get("lastfm_listeners") or 0)
        is_verified_single = single_confidence in ("high", "medium", "user")

        valid_catalogue_scores = [
            float(v) for v in (artist_scores or []) if float(v or 0) > 0
        ]

        if compilation_online_rules is not None:
            _online_rules = compilation_online_rules
        else:
            try:
                from helpers.config_helpers import get_compilation_online_catalogue_config
                _online_rules = get_compilation_online_catalogue_config()
            except Exception as exc:
                logger.debug("Compilation online config read failed", error=str(exc))
                _online_rules = {}

        try:
            _min_local = int(float(_online_rules.get("min_local_catalogue") or 5.0))
        except (TypeError, ValueError):
            _min_local = 5

        has_usable_catalogue = (
            not is_va_compilation and len(valid_catalogue_scores) >= _min_local
        )

        if has_usable_catalogue:
            catalogue_z = artist_z
            _epsilon_z = _star_epsilon_z(artist_spread, th["epsilon"])

            if catalogue_z >= th["star5_artist_z"] - _epsilon_z:
                comp_stars = 5
            elif catalogue_z >= th["star4_artist_z"] - _epsilon_z:
                comp_stars = 4
            elif catalogue_z >= 0.0 - _epsilon_z:
                comp_stars = 3
            elif catalogue_z >= th["star2_album_z"] - _epsilon_z:
                comp_stars = 2
            else:
                comp_stars = 1

            if is_verified_single and organic and not popularity_only:
                comp_stars = max(comp_stars, 3)

            track["_compilation_rating_mode"] = "track_artist_catalogue"
            track["_compilation_artist_z"] = catalogue_z
        else:
            _online_stars = 0
            _online_detail: dict[str, Any] = {"source": "none"}
            try:
                _online_stars, _online_detail = _online_catalogue_stars(
                    credited_artist=_compilation_track_artist(track),
                    track_title=str(track.get("title") or ""),
                    rules=_online_rules,
                )
            except Exception as exc:
                logger.debug(
                    "Online catalogue rating failed",
                    artist=_compilation_track_artist(track),
                    title=track.get("title"), error=str(exc),
                )

            if _online_stars > 0:
                comp_stars = _online_stars
                if is_verified_single and organic and not popularity_only:
                    comp_stars = max(comp_stars, 3)
                track["_compilation_rating_mode"] = "track_artist_online_catalogue"
                track["_compilation_artist_z"] = None
                track["_compilation_online_rank"] = int(_online_detail.get("rank") or 0)
                track["_compilation_online_total"] = int(_online_detail.get("total") or 0)
                track["_compilation_online_percentile"] = float(
                    _online_detail.get("percentile") or 0.0
                )
                track["_compilation_online_source"] = str(_online_detail.get("source") or "")
                logger.info(
                    "Compilation track rated on artist's online catalogue",
                    title=track.get("title"),
                    artist=_compilation_track_artist(track),
                    rank=int(_online_detail.get("rank") or 0),
                    total=int(_online_detail.get("total") or 0),
                    percentile=round(float(_online_detail.get("percentile") or 0.0), 4),
                    stars=comp_stars,
                )
            elif is_verified_single and not popularity_only:
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

            if _online_stars <= 0:
                track["_compilation_rating_mode"] = "absolute_thin_catalogue_fallback"
                track["_compilation_artist_z"] = None

        track["_compilation_catalogue_size"] = len(valid_catalogue_scores)

        # Two absolute guards on the compilation tier (re-using _online_rules)
        try:
            _min_score = float(_online_rules.get("top_tier_min_score") or 0.0)
            _min_listeners = float(_online_rules.get("top_tier_min_listeners") or 0.0)
        except (TypeError, ValueError):
            _min_score, _min_listeners = 0.0, 0.0

        if comp_stars >= 5 and (_min_score > 0 or _min_listeners > 0):
            _clears_absolute = (
                (_min_score > 0 and score >= _min_score)
                or (_min_listeners > 0 and raw_lf >= _min_listeners)
            )
            if not _clears_absolute:
                comp_stars = 4
                track["_compilation_top_tier_gated"] = True
                logger.info(
                    "Compilation 5★ gated — absolute popularity below the bar",
                    title=track.get("title"),
                    artist=_compilation_track_artist(track),
                    score=round(float(score or 0), 1),
                    lastfm_listeners=int(raw_lf),
                    min_score=_min_score,
                    min_listeners=int(_min_listeners),
                )

        try:
            _floor_pct = float(_online_rules.get("standout_floor_percentile") or 0.0)
            _floor_stars = int(_online_rules.get("standout_floor_stars") or 0)
        except (TypeError, ValueError):
            _floor_pct, _floor_stars = 0.0, 0

        if (
            _floor_pct > 0
            and 1 <= _floor_stars <= 5
            and comp_stars < _floor_stars
            and compilation_raw_scores
        ):
            _valid_raw = [float(v) for v in compilation_raw_scores if float(v or 0) > 0]
            if len(_valid_raw) >= 5:
                _track_raw = float(track.get("_raw_combined") or 0) or float(score or 0)
                if _track_raw > 0:
                    _above = sum(1 for v in _valid_raw if v > _track_raw)
                    _rank_pct = (_above + 1) / len(_valid_raw)
                    if _rank_pct <= _floor_pct:
                        _previous_stars = comp_stars
                        comp_stars = _floor_stars
                        track["_compilation_standout_floor"] = _floor_stars
                        logger.info(
                            "Compilation standout floored — top of the compilation, buried in the artist catalogue",
                            title=track.get("title"),
                            artist=_compilation_track_artist(track),
                            raw=round(_track_raw, 1),
                            rank_pct=round(_rank_pct, 4),
                            stars=f"{_previous_stars}★ → {_floor_stars}★",
                        )

        if is_live:
            comp_stars = max(1, comp_stars - 1)
        if comp_stars == 5:
            track["_era_5star"] = True
        return comp_stars

    base_stars = _album_z_band_star(
        score=score,
        album_scores=_album_pool,
        reference_scores=_album_pool,
        artist_scores=artist_scores,
        is_live=is_live,
        single_confidence=single_confidence,
        thresholds=th,
        precomputed_album_z=(album_z, album_spread),
        precomputed_artist_z=(artist_z, artist_spread),
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
            if _album_rank(score, _album_pool) > album_top_n:
                five_star_eligible = False

        if five_star_eligible:
            current_yr = datetime.now().year
            try:
                track_yr_raw = track.get("year") or track.get("release_year")
                track_yr = int(str(track_yr_raw)[:4]) if track_yr_raw else current_yr
            except (ValueError, TypeError):
                track_yr = current_yr

            album_age = current_yr - track_yr
            album_era = album_model.get("era", "peak") if album_model else "peak"

            if album_age > 2 and album_era == "minor":
                _ratio_ok, _ratio_reason = _album_ratio_standout_ok(
                    track,
                    album_lf_listeners=album_lf_listeners,
                    album_lb_listens=album_lb_listens,
                    live=is_live,
                )
            else:
                _ratio_ok = True
                _ratio_reason = f"bypassed_by_context(age={album_age}, era={album_era})"

            track["_ratio_5star_reason"] = _ratio_reason
            if not _ratio_ok:
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
            album_prominence_median,
            album_prominence_score,
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
                    "SELECT title, album, COALESCE(NULLIF(raw_score, 0), final_score) AS final_score, year, "
                    "COALESCE(lastfm_listeners, 0) AS lastfm_listeners, "
                    "COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens "
                    "FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND COALESCE(NULLIF(raw_score, 0), final_score) > 0"
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

            _score = float(row_get(row, "raw_score") or row_get(row, "final_score") or 0)
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
            _score = float(
                r.get("_raw_combined")
                or r.get("raw_score")
                or r.get("popularity_score")
                or r.get("final_score")
                or 0
            )
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
                medians[_album] = median(_scores)
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


def _effective_order_score(
    row: dict[str, Any],
    mode: str,
) -> float:
    """The score a PLAYLIST sorts on, in a form comparable ACROSS albums."""
    if mode == "prominence":
        prominence = playlist_popularity_score(
            int(row.get("lastfm_listeners") or 0),
            int(row.get("listenbrainz_listens") or 0),
        )
        if prominence > 0:
            return prominence
    return float(row.get("popularity_score") or row.get("score") or row.get("final_score") or 0)


def _playlist_order_key(
    row: dict[str, Any],
    mode: str = "prominence",
) -> tuple:
    """Full sort key for a generated playlist, most preferred first."""
    return (
        -int(row.get("stars") or 0),
        -float(_effective_order_score(row, mode)),
        -float(row.get("popularity_score") or row.get("score") or 0),
        str(row.get("title") or "").casefold(),
    )


def _apply_artist_cap(
    ordered: list[dict[str, Any]],
    max_per_artist: int | None,
) -> list[dict[str, Any]]:
    """Limit how many tracks ONE credited artist may contribute."""
    if not max_per_artist or max_per_artist <= 0:
        return ordered

    counts: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    for row in ordered:
        key = _normalise_artist_key(row.get("artist") or row.get("album_artist") or "")
        if counts.get(key, 0) < max_per_artist:
            counts[key] = counts.get(key, 0) + 1
            kept.append(row)
    return kept


def _interleave_artists(ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spread a ranked list so consecutive tracks rarely share an artist."""
    buckets: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    artist_order: list[str] = []
    for rank, row in enumerate(ordered):
        key = _normalise_artist_key(row.get("artist") or row.get("album_artist") or "")
        if key not in buckets:
            artist_order.append(key)
        buckets[key].append((rank, row))

    if len(artist_order) <= 1:
        return list(ordered)

    positions: dict[str, int] = {key: 0 for key in artist_order}
    out: list[dict[str, Any]] = []
    last_key: str | None = None

    for _ in range(len(ordered)):
        pick_key: str | None = None
        pick_rank: int | None = None

        for key in artist_order:
            pos = positions[key]
            bucket = buckets[key]
            if pos >= len(bucket) or key == last_key:
                continue
            rank = bucket[pos][0]
            if pick_rank is None or rank < pick_rank:
                pick_rank, pick_key = rank, key

        if pick_key is None:
            for key in artist_order:
                pos = positions[key]
                bucket = buckets[key]
                if pos >= len(bucket):
                    continue
                rank = bucket[pos][0]
                if pick_rank is None or rank < pick_rank:
                    pick_rank, pick_key = rank, key

        if pick_key is None:
            break

        pos = positions[pick_key]
        out.append(buckets[pick_key][pos][1])
        positions[pick_key] = pos + 1
        last_key = pick_key

    return out


def _ordered_playlist_rows(
    winners: list[dict[str, Any]],
    *,
    order_mode: str,
    max_per_artist: int | None = None,
    interleave: bool = False,
) -> list[dict[str, Any]]:
    """Apply the full ordering pipeline to a playlist's winning tracks."""
    ordered = sorted(winners, key=lambda r: _playlist_order_key(r, order_mode))
    ordered = _apply_artist_cap(ordered, max_per_artist)
    if interleave:
        ordered = _interleave_artists(ordered)
    return ordered


def _rotation_week(when: datetime | None = None) -> str:
    """The ISO year-week that seeds a week's rotation, e.g. ``2026-W44``."""
    moment = when or datetime.utcnow()
    iso = moment.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _weekly_shuffle(
    rows: list[dict[str, Any]],
    *,
    playlist_key: str,
    enabled: bool | None = None,
    when: datetime | None = None,
) -> list[dict[str, Any]]:
    """Reorder a finished playlist ONCE PER WEEK, deterministically.

    ⭐⭐ THE SAME TRACKS, A DIFFERENT SEQUENCE — never a different selection.

    This runs AFTER ranking, the per-artist quota, interleaving and the
    ``max_tracks`` slice, so membership is already settled. That is what makes
    the rotation safe:

    * the playlist's CONTENT only changes when the library does, so the
      zero-request sync still skips an unchanged week;
    * the rotation changes exactly once per ISO week, so it costs one rewrite a
      week rather than one per scan.

    Shuffling BEFORE the cap would be a bug: the shuffled tail is what gets
    sliced away, so genuinely top tracks would be dropped at random.

    The seed is ``<iso-week>::<playlist name>``. It is passed as a STRING to
    ``random.Random``, which derives the state from a SHA-512 of the text — so
    the order is identical in every process and after every restart, and two
    playlists never share an order by accident.
    """
    if not rows or len(rows) < 2:
        return rows

    if enabled is None:
        try:
            from helpers.config_helpers import get_playlist_shuffle_config

            enabled = bool(get_playlist_shuffle_config().get("enabled", True))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Playlist shuffle config unreadable — rotation off", error=str(exc))
            enabled = False

    if not enabled:
        return rows

    seed = f"{_rotation_week(when)}::{str(playlist_key or '').casefold()}"
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def _ordered_by_percentile_then_prominence(
    winners: list[dict[str, Any]],
    mode: str = "prominence",
) -> list[dict[str, Any]]:
    """Essential-Collection ordering: star tier, then prominence (or percentile in stored mode)."""
    if str(mode or "prominence").strip().lower() == "stored":
        return sorted(
            winners,
            key=lambda r: (
                -int(r.get("stars") or 0),
                -float(r.get("percentile") or 0.0),
                -float(_effective_order_score(r, mode)),
                str(r.get("title") or "").casefold(),
            ),
        )
    return sorted(
        winners,
        key=lambda r: (
            -int(r.get("stars") or 0),
            -float(_effective_order_score(r, mode)),
            -float(r.get("percentile") or 0.0),
            str(r.get("title") or "").casefold(),
        ),
    )


def _resolve_max_per_artist(cfg: dict[str, Any]) -> int | None:
    """Per-artist track cap for a generated playlist."""
    raw = cfg.get("genre_playlists_max_per_artist", 25)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 25
    return None if value <= 0 else value


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

    try:
        from helpers.normalization_service import normalize_the_prefix
        artist_display = normalize_the_prefix(artist)
    except Exception:
        artist_display = artist

    name = str(template).replace("{artist}", artist_display).strip()
    return name or f"{artist_display} - Essential Collection"


def _normalise_essential_title(title: str) -> str:
    """Key used to decide whether two rows are the SAME track."""
    t = normalize_unicode_punctuation(str(title or "")).strip().lower()
    t = _ESSENTIAL_TITLE_NOISE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _normalise_artist_key(artist: Any) -> str:
    """Key used to decide whether two rows share a credited ARTIST."""
    return normalize_unicode_punctuation(str(artist or "")).strip().casefold()


def _is_excluded_essential_artist(artist: str) -> bool:
    """True when an artist must not get an Essential Collection."""
    if str(artist or "").strip().casefold() in _ESSENTIAL_EXCLUDED_ARTISTS:
        return True
    try:
        from helpers.config_helpers import is_artist_excluded_from_playlists

        return is_artist_excluded_from_playlists(artist)
    except Exception as exc:
        logger.debug("Excluded-artist config read failed", error=str(exc))
        return False


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


_NEW_MUSIC_MIN_TRACKS = 25
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


# ---------------------------------------------------------------------------
# Seasonal (holiday) exclusion policy
# ---------------------------------------------------------------------------

_HOLIDAY_POOL_KEY = "christmas holiday"
_HOLIDAY_POOL_DISPLAY = "Holiday"


def _christmas_exclusion_enabled() -> bool:
    """True when Christmas music must be kept out of ordinary playlists."""
    try:
        from helpers.config_helpers import get_playlists_config
        return bool(get_playlists_config().get("exclude_christmas_from_playlists", True))
    except Exception:
        return True


def _christmas_playlist_marker() -> str:
    """The name fragment that marks a playlist as Christmas-eligible."""
    try:
        from helpers.config_helpers import get_playlists_config
        return str(
            get_playlists_config().get("christmas_playlist_marker") or "christmas"
        ).strip().lower() or "christmas"
    except Exception:
        return "christmas"


def _playlist_allows_christmas(playlist_name: str) -> bool:
    """True when ``playlist_name`` is allowed to contain Christmas tracks."""
    marker = _christmas_playlist_marker()
    return marker in str(playlist_name or "").casefold()


def _filter_christmas_rows(
    rows: list[dict[str, Any]],
    playlist_name: str,
) -> list[dict[str, Any]]:
    """Drop Christmas tracks unless ``playlist_name`` is a Christmas playlist."""
    if not _christmas_exclusion_enabled():
        return rows
    if _playlist_allows_christmas(playlist_name):
        return rows

    kept: list[dict[str, Any]] = []
    removed = 0
    for row in rows:
        if is_christmas_track(
            title=str(row.get("title") or ""),
            album=str(row.get("album") or ""),
            genres=row.get("genres"),
        ):
            removed += 1
            continue
        kept.append(row)

    if removed:
        logger.info(
            "Excluded Christmas tracks from playlist",
            playlist=playlist_name,
            excluded=removed,
            kept=len(kept),
        )
    return kept


def _create_new_music_playlist() -> int:
    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, album, file_path, duration,
                           COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           COALESCE(lastfm_listeners, 0) AS lastfm_listeners,
                           COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens,
                           genres,
                           COALESCE(navidrome_added_at, updated_at) AS added_at,
                           updated_at
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND file_path IS NOT NULL AND TRIM(file_path) <> ''
                    ORDER BY COALESCE(navidrome_added_at, updated_at) DESC NULLS LAST
                    LIMIT :max_rows
                """),
                {"max_rows": _NEW_MUSIC_MAX_TRACKS * 5},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("New Music fetch failed", error=str(exc))
        return 0

    try:
        from helpers.config_helpers import get_excluded_playlist_artists, get_playlists_config
        excluded_artists = get_excluded_playlist_artists()
        pl_cfg = get_playlists_config()
    except Exception:
        excluded_artists = set()
        pl_cfg = {}

    if excluded_artists:
        rows = [
            r for r in rows
            if str(r.get("artist") or "").strip().casefold() not in excluded_artists
        ]

    rows = _filter_christmas_rows(rows, "New Music")

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            _normalise_artist_key(row.get("artist")),
            _normalise_essential_title(str(row.get("title") or "")),
        )
        if not key[1]:
            continue
        grouped.setdefault(key, []).append(row)

    winners: list[dict[str, Any]] = []
    for group in grouped.values():
        group.sort(
            key=lambda r: (
                int(r.get("is_live") or 0),
                int(r.get("is_compilation") or 0),
                -int(r.get("stars") or 0),
                -float(r.get("popularity_score") or 0.0),
            )
        )
        winners.append(group[0])

    max_per_artist = _resolve_max_per_artist(pl_cfg)
    winners = _apply_artist_cap(winners, max_per_artist)
    if pl_cfg.get("playlist_interleave_artists", True):
        winners = _interleave_artists(winners)

    if len(winners) < _NEW_MUSIC_MIN_TRACKS:
        _delete_playlist_from_navidrome("New Music")
        return 0

    winners = winners[:_NEW_MUSIC_MAX_TRACKS]

    # Rotation after the slice: the SAME tracks in a different sequence, once
    # per ISO week. See ``_weekly_shuffle`` for why the order matters.
    winners = _weekly_shuffle(winners, playlist_key=playlist_name)

    _song_ids = [str(r.get("id") or "").strip() for r in winners if str(r.get("id") or "").strip()]
    if not _song_ids:
        return 0

    try:
        sync_res = _sync_playlist_to_navidrome("New Music", _song_ids, rows=winners)
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
                    SELECT id, title, album, file_path, duration,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           COALESCE(lastfm_listeners, 0) AS lastfm_listeners,
                           COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens,
                           year, release_year, artist, genres
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
        order_mode = str(cfg.get("playlist_order_mode", "prominence") or "prominence").strip().lower()
        if order_mode not in ("prominence", "stored"):
            order_mode = "prominence"
    except Exception:
        max_tracks = 50
        order_mode = "prominence"

    if artist_scores is None:
        try:
            with db_session() as session:
                res = session.execute(
                    text(
                        "SELECT COALESCE(NULLIF(raw_score, 0), final_score) FROM tracks "
                        "WHERE LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) "
                        "      = LOWER(TRIM(:artist)) "
                        "  AND COALESCE(NULLIF(raw_score, 0), final_score, 0) > 0"
                    ),
                    {"artist": artist},
                )
                artist_scores = [float(r[0]) for r in res.fetchall() or [] if r and r[0]]
        except Exception as exc:
            logger.debug("Essential artist score fetch failed", artist=artist, error=str(exc))
            artist_scores = []

    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, album, file_path, duration,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           COALESCE(lastfm_listeners, 0) AS lastfm_listeners,
                           COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens,
                           year, release_year, artist, album_artist, genres
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

    rows = _filter_christmas_rows(rows, playlist_name)

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
                int(r.get("is_live") or 0),
                -int(r.get("stars") or 0),
                -float(r.get("popularity_score") or 0),
                int(r.get("is_compilation") or 0),
                _track_year(r),
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

    winners = _ordered_by_percentile_then_prominence(winners, order_mode)
    winners = winners[:max_tracks]

    # Rotation after the slice: the SAME tracks in a different sequence, once
    # per ISO week. See ``_weekly_shuffle`` for why the order matters.
    winners = _weekly_shuffle(winners, playlist_key=playlist_name)

    _song_ids = [str(r.get("id") or "").strip() for r in winners if str(r.get("id") or "").strip()]
    if not _song_ids:
        return False

    try:
        sync_res = _sync_playlist_to_navidrome(playlist_name, _song_ids, rows=winners)
        if not _playlist_sync_succeeded(sync_res):
            return False
        if sync_res.get("skipped"):
            return True
        log_unified(f"📄 Playlist: Synced '{playlist_name}' to Navidrome ({len(_song_ids)} tracks)")
        try:
            from services.playlists.playlist_service import attach_playlist_cover
            attach_playlist_cover(
                playlist_name,
                artist,
                playlist_id=str(sync_res.get("playlist_id") or "") or None,
            )
        except Exception as cover_exc:
            logger.debug("Playlist cover attach failed", playlist=playlist_name, error=str(cover_exc))
        return True
    except Exception as exc:
        logger.warning("Essential collection API sync failed", artist=artist, error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Genre top-tracks playlists
# ---------------------------------------------------------------------------

_GENRE_PLAYLIST_DEFAULT_MAX_TRACKS = 300


def _resolve_genre_max_tracks(cfg: dict[str, Any]) -> int | None:
    """Max tracks per genre playlist; ``None`` means UNLIMITED."""
    raw = cfg.get("genre_playlists_max_tracks", _GENRE_PLAYLIST_DEFAULT_MAX_TRACKS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _GENRE_PLAYLIST_DEFAULT_MAX_TRACKS
    if value <= 0:
        return None
    return value


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
_PLAYLIST_SIGNATURES: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_PLAYLIST_SIG_MAX = 2000
#: A remembered push expires after this long, so a playlist deleted or edited
#: in Navidrome's own UI is re-pushed without needing an app restart.
_PLAYLIST_SIG_TTL_SECONDS = 3600.0


def _navidrome_server_key(base_url: str) -> str:
    """A comparison key that identifies the SERVER behind a base URL."""
    text_val = str(base_url or "").strip().rstrip("/")
    if not text_val:
        return ""
    try:
        parts = urlsplit(text_val)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    except Exception:
        pass
    return text_val.casefold()


def _navidrome_clients() -> list[Any]:
    """One client per SERVER — not one per configured user."""
    global _CLIENTS_CACHE, _CLIENTS_CACHED_AT
    now = time.monotonic()
    with _CLIENTS_LOCK:
        if _CLIENTS_CACHE is not None and (now - _CLIENTS_CACHED_AT) < _CLIENTS_TTL_SECONDS:
            return list(_CLIENTS_CACHE)

    clients: list[Any] = []
    try:
        from api_clients.navidrome import NavidromeClient
        from helpers.config_helpers import get_navidrome_users_normalized

        seen_servers: dict[str, str] = {}
        for user in get_navidrome_users_normalized():
            base_url = (user.get("base_url") or "").strip()
            username = (user.get("user") or "").strip()
            password = (user.get("pass") or "").strip()
            if not (base_url and username and password):
                continue

            key = _navidrome_server_key(base_url)
            if key and key in seen_servers:
                logger.info(
                    "Navidrome user skipped — same server as an earlier entry",
                    user=username,
                    base_url=base_url,
                    using_user=seen_servers[key],
                    reason="playlists are server-scoped and public",
                )
                continue
            if key:
                seen_servers[key] = username

            clients.append(NavidromeClient(base_url, username, password))
    except Exception as exc:
        logger.warning("Navidrome client resolution failed", error=str(exc))

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
        if not existing:
            return False
        sig, pushed_at = existing
        if (time.monotonic() - pushed_at) >= _PLAYLIST_SIG_TTL_SECONDS:
            _PLAYLIST_SIGNATURES.pop(key, None)
            return False
        if sig == signature:
            _PLAYLIST_SIGNATURES.move_to_end(key)
            return True
    return False


def _record_playlist_push(playlist_name: str, signature: str) -> None:
    key = str(playlist_name or "").strip().casefold()
    with _PLAYLIST_SIG_LOCK:
        _PLAYLIST_SIGNATURES[key] = (signature, time.monotonic())
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
                logger.info("Deleted Navidrome playlist", name=playlist_name)
    except Exception as exc:
        logger.warning("Navidrome playlist delete failed", name=playlist_name, error=str(exc))

    if deleted:
        _forget_playlist_push(playlist_name)
    return deleted


def _sweep_orphaned_genre_playlists_from_navidrome(keep_playlist_names: set[str]) -> None:
    try:
        if not _genre_playlists_delete_enabled():
            return
        sentinel = "__GENRE_SENTINEL__"
        rendered = _genre_playlist_name(sentinel)
        if sentinel not in rendered:
            return
        prefix, suffix = rendered.split(sentinel, 1)
        prefix_cf = prefix.casefold()
        suffix_cf = suffix.casefold()
        if not prefix_cf and not suffix_cf:
            return

        keep_keys = {str(name).strip().casefold() for name in keep_playlist_names if str(name).strip()}
        swept = 0
        for client in _navidrome_clients():
            for playlist in client.fetch_all_playlists() or []:
                name = str(playlist.get("name") or "").strip()
                name_cf = name.casefold()
                if prefix_cf and not name_cf.startswith(prefix_cf):
                    continue
                if suffix_cf and not name_cf.endswith(suffix_cf):
                    continue
                if len(name_cf) <= len(prefix_cf) + len(suffix_cf):
                    continue
                if name_cf in keep_keys:
                    continue
                playlist_id = str(playlist.get("id") or "").strip()
                if playlist_id and client.delete_playlist(playlist_id):
                    _forget_playlist_push(name)
                    swept += 1
                    logger.info("Swept orphaned Navidrome genre playlist", name=name)
        if swept:
            log_unified(f"🧹 Playlist: swept {swept} orphaned genre playlist(s) from Navidrome")
    except Exception as exc:
        logger.warning("Genre playlist Navidrome sweep failed", error=str(exc))


def _log_dropped_playlist_tracks(
    playlist_name: str,
    dropped_ids: list[str],
    rows: list[dict[str, Any]] | None,
) -> None:
    """Name the tracks Navidrome refused to store."""
    by_id = {str(r.get("id") or ""): r for r in (rows or [])}
    named: list[str] = []
    for track_id in dropped_ids[:8]:
        row = by_id.get(track_id)
        if row:
            who = str(row.get("artist") or row.get("album_artist") or "").strip()
            title = str(row.get("title") or "").strip()
            named.append(f"{who} - {title}" if who else title)
        else:
            named.append(f"<unknown id {track_id}>")
    logger.warning(
        "[PLAYLISTS] Navidrome did not store these track(s) in the playlist",
        playlist=playlist_name,
        dropped=len(dropped_ids),
        tracks=named,
        hint="stale ids are refreshed by a Navidrome import; ids for files "
             "Navidrome has not indexed yet need a Navidrome scan first "
             "(run a scan from Navidrome's admin UI, or shorten its "
             "ScanSchedule setting — e.g. @every 1m — so newly downloaded "
             "files are indexed before the next playlist sync)",
    )


def _sync_playlist_to_navidrome(
    playlist_name: str,
    song_ids: list[str],
    rows: list[dict[str, Any]] | None = None,
    existing_playlist_id: str | None = None,
) -> dict[str, Any]:
    synced: dict[str, Any] = {
        "updated": 0,
        "created": 0,
        "unchanged": 0,
        "deduped": 0,
        "failed": 0,
        "clients": 0,
        "skipped": 0,
        "playlist_id": str(existing_playlist_id or ""),
    }

    song_ids = list(song_ids or [])
    signature = _playlist_signature(song_ids)
    if _playlist_already_pushed(playlist_name, signature):
        synced["skipped"] = 1
        return synced

    try:
        from services.playlists.playlist_navidrome_service import sync_playlist_by_name
    except Exception as exc:
        logger.warning("Playlist sync service import failed", name=playlist_name, error=str(exc))
        synced["failed"] = 1
        return synced

    clients = _navidrome_clients()
    if not clients:
        logger.debug("No Navidrome clients configured; playlist sync skipped", name=playlist_name)
        return synced

    for client in clients:
        synced["clients"] += 1
        try:
            result = (
                sync_playlist_by_name(
                    client,
                    playlist_name,
                    list(song_ids),
                    existing_playlist_id=existing_playlist_id,
                )
                or {}
            )
            if result.get("playlist_id") and not synced.get("playlist_id"):
                synced["playlist_id"] = str(result.get("playlist_id"))
            if result.get("updated"):
                synced["updated"] += 1
            if result.get("created"):
                synced["created"] += 1
            if result.get("unchanged"):
                synced["unchanged"] += 1
            synced["deduped"] += int(result.get("deduped") or 0)

            _dropped = [str(i) for i in (result.get("dropped_ids") or [])]
            if _dropped:
                _log_dropped_playlist_tracks(playlist_name, _dropped, rows)

            if (
                result.get("success")
                and not result.get("created")
                and not result.get("updated")
                and not result.get("unchanged")
            ):
                synced["unchanged"] += 1
            elif not result.get("success"):
                synced["failed"] += 1
        except Exception as exc:
            logger.warning("Navidrome playlist push failed", name=playlist_name, error=str(exc))
            synced["failed"] += 1

    if _playlist_sync_succeeded(synced):
        if not synced["failed"]:
            _record_playlist_push(playlist_name, signature)

    return synced


#: The per-source genre columns a track OWNS (written for that track by the
#: track stage), as opposed to ``tracks.genres``, which on an ordinary album is
#: a single blended list fanned out to every row.
_TRACK_OWN_GENRE_COLUMNS: tuple[str, ...] = (
    "lastfm_tags",
    "musicbrainz_genres",
    "discogs_genres",
    "listenbrainz_genres",
    "spotify_genres",
    "essentia_genres",
    "manual_genres",
    "navidrome_genres",
    "audiodb_genres",
    "wikidata_genres",
)


def _track_own_genre_tokens(row: dict[str, Any]) -> list[str]:
    """Every genre token the TRACK itself carries, from its per-source columns."""
    tokens: list[str] = []
    for column in _TRACK_OWN_GENRE_COLUMNS:
        raw = row.get(column)
        if not raw:
            continue
        tokens.extend(iter_genre_tokens(raw))
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _resolve_playlist_genres(
    row: dict[str, Any],
    *,
    max_genres: int,
) -> tuple[list[str], bool]:
    """The genres a track is POOLED BY, and whether they are the track's own.

    Returns ``(genres, owned)``.

    ⭐⭐ THE POOL COMES FROM THE TRACK'S FINAL ``genres`` VALUE — the three
    applied musical genres — and only falls back to the provider columns when
    that value is empty.

    This is the fix for the reported *"it was adjusted to remove Alternative
    Metal, Navidrome doesn't show Alternative Metal, but it's still on the
    Alternative Metal Playlist"*. The pool used to be built from the per-source
    columns (``lastfm_tags``, ``musicbrainz_genres``, ``discogs_genres``…),
    which keep whatever the provider originally returned. A genre removed from
    the track — by the selection engine, a manual correction or a re-scan —
    therefore survived in those columns and kept the track in a playlist the
    metadata no longer claimed it belonged to. The displayed ``genres`` value is
    what Navidrome shows; the playlist must agree with it.

    Performance markers (Cover/Live/Acoustic/Remix/Remaster/Instrumental) and
    the holiday family are dropped by ``playlist_pool_genres``, so they never
    consume one of the ``max_genres`` slots — they are EXTRAS on top of the
    three, exactly as written to the metadata.

    ``owned=True`` means the track supplied its own genre evidence;
    ``owned=False`` means the album-level blend had to stand in.
    """
    final = playlist_pool_genres(row.get("genres"), limit=max_genres)
    if final:
        return final, True

    # No final value yet (never aggregated, or genuinely empty). Fall back to
    # the track's own source tags so a track is not silently dropped from every
    # playlist, then to the album blend.
    own = [
        g
        for g in _track_own_genre_tokens(row)
        if is_playlist_pool_genre(g)
    ]
    if own:
        return own[:max_genres], True
    return [], False


def _full_genre_tokens(row: dict[str, Any]) -> list[str]:
    """Every genre token available for this track: its own plus the blend."""
    tokens = _track_own_genre_tokens(row)
    tokens.extend(iter_genre_tokens(row.get("genres")))
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _track_is_metal_by_identity(
    tokens: list[str],
    *,
    use_family_tiebreak: bool,
) -> bool:
    """Is this track's genre identity METAL?"""
    if not tokens:
        return False

    if use_family_tiebreak:
        try:
            from services.enrichment.genre_vocabulary import get_vocabulary

            family, unknown = get_vocabulary().deciding_family(tokens)
            if family == METAL_GENRE_FAMILY:
                return True
            if family:
                return bool(unknown) and is_metal_genre(unknown)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Genre family tie-break unavailable", error=str(exc))

    return is_metal_genre(tokens)


def _playlist_identity_tokens(
    row: dict[str, Any],
    *,
    own_tags_win: bool,
) -> list[str]:
    """The genre tokens that decide which playlists a track may enter."""
    if own_tags_win:
        own = _track_own_genre_tokens(row)
        if own:
            return own
    return _full_genre_tokens(row)


def _genre_playlist_track_genres(
    row: dict[str, Any],
    *,
    max_genres: int = 3,
) -> list[str]:
    raw = row.get("genres")
    if not raw:
        return []

    if isinstance(raw, list):
        return [str(g).strip() for g in raw if str(g).strip()][:max_genres]

    raw_str = str(raw).strip()
    if not raw_str or raw_str.lower() in ("null", "none", "[]", "{}"):
        return []

    if raw_str.startswith("["):
        try:
            parsed = json.loads(raw_str)
            if isinstance(parsed, list):
                return [str(g).strip() for g in parsed if str(g).strip()][:max_genres]
        except Exception:
            pass

    genres = [g.strip() for g in re.split(r"[,;/\\]+", raw_str) if g.strip()]
    return genres[:max_genres]


# ---------------------------------------------------------------------------
# Genre-playlist source rows (full-library query, cached briefly)
# ---------------------------------------------------------------------------

_GENRE_ROWS_LOCK = threading.Lock()
_GENRE_ROWS_CACHE: list[dict[str, Any]] | None = None
_GENRE_ROWS_MIN_STARS: int | None = None
_GENRE_ROWS_AT = 0.0
_GENRE_ROWS_TTL_SECONDS = 120.0

_GENRE_ROWS_SQL = """
    SELECT id, title, album, file_path, duration, artist, album_artist,
           COALESCE(stars, star_rating) AS stars,
           COALESCE(popularity, final_score, 0) AS popularity_score,
           COALESCE(is_live, 0) AS is_live,
           COALESCE(is_compilation, 0) AS is_compilation,
           COALESCE(lastfm_listeners, 0) AS lastfm_listeners,
           COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens,
           genres,
           lastfm_tags, musicbrainz_genres, discogs_genres,
           listenbrainz_genres, spotify_genres, essentia_genres,
           manual_genres, navidrome_genres, audiodb_genres, wikidata_genres
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
            return list(_GENRE_ROWS_CACHE)

    rows: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(text(_GENRE_ROWS_SQL), {"min_stars": min_stars})
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.warning("Genre playlist row fetch failed", min_stars=min_stars, error=str(exc))
        return []

    with _GENRE_ROWS_LOCK:
        _GENRE_ROWS_CACHE = rows
        _GENRE_ROWS_MIN_STARS = min_stars
        _GENRE_ROWS_AT = now
    return list(rows)


def invalidate_genre_playlist_rows() -> None:
    global _GENRE_ROWS_CACHE, _GENRE_ROWS_MIN_STARS
    with _GENRE_ROWS_LOCK:
        _GENRE_ROWS_CACHE = None
        _GENRE_ROWS_MIN_STARS = None


_GENRE_ALBUM_ROWS_SQL = """
    SELECT id, title, file_path, duration, artist, album_artist,
           COALESCE(stars, star_rating) AS stars,
           COALESCE(popularity, final_score, 0) AS popularity_score,
           COALESCE(is_live, 0) AS is_live,
           COALESCE(is_compilation, 0) AS is_compilation,
           genres,
           lastfm_tags, musicbrainz_genres, discogs_genres,
           listenbrainz_genres, spotify_genres, essentia_genres,
           manual_genres, navidrome_genres, audiodb_genres,
           wikidata_genres
    FROM tracks
    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
      AND album = :album
      AND COALESCE(stars, star_rating) >= :min_stars
"""


def _genre_playlist_rows_for_album(
    artist: str, album: str, min_stars: int,
) -> list[dict[str, Any]]:
    """The tracks of ONE album that can pool into a genre playlist."""
    with db_session() as session:
        result = session.execute(
            text(_GENRE_ALBUM_ROWS_SQL),
            {"artist": artist, "album": album, "min_stars": min_stars},
        )
        return [dict(r._mapping) for r in result.fetchall() or []]


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
        rows = _genre_playlist_rows_for_album(artist, album, min_stars)

        if not rows:
            return 0

        max_genres = max(1, int(_cfg.get("genre_playlists_max_genres", 3) or 3))
        affected: set[str] = set()
        for row in rows:
            genres, _owned = _resolve_playlist_genres(row, max_genres=max_genres)
            for genre in genres:
                affected.add(genre)

        if not affected:
            return 0
        return _create_genre_top_track_playlists(only_genres=affected)
    except Exception as exc:
        logger.debug("Per-album genre playlist refresh failed", artist=artist, album=album, error=str(exc))
        return 0


def _genre_affected_genres_for_albums(artist: str, albums: list[str]) -> set[str]:
    """The genre playlists the tracks of *albums* can pool into."""
    try:
        from helpers.config_helpers import get_config
        _cfg = (get_config() or {}).get("playlists") or {}
    except Exception:
        _cfg = {}

    min_stars = max(1, int(_cfg.get("genre_playlists_min_stars", 4) or 4))
    max_genres = max(1, int(_cfg.get("genre_playlists_max_genres", 3) or 3))

    affected: set[str] = set()
    for album in albums:
        for row in _genre_playlist_rows_for_album(artist, album, min_stars):
            genres, _owned = _resolve_playlist_genres(row, max_genres=max_genres)
            for genre in genres:
                affected.add(genre)
    return affected


# ---------------------------------------------------------------------------
# Per-artist AI-cover pass (the full-library-scan gap)
# ---------------------------------------------------------------------------

_GENRE_COVER_ID_CACHE: dict[str, str] = {}
_GENRE_COVER_ID_CACHE_AT = 0.0
_GENRE_COVER_ID_CACHE_TTL = 300.0
_GENRE_COVER_ID_LOCK = threading.Lock()


def _genre_playlist_ids_by_name() -> dict[str, str]:
    """``playlist name (casefolded) -> Navidrome playlist id``."""
    global _GENRE_COVER_ID_CACHE, _GENRE_COVER_ID_CACHE_AT

    now = time.monotonic()
    with _GENRE_COVER_ID_LOCK:
        if (
            _GENRE_COVER_ID_CACHE
            and (now - _GENRE_COVER_ID_CACHE_AT) < _GENRE_COVER_ID_CACHE_TTL
        ):
            return dict(_GENRE_COVER_ID_CACHE)

        ids: dict[str, str] = {}
        for client in _navidrome_clients():
            try:
                for pl in client.fetch_all_playlists() or []:
                    name = str(pl.get("name") or "").strip()
                    pid = str(pl.get("id") or "").strip()
                    if name and pid:
                        ids.setdefault(name.casefold(), pid)
            except Exception as exc:
                logger.debug("Genre cover playlist id fetch failed", error=str(exc))

        _GENRE_COVER_ID_CACHE = ids
        _GENRE_COVER_ID_CACHE_AT = now
        return dict(ids)


def refresh_genre_playlist_covers_for_artist(artist: str, albums: list[str]) -> list[str]:
    """Ensure the AI cover art for the genre playlists *artist*'s albums feed."""
    album_names = [str(a).strip() for a in (albums or []) if str(a or "").strip()]
    if not album_names:
        return []

    try:
        from helpers.config_helpers import get_gemini_config, get_genre_cover_config

        cover_cfg = get_genre_cover_config()
        gem = get_gemini_config()
        if not cover_cfg["enabled"]:
            if gem["enabled"] and gem["api_key"]:
                log_unified(
                    "🎨 Genre playlist covers: OFF — tick 'AI Genre Covers' in "
                    "Config ▸ Playlists (the Gemini key alone does not enable them)"
                )
            return []
        if not gem["enabled"] or not gem["api_key"]:
            log_unified(
                "🎨 Genre playlist covers: Gemini is not configured "
                "(Config ▸ API Integrations ▸ Google Gemini)"
            )
            return []

        if not _genre_playlists_active():
            return []

        affected = _genre_affected_genres_for_albums(artist, album_names)
        if not affected:
            return []

        from services.playlists.genre_cover_service import generate_genre_cover

        playlist_ids = _genre_playlist_ids_by_name()
        clients = _navidrome_clients()
        if not clients:
            return []

        touched: list[str] = []
        applied = 0
        pending: list[tuple[str, str, str]] = []
        for genre in sorted(affected):
            display = _display_genre(genre)
            playlist_name = _genre_playlist_name(display)
            touched.append(playlist_name)
            playlist_id = playlist_ids.get(playlist_name.casefold())
            if not playlist_id:
                logger.debug(
                    "Genre playlist not on the server yet — cover deferred",
                    playlist=playlist_name,
                )
                continue
            pending.append((display, playlist_name, playlist_id))

        for display, playlist_name, playlist_id in pending:
            for client in clients:
                cover = generate_genre_cover(
                    display,
                    playlist_name,
                    client,
                    regenerate=cover_cfg["regenerate"],
                    playlist_id=playlist_id,
                )
                if cover.get("ok"):
                    if cover.get("status") == "generated":
                        applied += 1
                        log_unified(
                            f"🎨 Playlist cover: AI artwork set for '{playlist_name}'"
                        )
                    break
                logger.warning(
                    "Genre playlist cover not applied",
                    playlist=playlist_name,
                    reason=cover.get("reason"),
                )

        if touched:
            log_unified(
                f"🎨 Genre playlists for '{artist}': {', '.join(touched)} "
                f"— {applied} cover(s) generated"
            )
        return touched
    except Exception as exc:
        logger.warning(
            "Per-artist genre playlist cover pass failed", artist=artist, error=str(exc),
        )
        return []


def _create_genre_top_track_playlists(
    prune_only: bool = False,
    only_genres: set[str] | None = None,
) -> int:
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
    except Exception:
        cfg = {}

    min_stars = max(1, int(cfg.get("genre_playlists_min_stars", 4) or 4))
    max_genres = max(1, int(cfg.get("genre_playlists_max_genres", 3) or 3))
    create_threshold = max(1, int(cfg.get("genre_playlists_create_threshold", 100) or 100))
    delete_threshold = max(1, int(cfg.get("genre_playlists_delete_threshold", 80) or 80))
    max_tracks = _resolve_genre_max_tracks(cfg)
    order_mode = str(cfg.get("playlist_order_mode", "prominence") or "prominence").strip().lower()
    if order_mode not in ("prominence", "stored"):
        order_mode = "prominence"
    max_per_artist = _resolve_max_per_artist(cfg)
    interleave_artists = bool(cfg.get("playlist_interleave_artists", True))

    create_enabled = _genre_playlists_enabled()
    delete_enabled = _genre_playlists_delete_enabled()

    rows = _fetch_genre_playlist_rows(min_stars)
    if not rows:
        return 0

    try:
        from helpers.config_helpers import get_excluded_playlist_artists

        _excluded_artists = get_excluded_playlist_artists()
    except Exception as exc:
        logger.debug("Excluded-artist config read failed", error=str(exc))
        _excluded_artists = set()

    if _excluded_artists:
        _before = len(rows)
        rows = [
            r
            for r in rows
            if str(r.get("album_artist") or r.get("artist") or "")
            .strip()
            .casefold()
            not in _excluded_artists
        ]
        if len(rows) != _before:
            logger.info(
                "Genre playlists: skipped excluded artist(s)",
                tracks_skipped=_before - len(rows),
                artists=sorted(_excluded_artists),
            )
        if not rows:
            return 0

    existing_playlists = set()
    existing_playlist_ids: dict[str, str] = {}
    if create_enabled or delete_enabled:
        for client in _navidrome_clients():
            for pl in client.fetch_all_playlists() or []:
                _pl_name = str(pl.get("name") or "").strip()
                if not _pl_name:
                    continue
                existing_playlists.add(_pl_name.casefold())
                _pl_id = str(pl.get("id") or "").strip()
                if _pl_id:
                    existing_playlist_ids.setdefault(_pl_name.casefold(), _pl_id)

    def _track_genres(row: dict[str, Any]) -> list[str]:
        genres, _owned = _resolve_playlist_genres(
            row,
            max_genres=max_genres,
        )
        return genres

    try:
        from helpers.config_helpers import get_genre_exclusion_config
        _excl = get_genre_exclusion_config()
    except Exception as exc:
        logger.debug("Genre exclusion config read failed", error=str(exc))
        _excl = {
            "enabled": True,
            "own_tags_win": True,
            "metal_excluded_from": ["alternative rock"],
        }

    _excl_enabled = bool(_excl.get("enabled", True))
    _own_tags_win = bool(_excl.get("own_tags_win", True))
    _use_family_tiebreak = bool(_excl.get("use_family_tiebreak", True))
    _metal_excluded_from = {
        re.sub(r"[^\w\s-]", "", str(g).strip().lower()).strip()
        for g in (_excl.get("metal_excluded_from") or [])
    }
    _metal_excluded_from.discard("")
    _excluded_counts: dict[str, int] = defaultdict(int)

    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    genre_freq: dict[str, int] = defaultdict(int)
    genre_display: dict[str, str] = {}

    for row in rows:
        track_genres = _track_genres(row)

        _identity = _playlist_identity_tokens(row, own_tags_win=_own_tags_win)
        _track_is_metal = _excl_enabled and _track_is_metal_by_identity(
            _identity, use_family_tiebreak=_use_family_tiebreak
        )

        is_christmas = is_christmas_track(
            title=str(row.get("title") or ""),
            album=str(row.get("album") or ""),
            genres=row.get("genres"),
        )

        # ⭐ A seasonal track with no musical genre still belongs in the holiday
        # pool. ``playlist_pool_genres`` drops the Christmas/Holiday tokens
        # (they are extras, not pool keys), so a track whose ONLY genre was
        # "Christmas" would otherwise vanish from every playlist. The pool key
        # below is the holiday key, which is where the seasonal branch would
        # have put it anyway.
        if not track_genres and is_christmas:
            track_genres = [_HOLIDAY_POOL_DISPLAY]

        for genre in track_genres:
            norm_key = re.sub(r"[^\w\s-]", "", genre.lower()).strip()
            if not norm_key:
                continue

            if is_christmas and not is_christmas_genre_token(norm_key):
                target_norm_key = f"christmas {norm_key}"
                target_display = f"Christmas {genre.title()}"
            elif is_christmas:
                target_norm_key = _HOLIDAY_POOL_KEY
                target_display = _HOLIDAY_POOL_DISPLAY
            else:
                target_norm_key = norm_key
                target_display = genre

            if _track_is_metal and norm_key in _metal_excluded_from:
                _excluded_counts[norm_key] += 1
                continue

            pools[target_norm_key].append({
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Unknown"),
                "file_path": str(row.get("file_path") or ""),
                "duration": row.get("duration"),
                "stars": int(row.get("stars") or 0),
                "score": float(row.get("popularity_score") or row.get("final_score") or 0),
                "popularity_score": float(row.get("popularity_score") or row.get("final_score") or 0),
                "lastfm_listeners": int(row.get("lastfm_listeners") or 0),
                "listenbrainz_listens": int(row.get("listenbrainz_listens") or 0),
                "artist": str(row.get("artist") or row.get("album_artist") or ""),
                "is_live": int(row.get("is_live") or 0),
                "is_compilation": int(row.get("is_compilation") or 0),
            })
            genre_freq[target_display] += 1
            current_top = genre_display.get(target_norm_key)
            if not current_top or genre_freq[target_display] > genre_freq[current_top]:
                genre_display[target_norm_key] = target_display

    def _tiebreak(item: dict[str, Any]) -> tuple:
        return (
            item["is_live"],
            item["is_compilation"],
            -item["stars"],
            -item["score"],
            item["title"].casefold(),
        )

    if _excluded_counts:
        _parts = ", ".join(
            f"{genre}: {count}" for genre, count in sorted(_excluded_counts.items())
        )
        total_excluded = sum(_excluded_counts.values())
        log_unified(
            f"[FINALISE_STAGE] Genre exclusion — {total_excluded} metal track(s) "
            f"kept out of {len(_excluded_counts)} playlist(s): {_parts}"
        )
        logger.info(
            "Genre playlists excluded metal tracks",
            per_genre=dict(_excluded_counts),
            total=total_excluded,
        )

    written = 0
    skipped_unchanged = 0
    keep_playlist_names: set[str] = set()
    _cover_checked = 0
    _cover_generated = 0
    _cover_skipped = 0
    _cover_failed = 0
    _cover_flag_off = False
    _cover_enabled = False

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
                _normalise_artist_key(t["artist"]),
                _normalise_essential_title(t["title"]),
            )
            grouped[key].append(t)

        winners = [min(group, key=_tiebreak) for group in grouped.values()]
        qualifying_count = len(winners)
        playlist_name = _genre_playlist_name(_display_genre(genre))
        playlist_exists = playlist_name.casefold() in existing_playlists

        if qualifying_count >= delete_threshold:
            keep_playlist_names.add(playlist_name)

        if prune_only:
            continue

        if not playlist_exists and qualifying_count < create_threshold:
            continue

        if not create_enabled and not playlist_exists:
            continue

        winners = _ordered_playlist_rows(
            winners,
            order_mode=order_mode,
            max_per_artist=max_per_artist,
            interleave=interleave_artists,
        )
        if max_tracks:
            winners = winners[:max_tracks]

        # ⭐ AFTER the slice, always. Shuffling before it would let the cap drop
        # genuinely top tracks at random; here the rotation can only reorder the
        # tracks that already qualified.
        winners = _weekly_shuffle(winners, playlist_key=playlist_name)

        _song_ids = [str(t.get("id") or "").strip() for t in winners if str(t.get("id") or "").strip()]
        if not _song_ids:
            continue

        try:
            _known_pl_id = existing_playlist_ids.get(playlist_name.casefold())
            sync_res = _sync_playlist_to_navidrome(
                playlist_name,
                _song_ids,
                rows=winners,
                existing_playlist_id=_known_pl_id,
            )
            if not _playlist_sync_succeeded(sync_res):
                logger.warning(
                    "Genre playlist sync failed — Navidrome keeps its previous contents",
                    genre=genre,
                    playlist=playlist_name,
                    requested=len(_song_ids),
                    failed_clients=sync_res.get("failed") if isinstance(sync_res, dict) else None,
                )
                continue
            keep_playlist_names.add(playlist_name)
            _resolved_pl_id = str(sync_res.get("playlist_id") or "") or _known_pl_id
            if _resolved_pl_id:
                existing_playlist_ids[playlist_name.casefold()] = _resolved_pl_id
            if sync_res.get("skipped"):
                skipped_unchanged += 1
            else:
                log_unified(f"📄 Playlist: Synced '{playlist_name}' to Navidrome ({len(_song_ids)} tracks)")
                written += 1

            try:
                from helpers.config_helpers import get_genre_cover_config

                _genre_cover_cfg = get_genre_cover_config()
                if not _genre_cover_cfg["enabled"]:
                    _cover_flag_off = True
                else:
                    _cover_enabled = True
                if _genre_cover_cfg["enabled"]:
                    from services.playlists.genre_cover_service import (
                        generate_genre_cover,
                    )

                    _cover_display = _display_genre(genre)
                    _cover_checked += 1
                    for _cover_client in _navidrome_clients():
                        _cover = generate_genre_cover(
                            _cover_display,
                            playlist_name,
                            _cover_client,
                            regenerate=_genre_cover_cfg["regenerate"],
                            playlist_id=_resolved_pl_id,
                        )
                        if _cover.get("ok"):
                            if _cover.get("status") == "generated":
                                _cover_generated += 1
                                log_unified(
                                    f"🎨 Playlist cover: AI artwork set for '{playlist_name}'"
                                )
                            else:
                                _cover_skipped += 1
                            break
                        logger.warning(
                            "Genre playlist cover not applied",
                            playlist=playlist_name,
                            reason=_cover.get("reason"),
                        )
                    else:
                        _cover_failed += 1
            except Exception as exc:
                _cover_failed += 1
                logger.warning("Genre playlist cover generation failed", error=str(exc))
        except Exception as exc:
            logger.warning("Genre playlist API sync failed", genre=genre, error=str(exc))

    if skipped_unchanged:
        logger.debug("Genre playlists unchanged", count=skipped_unchanged)

    if _cover_checked:
        log_unified(
            f"🎨 Genre playlist covers — {_cover_generated} applied, "
            f"{_cover_skipped} already present, {_cover_failed} failed "
            f"({_cover_checked} playlist(s) checked)"
        )
    elif _cover_enabled:
        log_unified(
            "🎨 Genre playlist covers — enabled, but no genre playlist reached "
            "the cover check this pass (nothing was synced)"
        )
    elif _cover_flag_off:
        try:
            from helpers.config_helpers import get_gemini_config

            _gem = get_gemini_config()
            _gem_ready = bool(_gem.get("enabled") and _gem.get("api_key"))
        except Exception:
            _gem_ready = False
        if _gem_ready:
            log_unified(
                "🎨 Genre playlist covers: OFF — tick 'AI covers for Top Genre "
                "playlists' in Config ▸ Playlists (adding the Gemini key alone "
                "does not enable them)"
            )

    if delete_enabled and only_genres is None:
        if pools:
            try:
                _sweep_orphaned_genre_playlists_from_navidrome(keep_playlist_names)
            except Exception as exc:
                logger.warning("Genre playlist sweep failed", error=str(exc))

    return written


def prune_genre_playlists_for_deletion() -> None:
    """Delete genre playlists whose qualifying pool dropped below the delete threshold."""
    try:
        from services.popularity.scan_scope import global_playlists_deferred

        if global_playlists_deferred():
            return
        if not _genre_playlists_delete_enabled():
            return
        _create_genre_top_track_playlists(prune_only=True)
    except Exception as exc:
        logger.debug("Genre playlist prune failed", error=str(exc))


# ---------------------------------------------------------------------------
# Per-album star rating posting
# ---------------------------------------------------------------------------

def compute_artist_scores(
    artist: str,
    scan_scores: list[float],
    scanned_keys: set[tuple[str, str]] | None = None,
) -> list[float]:
    """Artist-wide score distribution = scan results so far + existing DB scores."""
    scanned_keys = scanned_keys or set()
    from services.catalog.album_classification_service import is_bonus_track_title

    artist_scores = [float(s) for s in scan_scores if float(s or 0) > 0]
    db_rows: list[tuple[str, float]] = []

    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "SELECT title, album, COALESCE(NULLIF(raw_score, 0), final_score) AS final_score FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND COALESCE(NULLIF(raw_score, 0), final_score) > 0"
                ),
                {"artist": artist},
            )
            for row in result.fetchall() or []:
                score_val = float(row_get(row, "raw_score") or row_get(row, "final_score") or 0)
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
        logger.debug("Artist DB score fetch failed", artist=artist, error=str(exc))

    try:
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        db_scores = list(reanchor_scores_to_album_relative(db_rows))
    except Exception as exc:
        logger.debug("Artist DB score re-anchor failed", artist=artist, error=str(exc))
        db_scores = [float(s) for _alb, s in db_rows]

    return list(artist_scores) + db_scores


def _album_scaling_configured() -> bool:
    try:
        cfg = get_standout_config() or {}
        return isinstance(cfg.get("album_scaling"), dict) and bool(cfg.get("album_scaling"))
    except Exception:
        return False


def _log_scan_weights(
    artist: str,
    album: str,
    album_model: dict[str, Any],
    is_live_album: bool = False,
) -> None:
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

        if is_live_album:
            lr = _live_album_rules()
            log_unified(
                f"🎤 LIVE ALBUM: {str(artist or '').strip()} — {str(album or '').strip()} "
                f"| mode={lr['mode']} (artist-z driven) "
                f"| bands: 5★≥{float(lr['artist_z_star5']):.2f} 4★≥{float(lr['artist_z_star4']):.2f} "
                f"3★≥{float(lr['artist_z_star3']):.2f} 2★≥{float(lr['artist_z_star2']):.2f} "
                f"| album_z gate={float(lr['album_z_gate']):.2f}, promote_artist_z≥{float(lr['promote_artist_z_min']):.2f} "
                f"| caps: max={int(lr['max_stars'])}★, 5★ slots={int(lr['max_5star_slots'])}, 4★ slots={int(lr['max_4star_slots'])} "
                f"| source: {'config' if _live_album_scaling_configured() else 'defaults'}"
            )
    except Exception as exc:
        logger.debug("Weights log failed", artist=artist, album=album, error=str(exc))


def _apply_live_album_slot_caps(
    album_results: list[dict[str, Any]],
    artist: str,
    album: str,
) -> None:
    """Cap 5★ and 4★ counts on a live album."""
    rules = _live_album_rules()
    max5 = max(0, int(rules["max_5star_slots"]))
    max4 = max(0, int(rules["max_4star_slots"]))

    def _protected(track: dict[str, Any]) -> bool:
        return (
            bool(track.get("_lock_honoured"))
            or str(track.get("single_confidence") or "").strip().casefold() == "user"
            or int(track.get("_force_floor") or 0) >= 5
        )

    def _score(track: dict[str, Any]) -> float:
        return float(track.get("popularity_score") or track.get("final_score") or 0)

    # --- 5★ slots ---------------------------------------------------------
    protected5 = [t for t in album_results if int(t.get("stars") or 0) == 5 and _protected(t)]
    automatic5 = [t for t in album_results if int(t.get("stars") or 0) == 5 and not _protected(t)]
    automatic5.sort(key=_score, reverse=True)
    for track in automatic5[max(0, max5 - len(protected5)):]:
        track["stars"] = 4
        track["_era_5star"] = False
        logger.info(
            "Live album cap applied: 5★ → 4★",
            artist=artist, album=album, title=track.get("title"), cap=max5,
        )

    # --- 4★ slots ---------------------------------------------------------
    protected4 = [
        t for t in album_results
        if int(t.get("stars") or 0) == 4
        and (_protected(t) or int(t.get("_force_floor") or 0) >= 4)
    ]
    automatic4 = [
        t for t in album_results
        if int(t.get("stars") or 0) == 4
        and not (_protected(t) or int(t.get("_force_floor") or 0) >= 4)
    ]
    automatic4.sort(key=_score, reverse=True)
    for track in automatic4[max(0, max4 - len(protected4)):]:
        track["stars"] = 3
        logger.info(
            "Live album cap applied: 4★ → 3★",
            artist=artist, album=album, title=track.get("title"), cap=max4,
        )


def _apply_short_track_star_cap(
    album_results: list[dict[str, Any]],
    *,
    durations: dict[str, float] | None = None,
    artist: str = "",
    album: str = "",
) -> int:
    """Clamp very short tracks to at most ``max_stars``."""
    try:
        from helpers.config_helpers import get_short_track_star_cap_config
        _cfg = get_short_track_star_cap_config()
    except Exception as exc:
        logger.debug("Short-track cap config read failed", error=str(exc))
        return 0

    if not _cfg.get("enabled"):
        return 0
    max_duration = float(_cfg.get("max_duration_seconds") or 0)
    max_stars = int(_cfg.get("max_stars") or 0)
    if max_duration <= 0 or max_stars < 1 or max_stars >= 5:
        return 0

    _durations = durations or {}
    capped = 0
    for track in album_results:
        if int(track.get("stars") or 0) <= max_stars:
            continue
        if _short_track_cap_is_protected(track):
            continue

        duration = _track_duration_seconds(track, _durations)
        if duration is None or duration >= max_duration:
            continue

        _previous = int(track.get("stars") or 0)
        track["stars"] = max_stars
        track["_era_5star"] = False
        _force = int(track.get("_force_floor") or 0)
        if _force > max_stars:
            track["_force_floor"] = max_stars
        capped += 1
        logger.info(
            "Short track cap applied",
            artist=artist, album=album, title=track.get("title"),
            duration_s=round(duration, 1), stars=f"{_previous}★ → {max_stars}★",
            max_duration_s=max_duration,
        )

    if capped:
        log_unified(
            f"⏱ SHORT TRACK CAP: {capped} track(s) under {max_duration:.0f}s "
            f"capped at {max_stars}★ — {str(album or '').strip()}"
        )
    return capped


def _short_track_cap_is_protected(track: dict[str, Any]) -> bool:
    """True when a track's rating is an explicit instruction, not a guess."""
    if bool(track.get("_lock_honoured")):
        return True
    if str(track.get("single_confidence") or "").strip().casefold() == "user":
        return True
    if bool(track.get("is_favourite")) or bool(track.get("favourite")):
        return True
    return False


def _track_duration_seconds(
    track: dict[str, Any],
    durations: dict[str, float],
) -> float | None:
    """Best-effort track duration in seconds from the row or the DB lookup."""
    raw = track.get("duration")
    if raw in (None, ""):
        track_id = str(track.get("track_id") or "").strip()
        raw = durations.get(track_id) if track_id else None
    try:
        duration = float(raw or 0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    if duration > 3600:
        duration = duration / 1000.0
    return duration if duration > 0 else None


def post_album_star_ratings(
    *,
    album_results: list[dict[str, Any]],
    artist: str,
    artist_scores: list[float],
    options: dict[str, Any],
    is_compilation: bool | None = None,
    is_va_compilation: bool = False,
    stars_reconciled: bool | None = None,
    provisional: bool = False,
) -> dict[str, int]:
    """Assign, persist, log and sync star ratings for ONE album."""
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
        if is_compilation is None:
            is_compilation, is_va_compilation = _resolve_compilation_flags(
                album_results=album_results,
                artist=artist,
                album=album,
            )
        else:
            is_compilation = bool(is_compilation or is_va_compilation)

        if is_compilation:
            logger.info(
                "Compilation detected — rating on per-track-artist catalogues",
                artist=artist,
                album=album,
                va_compilation=bool(is_va_compilation),
            )
        is_live_album = _detect_live_album(album_results)
        if is_live_album:
            logger.info(
                "Live album detected — rating on artist-z",
                artist=artist, album=album, tracks=len(album_results),
            )

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
            logger.debug("Album DB score merge failed", artist=artist, album=album, error=str(exc))

        album_model: dict[str, Any] = {}
        try:
            album_model = _build_album_model(artist, album_results, artist_scores)
            if album_model.get("has_benchmark"):
                logger.info(
                    "Album era mapped",
                    artist=artist, album=album, era=album_model.get("era"),
                    m_peak=float(album_model.get("m_peak") or 0),
                    album_median=float(album_model.get("album_median") or 0),
                    reff=float(album_model.get("reff") or 0),
                    benchmark_source=album_model.get("benchmark_source"),
                )
        except Exception as exc:
            logger.debug("Album model build failed", artist=artist, album=album, error=str(exc))

        _log_scan_weights(artist, album, album_model, is_live_album=is_live_album)

        compilation_track_artist_scores: dict[str, list[float]] = {}
        compilation_raw_scores: list[float] = []
        if is_compilation:
            compilation_track_artist_scores = _build_compilation_track_artist_scores(
                album_results, exclude_album=album
            )
            compilation_raw_scores = [
                float(r.get("_raw_combined") or 0)
                for r in album_results
                if float(r.get("_raw_combined") or 0) > 0
            ]

        _stored_stars: dict[str, int] = {}
        _stored_paths: dict[str, str] = {}
        _stored_durations: dict[str, float] = {}
        # Tracks whose stored stars are PROVISIONAL (``stars_reconciled`` is
        # explicitly FALSE). The provisional pass persists stars without
        # writing the file tag or syncing Navidrome, so on the reconcile pass
        # these rows must count as "needs writing" even when the star value
        # did not move — otherwise an unchanged rating never leaves the DB.
        # NULL counts as reconciled (rows that predate the column).
        _stored_unreconciled: set[str] = set()
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
                            "SELECT id, COALESCE(stars, star_rating, 0) AS stars, stars_reconciled, file_path, duration "
                            "FROM tracks WHERE CAST(id AS TEXT) IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": _album_ids},
                    ).fetchall() or []
                for _r in _rows:
                    _m = _r._mapping
                    _stored_stars[str(_m.get("id"))] = int(_m.get("stars") or 0)
                    if _m.get("stars_reconciled") is False:
                        _stored_unreconciled.add(str(_m.get("id")))
                    _p = str(_m.get("file_path") or "").strip()
                    if _p:
                        _stored_paths[str(_m.get("id"))] = _p
                    try:
                        _d = float(_m.get("duration") or 0)
                    except (TypeError, ValueError):
                        _d = 0.0
                    if _d > 0:
                        _stored_durations[str(_m.get("id"))] = _d
        except Exception as exc:
            logger.debug("Album rating/path batch load failed", artist=artist, album=album, error=str(exc))

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
            logger.debug("Artist listen distribution failed", artist=artist, error=str(exc))

        # Pre-resolve album-wide configs once to avoid per-track re-reads in _assign_stars
        _thresholds = _live_star_thresholds()
        try:
            from services.popularity.popularity_config import get_single_organic_floor
            _organic_floor: tuple[float, float] | None = get_single_organic_floor()
        except Exception:
            _organic_floor = (45.0, 1000.0)

        try:
            from services.popularity.popularity_config import get_artist_force_star_percentiles
            _force_star_pcts: tuple[float, float] | None = get_artist_force_star_percentiles()
        except Exception:
            _force_star_pcts = None

        _comp_online_rules: dict[str, Any] | None = None
        if is_compilation:
            try:
                from helpers.config_helpers import get_compilation_online_catalogue_config
                _comp_online_rules = get_compilation_online_catalogue_config()
            except Exception:
                _comp_online_rules = {}

        # 1. Assign star ratings in memory
        _lb_unusable_ids, _lb_cohort_scores = _lb_unusable_cohort(album_results)
        for track in album_results:
            try:
                if is_compilation:
                    _credited_artist = _compilation_track_artist(track)
                    _rating_artist_scores = compilation_track_artist_scores.get(
                        _credited_artist.casefold(), []
                    )
                    track["_rating_artist"] = _credited_artist
                    track["_rating_catalogue_size"] = len(_rating_artist_scores)
                else:
                    _rating_artist_scores = artist_scores

                stars = _assign_stars(
                    track,
                    album_scores,
                    _rating_artist_scores,
                    album_lf_listeners,
                    album_lb_listens,
                    popularity_only=bool(options.get("popularity_only")),
                    album_model=album_model,
                    is_compilation=is_compilation,
                    artist_listen_distribution=_artist_listen_distribution,
                    is_va_compilation=bool(is_va_compilation),
                    is_live_album=is_live_album,
                    source_unusable=(
                        str(track.get("track_id") or "").strip() in _lb_unusable_ids
                    ),
                    cohort_scores=_lb_cohort_scores,
                    compilation_raw_scores=compilation_raw_scores,
                    thresholds=_thresholds,
                    organic_floor=_organic_floor,
                    force_star_percentiles=_force_star_pcts,
                    compilation_online_rules=_comp_online_rules,
                )
            except Exception as exc:
                track_id = str(track.get("track_id") or "").strip()
                fallback_stars = int(_stored_stars.get(track_id, 0))
                if fallback_stars < 1:
                    fallback_stars = 1
                logger.warning(
                    "Star assignment failed — falling back to stored rating",
                    artist=artist, album=album, title=track.get("title"),
                    fallback_stars=fallback_stars, error=str(exc),
                )
                stars = fallback_stars

            track["stars"] = stars
            total_star_ratings += 1

        # 2. Enforce slot caps before persistence
        if is_live_album and not is_compilation:
            _apply_live_album_slot_caps(album_results, artist, album)
        elif album_model.get("has_benchmark") and not is_compilation:
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
                        bool(track.get("_lock_honoured"))
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
                    float(
                        track.get("_computed_album_z")
                        if track.get("_computed_album_z") is not None
                        else _compute_album_z(
                            float(track.get("popularity_score") or track.get("final_score") or 0),
                            album_scores,
                        )[0]
                    ),
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

        # 2.9 Clamp very short tracks (skits / interludes / jokes).
        _apply_short_track_star_cap(
            album_results,
            durations=_stored_durations,
            artist=artist,
            album=album,
        )

        def _needs_external_write(track_id: str, stars: int) -> bool:
            """Must this rating be written to the FILE and to Navidrome?

            True when the value changed, or when this is the RECONCILE pass and
            the stored row is still provisional (its earlier write deliberately
            skipped the file + Navidrome, so this pass owes them).
            """
            if stars != _stored_stars.get(track_id, 0):
                return True
            return stars_reconciled is True and track_id in _stored_unreconciled

        # 3. Persist calculated and capped ratings (batched with per-row fallback)
        _ratings_changed = False
        _persist_failures = 0
        _batch_params: list[dict[str, Any]] = []
        for track in album_results:
            track_id = str(track.get("track_id") or "").strip()
            stars = int(track.get("stars") or 0)
            if not track_id or stars < 1:
                continue
            if stars == _stored_stars.get(track_id, 0) and not (
                stars_reconciled is True and track_id in _stored_unreconciled
            ):
                # Unchanged, and nothing to promote from provisional to
                # reconciled. A provisional pass never downgrades an already
                # reconciled row whose stars did not move.
                continue
            if stars != _stored_stars.get(track_id, 0):
                _ratings_changed = True
            param: dict[str, Any] = {"stars": stars, "tid": track_id}
            if stars_reconciled is not None:
                param["rec"] = bool(stars_reconciled)
            _batch_params.append(param)

        if _batch_params:
            _sets_sql = "stars = :stars" + (", stars_reconciled = :rec" if stars_reconciled is not None else "")
            _update_stmt = text(f"UPDATE tracks SET {_sets_sql} WHERE CAST(id AS TEXT) = :tid")
            try:
                with db_session() as session:
                    session.execute(_update_stmt, _batch_params)
            except Exception as batch_exc:
                logger.debug("Batch rating persist failed, falling back to per-row savepoints", error=str(batch_exc))
                with db_session() as session:
                    for param in _batch_params:
                        nested = session.begin_nested()
                        try:
                            session.execute(_update_stmt, param)
                            if nested.is_active:
                                nested.commit()
                        except Exception as exc:
                            _persist_failures += 1
                            logger.warning(
                                "Rating persist failed", track_id=param["tid"],
                                artist=artist, album=album, error=str(exc),
                            )
                            try:
                                nested.rollback()
                            except Exception as rollback_exc:
                                logger.debug("Rating persist rollback failed", track_id=param["tid"], error=str(rollback_exc))

        if _persist_failures:
            logger.warning(
                "Some ratings could not be persisted",
                artist=artist, album=album, failures=_persist_failures,
            )

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
            logger.debug("Favourite rating floor skipped", artist=artist, album=album, error=str(_floor_err))

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
                logger.warning(
                    "Final rating reload failed — in-memory values may differ from DB",
                    artist=artist, album=album, error=str(_reload_err),
                )

        # 6. Write file tags and log progress using final values
        for track in album_results:
            stars = int(track.get("stars") or 0)
            if stars < 1:
                continue

            track_id = str(track.get("track_id") or "").strip()
            _track_score = float(track.get("popularity_score") or track.get("final_score") or 0)
            _final_score = float(track.get("final_score") or _track_score or 0)

            if track.get("_computed_album_z") is not None and track.get("_computed_artist_z") is not None:
                _album_z = float(track["_computed_album_z"])
                _artist_z = float(track["_computed_artist_z"])
            elif is_compilation:
                _rating_scores_for_log = compilation_track_artist_scores.get(
                    _compilation_track_artist(track).casefold(), []
                )
                _album_z = _compute_artist_z(_track_score, _rating_scores_for_log)[0]
                _artist_z = _album_z
            else:
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
                logger.debug("single_sources parse failed for log line", track_id=track_id, error=str(source_exc))

            _src_part = f", matched=[{', '.join(_src_names)}]" if _src_names else ""
            _live_part = ""
            if track.get("_live_reason"):
                _live_part = f", live={track.get('_live_reason')}"

            if is_compilation:
                _credited_artist_for_log = _compilation_track_artist(track) or artist
                _catalogue_n = int(track.get("_rating_catalogue_size") or 0)
                _rating_mode = str(track.get("_compilation_rating_mode") or "unknown")
                if _rating_mode == "track_artist_online_catalogue":
                    _rank = int(track.get("_compilation_online_rank") or 0)
                    _total = int(track.get("_compilation_online_total") or 0)
                    _pct = float(track.get("_compilation_online_percentile") or 0.0)
                    _comparison_part = (
                        f"track_artist={_credited_artist_for_log}, "
                        f"online_rank=#{_rank}/{_total} (top {_pct * 100:.0f}%), "
                        f"mode={_rating_mode}"
                    )
                else:
                    _comparison_part = (
                        f"track_artist={_credited_artist_for_log}, "
                        f"catalogue_z={_artist_z:.2f}, catalogue_n={_catalogue_n}, "
                        f"mode={_rating_mode}"
                    )
            else:
                _comparison_part = f"album_z={_album_z:.2f}, artist_z={_artist_z:.2f}"

            if not provisional:
                log_unified(
                    f"[TRACK_RESULT] {artist} - {track.get('title')} → {stars}★ "
                    f"(final_score={_final_score:.1f}, {_comparison_part}, "
                    f"single={track.get('is_single')}/{track.get('single_confidence')}"
                    + (f", era={album_model.get('era')}/R={float(album_model.get('reff') or 0):.2f}" if album_model.get("has_benchmark") else "")
                    + _live_part
                    + f"{_src_part})"
                )

            if not track_id:
                continue

            _rating_changed = _needs_external_write(track_id, stars)
            # Provisional ratings are reconciled at the artist boundary; the
            # reconcile pass writes the file tag (including ratings that did
            # not move but were stored provisionally), so skip it here.
            if not provisional and not (_skip_unchanged and not _rating_changed):
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
                    logger.debug("Rating tag write failed", track_id=track_id, error=str(tag_exc))

        try:
            star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for t in album_results:
                s = int(t.get("stars") or 0)
                if 1 <= s <= 5:
                    star_counts[s] += 1

            _pop_only = bool(options.get("popularity_only"))
            singles_detected = [] if _pop_only else [t for t in album_results if t.get("is_single")]

            if provisional:
                log_unified(
                    f"[RATINGS] Provisional ratings for '{album}' "
                    f"(reconciled at the artist boundary): "
                    + " | ".join(f"{k}★: {v}" for k, v in sorted(star_counts.items(), reverse=True) if v)
                )
            else:
                if _pop_only:
                    log_unified(f"Singles Detection - skipped (popularity only) in '{album}'")
                else:
                    log_unified(
                        f"Singles Detection - Detected {len(singles_detected)} single(s) in '{album}'"
                    )

                rows: list[dict[str, Any]] = []
                for t in album_results:
                    t_stars = int(t.get("stars") or 0)
                    t_conf = str(t.get("single_confidence") or "low").lower()
                    t_title = str(t.get("title") or "Unknown").strip()
                    t_score = float(t.get("popularity_score") or t.get("final_score") or 0)

                    if t.get("_computed_album_z") is not None:
                        album_z = float(t["_computed_album_z"])
                    elif is_compilation:
                        _ref_scores_for_row = compilation_track_artist_scores.get(
                            _compilation_track_artist(t).casefold(), []
                        )
                        album_z, _ = _compute_artist_z(t_score, _ref_scores_for_row)
                    else:
                        album_z, _ = _compute_album_z(t_score, album_scores)

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
                    except Exception as note_exc:
                        logger.debug("Summary note build failed", title=t_title, error=str(note_exc))

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

                _header_suffix = " [LIVE]" if is_live_album else ""
                if is_compilation:
                    _header_suffix += " [COMPILATION: per-track-artist rating]"
                log_unified("=" * 80)
                log_unified(f"📊 SCAN RESULTS: {str(artist or '').strip()} — {str(album or '').strip()}{_header_suffix} ({len(album_results)} Tracks)")
                log_unified("=" * 80)
                _z_header = "CAT-Z" if is_compilation else "Z-SCORE"
                log_unified(f"{'RATING':<7} {'TRACK TITLE':<34} {_z_header:>7} {'SCORE':>6} {'LF LISTENS':>10}  SINGLE CONF")
                log_unified("-" * 80)
                for r in rows:
                    star_str = "★" * r["stars"] + "☆" * (5 - r["stars"])
                    log_unified(f"{star_str:<7} {r['title']:<34} {r['z']:>+7.2f} {r['score']:>6.1f} {r['lf']:>10}  {r['conf']}{r['note']}")
                log_unified("-" * 80)
                log_unified(f"⭐ Distribution: 5★: {star_counts[5]} | 4★: {star_counts[4]} | 3★: {star_counts[3]} | 2★: {star_counts[2]} | 1★: {star_counts[1]}")
                log_unified("=" * 80)
        except Exception as log_exc:
            logger.debug("Album progress log failed", artist=artist, album=album, error=str(log_exc))

        if options.get("sync_navidrome", True) and not provisional:
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
                    if _skip_unchanged and not _needs_external_write(track_id, int(stars)):
                        _skipped += 1
                        continue

                    if _sync_clients is None:
                        _sync_clients = _build_rating_sync_clients()
                        if not _sync_clients:
                            logger.warning(
                                "No Navidrome rating clients available — skipping rating sync",
                                artist=str(artist or "").strip(),
                            )
                            break

                    _attempted += 1
                    if _sync_rating_to_navidrome(track_id, stars, clients=_sync_clients):
                        navidrome_synced += 1
                        _synced += 1
                        _consecutive_failures = 0
                    else:
                        _consecutive_failures += 1
                        if _consecutive_failures >= 3:
                            logger.warning(
                                "Aborting Navidrome rating sync after consecutive failures — Navidrome unreachable?",
                                artist=str(artist or "").strip(), failures=_consecutive_failures,
                            )
                            break

            _failed = _attempted - _synced
            if _skipped > 0:
                log_unified(f"🔗 Navidrome: skipped {_skipped} unchanged rating(s) for '{artist}'")
            if _synced > 0:
                log_unified(f"🔗 Navidrome: synced {_synced} rating(s) for '{artist}'")
            if _failed > 0:
                logger.warning(
                    "Navidrome rating syncs failed — check credentials",
                    failed=_failed, attempted=_attempted, artist=str(artist or "").strip(),
                )

    except Exception as exc:
        logger.error("Album finalisation failed", artist=artist, album=album, error=str(exc))
        log_unified(f"[FINALISE_STAGE] Album '{artist} - {album}' finalisation failed ({exc})")

    return {"star_ratings": total_star_ratings, "navidrome_synced": navidrome_synced}


# ---------------------------------------------------------------------------
# Curated smart playlists (Decades, Hit Singles, Hidden Gems, New Releases,
# Covers, Acoustic, High Energy, Mood Mixes)
# ---------------------------------------------------------------------------

_CURATED_ROWS_SQL = """
    SELECT id, title, album, file_path, duration, artist, album_artist,
           year, release_year, originalyear,
           COALESCE(stars, star_rating, 0) AS stars,
           COALESCE(popularity, final_score, 0) AS popularity_score,
           COALESCE(is_live, 0) AS is_live,
           COALESCE(is_compilation, 0) AS is_compilation,
           COALESCE(is_single, FALSE) AS is_single,
           COALESCE(is_cover, 0) AS is_cover,
           COALESCE(is_acoustic, 0) AS is_acoustic,
           COALESCE(lastfm_listeners, 0) AS lastfm_listeners,
           COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens,
           COALESCE(danceability, 0) AS danceability,
           COALESCE(bpm, 0) AS bpm,
           mood, genres, lastfm_tags, musicbrainz_genres, essentia_genres
    FROM tracks
    WHERE COALESCE(stars, star_rating, 0) >= :min_stars
"""


def _fetch_curated_playlist_rows(min_stars: int = 4) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(text(_CURATED_ROWS_SQL), {"min_stars": min_stars})
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.warning("Curated playlist row fetch failed", min_stars=min_stars, error=str(exc))
        return []

    if not rows:
        return []

    try:
        from helpers.config_helpers import get_excluded_playlist_artists
        excluded_artists = get_excluded_playlist_artists()
    except Exception:
        excluded_artists = set()

    filtered: list[dict[str, Any]] = []
    for r in rows:
        who = str(r.get("album_artist") or r.get("artist") or "").strip().casefold()
        if excluded_artists and who in excluded_artists:
            continue
        if is_christmas_track(
            title=str(r.get("title") or ""),
            album=str(r.get("album") or ""),
            genres=r.get("genres"),
        ):
            continue
        r["score"] = float(r.get("popularity_score") or 0)
        filtered.append(r)
    return filtered


def _dedupe_curated_pool(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in tracks:
        artist_val = str(t.get("artist") or t.get("album_artist") or "")
        title_val = str(t.get("title") or "")
        key = (_normalise_artist_key(artist_val), _normalise_essential_title(title_val))
        grouped[key].append(t)

    def _tiebreak(item: dict[str, Any]) -> tuple:
        return (
            int(item.get("is_live") or 0),
            int(item.get("is_compilation") or 0),
            -int(item.get("stars") or 0),
            -float(item.get("popularity_score") or item.get("score") or 0),
            str(item.get("title") or "").casefold(),
        )

    return [min(group, key=_tiebreak) for group in grouped.values()]


def _extract_track_year(row: dict[str, Any]) -> int | None:
    for col in ("originalyear", "release_year", "year"):
        raw = row.get(col)
        if raw is None:
            continue
        m = re.search(r"\b(19\d{2}|20\d{2})\b", str(raw))
        if m:
            yr = int(m.group(1))
            if 1900 <= yr <= 2100:
                return yr
    return None


def _sync_single_curated_playlist(
    playlist_name: str,
    pool: list[dict[str, Any]],
    *,
    max_tracks: int,
    cfg: dict[str, Any],
) -> bool:
    order_mode = str(cfg.get("playlist_order_mode", "prominence") or "prominence").strip().lower()
    if order_mode not in ("prominence", "stored"):
        order_mode = "prominence"
    max_per_artist = _resolve_max_per_artist(cfg)
    interleave_artists = bool(cfg.get("playlist_interleave_artists", True))

    winners = _dedupe_curated_pool(pool)
    if not winners:
        return False

    winners = _ordered_playlist_rows(
        winners,
        order_mode=order_mode,
        max_per_artist=max_per_artist,
        interleave=interleave_artists,
    )
    if max_tracks and max_tracks > 0:
        winners = winners[:max_tracks]

    song_ids = [str(r.get("id") or "").strip() for r in winners if str(r.get("id") or "").strip()]
    if not song_ids:
        return False

    sync_res = _sync_playlist_to_navidrome(playlist_name, song_ids, rows=winners)
    if not _playlist_sync_succeeded(sync_res):
        return False
    if not sync_res.get("skipped"):
        log_unified(f"📄 Playlist: Synced '{playlist_name}' to Navidrome ({len(song_ids)} tracks)")
    return True


def _create_decade_playlists(cfg: dict[str, Any]) -> int:
    if not bool(cfg.get("decade_playlists_enabled", False)):
        return 0
    template = str(cfg.get("decade_playlists_name_template") or "{decade}s - Top Tracks").strip()
    if "{decade}" not in template:
        template = "{decade}s - Top Tracks"
    min_tracks = max(5, int(cfg.get("decade_playlists_min_tracks", 50) or 50))
    max_tracks = max(10, int(cfg.get("decade_playlists_max_tracks", 200) or 200))

    rows = _fetch_curated_playlist_rows(min_stars=4)
    if not rows:
        return 0

    pools: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        yr = _extract_track_year(r)
        if yr and 1950 <= yr <= 2099:
            decade = (yr // 10) * 10
            pools[decade].append(r)

    written = 0
    for decade in sorted(pools.keys()):
        deduped = _dedupe_curated_pool(pools[decade])
        if len(deduped) < min_tracks:
            continue
        pl_name = template.replace("{decade}", str(decade)).strip()
        if _sync_single_curated_playlist(pl_name, deduped, max_tracks=max_tracks, cfg=cfg):
            written += 1
    return written


def _create_hit_singles_playlist(cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("hit_singles_playlist_enabled", False)):
        return False
    pl_name = str(cfg.get("hit_singles_playlist_name") or "Hit Singles").strip() or "Hit Singles"
    max_tracks = max(10, int(cfg.get("hit_singles_max_tracks", 250) or 250))
    rows = _fetch_curated_playlist_rows(min_stars=4)
    pool = [r for r in rows if bool(r.get("is_single"))]
    return _sync_single_curated_playlist(pl_name, pool, max_tracks=max_tracks, cfg=cfg)


def _create_hidden_gems_playlist(cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("hidden_gems_playlist_enabled", False)):
        return False
    pl_name = str(cfg.get("hidden_gems_playlist_name") or "Hidden Gems").strip() or "Hidden Gems"
    max_tracks = max(10, int(cfg.get("hidden_gems_max_tracks", 200) or 200))
    rows = _fetch_curated_playlist_rows(min_stars=4)
    pool = [
        r for r in rows
        if not bool(r.get("is_single"))
        and int(r.get("is_live") or 0) == 0
        and int(r.get("is_compilation") or 0) == 0
    ]
    return _sync_single_curated_playlist(pl_name, pool, max_tracks=max_tracks, cfg=cfg)


def _create_new_releases_playlist(cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("new_releases_playlist_enabled", False)):
        return False
    pl_name = str(cfg.get("new_releases_playlist_name") or "New Releases").strip() or "New Releases"
    max_tracks = max(10, int(cfg.get("new_releases_max_tracks", 100) or 100))
    current_year = datetime.now().year
    cutoff_year = current_year - 1
    rows = _fetch_curated_playlist_rows(min_stars=4)
    pool = []
    for r in rows:
        yr = _extract_track_year(r)
        if yr and yr >= cutoff_year:
            pool.append(r)
    return _sync_single_curated_playlist(pl_name, pool, max_tracks=max_tracks, cfg=cfg)


def _create_covers_playlist(cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("covers_playlist_enabled", False)):
        return False
    pl_name = str(cfg.get("covers_playlist_name") or "Great Covers").strip() or "Great Covers"
    max_tracks = max(10, int(cfg.get("covers_max_tracks", 150) or 150))
    rows = _fetch_curated_playlist_rows(min_stars=3)
    pool = [r for r in rows if int(r.get("is_cover") or 0) == 1]
    return _sync_single_curated_playlist(pl_name, pool, max_tracks=max_tracks, cfg=cfg)


_ACOUSTIC_RE = re.compile(r"\b(acoustic|unplugged|stripped)\b", re.IGNORECASE)


def _create_acoustic_playlist(cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("acoustic_playlist_enabled", False)):
        return False
    pl_name = str(cfg.get("acoustic_playlist_name") or "Acoustic Essentials").strip() or "Acoustic Essentials"
    max_tracks = max(10, int(cfg.get("acoustic_max_tracks", 150) or 150))
    rows = _fetch_curated_playlist_rows(min_stars=4)
    pool = []
    for r in rows:
        if int(r.get("is_acoustic") or 0) == 1:
            pool.append(r)
            continue
        blob = " ".join(
            str(r.get(k) or "")
            for k in ("title", "album", "genres", "lastfm_tags", "musicbrainz_genres")
        )
        if _ACOUSTIC_RE.search(blob):
            pool.append(r)
    return _sync_single_curated_playlist(pl_name, pool, max_tracks=max_tracks, cfg=cfg)


_HIGH_ENERGY_RE = re.compile(
    r"\b(energetic|upbeat|party|dance|club|workout|high[- ]energy|driving|anthem)\b",
    re.IGNORECASE,
)


def _create_high_energy_playlist(cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("high_energy_playlist_enabled", False)):
        return False
    pl_name = str(cfg.get("high_energy_playlist_name") or "High Energy & Upbeat").strip() or "High Energy & Upbeat"
    max_tracks = max(10, int(cfg.get("high_energy_max_tracks", 200) or 200))
    rows = _fetch_curated_playlist_rows(min_stars=4)
    pool = []
    for r in rows:
        dance = float(r.get("danceability") or 0)
        bpm = float(r.get("bpm") or 0)
        if dance >= 0.65 and bpm >= 115:
            pool.append(r)
            continue
        mood_blob = " ".join(str(r.get(k) or "") for k in ("mood", "genres", "lastfm_tags", "essentia_genres"))
        if _HIGH_ENERGY_RE.search(mood_blob):
            pool.append(r)
    return _sync_single_curated_playlist(pl_name, pool, max_tracks=max_tracks, cfg=cfg)


def _create_mood_playlists(cfg: dict[str, Any]) -> int:
    if not bool(cfg.get("mood_playlists_enabled", False)):
        return 0
    template = str(cfg.get("mood_playlists_name_template") or "{mood} - Mix").strip()
    if "{mood}" not in template:
        template = "{mood} - Mix"
    min_tracks = max(5, int(cfg.get("mood_playlists_min_tracks", 40) or 40))
    max_tracks = max(10, int(cfg.get("mood_playlists_max_tracks", 150) or 150))

    rows = _fetch_curated_playlist_rows(min_stars=4)
    if not rows:
        return 0

    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        raw_mood = str(r.get("mood") or "").strip()
        if not raw_mood or raw_mood.lower() in ("null", "none", "[]", "{}"):
            continue
        tokens: list[str] = []
        if raw_mood.startswith("["):
            try:
                parsed = json.loads(raw_mood)
                if isinstance(parsed, list):
                    tokens = [str(m).strip() for m in parsed if str(m).strip()]
            except Exception:
                pass
        if not tokens:
            tokens = [m.strip() for m in re.split(r"[,;/\\|]+", raw_mood) if m.strip()]
        for tok in tokens[:3]:
            norm = re.sub(r"[^\w\s-]", "", tok.lower()).strip()
            if norm and len(norm) >= 3:
                pools[norm].append(r)

    written = 0
    for mood_key in sorted(pools.keys()):
        deduped = _dedupe_curated_pool(pools[mood_key])
        if len(deduped) < min_tracks:
            continue
        display_mood = _display_genre(mood_key)
        pl_name = template.replace("{mood}", display_mood).strip()
        if _sync_single_curated_playlist(pl_name, deduped, max_tracks=max_tracks, cfg=cfg):
            written += 1
    return written


def _run_curated_playlists() -> None:
    try:
        from helpers.config_helpers import get_playlists_config
        cfg = get_playlists_config()
    except Exception as exc:
        logger.debug("Curated playlists config read failed", error=str(exc))
        return

    for fn_name, fn in (
        ("Decade playlists", _create_decade_playlists),
        ("Hit Singles playlist", _create_hit_singles_playlist),
        ("Hidden Gems playlist", _create_hidden_gems_playlist),
        ("New Releases playlist", _create_new_releases_playlist),
        ("Covers playlist", _create_covers_playlist),
        ("Acoustic playlist", _create_acoustic_playlist),
        ("High Energy playlist", _create_high_energy_playlist),
        ("Mood playlists", _create_mood_playlists),
    ):
        try:
            fn(cfg)
        except Exception as exc:
            logger.warning(f"{fn_name} generation failed", error=str(exc))


# ---------------------------------------------------------------------------
# Global playlist rebuild coalescing
# ---------------------------------------------------------------------------

_GLOBAL_PLAYLIST_LOCK = threading.Lock()


def _run_global_playlist_rebuild(
    options: dict[str, Any],
    *,
    force: bool = False,
) -> None:
    """Rebuild the GLOBAL playlists (genre top-tracks, New Music) + the index."""
    marker = "_global_playlists_done"

    if not force:
        try:
            from services.popularity.scan_scope import global_playlists_deferred

            if global_playlists_deferred():
                return
        except Exception:
            pass

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
            logger.warning("Genre playlist generation failed", error=str(exc))

        try:
            if _new_music_playlist_enabled():
                _create_new_music_playlist()
        except Exception as exc:
            logger.warning("New Music playlist generation failed", error=str(exc))

        try:
            _run_curated_playlists()
        except Exception as exc:
            logger.warning("Curated playlists generation failed", error=str(exc))

        try:
            from helpers.config_helpers import get_favourite_playlists_config

            _fav_cfg = get_favourite_playlists_config()
            if _fav_cfg.get("sync_favourites_from_navidrome"):
                from services.favourites_service import sync_favourites_for_all_users

                _fav_sync = sync_favourites_for_all_users()
                if _fav_sync.get("errors"):
                    logger.warning("Favourite sync reported errors", errors=_fav_sync["errors"])
            if _fav_cfg.get("favourite_playlists_enabled"):
                from services.playlists.favourite_playlist_service import (
                    sync_favourite_playlists,
                )

                _fav_pl = sync_favourite_playlists()
                if _fav_pl.get("errors"):
                    logger.warning("Favourite playlists reported errors", errors=_fav_pl["errors"])
                else:
                    log_unified(
                        f"[FINALISE_STAGE] Favourite playlists synced for "
                        f"{len(_fav_pl.get('users') or [])} user(s)"
                    )
        except Exception as exc:
            logger.warning("Favourite playlist sync failed", error=str(exc))

        try:
            from services.playlists.playlist_index_service import sync_playlist_index

            _index_stats = sync_playlist_index(_navidrome_clients())
            if _index_stats.get("changed") or _index_stats.get("pruned"):
                logger.info("Playlist index synced", **_index_stats)
        except Exception as exc:
            logger.debug("Playlist index sync failed", error=str(exc))

        try:
            options[marker] = True
        except Exception as exc:
            logger.debug("Could not set global playlist marker", error=str(exc))


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def _sync_isrc_popularity() -> int:
    """Sync popularity stats across tracks sharing the same ISRC **and title**.

    Preserves existing album-relative ``final_score`` / ``popularity`` on already
    scored albums (only backfilling unscored rows) while syncing recording-level
    raw signals (``raw_score``, ``lastfm_listeners``, ``listenbrainz_listens``,
    and ``is_single``).
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks AS target
                    SET popularity = CASE
                            WHEN COALESCE(target.popularity, target.final_score, 0) <= 0
                            THEN source.max_score
                            ELSE target.popularity
                        END,
                        final_score = CASE
                            WHEN COALESCE(target.final_score, target.popularity, 0) <= 0
                            THEN source.max_score
                            ELSE target.final_score
                        END,
                        raw_score = CASE
                            WHEN COALESCE(target.raw_score, 0) <= 0 AND source.max_raw_score > 0
                            THEN source.max_raw_score
                            ELSE target.raw_score
                        END,
                        lastfm_listeners = GREATEST(COALESCE(target.lastfm_listeners, 0), source.max_lf),
                        listenbrainz_listens = GREATEST(COALESCE(target.listenbrainz_listens, 0), source.max_lb),
                        is_single = source.max_single_status
                    FROM (
                        SELECT
                            isrc,
                            LOWER(TRIM(title)) AS title_key,
                            MAX(COALESCE(popularity, final_score, 0)) AS max_score,
                            MAX(COALESCE(raw_score, 0)) AS max_raw_score,
                            MAX(COALESCE(lastfm_listeners, 0)) AS max_lf,
                            MAX(COALESCE(listenbrainz_listens, 0)) AS max_lb,
                            BOOL_OR(COALESCE(is_single, FALSE)) AS max_single_status
                        FROM tracks
                        WHERE isrc IS NOT NULL
                          AND TRIM(isrc) <> ''
                        GROUP BY isrc, LOWER(TRIM(title))
                    ) AS source
                    WHERE target.isrc = source.isrc
                      AND LOWER(TRIM(target.title)) = source.title_key
                      AND (
                          (COALESCE(target.final_score, target.popularity, 0) <= 0 AND source.max_score > 0)
                          OR (COALESCE(target.raw_score, 0) <= 0 AND source.max_raw_score > 0)
                          OR COALESCE(target.lastfm_listeners, 0) < source.max_lf
                          OR COALESCE(target.listenbrainz_listens, 0) < source.max_lb
                          OR COALESCE(target.is_single, FALSE)
                              IS DISTINCT FROM source.max_single_status
                      )
                """)
            )
            return max(0, int(result.rowcount or 0))
    except Exception as exc:
        logger.warning("ISRC popularity sync failed", error=str(exc))
        return 0


def finalise_scan(*, results: list[dict[str, Any]], options: dict[str, Any]) -> None:
    """Finalise the scan: assign star ratings, sync to Navidrome, create playlists, log summary."""
    track_count = len(results) if results else 0
    log_unified(f"[FINALISE_STAGE] Finalising scan — {track_count} tracks processed")

    try:
        from helpers.config_helpers import get_config
        from services.popularity.popularity_config import get_single_organic_floor
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

        _lr = _live_album_rules()
        log_unified(
            f"🎤 LIVE CONFIG: mode={_lr['mode']}, max={int(_lr['max_stars'])}★, "
            f"artist_z 5★≥{float(_lr['artist_z_star5']):.2f}/4★≥{float(_lr['artist_z_star4']):.2f}, "
            f"album_z gate={float(_lr['album_z_gate']):.2f} "
            f"({'config' if _live_album_scaling_configured() else 'defaults'})"
        )
    except Exception as exc:
        logger.debug("Star config banner failed", error=str(exc))

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
            logger.error("Finalisation failed on empty results", error=str(exc))
        return

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
                    _album_names = {
                        str(r.get("album") or "").strip().casefold()
                        for r in artist_results
                        if str(r.get("album") or "").strip()
                    }
                    _artist_id = _resolve_navidrome_artist_id(artist) or artist

                    with db_session() as session:
                        _db_albums = session.execute(
                            text(
                                "SELECT DISTINCT LOWER(TRIM(album)) FROM tracks "
                                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                                "  AND album IS NOT NULL AND TRIM(album) <> ''"
                            ),
                            {"artist": artist},
                        ).fetchall() or []
                        for _row in _db_albums:
                            if _row and _row[0]:
                                _album_names.add(str(_row[0]))

                        _album_count = len(_album_names)
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
                    logger.info(
                        "artist_stats updated",
                        artist=artist, track_count=len(_valid_scores), median=_med,
                    )
            except Exception as exc:
                logger.warning("artist_stats persist failed", artist=artist, error=str(exc))

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
            from services.popularity.scan_scope import global_playlists_deferred

            if not global_playlists_deferred():
                _isrc_updated = _sync_isrc_popularity()
                if _isrc_updated:
                    invalidate_genre_playlist_rows()
                    log_unified(
                        f"[FINALISE_STAGE] ISRC sync: {_isrc_updated} track(s) updated; "
                        f"affected star ratings will be recalculated during the next rating pass"
                    )
        except Exception as exc:
            logger.warning("ISRC sync commit failed", error=str(exc))

        _run_global_playlist_rebuild(options)

    except Exception as exc:
        logger.error("Finalisation loop failed", error=str(exc))
        log_unified(f"[FINALISE_STAGE] Finalisation loop failed ({exc})")

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
