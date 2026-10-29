"""Shared listener-count z-score helpers for popularity scoring.

Used by single detection (``services/enrichment/single_detection_service``)
and the star-rating finalisation stage (``services/popularity/stages/
finalise_stage``) — previously duplicated as ``_log_listener_z`` /
``_composite_album_z`` / ``_listener_z`` / ``_composite_listener_z`` in each
module.  Both stages log-scale listener/listen counts (right-skewed: one hit
dominates an album) and blend Last.fm / ListenBrainz z-scores against the
album's own tracklist distribution.
"""

from __future__ import annotations

import structlog

from services.popularity.popularity_math import (
    LOG_LISTENER_Z_MIN_SPREAD,
    LOG_LISTENER_Z_RELATIVE_SPREAD,
    calculate_robust_log_zscore,
)

logger = structlog.get_logger(__name__)

# Backwards-compatible names.  The floors now live beside the helper that uses
# them (``popularity_math``), so the ALBUM SCORE z and this standout z cannot
# drift onto different scales.
LOG_LISTENER_Z_MIN_SIGMA = LOG_LISTENER_Z_MIN_SPREAD
LOG_LISTENER_Z_RELATIVE_SIGMA = LOG_LISTENER_Z_RELATIVE_SPREAD


def log_listener_z(count: float, counts: list[float]) -> float:
    """Outlier-resistant log-scaled z of a track's count within its album.

    ⭐ MEDIAN + MAD, not mean + stdev.  Listener counts are heavy-tailed: one
    runaway hit inflates a mean/stdev reference so much that every sibling
    lands at a negative z and stops looking like a standout at all.  In log
    space a 2M-listener single beside 30k-listener album tracks no longer moves
    the centre, so "how unusual is this track" stays a question about the
    album's own typical spread.

    The noise floors (a small absolute value and a fraction of the centre) stop
    a near-uniform album from turning measurement noise into huge z values.
    Zero-variance distributions (all counts identical, or fewer than 3 positive
    counts) carry no outlier signal — return 0.0.
    """
    try:
        return calculate_robust_log_zscore(float(count or 0), list(counts or []))
    except Exception:
        return 0.0


def composite_listener_z(
    lastfm_listeners: int | float | None,
    listenbrainz_listens: int | float | None,
    artist: str | None = None,
    album: str | None = None,
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
) -> float:
    """Album-local composite z-score from the track's raw LF/LB counts.

    ``z_composite = (w_LF * z_LF + w_LB * z_LB) / (w_LF + w_LB)`` where each
    provider z is the track's log-scaled z within ITS ALBUM'S OWN tracklist
    distribution (never the artist catalogue).  When only one provider has
    usable data the composite collapses to that provider's z (the "LB
    invalid/bypassed → score on Last.fm only" behaviour).

    Caller-supplied album distributions are used when provided; otherwise the
    album's stored listener counts are loaded from the DB (keyed by the album
    artist, matching every other popularity-stats lookup).  Returns 0.0 when
    there is not enough album data for a meaningful signal.
    """
    try:
        if album and (album_lf_listeners is None or album_lb_listens is None):
            from services.popularity.popularity_stats_service import calculate_album_listener_stats
            _db_lf, _db_lb = calculate_album_listener_stats(None, artist, album)
            if album_lf_listeners is None:
                album_lf_listeners = _db_lf
            if album_lb_listens is None:
                album_lb_listens = _db_lb
        z_lf = log_listener_z(float(lastfm_listeners or 0), album_lf_listeners or [])
        z_lb = log_listener_z(float(listenbrainz_listens or 0), album_lb_listens or [])
        if not z_lf and not z_lb:
            return 0.0
        if not z_lb:
            return z_lf
        if not z_lf:
            return z_lb
        try:
            from services.popularity.popularity_config import resolve_weights
            w_lf, w_lb, _ = resolve_weights()
        except Exception:
            w_lf, w_lb = 0.6, 0.4
        total = w_lf + w_lb
        if total <= 0:
            return z_lf
        return (w_lf * z_lf + w_lb * z_lb) / total
    except Exception as exc:
        logger.debug(
            "Composite listener z failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return 0.0