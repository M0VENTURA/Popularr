"""MusicBrainz upcoming and recently released album discovery.

Discovers MusicBrainz release groups from:

1. Artists already present in the local music collection.
2. A configurable global discovery pass for artists outside the collection.

All MusicBrainz API requests are routed through
``api_clients.musicbrainz_http.MusicBrainzHttpClient``.

This module does not perform direct HTTP requests, rate limiting, retries,
TLS handling, or HTTP response caching. Those responsibilities belong to the
shared MusicBrainz HTTP client.

Release groups are filtered, normalised, deduplicated by release-group MBID,
and persisted into the ``upcoming_releases`` table.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator

import structlog
from sqlalchemy import text

from api_clients.musicbrainz_http import (
    MusicBrainzHttpClient,
    escape_lucene_special_chars,
)
from db.engine import db_session

logger = structlog.get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

SOURCE_NAME = "MusicBrainz Daily Collection"
MBID_SOURCE_NAME = "musicbrainz_daily_scan"

_ALLOWED_PRIMARY_TYPES = frozenset(
    {
        "album",
        "ep",
        "single",
    }
)

_DISQUALIFYING_SECONDARY_TYPES = frozenset(
    {
        "live",
        "remix",
        "compilation",
    }
)

_MAX_MUSICBRAINZ_QUERY_RESULTS = 100
_MAX_COLLECTION_ARTISTS = 50_000
_MAX_DATE_WINDOW_DAYS = 365
_QUERY_DATE_MARGIN_DAYS = 90
_MAX_CONSECUTIVE_FAILURES = 20


# =============================================================================
# Shared MusicBrainz client
# =============================================================================

_mb_client: MusicBrainzHttpClient | None = None
_client_lock = threading.Lock()


def _get_mb_client() -> MusicBrainzHttpClient:
    """Return the shared MusicBrainz HTTP client instance."""
    global _mb_client

    if _mb_client is not None:
        return _mb_client

    with _client_lock:
        if _mb_client is None:
            _mb_client = MusicBrainzHttpClient(enabled=True)

    return _mb_client


# =============================================================================
# Background refresh state
# =============================================================================

_refresh_lock = threading.Lock()
_refresh_running = False

_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "total": 0,
    "current_artist": None,
    "updated_at": None,
    "last_stats": None,
    "last_error": None,
}


def _set_status(**changes: Any) -> None:
    """Apply changes to the thread-safe refresh status."""
    with _status_lock:
        _status.update(changes)
        _status["updated_at"] = datetime.now().isoformat(
            timespec="seconds"
        )


def get_refresh_status() -> dict[str, Any]:
    """Return a snapshot of the current MusicBrainz refresh status."""
    with _status_lock:
        return dict(_status)


def is_refresh_running() -> bool:
    """Return whether the background MusicBrainz refresh is active."""
    with _refresh_lock:
        return _refresh_running


# =============================================================================
# General helpers
# =============================================================================

_NON_ALPHANUMERIC_RE = re.compile(r"[^a-z0-9\s]+")
_MULTIPLE_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_artist(name: str) -> str:
    """Normalise an artist name for MusicBrainz credit comparison.

    This normalisation is used only for comparing MusicBrainz artist-credit
    names. It is not written back to the database.
    """
    if not name:
        return ""

    value = unicodedata.normalize("NFKD", str(name))

    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )

    value = value.lower()
    value = _NON_ALPHANUMERIC_RE.sub(" ", value)
    value = _MULTIPLE_WHITESPACE_RE.sub(" ", value)

    return value.strip()


def _database_comparison_key(value: str) -> str:
    """Build the equivalent of the database REGEXP_REPLACE comparison key."""
    return re.sub(
        r"[^a-zA-Z0-9]",
        "",
        str(value or ""),
    ).lower()


def _parse_release_date(raw: Any) -> str | None:
    """Validate a MusicBrainz release date.

    MusicBrainz may return:

    * YYYY-MM-DD
    * YYYY-MM
    * YYYY
    """
    if not raw:
        return None

    raw_value = str(raw).strip()

    for date_format in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            datetime.strptime(raw_value, date_format)
            return raw_value
        except ValueError:
            continue

    return None


def _normalize_release_date(raw: Any) -> str | None:
    """Expand a partial MusicBrainz date to a database-compatible ISO date."""
    parsed = _parse_release_date(raw)

    if not parsed:
        return None

    if len(parsed) == 4:
        return f"{parsed}-01-01"

    if len(parsed) == 7:
        return f"{parsed}-01"

    return parsed


def _release_year(raw: str | None) -> int | None:
    """Extract the release year from a normalised date."""
    if not raw:
        return None

    if len(raw) < 4 or not raw[:4].isdigit():
        return None

    return int(raw[:4])


def _month_end(value: date) -> date:
    """Return the final day of the month containing the supplied date."""
    if value.month == 12:
        next_month = value.replace(
            year=value.year + 1,
            month=1,
            day=1,
        )
    else:
        next_month = value.replace(
            month=value.month + 1,
            day=1,
        )

    return next_month - timedelta(days=1)


def _within_window(
    raw_date: str,
    minimum_date: date,
    maximum_date: date,
) -> bool:
    """Return whether a complete or partial MusicBrainz date overlaps a window."""
    try:
        if len(raw_date) >= 10:
            parsed_date = datetime.strptime(
                raw_date[:10],
                "%Y-%m-%d",
            ).date()

            return minimum_date <= parsed_date <= maximum_date

        if len(raw_date) == 7:
            parsed_month = datetime.strptime(
                raw_date,
                "%Y-%m",
            ).date()

            month_start = parsed_month.replace(day=1)
            month_finish = _month_end(parsed_month)

            return (
                month_start <= maximum_date
                and month_finish >= minimum_date
            )

        if len(raw_date) == 4:
            parsed_year = int(raw_date)

            return (
                minimum_date.year
                <= parsed_year
                <= maximum_date.year
            )

    except (TypeError, ValueError):
        return False

    return False


# =============================================================================
# Feature configuration
# =============================================================================

def _feature_int(key: str, default: int) -> int:
    """Read an integer feature setting with a safe fallback."""
    try:
        from helpers.config_helpers import get_feature

        value = get_feature(key, default)

        if value is None:
            return default

        return int(value)

    except Exception as exc:
        logger.debug(
            "Could not read integer feature setting",
            key=key,
            default=default,
            error=str(exc),
        )
        return default


def _feature_bool(key: str, default: bool) -> bool:
    """Read a Boolean feature setting with explicit string handling."""
    try:
        from helpers.config_helpers import get_feature

        value = get_feature(key, default)

    except Exception as exc:
        logger.debug(
            "Could not read Boolean feature setting",
            key=key,
            default=default,
            error=str(exc),
        )
        return default

    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalised = value.strip().lower()

        if normalised in {
            "true",
            "1",
            "yes",
            "on",
            "enabled",
        }:
            return True

        if normalised in {
            "false",
            "0",
            "no",
            "off",
            "disabled",
        }:
            return False

    return bool(value)


def _scan_enabled() -> bool:
    """Read the preferred scan setting with support for the legacy key."""
    try:
        from helpers.config_helpers import get_feature

        current_value = get_feature(
            "upcoming_releases_scan_enabled",
            None,
        )

        if current_value is not None:
            if isinstance(current_value, str):
                return current_value.strip().lower() in {
                    "true",
                    "1",
                    "yes",
                    "on",
                    "enabled",
                }

            return bool(current_value)

    except Exception as exc:
        logger.debug(
            "Could not read primary upcoming releases setting",
            error=str(exc),
        )

    return _feature_bool(
        "daily_musicbrainz_release_scan_enabled",
        True,
    )


# =============================================================================
# Artist helpers
# =============================================================================

def _release_group_artist(
    release_group: dict[str, Any],
) -> str:
    """Build a display artist from a MusicBrainz artist-credit array."""
    credits = release_group.get("artist-credit") or []
    parts: list[str] = []

    for credit in credits:
        if isinstance(credit, str):
            parts.append(credit)
            continue

        if not isinstance(credit, dict):
            continue

        artist_data = credit.get("artist") or {}

        name = (
            credit.get("name")
            or artist_data.get("name")
            or ""
        )

        join_phrase = credit.get("joinphrase") or ""

        if name:
            parts.append(str(name))

        if join_phrase:
            parts.append(str(join_phrase))

    return "".join(parts).strip()


def _release_group_credit_names(
    release_group: dict[str, Any],
) -> list[str]:
    """Return individual artist names from MusicBrainz artist credits."""
    names: list[str] = []

    for credit in release_group.get("artist-credit") or []:
        if isinstance(credit, str):
            credit_name = credit.strip()

            if credit_name:
                names.append(credit_name)

            continue

        if not isinstance(credit, dict):
            continue

        artist_data = credit.get("artist") or {}

        name = (
            artist_data.get("name")
            or credit.get("name")
            or ""
        )

        name = str(name).strip()

        if name:
            names.append(name)

    return names


def _collection_artists(limit: int) -> list[str]:
    """Return distinct album artists from the local tracks table."""
    safe_limit = max(
        1,
        min(int(limit), _MAX_COLLECTION_ARTISTS),
    )

    try:
        with db_session() as session:
            result = session.execute(
                text(
                    """
                    SELECT artist_name
                    FROM (
                        SELECT DISTINCT
                            COALESCE(
                                NULLIF(TRIM(album_artist), ''),
                                NULLIF(TRIM(artist), '')
                            ) AS artist_name,
                            LOWER(
                                COALESCE(
                                    NULLIF(TRIM(album_artist), ''),
                                    NULLIF(TRIM(artist), '')
                                )
                            ) AS artist_sort
                        FROM tracks
                        WHERE COALESCE(
                            NULLIF(TRIM(album_artist), ''),
                            NULLIF(TRIM(artist), '')
                        ) IS NOT NULL
                    ) AS artist_rows
                    WHERE artist_name IS NOT NULL
                      AND artist_name <> ''
                    ORDER BY artist_sort
                    LIMIT :limit
                    """
                ),
                {
                    "limit": safe_limit,
                },
            )

            return [
                str(row[0]).strip()
                for row in result.fetchall() or []
                if row[0] and str(row[0]).strip()
            ]

    except Exception as exc:
        logger.error(
            "Collection artist query failed",
            limit=safe_limit,
            error=str(exc),
        )
        return []


def _collection_artist_keys() -> set[str]:
    """Return punctuation-insensitive keys for all collection artists.

    The full set is loaded once for the global discovery pass rather than
    opening a database session for every globally discovered release.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    """
                    SELECT DISTINCT
                        LOWER(
                            REGEXP_REPLACE(
                                COALESCE(
                                    NULLIF(TRIM(album_artist), ''),
                                    NULLIF(TRIM(artist), '')
                                ),
                                '[^a-zA-Z0-9]',
                                '',
                                'g'
                            )
                        ) AS artist_key
                    FROM tracks
                    WHERE COALESCE(
                        NULLIF(TRIM(album_artist), ''),
                        NULLIF(TRIM(artist), '')
                    ) IS NOT NULL
                    """
                )
            )

            return {
                str(row[0]).strip()
                for row in result.fetchall() or []
                if row[0] and str(row[0]).strip()
            }

    except Exception as exc:
        logger.warning(
            "Collection artist key query failed",
            error=str(exc),
        )
        return set()


# =============================================================================
# MusicBrainz retrieval helpers
# =============================================================================

def _build_date_query_range(
    minimum_date: date,
    maximum_date: date,
) -> str:
    """Build an expanded MusicBrainz first-release-date query range."""
    margin = timedelta(days=_QUERY_DATE_MARGIN_DAYS)

    query_start = minimum_date - margin
    query_end = maximum_date + margin

    return (
        f"[{query_start.isoformat()} "
        f"TO {query_end.isoformat()}]"
    )


def _is_allowed_release_group(
    release_group: dict[str, Any],
) -> bool:
    """Return whether a release group has an allowed primary/secondary type."""
    primary_type = str(
        release_group.get("primary-type") or ""
    ).strip().lower()

    if primary_type not in _ALLOWED_PRIMARY_TYPES:
        return False

    secondary_types = {
        str(value).strip().lower()
        for value in release_group.get("secondary-types") or []
        if value
    }

    return not bool(
        secondary_types.intersection(
            _DISQUALIFYING_SECONDARY_TYPES
        )
    )


def _serialise_release_group(
    release_group: dict[str, Any],
    minimum_date: date,
    maximum_date: date,
    *,
    include_artist: bool,
) -> dict[str, Any] | None:
    """Validate and convert a MusicBrainz release group."""
    if not isinstance(release_group, dict):
        return None

    if not _is_allowed_release_group(release_group):
        return None

    release_group_mbid = str(
        release_group.get("id") or ""
    ).strip()

    title = str(
        release_group.get("title") or ""
    ).strip()

    release_date = _parse_release_date(
        release_group.get("first-release-date")
    )

    if not release_group_mbid:
        return None

    if not title:
        return None

    if not release_date:
        return None

    if not _within_window(
        release_date,
        minimum_date,
        maximum_date,
    ):
        return None

    item: dict[str, Any] = {
        "id": release_group_mbid,
        "title": title,
        "first_release_date": release_date,
        "primary_type": str(
            release_group.get("primary-type") or ""
        ).strip().lower(),
    }

    if include_artist:
        item["artist"] = _release_group_artist(
            release_group
        )

    return item


def _fetch_artist_release_groups(
    client: MusicBrainzHttpClient,
    artist: str,
    limit: int,
    minimum_date: date,
    maximum_date: date,
) -> list[dict[str, Any]]:
    """Fetch release groups belonging to a collection artist.

    The MusicBrainz API call passes exclusively through
    ``MusicBrainzHttpClient.search_release_groups``.
    """
    escaped_artist = escape_lucene_special_chars(artist)

    query_range = _build_date_query_range(
        minimum_date,
        maximum_date,
    )

    query = (
        f'artist:"{escaped_artist}" '
        "AND (primarytype:album "
        "OR primarytype:ep "
        "OR primarytype:single) "
        f"AND firstreleasedate:{query_range}"
    )

    search_limit = max(
        1,
        min(
            int(limit),
            _MAX_MUSICBRAINZ_QUERY_RESULTS,
        ),
    )

    raw_release_groups = client.search_release_groups(
        query,
        limit=search_limit,
    )

    requested_artist = _normalize_artist(artist)

    results: list[dict[str, Any]] = []
    seen_mbids: set[str] = set()

    for release_group in raw_release_groups or []:
        if not isinstance(release_group, dict):
            continue

        credit_names = _release_group_credit_names(
            release_group
        )

        if credit_names:
            normalised_credits = {
                _normalize_artist(name)
                for name in credit_names
                if name
            }

            if requested_artist not in normalised_credits:
                continue

        item = _serialise_release_group(
            release_group,
            minimum_date,
            maximum_date,
            include_artist=False,
        )

        if not item:
            continue

        release_group_mbid = item["id"]

        if release_group_mbid in seen_mbids:
            continue

        seen_mbids.add(release_group_mbid)
        results.append(item)

    return results


def _fetch_global_upcoming_release_groups(
    client: MusicBrainzHttpClient,
    limit: int,
    minimum_date: date,
    maximum_date: date,
) -> list[dict[str, Any]]:
    """Fetch globally discovered albums, EPs, and singles.

    Albums are requested first. EPs and singles use any remaining configured
    capacity. All calls pass through the shared MusicBrainz client.
    """
    safe_limit = max(
        1,
        min(
            int(limit),
            _MAX_MUSICBRAINZ_QUERY_RESULTS,
        ),
    )

    query_range = _build_date_query_range(
        minimum_date,
        maximum_date,
    )

    results: list[dict[str, Any]] = []
    seen_mbids: set[str] = set()

    for primary_type in ("album", "ep", "single"):
        remaining = safe_limit - len(results)

        if remaining <= 0:
            break

        query = (
            f"primarytype:{primary_type} "
            f"AND firstreleasedate:{query_range}"
        )

        raw_release_groups = client.search_release_groups(
            query,
            limit=remaining,
        )

        for release_group in raw_release_groups or []:
            item = _serialise_release_group(
                release_group,
                minimum_date,
                maximum_date,
                include_artist=True,
            )

            if not item:
                continue

            if not item.get("artist"):
                continue

            release_group_mbid = item["id"]

            if release_group_mbid in seen_mbids:
                continue

            seen_mbids.add(release_group_mbid)
            results.append(item)

            if len(results) >= safe_limit:
                break

    results.sort(
        key=lambda item: (
            item.get("first_release_date") or ""
        ),
        reverse=True,
    )

    return results[:safe_limit]


# =============================================================================
# Database SQL
# =============================================================================

# ---------------------------------------------------------------------------
# Dedup identity: (normalised artist, normalised album, primary type).
#
# A same-titled SINGLE and ALBUM (the "album named after its lead single"
# pattern) are two distinct MusicBrainz release groups and must live in two
# rows — keying on artist+album alone collapsed them into one and the
# earliest-date rule discarded the album's future date, hiding the upcoming
# album.  Untyped rows (Wikipedia, legacy data) are still absorbable: a
# typed MusicBrainz write first matches its own typed row, then falls back
# to the untyped row for the same artist+album.
# ---------------------------------------------------------------------------

_FIND_EXISTING_RELEASE_SQL = text(
    """
    SELECT id
    FROM upcoming_releases
    WHERE LOWER(
              REGEXP_REPLACE(
                  artist_name,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          ) = LOWER(
              REGEXP_REPLACE(
                  :artist,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          )
      AND LOWER(
              REGEXP_REPLACE(
                  album_name,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          ) = LOWER(
              REGEXP_REPLACE(
                  :album,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          )
      AND LOWER(
              COALESCE(
                  TRIM(primary_type),
                  ''
              )
          ) = :ptype
    LIMIT 1
    """
)

_FIND_LEGACY_RELEASE_SQL = text(
    """
    SELECT id
    FROM upcoming_releases
    WHERE LOWER(
              REGEXP_REPLACE(
                  artist_name,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          ) = LOWER(
              REGEXP_REPLACE(
                  :artist,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          )
      AND LOWER(
              REGEXP_REPLACE(
                  album_name,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          ) = LOWER(
              REGEXP_REPLACE(
                  :album,
                  '[^a-zA-Z0-9]',
                  '',
                  'g'
              )
          )
      AND COALESCE(
              NULLIF(
                  TRIM(primary_type),
                  ''
              ),
              ''
          ) = ''
    LIMIT 1
    """
)

_UPDATE_TYPED_RELEASE_SQL = text(
    """
    UPDATE upcoming_releases
    SET
        last_seen_at = CURRENT_TIMESTAMP,
        source = :source,
        primary_type = LOWER(
            COALESCE(
                NULLIF(:primary_type, ''),
                upcoming_releases.primary_type
            )
        ),

        release_date = COALESCE(
            :release_date,
            upcoming_releases.release_date
        ),

        release_year = COALESCE(
            :release_year,
            upcoming_releases.release_year
        ),

        artist_in_collection = (
            COALESCE(
                upcoming_releases.artist_in_collection,
                FALSE
            )
            OR :artist_in_collection
        ),

        release_group_mbid = COALESCE(
            :release_group_mbid,
            upcoming_releases.release_group_mbid
        ),

        mbid_match_status = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_match_status
            ELSE 'matched'
        END,

        mbid_confidence = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_confidence
            ELSE 'high'
        END,

        mbid_source = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_source
            ELSE :mbid_source
        END,

        mbid_match_score = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_match_score
            ELSE 1.0
        END,

        mbid_last_checked_at = :checked_at,
        updated_at = CURRENT_TIMESTAMP

    WHERE id = :id
    """
)


_UPDATE_RELEASE_SQL = text(
    """
    UPDATE upcoming_releases
    SET
        last_seen_at = CURRENT_TIMESTAMP,
        source = :source,

        primary_type = COALESCE(
            NULLIF(:primary_type, ''),
            upcoming_releases.primary_type
        ),

        release_date = CASE
            WHEN upcoming_releases.release_date IS NULL
                THEN :release_date
            WHEN :release_date IS NULL
                THEN upcoming_releases.release_date
            WHEN :release_date < upcoming_releases.release_date
                THEN :release_date
            ELSE upcoming_releases.release_date
        END,

        release_year = COALESCE(
            :release_year,
            upcoming_releases.release_year
        ),

        artist_in_collection = (
            COALESCE(
                upcoming_releases.artist_in_collection,
                FALSE
            )
            OR :artist_in_collection
        ),

        release_group_mbid = COALESCE(
            :release_group_mbid,
            upcoming_releases.release_group_mbid
        ),

        mbid_match_status = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_match_status
            ELSE 'matched'
        END,

        mbid_confidence = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_confidence
            ELSE 'high'
        END,

        mbid_source = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_source
            ELSE :mbid_source
        END,

        mbid_match_score = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_match_score
            ELSE 1.0
        END,

        mbid_last_checked_at = :checked_at,
        updated_at = CURRENT_TIMESTAMP

    WHERE id = :id
    """
)


_INSERT_RELEASE_SQL = text(
    """
    INSERT INTO upcoming_releases (
        artist_name,
        album_name,
        release_date,
        release_year,
        source,
        primary_type,
        artist_in_collection,
        release_group_mbid,
        mbid_match_status,
        mbid_source,
        mbid_confidence,
        mbid_match_score,
        mbid_last_checked_at,
        status,
        last_seen_at,
        updated_at
    )
    VALUES (
        :artist,
        :album,
        :release_date,
        :release_year,
        :source,
        COALESCE(
            NULLIF(:primary_type, ''),
            ''
        ),
        :artist_in_collection,
        :release_group_mbid,
        'matched',
        :mbid_source,
        'high',
        1.0,
        :checked_at,
        'discovered',
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    )

    ON CONFLICT (artist_name, album_name, primary_type)
    DO UPDATE SET
        last_seen_at = CURRENT_TIMESTAMP,
        source = EXCLUDED.source,

        primary_type = COALESCE(
            EXCLUDED.primary_type,
            upcoming_releases.primary_type
        ),

        release_date = CASE
            WHEN upcoming_releases.release_date IS NULL
                THEN EXCLUDED.release_date
            WHEN EXCLUDED.release_date IS NULL
                THEN upcoming_releases.release_date
            WHEN EXCLUDED.release_date
                 < upcoming_releases.release_date
                THEN EXCLUDED.release_date
            ELSE upcoming_releases.release_date
        END,

        release_year = COALESCE(
            EXCLUDED.release_year,
            upcoming_releases.release_year
        ),

        artist_in_collection = (
            COALESCE(
                upcoming_releases.artist_in_collection,
                FALSE
            )
            OR EXCLUDED.artist_in_collection
        ),

        release_group_mbid = COALESCE(
            EXCLUDED.release_group_mbid,
            upcoming_releases.release_group_mbid
        ),

        mbid_match_status = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_match_status
            ELSE EXCLUDED.mbid_match_status
        END,

        mbid_confidence = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_confidence
            ELSE EXCLUDED.mbid_confidence
        END,

        mbid_source = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_source
            ELSE EXCLUDED.mbid_source
        END,

        mbid_match_score = CASE
            WHEN COALESCE(
                upcoming_releases.mbid_manual_override,
                FALSE
            )
                THEN upcoming_releases.mbid_match_score
            ELSE EXCLUDED.mbid_match_score
        END,

        mbid_last_checked_at = EXCLUDED.mbid_last_checked_at,
        updated_at = CURRENT_TIMESTAMP
    """
)


# =============================================================================
# Persistence helpers
# =============================================================================

def _prepare_release_for_persistence(
    artist: str,
    release: dict[str, Any],
    *,
    artist_in_collection: bool,
) -> dict[str, Any] | None:
    """Sanitise one MusicBrainz release for database persistence."""
    from services.upcoming_releases.matching_service import (
        sanitize_wiki_entry,
    )

    cleaned_artist, cleaned_album = sanitize_wiki_entry(
        artist,
        release.get("title") or "",
    )

    cleaned_artist = str(cleaned_artist or "").strip()
    cleaned_album = str(cleaned_album or "").strip()

    if not cleaned_artist or not cleaned_album:
        return None

    normalised_release_date = _normalize_release_date(
        release.get("first_release_date")
    )

    if not normalised_release_date:
        return None

    release_group_mbid = str(
        release.get("id") or ""
    ).strip() or None

    primary_type = str(
        release.get("primary_type") or ""
    ).strip().lower()

    checked_at = datetime.now().isoformat(
        timespec="seconds"
    )

    return {
        "artist": cleaned_artist,
        "album": cleaned_album,
        "release_date": normalised_release_date,
        "release_year": _release_year(
            normalised_release_date
        ),
        "source": SOURCE_NAME,
        "primary_type": primary_type,
        "artist_in_collection": bool(
            artist_in_collection
        ),
        "release_group_mbid": release_group_mbid,
        "mbid_source": MBID_SOURCE_NAME,
        "checked_at": checked_at,
    }


def _persist_releases(
    release_entries: Iterable[
        tuple[str, dict[str, Any], bool]
    ],
) -> tuple[int, int]:
    """Persist collection and global releases through one shared path.

    Each iterable item contains:

        artist name,
        release dictionary,
        artist-in-collection flag
    """
    inserted = 0
    updated = 0

    try:
        with db_session() as session:
            for (
                artist,
                release,
                artist_in_collection,
            ) in release_entries:
                values = _prepare_release_for_persistence(
                    artist,
                    release,
                    artist_in_collection=artist_in_collection,
                )

                if not values:
                    continue

                ptype = str(
                    values.get("primary_type") or ""
                ).strip().lower()

                lookup_params = {
                    "artist": values["artist"],
                    "album": values["album"],
                    "ptype": ptype,
                }

                existing = session.execute(
                    _FIND_EXISTING_RELEASE_SQL,
                    lookup_params,
                ).fetchone()

                if existing:
                    # Same-typed row: MusicBrainz is the authority for its
                    # own typed release group, so refresh the date (the
                    # earliest-date merge belongs only to the legacy
                    # untyped absorb below).
                    session.execute(
                        _UPDATE_TYPED_RELEASE_SQL,
                        {
                            **values,
                            "id": existing[0],
                        },
                    )

                    updated += 1
                    continue

                existing = session.execute(
                    _FIND_LEGACY_RELEASE_SQL,
                    lookup_params,
                ).fetchone()

                if existing:
                    # Untyped legacy/Wikipedia row: absorb it (earliest-date
                    # merge semantics preserved).
                    session.execute(
                        _UPDATE_RELEASE_SQL,
                        {
                            **values,
                            "id": existing[0],
                        },
                    )

                    updated += 1
                    continue

                session.execute(
                    _INSERT_RELEASE_SQL,
                    values,
                )

                inserted += 1

        return inserted, updated

    except Exception as exc:
        logger.error(
            "MusicBrainz release persistence failed",
            inserted_before_failure=inserted,
            updated_before_failure=updated,
            error=str(exc),
        )

        return 0, 0


def _persist_artist_releases(
    artist: str,
    releases: list[dict[str, Any]],
) -> tuple[int, int]:
    """Persist releases found for an artist already in the collection."""
    if not releases:
        return 0, 0

    entries = (
        (
            artist,
            release,
            True,
        )
        for release in releases
    )

    return _persist_releases(entries)


def _global_persistence_entries(
    releases: list[dict[str, Any]],
    collection_keys: set[str],
) -> Iterator[tuple[str, dict[str, Any], bool]]:
    """Generate persistence entries for globally discovered releases."""
    for release in releases:
        artist = str(
            release.get("artist") or ""
        ).strip()

        if not artist:
            continue

        artist_key = _database_comparison_key(artist)

        yield (
            artist,
            release,
            artist_key in collection_keys,
        )


def _persist_global_releases(
    releases: list[dict[str, Any]],
) -> tuple[int, int]:
    """Persist globally discovered MusicBrainz releases."""
    if not releases:
        return 0, 0

    collection_keys = _collection_artist_keys()

    entries = _global_persistence_entries(
        releases,
        collection_keys,
    )

    return _persist_releases(entries)


# =============================================================================
# Main refresh
# =============================================================================

def fetch_musicbrainz_upcoming_releases(
    artists_limit: int | None = None,
    per_artist_limit: int | None = None,
    lookback_days: int | None = None,
    lookahead_days: int | None = None,
) -> dict[str, Any]:
    """Run global and collection-specific MusicBrainz discovery."""
    if not _scan_enabled():
        skipped_result = {
            "skipped": True,
            "reason": "upcoming_releases_scan_enabled is false",
        }

        _set_status(
            status="idle",
            current_artist=None,
            last_stats=skipped_result,
            last_error=None,
        )

        return skipped_result

    configured_artist_limit = (
        artists_limit
        if artists_limit is not None
        else _feature_int(
            "daily_musicbrainz_release_max_artists",
            _MAX_COLLECTION_ARTISTS,
        )
    )

    configured_per_artist_limit = (
        per_artist_limit
        if per_artist_limit is not None
        else _feature_int(
            "daily_musicbrainz_release_per_artist_limit",
            _MAX_MUSICBRAINZ_QUERY_RESULTS,
        )
    )

    configured_lookback = (
        lookback_days
        if lookback_days is not None
        else _feature_int(
            "daily_musicbrainz_release_lookback_days",
            42,
        )
    )

    configured_lookahead = (
        lookahead_days
        if lookahead_days is not None
        else _feature_int(
            "daily_musicbrainz_release_lookahead_days",
            120,
        )
    )

    safe_artist_limit = max(
        1,
        min(
            int(configured_artist_limit),
            _MAX_COLLECTION_ARTISTS,
        ),
    )

    safe_per_artist_limit = max(
        1,
        min(
            int(configured_per_artist_limit),
            _MAX_MUSICBRAINZ_QUERY_RESULTS,
        ),
    )

    safe_lookback = max(
        1,
        min(
            int(configured_lookback),
            _MAX_DATE_WINDOW_DAYS,
        ),
    )

    safe_lookahead = max(
        1,
        min(
            int(configured_lookahead),
            _MAX_DATE_WINDOW_DAYS,
        ),
    )

    today = datetime.now().date()

    minimum_date = today - timedelta(
        days=safe_lookback
    )

    maximum_date = today + timedelta(
        days=safe_lookahead
    )

    stats: dict[str, Any] = {
        "artists_scanned": 0,
        "artists_failed": 0,
        "inserted": 0,
        "updated": 0,
        "global_candidates": 0,
        "global_inserted": 0,
        "global_updated": 0,
        "aborted": False,
    }

    client = _get_mb_client()

    _set_status(
        status="starting",
        progress=0,
        total=0,
        current_artist=None,
        last_stats=None,
        last_error=None,
    )

    # =========================================================================
    # Global discovery pass
    # =========================================================================

    try:
        global_limit = _feature_int(
            "daily_musicbrainz_release_global_limit",
            50,
        )

        if global_limit > 0:
            global_releases = (
                _fetch_global_upcoming_release_groups(
                    client,
                    global_limit,
                    minimum_date,
                    maximum_date,
                )
            )

            stats["global_candidates"] = len(
                global_releases
            )

            (
                global_inserted,
                global_updated,
            ) = _persist_global_releases(
                global_releases
            )

            stats["global_inserted"] = global_inserted
            stats["global_updated"] = global_updated
            stats["inserted"] += global_inserted
            stats["updated"] += global_updated

            logger.info(
                "Global MusicBrainz discovery complete",
                candidates=len(global_releases),
                inserted=global_inserted,
                updated=global_updated,
            )

    except Exception as exc:
        logger.warning(
            "Global MusicBrainz discovery failed",
            error=str(exc),
        )

    # =========================================================================
    # Collection artist pass
    # =========================================================================

    artists = _collection_artists(
        safe_artist_limit
    )

    if not artists:
        stats["skipped"] = True
        stats["reason"] = "no collection artists"

        _set_status(
            status="idle",
            progress=0,
            total=0,
            current_artist=None,
            last_stats=dict(stats),
            last_error=None,
        )

        return stats

    total_artists = len(artists)
    failed_artists: list[str] = []
    consecutive_failures = 0

    _set_status(
        status="running",
        progress=0,
        total=total_artists,
        current_artist=None,
        last_error=None,
    )

    try:
        for artist in artists:
            stats["artists_scanned"] += 1

            try:
                release_groups = (
                    _fetch_artist_release_groups(
                        client,
                        artist,
                        safe_per_artist_limit,
                        minimum_date,
                        maximum_date,
                    )
                )

                inserted, updated = (
                    _persist_artist_releases(
                        artist,
                        release_groups,
                    )
                )

                stats["inserted"] += inserted
                stats["updated"] += updated

                consecutive_failures = 0

            except Exception as exc:
                consecutive_failures += 1
                failed_artists.append(artist)

                stats["artists_failed"] = len(
                    failed_artists
                )

                if len(failed_artists) == 1:
                    logger.warning(
                        "MusicBrainz artist refresh failed",
                        artist=artist,
                        consecutive_failures=consecutive_failures,
                        error=str(exc),
                    )
                else:
                    logger.debug(
                        "MusicBrainz artist refresh failed",
                        artist=artist,
                        consecutive_failures=consecutive_failures,
                        error=str(exc),
                    )

                if (
                    consecutive_failures
                    >= _MAX_CONSECUTIVE_FAILURES
                ):
                    stats["aborted"] = True
                    stats["abort_reason"] = (
                        f"{consecutive_failures} "
                        "consecutive failures"
                    )

                    logger.warning(
                        "Aborting MusicBrainz refresh",
                        consecutive_failures=(
                            consecutive_failures
                        ),
                        last_artist=artist,
                    )

                    break

            finally:
                _set_status(
                    status="running",
                    progress=stats["artists_scanned"],
                    total=total_artists,
                    current_artist=artist,
                    stats=dict(stats),
                )

    finally:
        stats["artists_failed"] = len(
            failed_artists
        )

        _set_status(
            status="idle",
            progress=stats["artists_scanned"],
            total=total_artists,
            current_artist=None,
            last_stats=dict(stats),
        )

        try:
            client.clear_caches()

        except Exception as exc:
            logger.debug(
                "Could not clear MusicBrainz HTTP caches",
                error=str(exc),
            )

    if failed_artists:
        logger.warning(
            "MusicBrainz refresh completed with artist failures",
            failed_count=len(failed_artists),
            total_artists=total_artists,
            inserted=stats["inserted"],
            updated=stats["updated"],
            aborted=stats["aborted"],
        )
    else:
        logger.info(
            "MusicBrainz refresh complete",
            stats=stats,
        )

    return stats


# =============================================================================
# Background execution
# =============================================================================

def start_musicbrainz_refresh() -> bool:
    """Start the MusicBrainz refresh in a daemon thread.

    Returns False if another refresh is already running.
    """
    global _refresh_running

    with _refresh_lock:
        if _refresh_running:
            return False

        _refresh_running = True

    def _run() -> None:
        global _refresh_running

        try:
            stats = fetch_musicbrainz_upcoming_releases()

            _set_status(
                status="idle",
                current_artist=None,
                last_stats=(
                    stats
                    if isinstance(stats, dict)
                    else None
                ),
                last_error=None,
            )

        except Exception as exc:
            logger.exception(
                "Background MusicBrainz refresh failed",
                error=str(exc),
            )

            _set_status(
                status="error",
                current_artist=None,
                last_error=str(exc),
            )

        finally:
            with _refresh_lock:
                _refresh_running = False

    refresh_thread = threading.Thread(
        target=_run,
        daemon=True,
        name="upcoming-musicbrainz-refresh",
    )

    refresh_thread.start()

    return True


__all__ = [
    "fetch_musicbrainz_upcoming_releases",
    "get_refresh_status",
    "is_refresh_running",
    "start_musicbrainz_refresh",
]
