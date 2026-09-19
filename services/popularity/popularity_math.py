"""Pure popularity scoring and statistics helpers.

This module should not import API clients or database modules.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, median, stdev
from typing import Any

from services.popularity.popularity_config import resolve_weights

Z_SCORE_MIDPOINT = 50.0
Z_SCORE_TO_POPULARITY_SCALE = 16.7
# Minimum album spread (MAD*1.4826) when re-mapping a track's raw score to an
# album-relative popularity.
ALBUM_RELATIVE_MIN_SPREAD = 8.0
# Minimum number of valid album scores needed before album-relative re-mapping.
ALBUM_RELATIVE_MIN_ALBUM_TRACKS = 3


# -- Shared fuzzy string similarity -----------------------------------------
try:  # C-speed token-set matching; difflib fallback keeps CI working
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
    _fuzz = _fuzz
except Exception:
    _HAVE_RAPIDFUZZ = False
    _fuzz = None
from difflib import SequenceMatcher as _SequenceMatcher


def fuzzy_match_score(str1: str, str2: str) -> float:
    """Token-aware string similarity on a 0-1 scale (shared helper)."""
    if not str1 or not str2:
        return 0.0
    if str1 == str2:
        return 1.0
    if _HAVE_RAPIDFUZZ:
        return _fuzz.token_set_ratio(str1, str2) / 100.0
    return _SequenceMatcher(None, str1, str2).ratio()


ADAPTIVE_MIN_SPREAD_FRACTION = 0.10
Z_SCORE_LOGISTIC_K = Z_SCORE_TO_POPULARITY_SCALE / 25.0
LOG_SCALE_MULTIPLIER = 16.0
SINGLE_BOOST_FADE_START = 60.0
SINGLE_BOOST_FADE_END = 92.0
LOG_RATIO_DIVERGENCE_THRESHOLD = 0.85
LOG_RATIO_MIN_ALBUM_TRACKS = 3
LOG_RATIO_REJECT_LF_MIN_LB = 50
LOG_RATIO_REJECT_LB_MIN_LF = 100


def fmt_count(count: Any) -> str:
    """Format a listener/listen count compactly (14201 -> '14.2k')."""
    try:
        value = float(count or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def single_boost_fade(score: float) -> float:
    """Return the boost multiplier taper for a raw combined score."""
    if score <= SINGLE_BOOST_FADE_START:
        return 1.0
    if score >= SINGLE_BOOST_FADE_END:
        return 0.0
    return 1.0 - (score - SINGLE_BOOST_FADE_START) / (SINGLE_BOOST_FADE_END - SINGLE_BOOST_FADE_START)


def calculate_track_zscore(score: float, mean_value: float, stddev: float) -> float:
    """Calculate z-score for a track relative to a reference distribution."""
    if stddev and stddev > 0:
        return (score - mean_value) / stddev
    return 0.0


def zscore_to_popularity(z_score: float) -> float:
    """Convert z-score to a 0-100 popularity score with a soft ceiling."""
    if z_score <= 0:
        return min(100.0, max(0.0, Z_SCORE_MIDPOINT + (z_score * Z_SCORE_TO_POPULARITY_SCALE)))
    score = 100.0 / (1.0 + math.exp(-Z_SCORE_LOGISTIC_K * z_score))
    return min(100.0, max(0.0, score))


def calculate_robust_zscore(
    score: float,
    reference_scores: list[float],
    min_count: int = ALBUM_RELATIVE_MIN_ALBUM_TRACKS,
    min_spread: float = ALBUM_RELATIVE_MIN_SPREAD,
) -> tuple[float, float]:
    """Robust z-score (median + scaled-MAD) against a reference distribution."""
    valid = [float(s) for s in (reference_scores or []) if float(s or 0) > 0]
    if len(valid) < min_count:
        return 0.0, 0.0
    ref_median = median(valid)
    mad = median([abs(v - ref_median) for v in valid])
    spread = max(mad * 1.4826, min_spread, ADAPTIVE_MIN_SPREAD_FRACTION * ref_median)
    if spread <= 0:
        return 0.0, 0.0
    return (float(score) - ref_median) / spread, spread


def _remap_relative_popularity(raw_score: float, reference_scores: list[float]) -> float:
    """Shared robust-z re-map of a raw score against a reference distribution."""
    if raw_score is None or float(raw_score) <= 0:
        return float(raw_score or 0)
    valid = [float(s) for s in (reference_scores or []) if float(s or 0) > 0]
    if len(valid) < ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
        return float(raw_score)
    reference_median = median(valid)
    mad = median([abs(v - reference_median) for v in valid])
    spread = max(mad * 1.4826, ALBUM_RELATIVE_MIN_SPREAD)
    if spread <= 0:
        return float(raw_score)
    z = (float(raw_score) - reference_median) / spread
    return zscore_to_popularity(z)


def apply_album_relative_popularity(raw_score: float, album_scores: list[float]) -> float:
    """Re-map a raw popularity score relative to its ALBUM's distribution."""
    return _remap_relative_popularity(raw_score, album_scores)


def apply_track_artist_relative_popularity(raw_score: float, artist_scores: list[float]) -> float:
    """Re-map a raw popularity score relative to a TRACK ARTIST's distribution."""
    return _remap_relative_popularity(raw_score, artist_scores)


def reanchor_scores_to_album_relative(rows: list[tuple[str, float]]) -> list[float]:
    """Re-anchor stored ``(album, score)`` rows onto the album-relative scale.

    NOTE: the annotation previously read ``list[tuple[str, str, float]]`` while
    the body unpacks two-tuples, matching what ``compute_artist_scores`` in the
    finalise stage actually passes. The annotation is corrected here; behaviour
    is unchanged.
    """
    by_album: dict[str, list[float]] = {}
    for _album, _score in (rows or []):
        _s = float(_score or 0)
        if _s > 0:
            by_album.setdefault(str(_album or ""), []).append(_s)
    reanchored: list[float] = []
    for _album, _score in (rows or []):
        _s = float(_score or 0)
        if _s <= 0:
            continue
        reanchored.append(
            apply_album_relative_popularity(_s, by_album.get(str(_album or "")) or [])
        )
    return reanchored


# ---------------------------------------------------------------------------
# 3-step album scaling model (M_peak -> A_skew -> R_eff)
# ---------------------------------------------------------------------------

def age_skew_multiplier(
    album_year: float | int | None,
    current_year: int,
    peak_year: float | int | None = None,
) -> float:
    """Return the age-skew multiplier ``A_skew`` for an album."""
    y_album = float(album_year or 0)
    if y_album <= 0 or current_year <= 0:
        return 1.0
    y_peak = float(peak_year or 0)
    if y_peak > 0:
        if y_album > y_peak:
            years_since = max(1, int(current_year) - int(y_album))
            return 1.0 + 0.35 * math.log2(1.0 + 3.0 / years_since)
        if y_album < y_peak:
            return 1.0 + 0.15 * min(1.0, (y_peak - y_album) / 20.0)
    return 1.0


def effective_album_ratio(effective_median: float, m_peak: float) -> float:
    """Return ``R_eff`` -- where the album sits on the artist's career curve."""
    if not m_peak or float(m_peak) <= 0:
        return 1.0
    return min(1.0, max(0.0, float(effective_median) / float(m_peak)))


# ---------------------------------------------------------------------------
# Album standout RATIOS (shape-of-popularity test for 5★)
# ---------------------------------------------------------------------------
#
# Why ratios rather than z-scores or raw counts
# ---------------------------------------------
# A z-score answers "how many standard deviations above the mean is this
# track?" -- and on a SMALL, FLAT album it answers that misleadingly, because
# the spread (sigma/MAD) also shrinks. A 3-listener gap on an album whose
# counts are 8..14 produces a perfectly respectable z-score even though the
# listener numbers are indistinguishable from noise and the "top" track is
# only 7% ahead of the runner-up.
#
# Ratios (multipliers) measure the SHAPE of the album's popularity and are
# scale-free: "is #1 three times the median?" means the same thing whether the
# artist has 100 listeners or 10 million. A genuine standout eclipses the rest
# of the album; a flat album has no standout at any magnitude.
#
# Three independent multipliers, each answering a different question:
#
#   1. Runner-up gap  (top / #2)      -- is #1 separated from its nearest rival?
#      A true standout typically sits 1.5x-2.0x clear of the runner-up.
#      Near 1.0x means the top two are a tie (no standout).
#
#   2. Baseline       (top / median)  -- how far above the album's "standard
#      fare" is it? A genuine hit usually runs 3x-5x the album median.
#
#   3. Spread         (top / bottom)  -- total dynamic range of the album. On a
#      studio album with a hit and unpopular deep cuts this is routinely
#      10x-50x; on a uniformly-unlistened album it collapses toward 1x.
#
# Reference case (Anti-Flag, acoustic album; counts 14, 13, 13, ~11 median, 8):
#   runner-up 14/13 = 1.07x   baseline 14/11 = 1.27x   spread 14/8 = 1.75x
#   -> all three fail, correctly: that album has no standout.


#: Minimum usable counts before the ratio test is meaningful at all. Below
#: this the gate is INCONCLUSIVE and must not block a rating.
ALBUM_RATIO_MIN_COUNTS = 3


def album_standout_ratios(
    counts: list[float] | list[int] | None,
) -> dict[str, float]:
    """Return the three standout multipliers for one album's listener counts.

    Returns an empty dict when fewer than ``ALBUM_RATIO_MIN_COUNTS`` positive
    counts are available — the caller must treat that as "inconclusive" and
    NOT as a failure, so thin data can never suppress a rating.

    Keys:
        ``top``/``runner_up``/``median``/``floor``  the raw counts behind each
        ratio, so a log line can show the actual numbers a decision used.
        ``runner_up_ratio``/``median_ratio``/``floor_ratio``  the multipliers.
    """
    valid = sorted(
        (float(c) for c in (counts or []) if float(c or 0) > 0),
        reverse=True,
    )
    if len(valid) < ALBUM_RATIO_MIN_COUNTS:
        return {}

    top = valid[0]
    runner_up = valid[1]
    med = float(median(valid))
    floor = valid[-1]

    return {
        "top": top,
        "runner_up": runner_up,
        "median": med,
        "floor": floor,
        "n": float(len(valid)),
        "runner_up_ratio": (top / runner_up) if runner_up > 0 else 0.0,
        "median_ratio": (top / med) if med > 0 else 0.0,
        "floor_ratio": (top / floor) if floor > 0 else 0.0,
    }


def album_ratio_standout(
    count: float,
    counts: list[float] | list[int] | None,
    *,
    runner_up_min: float = 1.5,
    median_min: float = 3.0,
    floor_min: float = 10.0,
    min_passed: int = 3,
) -> tuple[bool, str, dict[str, float]]:
    """Is ``count`` a RATIO standout within ``counts``?

    The candidate must BE the album's top track: the multipliers describe the
    distance between #1 and the rest, so any other track is ineligible by
    definition. (This is also what stops a flat album from minting several
    5★ tracks — only one track can be the top.)

    ``min_passed`` is how many of the three multipliers must be satisfied;
    ``3`` (the default) means all of them. It exists so the three tests can be
    relaxed independently without adding three separate switches.

    Returns ``(ok, reason, ratios)``. ``ok`` is True when the data is
    INCONCLUSIVE (fewer than three counts), so a short album is never
    penalised for lacking a distribution to measure.
    """
    ratios = album_standout_ratios(counts)
    if not ratios:
        return True, "ratio_inconclusive(too_few_counts)", {}

    try:
        cand = float(count or 0)
    except (TypeError, ValueError):
        return True, "ratio_inconclusive(bad_count)", ratios

    top = float(ratios.get("top") or 0)
    if cand <= 0 or top <= 0:
        return True, "ratio_inconclusive(no_counts)", ratios

    # The ratios are a property of the album's #1 track.
    if cand < top:
        return False, f"ratio_not_album_top({cand:.0f}<{top:.0f})", ratios

    checks = (
        ("runner_up", float(ratios["runner_up_ratio"]), float(runner_up_min)),
        ("median", float(ratios["median_ratio"]), float(median_min)),
        ("floor", float(ratios["floor_ratio"]), float(floor_min)),
    )
    passed = [name for name, value, minimum in checks if value >= minimum]
    needed = max(1, min(int(min_passed or 1), len(checks)))

    detail = ", ".join(
        f"{name}={value:.2f}x/{minimum:.2f}x" for name, value, minimum in checks
    )

    if len(passed) >= needed:
        return True, f"ratio_standout({detail})", ratios
    failed = [name for name, value, minimum in checks if value < minimum]
    return False, f"ratio_flat({len(passed)}/{needed} passed; failed={','.join(failed)}; {detail})", ratios


def calculate_lastfm_popularity_score(listeners: int, artist_max_listeners: int = 0) -> float:
    """Calculate normalized Last.fm popularity from listener count."""
    if listeners is None or listeners <= 0:
        return 0.0
    if artist_max_listeners and artist_max_listeners > 0:
        return min(100.0, max(0.0, (listeners / artist_max_listeners) * 100.0))
    return min(100.0, math.log10(listeners + 1) * LOG_SCALE_MULTIPLIER)


def calculate_lastfm_zscore_popularity(
    listeners: int,
    playcount: int,
    album_listeners_list: list[int],
    album_playcounts_list: list[int],
) -> float:
    """Calculate Last.fm popularity using album-level z-score normalization."""
    valid_listeners = [int(x or 0) for x in album_listeners_list if x is not None and int(x or 0) > 0]
    if not valid_listeners:
        return calculate_lastfm_popularity_score(listeners)
    if len(valid_listeners) < 2:
        return calculate_lastfm_popularity_score(listeners, max(valid_listeners))
    listener_mean = mean(valid_listeners)
    listener_std = stdev(valid_listeners)
    z = calculate_track_zscore(float(listeners or 0), listener_mean, listener_std)
    return zscore_to_popularity(z)


def calculate_listenbrainz_popularity_score(listen_count: int) -> float:
    """Calculate normalized ListenBrainz popularity from global listen count."""
    if listen_count is None or listen_count <= 0:
        return 0.0
    return min(100.0, math.log10(listen_count + 1) * LOG_SCALE_MULTIPLIER)


def album_prominence_score(
    lastfm_listeners: int,
    listenbrainz_listens: int,
    *,
    lf_weight: float = 0.55,
    lb_weight: float = 0.45,
) -> float:
    """Return a log-scaled 0-100 prominence score for an album/track."""
    lf = int(lastfm_listeners or 0)
    lb = int(listenbrainz_listens or 0)
    if lf <= 0 and lb <= 0:
        return 0.0
    lf_score = calculate_lastfm_popularity_score(lf, 0)
    lb_score = calculate_listenbrainz_popularity_score(lb)
    if lf_score <= 0 and lb_score <= 0:
        return 0.0
    total_w = 0.0
    weighted = 0.0
    if lf_score > 0:
        weighted += lf_score * lf_weight
        total_w += lf_weight
    if lb_score > 0:
        weighted += lb_score * lb_weight
        total_w += lb_weight
    if total_w <= 0:
        return 0.0
    return min(100.0, max(0.0, weighted / total_w))


def album_prominence_median(track_rows: list[dict[str, Any]]) -> float:
    """Median album-prominence score across a set of track rows."""
    scores = [
        album_prominence_score(
            int(row_get_lf(row) or 0),
            int(row_get_lb(row) or 0),
        )
        for row in (track_rows or [])
    ]
    valid = [s for s in scores if s > 0]
    if not valid:
        return 0.0
    return median(valid)


def row_get_lf(row: Any) -> Any:
    """Best-effort ``lastfm_listeners`` read from a dict/SQLAlchemy row."""
    if row is None:
        return 0
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return mapping["lastfm_listeners"]
        except Exception:
            return 0
    if isinstance(row, dict):
        return row.get("lastfm_listeners", 0)
    return 0


def row_get_lb(row: Any) -> Any:
    """Best-effort ``listenbrainz_listens`` read from a dict/SQLAlchemy row."""
    if row is None:
        return 0
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return mapping["listenbrainz_listens"]
        except Exception:
            return 0
    if isinstance(row, dict):
        return row.get("listenbrainz_listens", 0)
    return 0


def calculate_listenbrainz_percentile(lb_listens: int, album_lb_listens: list[int]) -> float:
    """Calculate percentile of a track within album ListenBrainz distribution."""
    if lb_listens is None or lb_listens <= 0:
        return 0.0
    valid = [x for x in (album_lb_listens or []) if x is not None and x > 0]
    if not valid:
        return 0.0
    below = sum(1 for x in valid if x < lb_listens)
    equal = sum(1 for x in valid if x == lb_listens)
    return (below + equal / 2.0) / len(valid)


def evaluate_listenbrainz_validity(
    *,
    listenbrainz_listens: int = 0,
    lastfm_listeners: int = 0,
    album_lb_listens: list[int] | None = None,
    album_lf_lb_pairs: list[tuple[int, int]] | None = None,
    is_single: bool = False,
) -> tuple[bool, list[str]]:
    """Decide whether a track's ListenBrainz count is realistic for its album."""
    if is_single:
        return True, []
    lb = int(listenbrainz_listens or 0)
    if lb <= 0:
        return True, []
    reasons: list[str] = []

    valid = [int(x) for x in (album_lb_listens or []) if int(x or 0) > 0]
    if len(valid) >= 5:
        med = median(valid)
        mad = median([abs(v - med) for v in valid])
        spread = mad * 1.4826
        if spread > 0 and lb < med - 2 * spread:
            reasons.append("lb_far_below_album_median")

    lf = int(lastfm_listeners or 0)
    if lf > 0 and not reasons:
        ratios = [
            int(a) / int(b)
            for a, b in (album_lf_lb_pairs or [])
            if int(a or 0) > 0 and int(b or 0) > 0
        ]
        if len(ratios) >= 5:
            med_ratio = median(ratios)
            if med_ratio > 0 and (lf / lb) > 10 * med_ratio:
                reasons.append("lf_lb_ratio_outlier")

    return not reasons, reasons


# -- Log-Ratio Median Deviation (Log-MAD) audit ------------------------------

def evaluate_log_ratio_deviation(
    *,
    lastfm_listeners: int = 0,
    listenbrainz_listens: int = 0,
    album_lf_lb_pairs: list[tuple[int, int]] | None = None,
    divergence_threshold: float = LOG_RATIO_DIVERGENCE_THRESHOLD,
    min_album_tracks: int = LOG_RATIO_MIN_ALBUM_TRACKS,
    reject_lf_min_lb: int = LOG_RATIO_REJECT_LF_MIN_LB,
    reject_lb_min_lf: int = LOG_RATIO_REJECT_LB_MIN_LF,
) -> str:
    """Return the Log-MAD verdict for one track against its album.

    CAVEAT: this compares a track against its album's MEDIAN log-ratio, so it
    assumes the majority of the album's provider data is accurate. When most
    tracks on a release carry cross-version-contaminated Last.fm counts, the
    median is itself contaminated and the CORRECTLY matched tracks become the
    statistical outliers. Fix provider contamination at the source rather than
    relying on this audit to catch it.
    """
    pairs = [
        (int(a or 0), int(b or 0))
        for a, b in (album_lf_lb_pairs or [])
        if int(a or 0) > 0 and int(b or 0) > 0
    ]
    if len(pairs) < min_album_tracks:
        return "VALID"
    log_ratios = [math.log10((lf + 1) / (lb + 1)) for lf, lb in pairs]
    album_median_ratio = median(log_ratios)

    lf = int(lastfm_listeners or 0)
    lb = int(listenbrainz_listens or 0)
    track_ratio = math.log10((lf + 1) / (lb + 1))
    delta = track_ratio - album_median_ratio

    if delta < -divergence_threshold and lb > reject_lf_min_lb:
        return "REJECT_LF"
    if delta > divergence_threshold and lf > reject_lb_min_lf:
        return "REJECT_LB"
    return "VALID"


def audit_album_playcounts(
    tracks: list[Any],
    divergence_threshold: float = LOG_RATIO_DIVERGENCE_THRESHOLD,
) -> list[tuple[Any, str]]:
    """Log-MAD audit of an album's Last.fm / ListenBrainz counts."""
    def _lf(t: Any) -> int:
        if isinstance(t, dict):
            return int(t.get("lastfm_listeners") or 0)
        return int(getattr(t, "lf", 0) or 0)

    def _lb(t: Any) -> int:
        if isinstance(t, dict):
            return int(t.get("listenbrainz_listens") or 0)
        return int(getattr(t, "lb", 0) or 0)

    tracks = list(tracks or [])
    if len(tracks) < LOG_RATIO_MIN_ALBUM_TRACKS:
        return [(t, "VALID") for t in tracks]

    log_ratios = [math.log10((_lf(t) + 1) / (_lb(t) + 1)) for t in tracks]
    album_median_ratio = median(log_ratios)

    results = []
    for t in tracks:
        lf, lb = _lf(t), _lb(t)
        delta = math.log10((lf + 1) / (lb + 1)) - album_median_ratio
        if delta < -divergence_threshold and lb > LOG_RATIO_REJECT_LF_MIN_LB:
            verdict = "REJECT_LF"
        elif delta > divergence_threshold and lf > LOG_RATIO_REJECT_LB_MIN_LF:
            verdict = "REJECT_LB"
        else:
            verdict = "VALID"
        results.append((t, verdict))
    return results


def apply_log_ratio_audit_to_stored_score(
    *,
    lastfm_listeners: int = 0,
    listenbrainz_listens: int = 0,
    album_lf_lb_pairs: list[tuple[int, int]] | None = None,
    lastfm_score: float = 0.0,
    listenbrainz_score: float = 0.0,
    age_score: float = 0.0,
    divergence_threshold: float = LOG_RATIO_DIVERGENCE_THRESHOLD,
) -> tuple[str, dict[str, float] | None]:
    """Re-blend a STORED popularity score through the Log-MAD audit."""
    verdict = evaluate_log_ratio_deviation(
        lastfm_listeners=lastfm_listeners,
        listenbrainz_listens=listenbrainz_listens,
        album_lf_lb_pairs=album_lf_lb_pairs,
        divergence_threshold=divergence_threshold,
    )
    if verdict == "VALID":
        return verdict, None

    if verdict == "REJECT_LF":
        lf_w, lb_w, age_w = 0.0, 0.90, 0.10
    else:  # REJECT_LB
        lf_w, lb_w, age_w = 1.0, 0.0, 0.0

    scores: list[float] = []
    weights: list[float] = []
    if float(lastfm_score or 0) > 0 and lf_w > 0:
        scores.append(float(lastfm_score))
        weights.append(lf_w)
    if float(listenbrainz_score or 0) > 0 and lb_w > 0:
        scores.append(float(listenbrainz_score))
        weights.append(lb_w)
    if float(age_score or 0) > 0 and age_w > 0:
        scores.append(float(age_score))
        weights.append(age_w)

    if scores and weights:
        combined = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    else:
        combined = 0.0

    return verdict, {
        "combined_score": round(min(100.0, max(0.0, combined)), 3),
        "lastfm_score": round(float(lastfm_score or 0), 3),
        "listenbrainz_score": round(float(listenbrainz_score or 0), 3),
        "age_score": round(float(age_score or 0), 3),
    }


def score_by_age(playcount: int | float, release_str: str) -> tuple[float, int]:
    """Apply age decay to a playcount-like metric."""
    try:
        if len(release_str) == 4 and release_str.isdigit():
            release_date = datetime.strptime(release_str, "%Y")
        else:
            release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        return playcount * (1 / math.log2(capped_days + 2)), days_since
    except Exception:
        return 0.0, 9999


def calculate_combined_popularity_score(
    *,
    lastfm_listeners: int = 0,
    lastfm_artist_max_listeners: int = 0,
    listenbrainz_listens: int = 0,
    album_lb_listens: list[int] | None = None,
    album_lf_listeners: list[int] | None = None,
    age_source_value: float = 0.0,
    release_date: str | None = None,
    is_single: bool = False,
    has_metadata: bool = False,
    is_featured_track: bool = False,
    is_live_track: bool = False,
    is_instrumental_track: bool = False,
    lastfm_weight_override: float | None = None,
    source_audit: str = "VALID",
    single_boost: float = 1.15,
    metadata_score_floor: float = 5.0,
    live_weight_penalty: float = 0.5,
    instrumental_weight_penalty: float = 0.8,
) -> dict[str, float]:
    """Blend Last.fm + ListenBrainz + age into one weighted popularity score."""
    lastfm_log = calculate_lastfm_popularity_score(lastfm_listeners, 0)
    lb_log = calculate_listenbrainz_popularity_score(listenbrainz_listens)

    # Album context is present when the caller supplied an album-level listener
    # distribution to normalise against. This gates the absolute-floor override
    # further down -- see the comment there.
    has_album_context = bool(album_lf_listeners) or bool(album_lb_listens)

    if album_lf_listeners:
        lastfm_score = calculate_lastfm_zscore_popularity(
            lastfm_listeners,
            lastfm_listeners,
            album_lf_listeners,
            album_lf_listeners,
        )
    else:
        lastfm_score = lastfm_log

    lb_score = lb_log

    if album_lb_listens:
        lb_percentile = calculate_listenbrainz_percentile(listenbrainz_listens, album_lb_listens)
        lb_score = max(lb_log, lb_percentile * 100.0)

    age_score = 0.0
    if release_date and age_source_value:
        aged, _days = score_by_age(age_source_value, release_date)
        age_score = calculate_listenbrainz_popularity_score(int(aged or 0))

    has_mismatch = is_source_mismatch(lastfm_listeners, listenbrainz_listens)
    is_unreliable = is_lastfm_unreliable(lastfm_listeners, listenbrainz_listens)
    _audit = str(source_audit or "VALID")

    _live_lf_w, _live_lb_w, _live_age_w = resolve_weights()
    _inst_penalty = max(0.0, min(1.0, instrumental_weight_penalty))
    effective_lf_weight = lastfm_weight_override

    if effective_lf_weight is None:
        effective_lf_weight = _live_lf_w
        if is_live_track:
            effective_lf_weight = _live_lf_w * max(0.0, min(1.0, live_weight_penalty))
        elif is_instrumental_track:
            effective_lf_weight = _live_lf_w * _inst_penalty

    active_scores: list[float] = []
    active_weights: list[float] = []

    if has_mismatch or is_unreliable or _audit != "VALID":
        lf_weight, lb_weight = adjust_weights(
            lastfm_listeners,
            listenbrainz_listens,
            is_featured_track=is_featured_track,
            metadata_confirmed=has_metadata,
        )
        if _audit == "REJECT_LF":
            lf_weight, lb_weight = 0.0, 1.0
        elif _audit == "REJECT_LB":
            lf_weight, lb_weight = 1.0, 0.0

        if is_live_track:
            lf_weight = lf_weight * max(0.0, min(1.0, live_weight_penalty))
        elif is_instrumental_track:
            lf_weight = lf_weight * _inst_penalty

        if lastfm_score > 0 and lf_weight > 0:
            active_scores.append(lastfm_score)
            active_weights.append(lf_weight)
        if lb_score > 0 and lb_weight > 0:
            active_scores.append(lb_score)
            active_weights.append(lb_weight)
    else:
        if lastfm_score > 0:
            active_scores.append(lastfm_score)
            active_weights.append(effective_lf_weight)
        if lb_score > 0:
            active_scores.append(lb_score)
            active_weights.append(_live_lb_w)

    if age_score > 0 and _audit != "REJECT_LB":
        active_scores.append(age_score)
        active_weights.append(_live_age_w)

    if active_scores and active_weights:
        total_weight = sum(active_weights)
        combined = sum(s * w for s, w in zip(active_scores, active_weights)) / total_weight

        # FIXED: this absolute-component floor previously ran unconditionally.
        # ``lastfm_log``/``lb_log`` are RAW log-scale absolutes -- they carry no
        # album context at all -- so taking max() of them and overwriting the
        # weighted blend discarded every album-relative signal that had just
        # been computed (the z-normalised LF score, the LB percentile, the
        # configured LF/LB/Age weights, and any Log-MAD re-weighting).
        #
        # Because log10(n)*16 saturates hard (100k -> 80, 1M -> 96), an 11x
        # listener range collapsed into ~6 score points, every track on an
        # album landed in the low-to-mid 90s, and the downstream robust z had
        # almost no dispersion left to work with.
        #
        # The floor is still useful when there is NO album context to normalise
        # against -- a standalone track with no sibling distribution would
        # otherwise be scored against nothing -- so it is retained for that
        # case only.
        if not has_album_context:
            absolute_components = [s for s in (lastfm_log, lb_log, age_score) if s > 0]
            if len(absolute_components) >= 2:
                strongest = max(absolute_components)
                if strongest > combined:
                    combined = strongest
    else:
        combined = 0.0

    if has_metadata and 0.0 < combined < metadata_score_floor:
        combined = metadata_score_floor

    if is_single and combined > 0:
        combined *= 1.0 + (single_boost - 1.0) * single_boost_fade(combined)

    return {
        "combined_score": round(min(100.0, max(0.0, combined)), 3),
        "lastfm_score": round(lastfm_score, 3),
        "listenbrainz_score": round(lb_score, 3),
        "age_score": round(age_score, 3),
    }


def is_source_mismatch(lastfm_listeners: int, lb_listens: int) -> bool:
    """Detect large mismatch between Last.fm and ListenBrainz popularity."""
    lastfm_listeners = int(lastfm_listeners or 0)
    lb_listens = int(lb_listens or 0)
    if lastfm_listeners == 0 or lb_listens == 0:
        return False
    return lb_listens >= max(100, lastfm_listeners * 3) or lastfm_listeners >= max(100, lb_listens * 5)


def is_lastfm_unreliable(lastfm_listeners: int, lb_listens: int) -> bool:
    """Flag Last.fm as unreliable when LF is very low but LB is strong."""
    return int(lastfm_listeners or 0) <= 20 and int(lb_listens or 0) >= 75


def adjust_weights(
    lastfm_listeners: int,
    lb_listens: int,
    is_featured_track: bool = False,
    metadata_confirmed: bool = False
) -> tuple[float, float]:
    """Adjust Last.fm / ListenBrainz weights when sources are mismatched."""
    lastfm_listeners = int(lastfm_listeners or 0)
    lb_listens = int(lb_listens or 0)
    if lastfm_listeners < 20:
        lf_weight = 0.0
    elif lb_listens > lastfm_listeners * 2:
        lf_weight = 0.4
    else:
        lf_weight = 0.6

    if is_featured_track:
        lf_weight = min(lf_weight, 0.35)
    if metadata_confirmed:
        lf_weight = max(lf_weight, 0.25)

    lb_weight = 1.0 - lf_weight
    return lf_weight, lb_weight


# -- Short-interlude ListenBrainz outlier filter -----------------------------

INTERLUDE_LB_MAX_DURATION_S = 180.0
INTERLUDE_LB_RATIO_FACTOR = 3.0
INTERLUDE_LB_MIN_COUNT = 500


def is_interlude_lb_outlier(
    *,
    duration_seconds: float | None,
    lastfm_listeners: int,
    listenbrainz_listens: int,
    album_lf_lb_pairs: list[tuple[int, int]] | None = None,
    max_duration_s: float = INTERLUDE_LB_MAX_DURATION_S,
    ratio_factor: float = INTERLUDE_LB_RATIO_FACTOR,
    min_lb: int = INTERLUDE_LB_MIN_COUNT,
) -> bool:
    """True when a SHORT interlude carries an anomalously inflated LB count.

    FIXED: a percentile-star-rating function had previously been pasted in
    the middle of this function's body, between the setup checks above and
    the final ratio comparison below -- orphaning the ``track_ratio``/
    ``return`` lines after that function's own ``return`` statements so this
    function fell off the end returning ``None`` (always falsy) whenever
    ``album_median_ratio > 0``, silently disabling interlude-outlier
    detection. The misplaced function now lives on its own below, and the
    logic here is back to a single, complete function body.
    """
    try:
        duration = float(duration_seconds or 0)
    except (TypeError, ValueError):
        return False

    if duration <= 0 or duration > float(max_duration_s):
        return False

    lb = int(listenbrainz_listens or 0)
    lf = int(lastfm_listeners or 0)
    if lb < int(min_lb) or lf <= 0:
        return False

    ratios: list[float] = []
    for a, b in (album_lf_lb_pairs or []):
        a_i = int(a or 0)
        b_i = int(b or 0)
        if a_i > 0 and b_i > 0:
            ratios.append(b_i / a_i)

    if len(ratios) < LOG_RATIO_MIN_ALBUM_TRACKS:
        return False

    album_median_ratio = median(ratios)
    if album_median_ratio <= 0:
        return False

    track_ratio = lb / lf
    return track_ratio > album_median_ratio * float(ratio_factor)


# -- Live-album star rating (artist-z driven) --------------------------------
#
# Album-relative z is self-referential: every album is z-scored against its OWN
# tracks, so the strongest cut on a mediocre live record scores exactly like the
# strongest cut on a landmark studio album. For live releases that is the wrong
# question. What matters is "is this track notable within the artist's whole
# catalogue", which is precisely what artist-z measures.
#
# Because live recordings genuinely draw fewer listeners than their studio
# counterparts, most live tracks land at NEGATIVE artist-z and naturally fall
# into the 1-3 bands with no artificial compression needed. Tracks that were
# real singles (e.g. Metallica's "No Leaf Clover" from S&M) carry catalogue-
# level listener counts and rise on their own merit.

LIVE_MODE_ARTIST_Z = "artist_z"
LIVE_MODE_ARTIST_AND_ALBUM = "artist_and_album"

# Artist-z band boundaries used for tracks on a LIVE album. These are
# deliberately stricter than the studio artist-z thresholds: a live track has
# to be genuinely notable across the artist's catalogue to clear 4 stars.
LIVE_ARTIST_Z_STAR5 = 1.50
LIVE_ARTIST_Z_STAR4 = 1.00
LIVE_ARTIST_Z_STAR3 = -0.25
LIVE_ARTIST_Z_STAR2 = -1.20

# In "artist_and_album" mode a track must also clear this album-z before it is
# allowed to reach 4 or 5 stars. It is ALSO the promotion threshold: a track
# at or above it that is respectable catalogue-wide is lifted to 4 stars.
LIVE_ALBUM_Z_GATE = 2.00

# Minimum artist-z required before the album-z promotion path may fire. Stops
# a track that is merely the least-weak cut on a poor live album from being
# promoted purely on album-relative standing.
LIVE_PROMOTE_ARTIST_Z_MIN = 0.50

# Minimum catalogue size before artist-z is trustworthy enough to band on.
LIVE_ARTIST_Z_MIN_CATALOGUE = 8

LIVE_ALBUM_MAX_STARS = 3            # ceiling for an ordinary live track
LIVE_ALBUM_MAX_5STAR_SLOTS = 1
LIVE_ALBUM_MAX_4STAR_SLOTS = 2
LIVE_ALBUM_TRACK_FRACTION = 0.60    # share of live tracks that marks a release


def live_album_star_from_artist_z(
    artist_z: float,
    album_z: float = 0.0,
    *,
    mode: str = LIVE_MODE_ARTIST_AND_ALBUM,
    single_confidence: str = "low",
    organic: bool = False,
    star5_z: float = LIVE_ARTIST_Z_STAR5,
    star4_z: float = LIVE_ARTIST_Z_STAR4,
    star3_z: float = LIVE_ARTIST_Z_STAR3,
    star2_z: float = LIVE_ARTIST_Z_STAR2,
    album_z_gate: float = LIVE_ALBUM_Z_GATE,
    promote_artist_z_min: float = LIVE_PROMOTE_ARTIST_Z_MIN,
    default_max: int = LIVE_ALBUM_MAX_STARS,
    epsilon_z: float = 0.0,
) -> tuple[int, str]:
    """Return ``(stars, reason)`` for one track on a LIVE album.

    Banding is on ARTIST-z. Album-z is never used to lift a rating on its own
    merit alone; it acts in two specific roles:

    - as a GATE, when artist-z already wants 4-5 stars (``artist_and_album``
      mode only), so a weak-catalogue artist cannot mint a full row of 5-star
      tracks simply because the live album beats their own low median; and
    - as a PROMOTION path, when a track is a decisive standout within its own
      live release AND respectable catalogue-wide. This is the case pure
      artist-z misses: a live single whose studio sibling dominates the
      catalogue never clears the artist-z 4 threshold on its own, yet is
      unmistakably the peak of the live record.

    A user override always returns 5. A verified single (high/medium
    confidence with organic support) is floored at 4 and reaches 5 when it
    also clears the artist-z 5 band.
    """
    try:
        a_z = float(artist_z or 0.0)
        alb_z = float(album_z or 0.0)
    except (TypeError, ValueError):
        a_z = alb_z = 0.0

    conf = str(single_confidence or "low").strip().casefold()
    eps = max(0.0, float(epsilon_z or 0.0))
    combined_mode = str(mode or "").strip().casefold() == LIVE_MODE_ARTIST_AND_ALBUM

    if conf == "user":
        return 5, "user_override"

    verified_single = conf in ("high", "medium") and bool(organic)

    # ---- 1. Raw artist-z band -------------------------------------------
    if a_z >= float(star5_z) - eps:
        band = 5
    elif a_z >= float(star4_z) - eps:
        band = 4
    elif a_z >= float(star3_z) - eps:
        band = 3
    elif a_z >= float(star2_z) - eps:
        band = 2
    else:
        band = 1

    reason = f"artist_z_band({a_z:+.2f})"

    # ---- 2. Album-z gate on an already-high artist-z ---------------------
    if band > int(default_max) and not verified_single and combined_mode:
        if alb_z < float(album_z_gate):
            band = int(default_max)
            reason = f"album_z_gate_failed(album_z={alb_z:+.2f} < {float(album_z_gate):.2f})"
        else:
            reason = f"artist_z+album_z({a_z:+.2f}/{alb_z:+.2f})"

    # ---- 3. Album-z PROMOTION path ---------------------------------------
    if band < 4 and alb_z >= float(album_z_gate) and a_z >= float(promote_artist_z_min):
        band = 4
        reason = f"album_z_promotion(album_z={alb_z:+.2f}, artist_z={a_z:+.2f})"

    # ---- 4. Verified-single floor ----------------------------------------
    if verified_single:
        floor = 5 if a_z >= float(star5_z) - eps else 4
        if band < floor:
            band = floor
            reason = f"verified_single_floor(artist_z={a_z:+.2f})"

    return band, reason


def is_live_album_context(
    tracks: list[dict[str, Any]] | None,
    album_type: str = "",
    fraction: float = LIVE_ALBUM_TRACK_FRACTION,
) -> bool:
    """True when a RELEASE is a live album (not just one live track).

    A studio album carrying a single bonus live cut must NOT be treated as a
    live album, so this requires either an explicit live album type or a
    supermajority of tracks carrying a live flag.

    NOTE: the per-track ``is_live``/``album_context_live`` flags are commonly
    derived from TITLE markers, and a live release frequently ships plainly
    titled tracks ("Enter Sandman" on S&M). In that case ``live_count`` is 0
    and this falls back entirely on ``album_type`` -- so callers must pass a
    trustworthy, release-level album type here rather than relying on the
    track-fraction heuristic to notice.
    """
    if "live" in str(album_type or "").strip().casefold():
        return True

    rows = list(tracks or [])
    if len(rows) < 2:
        return False

    live_count = sum(
        1 for t in rows
        if bool(t.get("album_context_live")) or bool(t.get("is_live"))
    )
    threshold = max(2, math.ceil(len(rows) * max(0.0, min(1.0, float(fraction)))))
    return live_count >= threshold


# -- Percentile-based star ratings (artist / genre "top songs") --------------
#
# One generic function buckets a score into 1-5 stars AND returns the raw
# percentile against whatever reference cohort you pass in. The percentile
# is what "top songs" lists should actually sort/limit on -- star is a coarse
# display bucket, and many tracks tie within the same star tier.

def calculate_percentile_star_rating(
    track_score: float, reference_scores: list[float]
) -> tuple[int, float]:
    """1-5 star rating + raw percentile against any reference cohort.

    Brackets:
      - Top 10% (percentile >= 0.90): 5*
      - Top 20% (percentile >= 0.80): 4*
      - Top 30% (percentile >= 0.70): 3*
      - Top 50% (percentile >= 0.50): 2*
      - Rest (< 0.50): 1*
    """
    valid = [float(s) for s in (reference_scores or []) if float(s or 0) > 0]
    if not valid or track_score is None or float(track_score) <= 0:
        return 1, 0.0

    val = float(track_score)
    below_or_equal = sum(1 for s in valid if s <= val)
    percentile = below_or_equal / len(valid)

    if percentile >= 0.90:
        stars = 5
    elif percentile >= 0.80:
        stars = 4
    elif percentile >= 0.70:
        stars = 3
    elif percentile >= 0.50:
        stars = 2
    else:
        stars = 1
    return stars, percentile


def calculate_artist_percentile_star_rating(track_score: float, artist_scores: list[float]) -> int:
    """1-5 star rating for a track against its OWN artist's catalog.

    Thin, backward-compatible wrapper around ``calculate_percentile_star_rating``
    for callers that only need the star bucket, not the raw percentile.
    """
    stars, _percentile = calculate_percentile_star_rating(track_score, artist_scores)
    return stars


def calculate_genre_percentile_star_rating(track_score: float, genre_scores: list[float]) -> int:
    """1-5 star rating for a track against a GENRE-wide cohort (all artists)."""
    stars, _percentile = calculate_percentile_star_rating(track_score, genre_scores)
    return stars


def _rank_5star_then_4star(rated: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Shared ordering: 5-star tier first, backfilled with 4-star, each tier
    sorted by percentile (raw score breaks ties within a percentile bucket)."""
    def _sorted(tier: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(tier, key=lambda t: (t["percentile"], t["combined_score"]), reverse=True)

    five_star = _sorted([t for t in rated if t["stars"] == 5])
    four_star = _sorted([t for t in rated if t["stars"] == 4])

    ordered = five_star + four_star
    return ordered[:limit] if limit else ordered


MIN_ESSENTIAL_ARTIST_TRACKS = 8


def top_songs_by_artist(
    tracks: list[dict[str, Any]],
    artist_reference_scores: list[float],
    limit: int | None = None,
    min_qualifying_tracks: int = MIN_ESSENTIAL_ARTIST_TRACKS,
) -> list[dict[str, Any]]:
    """5-star tracks first, backfilled with 4-star, ranked within each tier.

    Scored against the artist's OWN distribution, so a decent track on a
    weak album can still rank above a merely-good track on a stronger one.

    Returns [] if the artist doesn't clear ``min_qualifying_tracks`` (>= --
    at least that many 4-star-or-better tracks, not strictly more). This is
    a pass/fail gate for "Essential Artist" eligibility, not a cutoff on how
    many songs make the final list -- use ``limit`` for that.

    ``tracks`` items need at least {"combined_score": float, ...}.
    """
    rated = []
    for track in tracks:
        score = float(track.get("combined_score") or 0)
        stars, percentile = calculate_percentile_star_rating(score, artist_reference_scores)
        if stars >= 4:
            rated.append({**track, "stars": stars, "percentile": percentile})

    if len(rated) < min_qualifying_tracks:
        return []

    return _rank_5star_then_4star(rated, limit)


def top_songs_by_genre(
    tracks: list[dict[str, Any]],
    genre_reference_scores: list[float],
    limit: int = 50,
) -> list[dict[str, Any]]:
    """5-star tracks first, backfilled with 4-star, ranked within each tier.

    Scored against a GENRE-wide cohort (all artists tagged with that genre),
    so this is naturally comparable across different artists -- unlike an
    already album-relative-remapped score, which is only meaningful within
    one album's own distribution.

    ``tracks`` items need at least {"combined_score": float, ...}.
    """
    rated = []
    for track in tracks:
        score = float(track.get("combined_score") or 0)
        stars, percentile = calculate_percentile_star_rating(score, genre_reference_scores)
        if stars >= 4:
            rated.append({**track, "stars": stars, "percentile": percentile})

    return _rank_5star_then_4star(rated, limit)


def build_essential_artist_playlists(
    all_tracks: list[dict[str, Any]],
    min_qualifying_tracks: int = MIN_ESSENTIAL_ARTIST_TRACKS,
    limit_per_artist: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Group by artist, gate, and order -- one call for the whole library.

    ``all_tracks`` items need at least {"artist": str, "combined_score": float}.
    Artists that don't meet the gate are simply absent from the result.
    """
    from collections import defaultdict

    by_artist: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for track in all_tracks:
        by_artist[str(track.get("artist") or "")].append(track)

    playlists: dict[str, list[dict[str, Any]]] = {}
    for artist, artist_tracks in by_artist.items():
        if not artist:
            continue
        reference_scores = [float(t.get("combined_score") or 0) for t in artist_tracks]
        songs = top_songs_by_artist(
            artist_tracks, reference_scores,
            limit=limit_per_artist, min_qualifying_tracks=min_qualifying_tracks,
        )
        if songs:
            playlists[artist] = songs
    return playlists
