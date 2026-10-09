"""Popularity cache policy helpers.

Determines when cached popularity data can be reused vs.
requiring a fresh API lookup. Uses age-based rules:
- Tracks older than *mature_track_min_age_years* (configurable, default 2)
  with existing scores are frozen.
- Recently looked-up tracks reuse cached Spotify scores.

Reduces API calls during repeated scans.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _get_mature_cutoff_years() -> int:
    """Read the mature-track freeze cutoff from config, default 2 years."""
    try:
        from helpers.config_helpers import get_feature
        return max(1, int(get_feature("mature_track_min_age_years", 2)))
    except Exception:
        return 2


def is_track_older_than_years(year: Any, years: int | None = None) -> bool:
    """Return True when a release year is at least *years* old.

    If *years* is None, reads from the ``mature_track_min_age_years``
    config key (default 2).

    ``year`` is stored as TEXT and may be ``"1995"`` or a full date like
    ``"1995-06-01"`` — the leading 4-digit year is parsed defensively.
    """
    if years is None:
        years = _get_mature_cutoff_years()
    if not year:
        return False
    try:
        if isinstance(year, int):
            release_year = year
        else:
            text_year = str(year).strip()
            if text_year[:4].isdigit():
                release_year = int(text_year[:4])
            else:
                return False
    except (TypeError, ValueError):
        return False
    return (datetime.now().year - release_year) >= years


def should_freeze_track(track: dict[str, Any]) -> bool:
    """Return True when a mature track with existing score should skip refresh.

    The minimum age threshold is read from config key
    ``features.mature_track_min_age_years`` (default 2 years).
    """
    cutoff = _get_mature_cutoff_years()
    return (
        is_track_older_than_years(track.get("year"), cutoff)
        and track.get("final_score")
    )


def _as_datetime(value):
    """Coerce a stored timestamp (datetime or ISO string) to a tz-aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def should_use_cached_score(track):
    """Return True when a cached score is fresh enough to reuse.

    Spotify lookups were removed from the popularity pipeline, so the legacy
    ``last_spotify_lookup`` column is never written.  This now checks the real
    per-source freshness timestamps (``lastfm_last_updated`` /
    ``listenbrainz_last_updated``) that ``track_stage`` writes, and treats the
    whole score as reusable when either source was refreshed within 7 days.
    """
    now = datetime.now().astimezone()
    for key in ("lastfm_last_updated", "listenbrainz_last_updated"):
        ts = _as_datetime(track.get(key))
        if ts is not None and (now - ts) < timedelta(days=7):
            return True
    return False


def get_cache_duration_hours(track_year: int | None = None) -> int:
    """Return cache TTL in hours based on track release age.

    Older releases change less frequently, so they can be cached longer:
    - 3+ years old: 168 hours (7 days)
    - 1-3 years old: 72 hours (3 days)
    - < 1 year or unknown: 24 hours (conservative)
    """
    if not track_year:
        return 24
    try:
        age_years = datetime.now().year - int(track_year)
        if age_years >= 3:
            return 168
        elif age_years >= 1:
            return 72
        else:
            return 24
    except (ValueError, TypeError):
        return 24


def singles_detection_is_fresh(track: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True when this track's singles verdict may be reused instead of re-derived.

    ``single_detection_last_updated`` is written ONLY by the singles block in
    ``track_stage`` (and cleared by the track page's "clear single detection"),
    so its presence means detection actually COMPLETED — whatever it concluded.
    The TTL is :func:`get_cache_duration_hours`, the same window the rest of
    this module uses.

    ⚠️ THE BUG THIS REPLACES: the freshness check ALSO required positive
    evidence — ``is_single`` or a matched source. That made "no" the one
    verdict that could never be cached: every NON-single was re-assessed on
    every pass, so Discogs and MusicBrainz ran again for each of them even
    though the answer could not change inside the TTL. On an already-scanned
    album that was tens of seconds of repeat network work *per track* (the
    reported "4 minutes for one album that had all recent scan data"), and it
    also disagreed with the album-level "unchanged" gate, which has always
    asked only whether the timestamp exists.

    An ERROR never stamps the timestamp, so a failed detection still retries.
    """
    raw = track.get("single_detection_last_updated")
    if not raw:
        return False

    ts = raw
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(ts, datetime):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    ttl_hours = get_cache_duration_hours(track.get("year") or track.get("release_year"))
    return (ref - ts).total_seconds() < ttl_hours * 3600