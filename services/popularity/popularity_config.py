"""Popularity config and weights.

All reads go through ``helpers.config_helpers.get_config()`` — the single
cached config source (env overrides + config.yaml).  Runtime lookups
(``resolve_weights``, ``get_zscore_thresholds``, …) therefore pick up saved
config changes immediately; only the import-time module constants below are
fixed per process.
"""

from __future__ import annotations

from helpers.config_helpers import get_config

DEFAULT_WEIGHTS = {
    "lastfm": 0.55,
    "listenbrainz": 0.35,
    "age": 0.10,
}


def resolve_weights(config: dict | None = None) -> tuple[float, float, float]:
    """Resolve Last.fm, ListenBrainz and age weights from config.

    ``popularity.weights`` takes precedence (used by the scan pipeline's UI
    section); falls back to the top-level ``weights`` block.
    """
    cfg = config if isinstance(config, dict) else get_config()
    weights = cfg.get("popularity", {}).get("weights") if isinstance(cfg, dict) else None
    if not isinstance(weights, dict):
        weights = cfg.get("weights") if isinstance(cfg, dict) else None
    weights = weights or {}
    try:
        lastfm = float(weights.get("lastfm", DEFAULT_WEIGHTS["lastfm"]))
        listenbrainz = float(weights.get("listenbrainz", DEFAULT_WEIGHTS["listenbrainz"]))
        age = float(weights.get("age", DEFAULT_WEIGHTS["age"]))
    except Exception:
        lastfm, listenbrainz, age = DEFAULT_WEIGHTS["lastfm"], DEFAULT_WEIGHTS["listenbrainz"], DEFAULT_WEIGHTS["age"]

    total = lastfm + listenbrainz + age
    if total <= 0:
        return DEFAULT_WEIGHTS["lastfm"], DEFAULT_WEIGHTS["listenbrainz"], DEFAULT_WEIGHTS["age"]
    return lastfm / total, listenbrainz / total, age / total


def get_zscore_thresholds(config: dict | None = None) -> dict:
    """Load single-detection z-score confidence boundaries from config.

    Returns ``{'medium': 0.6, 'high': 1.0, 'standout_gap_z': 0.75}`` by
    default, matching the legacy ``get_zscore_thresholds`` helper.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    if not isinstance(sd, dict):
        sd = {}
    return {
        "medium": float(sd.get("zscore_medium_threshold", 0.6)),
        "high": float(sd.get("zscore_high_threshold", 1.0)),
        "standout_gap_z": float(sd.get("standout_gap_z", 0.75)),
    }


def get_single_boost(config: dict | None = None) -> float:
    """Return the confirmed-single score boost multiplier (default 1.15)."""
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("single_boost", 1.15))
    except Exception:
        return 1.15


def get_metadata_score_floor(config: dict | None = None) -> float:
    """Return the minimum score floor for tracks with confirmed metadata (default 5.0)."""
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("metadata_score_floor", 5.0))
    except Exception:
        return 5.0


def get_live_weight_penalty(config: dict | None = None) -> float:
    """Return the Last.fm weight penalty fraction for live tracks (default 0.5)."""
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("live_weight_penalty", 0.5))
    except Exception:
        return 0.5


def get_instrumental_weight_penalty(config: dict | None = None) -> float:
    """Return the Last.fm weight penalty fraction for instrumental tracks (default 0.8).

    An instrumental version carries the same "recorded performance" listener
    counts as the vocal track but is not a single candidate, so its raw
    Last.fm/ListenBrainz weight is reduced by this fraction BEFORE the
    z-score is calculated — naturally suppressing its z-score so it does not
    mathematically bury the vocal tracks on the album.  Mirrors the live
    weight penalty.  ``0`` disables (no penalty).
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("instrumental_weight_penalty", 0.8))
    except Exception:
        return 0.8


def get_velocity_scoring_config(config: dict | None = None) -> dict[str, float]:
    """Age-adjusted velocity + ListenBrainz collision dampening.

    Both default OFF so an existing installation's ratings are unchanged until
    the operator turns them on; the scan's own preview is what validates the
    numbers before enabling them.

    Keys (all under ``popularity.velocity``):

    * ``enabled`` — run the pass at all (velocity blend AND collision damping);
    * ``weight`` — how much of the score the velocity term carries. ``0``
      (the default) leaves the blended score alone while still applying the
      collision damping, which is useful on its own;
    * ``collision_damping`` — the multiplier applied to a track whose
      ListenBrainz pair is shared with a different song (default 0.88);
    * ``collision_min_listens`` — the count below which a tie is coincidence
      rather than an MBID collision (default 50,000).
    """
    cfg = config if isinstance(config, dict) else get_config()
    block = {}
    if isinstance(cfg, dict):
        popularity = cfg.get("popularity")
        if isinstance(popularity, dict):
            candidate = popularity.get("velocity")
            if isinstance(candidate, dict):
                block = candidate

    def _num(key: str, default: float) -> float:
        try:
            return float(block.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    weight = max(0.0, min(1.0, _num("weight", 0.0)))
    return {
        # ⚠️ EXPLICIT, not implied by the weight. Setting a weight enables the
        # pass, but a weight of 0 must NOT silently switch the collision damping
        # off — the two terms are independently useful.
        "enabled": bool(block.get("enabled", "weight" in block)),
        "weight": weight,
        "collision_damping": max(0.0, min(1.0, _num("collision_damping", 0.88))),
        "collision_min_listens": max(0.0, _num("collision_min_listens", 50_000)),
    }


def get_exclude_from_median_below_seconds(config: dict | None = None) -> float:
    """Return the minimum track duration (seconds) to count in stats.

    Ambient interludes / skits (``< 1:30``) artificially compress the album
    median and shrink the variance used for z-scores, dragging real tracks
    down.  ``statistics.exclude_from_median_below_seconds`` (default 60)
    drops shorter tracks from the album reference distribution so the
    median/MAD measure the album's actual songs.
    """
    cfg = config if isinstance(config, dict) else get_config()
    stats = cfg.get("statistics", {}) if isinstance(cfg, dict) else {}
    try:
        return float(stats.get("exclude_from_median_below_seconds", 60) or 0)
    except Exception:
        return 60.0


_INTRO_INTERLUDE_TITLE_RE = r"(?i)(^intro$|\bintro$|interlude|\[intro\]|\(intro\))"


def get_exclude_title_regex(config: dict | None = None) -> str:
    """Return the title regex used to strip intro/interlude tracks from stats.

    Spoken-word intros and interludes (``"Dig Intro"``, ``"[Interlude]"``,
    ``"Golden Ratio (Interlude)"``) carry tiny listen counts and crater the
    album floor, inflating ordinary tracks' relative scores and poisoning the
    artist baseline.  ``filters.exclude_title_regex`` (default
    ``(^intro$|\\bintro$|interlude|\\[intro\\]|\\(intro\\))``, case-
    insensitive) matches them; empty disables.
    """
    cfg = config if isinstance(config, dict) else get_config()
    filters = cfg.get("filters", {}) if isinstance(cfg, dict) else {}
    try:
        value = str(filters.get("exclude_title_regex", "") or "").strip()
        return value or _INTRO_INTERLUDE_TITLE_RE
    except Exception:
        return _INTRO_INTERLUDE_TITLE_RE


def get_artist_force_star_percentiles(config: dict | None = None) -> tuple[float, float]:
    """Return the artist top-% (by raw listens) that FORCE 5★ / 4★.

    A failsafe against within-album normalization: when a track sits in the
    artist's absolute top-N% by raw Last.fm listeners, its popularity is
    undeniable regardless of how tight its own album's distribution is (the
    "consistent album" paradox — every L.D. 50 track is popular, so Dig's
    album_z looks ordinary).  ``single_detection.artist_top_percentile_force_5_star``
    (default 0.03 = top 3%) forces 5★; ``..._force_4_star`` (default 0.10 =
    top 10%) forces at least 4★.  ``0`` disables.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        force5 = float(sd.get("artist_top_percentile_force_5_star", 0.03) or 0)
        force4 = float(sd.get("artist_top_percentile_force_4_star", 0.10) or 0)
    except Exception:
        force5, force4 = 0.03, 0.10
    return force5, force4


def get_single_organic_floor(config: dict | None = None) -> tuple[float, float]:
    """Return the organic popularity floor gating single-driven star elevation.

    A metadata-tagged single (Discogs/MusicBrainz) with almost no organic
    audience must not leapfrog genuinely popular album tracks: single-driven
    elevation above 3★ (5★ award, 4★ Single Floor, era album-top-N) requires
    ``popularity_score >= score_floor`` OR ``Last.fm listeners >=
    listeners_floor``.  Defaults: ``(45.0, 1000)`` — configurable via
    ``single_detection.single_organic_floor_score`` /
    ``single_organic_floor_listeners``.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return (
            float(sd.get("single_organic_floor_score", 45.0)),
            float(sd.get("single_organic_floor_listeners", 1000)),
        )
    except Exception:
        return 45.0, 1000.0


DEFAULT_LOG_RATIO = {
    "enabled": True,
    "divergence_threshold": 0.85,
    "reject_lf_min_lb": 50,
    "reject_lb_min_lf": 100,
}


def get_log_ratio_config(config: dict | None = None) -> dict:
    """Return the Log-MAD cross-platform playcount audit settings.

    Reads ``single_detection.log_ratio_*`` keys:
    - ``log_ratio_enabled`` (default True) — master switch for the audit
    - ``log_ratio_divergence_threshold`` (default 0.85 ≈ 7x relative
      divergence) — a track's log10 LF/LB ratio must deviate by more than
      this from the album's median log ratio before it is rejected
    - ``log_ratio_reject_lf_min_lb`` (default 50) — minimum ListenBrainz
      listens before Last.fm is distrusted (LB must be healthy to trust)
    - ``log_ratio_reject_lb_min_lf`` (default 100) — minimum Last.fm
      listeners before ListenBrainz is distrusted

    A threshold of 1.00 corresponds to a 10x relative divergence (safe,
    conservative); 0.70 corresponds to ~5x.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    if not isinstance(sd, dict):
        sd = {}
    try:
        enabled = bool(sd.get("log_ratio_enabled", DEFAULT_LOG_RATIO["enabled"]))
    except Exception:
        enabled = DEFAULT_LOG_RATIO["enabled"]
    try:
        threshold = float(sd.get("log_ratio_divergence_threshold", DEFAULT_LOG_RATIO["divergence_threshold"]))
    except Exception:
        threshold = DEFAULT_LOG_RATIO["divergence_threshold"]
    try:
        reject_lf_min_lb = int(sd.get("log_ratio_reject_lf_min_lb", DEFAULT_LOG_RATIO["reject_lf_min_lb"]))
    except Exception:
        reject_lf_min_lb = DEFAULT_LOG_RATIO["reject_lf_min_lb"]
    try:
        reject_lb_min_lf = int(sd.get("log_ratio_reject_lb_min_lf", DEFAULT_LOG_RATIO["reject_lb_min_lf"]))
    except Exception:
        reject_lb_min_lf = DEFAULT_LOG_RATIO["reject_lb_min_lf"]
    return {
        "enabled": enabled,
        "divergence_threshold": threshold,
        "reject_lf_min_lb": reject_lf_min_lb,
        "reject_lb_min_lf": reject_lb_min_lf,
    }


DEFAULT_INTERLUDE_LB_OUTLIER = {
    "enabled": True,
    "max_duration_s": 180.0,
    "ratio_factor": 3.0,
    "min_lb": 500,
}


def get_interlude_lb_outlier_config(config: dict | None = None) -> dict:
    """Return the short-interlude ListenBrainz outlier filter settings.

    Reads ``single_detection.interlude_lb_*`` keys:
    - ``interlude_lb_outlier_enabled`` (default True) — master switch
    - ``interlude_lb_max_duration_s`` (default 180) — a track shorter than
      this is treated as an interlude candidate
    - ``interlude_lb_ratio_factor`` (default 3.0) — the track's LB/LF ratio
      must exceed the album's median LB/LF ratio by this factor before the LB
      is rejected (a short interlude with 20k+ LB listens on a 45k-LF track —
      higher than every single — is a recording-MBID artifact, not real
      audience)
    - ``interlude_lb_min_count`` (default 500) — minimum LB count before the
      filter applies (tiny/noisy counts are never flagged)
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    if not isinstance(sd, dict):
        sd = {}
    try:
        enabled = bool(sd.get("interlude_lb_outlier_enabled", DEFAULT_INTERLUDE_LB_OUTLIER["enabled"]))
    except Exception:
        enabled = DEFAULT_INTERLUDE_LB_OUTLIER["enabled"]
    try:
        max_duration_s = float(sd.get("interlude_lb_max_duration_s", DEFAULT_INTERLUDE_LB_OUTLIER["max_duration_s"]))
    except Exception:
        max_duration_s = DEFAULT_INTERLUDE_LB_OUTLIER["max_duration_s"]
    try:
        ratio_factor = float(sd.get("interlude_lb_ratio_factor", DEFAULT_INTERLUDE_LB_OUTLIER["ratio_factor"]))
    except Exception:
        ratio_factor = DEFAULT_INTERLUDE_LB_OUTLIER["ratio_factor"]
    try:
        min_lb = int(sd.get("interlude_lb_min_count", DEFAULT_INTERLUDE_LB_OUTLIER["min_lb"]))
    except Exception:
        min_lb = DEFAULT_INTERLUDE_LB_OUTLIER["min_lb"]
    return {
        "enabled": enabled,
        "max_duration_s": max_duration_s,
        "ratio_factor": ratio_factor,
        "min_lb": min_lb,
    }
