"""Artist scan service.

Responsible for external release comparison and scan orchestration.
DB persistence is delegated to repositories; network calls go through
the shared MusicBrainz client singleton.

Concurrency and outage behaviour:
- The full-library sweep visits every artist and browses MusicBrainz for
  each one. Running it while a popularity scan is active puts two
  independent consumers on the same 1 req/s MusicBrainz budget, which
  drove the server to 503 within seconds. The sweep now refuses to start
  while a popularity scan is running, and pauses if one starts mid-sweep.
- When the MusicBrainz client reports itself unavailable (circuit breaker
  open), the sweep waits instead of racing through the remaining artists
  turning every one into an empty result.
- An empty MusicBrainz response is never persisted as "nothing missing"
  unless the lookup genuinely succeeded. Persisting on failure deleted the
  artist's cached rows and silently emptied the missing-releases list.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.metadata import (
    fetch_artist_albums,
    fetch_artist_mbid,
)
from helpers.config_helpers import get_feature
from helpers.normalization_service import normalize_title_for_lookup
from helpers.musicbrainz_helpers import normalize_single_mbid
from services.enrichment.musicbrainz_service import get_shared_mb_client

logger = structlog.get_logger(__name__)

_PROGRESS_PATH = "missing_releases_scan_progress.json"
_scan_thread: threading.Thread | None = None
_scan_lock = threading.Lock()

# Pause/backoff tuning for the full-library sweep.
_ARTIST_DELAY_SECONDS = 1.1
_UNAVAILABLE_WAIT_SECONDS = 30.0
_UNAVAILABLE_MAX_WAITS = 20  # ~10 minutes before giving up on the sweep
_POPULARITY_WAIT_SECONDS = 60.0
_POPULARITY_MAX_WAITS = 120  # ~2 hours before giving up on the sweep

_popularity_probe_warned = False

# Candidate accessors for "is a popularity scan running?", tried in order.
#
# Only ONE accessor exists in this build. The four legacy candidates this list
# used to carry (``services.scanning.scan_state.is_popularity_scan_running``,
# ``services.scanning.scan_state.is_scan_running``,
# ``services.popularity.scan_state.is_scan_running`` and
# ``services.scheduler.scheduler_service.is_popularity_scan_active``) were all
# permanently dead: none of those modules/attributes has ever existed, because
# ``scheduler_service`` imports the function lazily inside a function body. The
# probe therefore NEVER resolved, so the sweep always concluded "no popularity
# scan is running" — including while one genuinely was — and ran concurrently
# with it against the shared 1 req/s MusicBrainz budget.
#
# The list remains a list so that a future move of the accessor can be covered
# by appending a candidate; the import/getattr probing below still degrades to a
# single warning rather than raising.
_CANONICAL_POPULARITY_ACCESSOR = (
    "services.scanning.pipelines.popularity_pipeline",
    "is_popularity_scan_active",
)
_POPULARITY_SCAN_PROBES: tuple[tuple[str, str], ...] = (
    _CANONICAL_POPULARITY_ACCESSOR,
)


# ---------------------------------------------------------------------------
# Cross-scan coordination
# ---------------------------------------------------------------------------

def _popularity_scan_active() -> bool:
    """Return True when a popularity scan is currently running.

    The accessor differs between builds, so several known entry points are
    probed and the result degrades to "not running" when none is available.
    That keeps this inert rather than blocking the sweep on a wrong guess.
    """
    global _popularity_probe_warned

    probes = _POPULARITY_SCAN_PROBES
    canonical_name = ".".join(_CANONICAL_POPULARITY_ACCESSOR)

    for module_name, attribute in probes:
        try:
            module = __import__(module_name, fromlist=[attribute])
        except Exception:
            continue
        checker = getattr(module, attribute, None)
        if not callable(checker):
            continue
        try:
            return bool(checker())
        except Exception as exc:
            logger.debug(
                "Popularity scan probe raised",
                probe=f"{module_name}.{attribute}",
                error=str(exc),
            )

    if not _popularity_probe_warned:
        _popularity_probe_warned = True
        logger.warning(
            "Cannot determine whether a popularity scan is active",
            reason=(
                "no known accessor found; the missing-releases sweep cannot "
                "serialise itself against the popularity scan"
            ),
            probed=[f"{m}.{a}" for m, a in probes],
            canonical=canonical_name,
        )
    return False


def _musicbrainz_available() -> bool:
    """Return False when the MusicBrainz client reports itself unavailable."""
    try:
        client = get_shared_mb_client()
    except Exception:
        return True
    checker = getattr(client, "is_available", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_release_name(album_name: str) -> str:
    """Strips '(Topshelf Edition)', '[Deluxe Version]', etc. for exact API matches."""
    if not album_name:
        return ""
    cleaned = re.sub(
        r'\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]',
        '',
        album_name,
        flags=re.IGNORECASE
    ).strip()
    return cleaned if cleaned else album_name


def _normalize_release_title(title: str) -> str:
    # First sanitize retail editions out, then let standard normalizer run
    clean_title = _sanitize_release_name(title)
    return normalize_title_for_lookup(clean_title or "")


def _fetch_musicbrainz_release_groups(artist_mbid: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Browse release-groups for an artist via the shared MusicBrainz client."""
    client = get_shared_mb_client()
    page = client.browse_artist_release_groups(
        artist_mbid, limit=limit, offset=offset,
    )
    return page.get("release_groups", []) or []


def _fetch_all_musicbrainz_releases(
    artist_mbid: str,
    max_pages: int = 4,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch all release-groups for an artist, paging through the browse endpoint.

    Returns ``(release_groups, ok)``. ``ok`` is False when the lookup could not
    be completed (client unavailable or the first page failed), so callers can
    tell "this artist has no release groups" apart from "we could not ask".
    That distinction matters because persisting the former deletes the
    artist's cached missing releases.
    """
    if not _musicbrainz_available():
        logger.info(
            "MusicBrainz release-group browse skipped",
            reason="MusicBrainz reported unavailable",
            artist_mbid=artist_mbid,
        )
        return [], False

    releases: list[dict[str, Any]] = []
    offset = 0
    for page_index in range(max_pages):
        try:
            page = _fetch_musicbrainz_release_groups(artist_mbid, limit=100, offset=offset)
        except Exception as exc:
            logger.warning(
                "MusicBrainz release-group page failed",
                artist_mbid=artist_mbid,
                offset=offset,
                error=str(exc),
            )
            # A later page failing still leaves usable data from earlier pages.
            return releases, bool(releases)

        if not page:
            # An empty FIRST page while the client is healthy is a genuine
            # "this artist has nothing" answer; an empty first page right
            # after the breaker tripped is not.
            if page_index == 0 and not _musicbrainz_available():
                return [], False
            break

        releases.extend(page)
        if len(page) < 100:
            break
        offset += len(page)

    return releases, True


def _categorize_release(release_group: dict[str, Any]) -> str:
    """Route a release-group into a display category.

    Returns a canonical category KEY (e.g. ``field_recording``), not a display
    label — ``services.catalog.release_categories`` owns the key set and the
    labels.  Delegating fixes the bug this function had: only ``live`` /
    ``compilation`` / ``remix`` were recognised, so ``Album + Field recording``
    and ``Album + DJ-mix + Mixtape/Street`` fell through to the STUDIO bucket.
    """
    from services.catalog.release_categories import category_for_musicbrainz

    primary = release_group.get("primary-type") or release_group.get("primary_type") or ""
    secondary = (
        release_group.get("secondary-types")
        or release_group.get("secondary_types")
        or []
    )
    return category_for_musicbrainz(str(primary), secondary)


def _release_cover_art_url(release_group: dict[str, Any]) -> str:
    """Build a Cover Art Archive URL for a release-group when artwork exists."""
    rg_id = release_group.get("id") or ""
    if not rg_id:
        return ""
    caa = release_group.get("cover-art-archive") or {}
    if caa.get("artwork") or caa.get("count", 0) > 0:
        return f"https://coverartarchive.org/release-group/{rg_id}/front-500"
    return ""


def _build_missing_release_items(
    release_groups: list[dict[str, Any]],
    existing_norm: set[str],
    include_singles_current_year_only: bool = False,
    library_track_titles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter release-groups into missing-release items.

    ``library_track_titles`` (optional) holds normalised titles of tracks the
    artist already has in the library.  A SINGLE whose track is present on a
    library album is NOT missing — e.g. the "Queen Dies" single is already
    covered by "The Realms of Fire and Death", so it must not appear as a
    missing single.
    """
    now_year = datetime.now().year
    library_tracks = library_track_titles or set()
    missing: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for rg in release_groups:
        title = rg.get("title") or ""
        norm_title = _normalize_release_title(title)
        if not norm_title or norm_title in existing_norm:
            continue

        primary_type = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
        if primary_type not in ("album", "ep", "single"):
            continue

        category = _categorize_release(rg)

        if category == "Single" and include_singles_current_year_only:
            first_release = (rg.get("first-release-date") or rg.get("first_release_date") or "")
            try:
                release_year = int(first_release.split("-")[0])
            except (ValueError, TypeError):
                release_year = 0
            if release_year < now_year:
                continue

        # A single whose track is already on a library album is not missing.
        if category == "Single" and norm_title in library_tracks:
            continue

        dedupe_key = (norm_title, category)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        release_id = rg.get("id", "") or f"{norm_title}-{category.lower()}"
        missing.append({
            "id": release_id,
            "title": title,
            "primary_type": rg.get("primary-type", rg.get("primary_type", "")),
            "first_release_date": rg.get("first-release-date", rg.get("first_release_date", "")),
            "cover_art_url": _release_cover_art_url(rg),
            "category": category,
        })

    return missing


def _persist_missing_releases(artist: str, missing_items: list[dict[str, Any]]) -> None:
    """Replace the artist's cached missing releases in the DB (delete + insert).

    Callers must only reach this after a SUCCESSFUL MusicBrainz lookup. The
    delete is unconditional, so calling it with an empty list after a failed
    lookup silently wipes the artist's cached rows.

    ── WHY THE EXISTING TRACKLISTS ARE CARRIED OVER ────────────────────────
    This is a DELETE + INSERT of the same release rows on every sweep, and the
    tracklist column is the one expensive thing attached to them: filling it
    costs up to three MusicBrainz calls per release (see
    ``backfill_missing_release_tracklists``). Re-inserting without it threw
    that work away on every scan, so the cache could never reach a state where
    a tracklist was already present — ``populate_missing_release_tracklists``
    only ever selects rows whose tracklist is NULL/empty, so it re-fetched the
    same releases forever and the artist page never had a cached list to serve.
    """
    if not artist:
        return

    preserved: dict[str, str] = {}
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT release_id, tracklist
                    FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND tracklist IS NOT NULL
                      AND tracklist <> ''
                      AND tracklist <> '[]'
                """),
                {"artist": artist},
            ).fetchall() or []
        preserved = {
            str(r[0]): str(r[1])
            for r in rows
            if r[0] and r[1]
        }
    except Exception as exc:
        logger.debug("Could not read existing tracklists", artist=artist, error=str(exc))

    with db_session() as session:
        session.execute(
            text("DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(:artist)"),
            {"artist": artist},
        )
        for item in missing_items:
            release_id = str(item.get("id", "") or "")
            session.execute(
                text("""
                    INSERT INTO missing_releases
                        (artist, release_id, title, primary_type, first_release_date,
                         cover_art_url, category, tracklist, last_checked)
                    VALUES (:artist, :release_id, :title, :primary_type,
                            :first_release_date, :cover_art_url, :category,
                            :tracklist, CURRENT_TIMESTAMP)
                """),
                {
                    "artist": artist,
                    "release_id": release_id,
                    "title": item.get("title", ""),
                    "primary_type": item.get("primary_type", "Album"),
                    "first_release_date": item.get("first_release_date", ""),
                    "cover_art_url": item.get("cover_art_url", ""),
                    "category": item.get("category", "Album"),
                    "tracklist": preserved.get(release_id),
                },
            )


# ---------------------------------------------------------------------------
# Missing-release tracklists
#
# The artist page's per-release tracklist is served from the cached
# ``missing_releases.tracklist`` column whenever it is populated, and only hits
# MusicBrainz when it is not. Filling that cache is deliberately BACKGROUND work
# rather than part of the artist scan, for the reason measured below.
# ---------------------------------------------------------------------------

#: How many missing releases per artist the sweep will fill a tracklist for.
#: 0 disables the backfill entirely. Config: features.missing_release_tracklist_limit
_DEFAULT_TRACKLIST_BACKFILL_LIMIT = 10


def get_tracklist_backfill_limit() -> int:
    """Per-artist cap on tracklist backfill work. 0 disables it."""
    try:
        raw = get_feature("missing_release_tracklist_limit", _DEFAULT_TRACKLIST_BACKFILL_LIMIT)
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_TRACKLIST_BACKFILL_LIMIT
    return max(0, min(200, value))


def _tracklist_titles_from_release(release: dict[str, Any]) -> list[str]:
    """Flatten a MusicBrainz release payload into a list of track titles."""
    titles: list[str] = []
    for medium in (release.get("media") or []):
        if not isinstance(medium, dict):
            continue
        for track in (medium.get("tracks") or []):
            if not isinstance(track, dict):
                continue
            recording = track.get("recording") if isinstance(track.get("recording"), dict) else {}
            title = str(
                recording.get("title") or track.get("title") or ""
            ).strip()
            if title:
                titles.append(title)
    return titles


def _cache_missing_release_tracklist(artist: str, release_id: str, titles: list[str]) -> None:
    if not release_id or not titles:
        return
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE missing_releases
                    SET tracklist = :tracklist, last_checked = CURRENT_TIMESTAMP
                    WHERE release_id = :release_id
                      AND (:artist = '' OR LOWER(artist) = LOWER(:artist))
                """),
                {
                    "tracklist": json.dumps(titles, ensure_ascii=False),
                    "release_id": release_id,
                    "artist": artist or "",
                },
            )
    except Exception as exc:
        logger.debug(
            "Could not cache missing-release tracklist",
            release_id=release_id,
            error=str(exc),
        )


def fetch_missing_release_tracklist(
    release_id: str,
    artist: str = "",
) -> list[str]:
    """Track titles for one cached missing release — DB cache first.

    ``missing_releases.release_id`` holds a MusicBrainz **release-GROUP** id
    (``_build_missing_release_items`` stores ``rg["id"]``), NOT a release id.
    A bare ``get_release(release_group_id)`` therefore 404s, which is why the
    previous cache-filler silently fetched nothing: every fetch raised, was
    swallowed, and left the row's tracklist NULL forever. Resolution goes via
    ``_resolve_mb_release``, which tries the id directly and then falls back to
    browsing the group's releases.

    Returns an empty list when there is no cached list AND MusicBrainz cannot be
    reached — never raises, so a page render is never broken by a dead API.
    """
    release_id = str(release_id or "").strip()
    if not release_id:
        return []

    # 1. Cached tracklist — free, and the normal path once the backfill ran.
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT tracklist
                    FROM missing_releases
                    WHERE release_id = :release_id
                      AND (:artist = '' OR LOWER(artist) = LOWER(:artist))
                    LIMIT 1
                """),
                {"release_id": release_id, "artist": artist or ""},
            ).fetchone()
        raw = row[0] if row else None
        if raw:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            titles = [str(t).strip() for t in (parsed or []) if str(t).strip()]
            if titles:
                return titles
    except Exception as exc:
        logger.debug(
            "Missing-release tracklist cache miss",
            release_id=release_id,
            error=str(exc),
        )

    # 2. Live lookup. A synthetic release_id (the builder falls back to
    #    "{normalised-title}-{category}" when MusicBrainz returned no id) can
    #    never resolve, so it is not worth a request.
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        release_id,
        re.IGNORECASE,
    ):
        return []

    if not _musicbrainz_available():
        return []

    try:
        client = get_shared_mb_client()
        release, _resolved = _resolve_mb_release(client, release_id)
        titles = _tracklist_titles_from_release(release or {})
        if titles:
            _cache_missing_release_tracklist(artist, release_id, titles)
        return titles
    except Exception as exc:
        logger.debug(
            "Missing-release tracklist fetch failed",
            release_id=release_id,
            error=str(exc),
        )
        return []


def backfill_missing_release_tracklists(
    artist: str,
    *,
    limit: int | None = None,
    should_stop: Any = None,
) -> int:
    """Populate cached tracklists for this artist's missing releases.

    ── WHY THIS IS BACKGROUND WORK, NOT PART OF THE ARTIST SCAN ────────────
    ``missing_releases.release_id`` is a release-GROUP id, so one tracklist
    costs up to THREE MusicBrainz requests per release:

      1. ``get_release(<group-id>)``      → 404, wasted
      2. ``get("release", release-group=…)`` → 1 concrete release id
      3. ``get_release(<release-id>, inc=recordings)``

    MusicBrainz is globally throttled to ~1 req/s
    (``api_clients/musicbrainz_http.py::_strict_throttle``), so a 20-release
    backfill is ~60s of the *shared* budget. Doing that inline in the
    popularity scan would push the scan's own MusicBrainz calls behind it by
    that much per artist — the same "the scan looks stuck" failure the
    page-load probe storm caused.

    So it runs in the missing-releases sweep instead, which:
      * is already a background daemon thread,
      * already refuses to start while a popularity scan is running, and
      * already pauses mid-sweep (``_wait_while(_popularity_scan_active, …)``).

    The extra guard below makes each artist's backfill stop at the first sign
    of a scan rather than merely pausing, so the sweep's artist budget is not
    consumed by tracklist work while a scan wants the rate budget.
    """
    artist = str(artist or "").strip()
    if not artist:
        return 0

    limit = get_tracklist_backfill_limit() if limit is None else int(limit)
    if limit <= 0:
        return 0

    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT release_id
                    FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND release_id IS NOT NULL AND TRIM(release_id) <> ''
                      AND (tracklist IS NULL OR tracklist = '' OR tracklist = '[]')
                    ORDER BY last_checked ASC NULLS FIRST, first_release_date DESC NULLS LAST
                    LIMIT :limit
                """),
                {"artist": artist, "limit": limit},
            ).fetchall() or []
        release_ids = [str(r[0]) for r in rows if r[0]]
    except Exception as exc:
        logger.debug("Tracklist backfill query failed", artist=artist, error=str(exc))
        return 0

    filled = 0
    for release_id in release_ids:
        # Stand down immediately — see the docstring. The sweep retries this
        # artist on its next pass, so nothing is lost by stopping here.
        if should_stop is not None and should_stop():
            break
        if _popularity_scan_active():
            logger.info(
                "Missing-release tracklist backfill halted",
                reason="a popularity scan started",
                artist=artist,
                filled=filled,
            )
            break
        if not _musicbrainz_available():
            break

        titles = fetch_missing_release_tracklist(release_id, artist)
        if titles:
            filled += 1

    if filled:
        logger.info(
            "Missing-release tracklists backfilled",
            artist=artist,
            filled=filled,
            attempted=len(release_ids),
        )
    return filled


def _cleanup_imported_releases() -> int:
    """Remove cached missing releases that have since been imported into the library.

    Matches on NORMALISED album title (punctuation/case-insensitive) so a
    missing "Queen Dies (Single)" row is removed once the "Queen Dies" album
    exists — the reported singles staying "Missing" after being added.  Also
    removes SINGLE rows whose track is now present in the library (on any
    album), mirroring the builder rule: a single is only missing when its
    track is not owned anywhere.
    """
    with db_session() as session:
        result = session.execute(text("""
            DELETE FROM missing_releases mr
            WHERE EXISTS (
                SELECT 1 FROM tracks t
                WHERE LOWER(COALESCE(NULLIF(t.album_artist, ''), t.artist)) = LOWER(mr.artist)
                  AND LOWER(REGEXP_REPLACE(TRIM(t.album), '[^a-z0-9]+', ' ', 'g')) = LOWER(REGEXP_REPLACE(TRIM(mr.title), '[^a-z0-9]+', ' ', 'g'))
            )
            OR (
                LOWER(COALESCE(mr.category, '')) = 'single'
                AND EXISTS (
                    SELECT 1 FROM tracks t
                    WHERE LOWER(COALESCE(NULLIF(t.album_artist, ''), t.artist)) = LOWER(mr.artist)
                      AND t.title IS NOT NULL AND TRIM(t.title) <> ''
                      AND LOWER(REGEXP_REPLACE(TRIM(t.title), '[^a-z0-9]+', ' ', 'g')) = LOWER(REGEXP_REPLACE(TRIM(mr.title), '[^a-z0-9]+', ' ', 'g'))
                )
            )
        """))
        return result.rowcount or 0


def _library_track_titles(artist: str) -> set[str]:
    """Normalised titles of every track the artist already owns."""
    if not artist:
        return set()
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT DISTINCT title FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND title IS NOT NULL AND TRIM(title) <> ''
                """),
                {"artist": artist},
            ).fetchall() or []
        return {_normalize_release_title(str(r[0])) for r in rows if r[0]}
    except Exception as exc:
        logger.debug("Library track titles fetch failed", artist=artist, error=str(exc))
        return set()


def _resolve_artist_mbid(artist: str) -> str | None:
    """Return a stable artist MBID, falling back to a MusicBrainz lookup."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT COALESCE(
                        NULLIF(TRIM(musicbrainz_albumartistid), ''),
                        NULLIF(TRIM(musicbrainz_artistid), '')
                    ) AS mbid
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND COALESCE(
                        NULLIF(TRIM(musicbrainz_albumartistid), ''),
                        NULLIF(TRIM(musicbrainz_artistid), '')
                      ) IS NOT NULL
                """),
                {"artist": artist},
            ).fetchall()

        mbids = []
        for row in rows:
            raw = row[0]
            if raw:
                normalized = normalize_single_mbid(str(raw))
                if normalized:
                    mbids.append(normalized)

        if mbids:
            return Counter(mbids).most_common(1)[0][0]
    except Exception as exc:
        logger.debug("MBID query failed", artist=artist, error=str(exc))

    try:
        mbid = fetch_artist_mbid(None, artist)
        if mbid:
            return normalize_single_mbid(mbid) or mbid
    except Exception as exc:
        logger.debug("fetch_artist_mbid failed", artist=artist, error=str(exc))

    # A network lookup is pointless while MusicBrainz is refusing requests.
    if not _musicbrainz_available():
        logger.debug(
            "Artist MBID lookup skipped",
            reason="MusicBrainz reported unavailable",
            artist=artist,
        )
        return None

    try:
        from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
        mbid = lookup_and_save_artist_mbid(artist)
        return normalize_single_mbid(mbid) or mbid
    except Exception as exc:
        logger.debug("Artist MBID lookup failed", artist=artist, error=str(exc))

    return None


def _scan_is_running() -> bool:
    """Return True when the full-library missing-releases scan is active."""
    return bool(_scan_thread and _scan_thread.is_alive())


def _cached_missing_releases(artist: str) -> list[dict[str, Any]]:
    """Read the artist's cached missing releases from the DB."""
    if not artist:
        return []
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT release_id, title, primary_type, first_release_date,
                       cover_art_url, category, last_checked
                FROM missing_releases
                WHERE LOWER(artist) = LOWER(:artist)
                ORDER BY first_release_date DESC NULLS LAST, title ASC
            """),
            {"artist": artist},
        )
        return [dict(r._mapping) for r in result.fetchall() or []]


def _cached_missing_payload(artist: str, **extra: Any) -> dict[str, Any]:
    """Shape cached rows into the API response body."""
    cached = _cached_missing_releases(artist)
    payload = {
        "artist": artist,
        "missing": [
            {
                "id": r.get("release_id", ""),
                "title": r.get("title", ""),
                "primary_type": r.get("primary_type", "Album"),
                "first_release_date": str(r.get("first_release_date", "")),
                "cover_art_url": r.get("cover_art_url", ""),
                "category": r.get("category", "Album"),
            }
            for r in cached
        ],
        "from_cache": True,
    }
    payload.update(extra)
    return payload


def get_missing_releases(artist: str, background: bool = False) -> tuple[dict[str, Any], int]:
    """Detect missing releases for an artist and persist the results."""
    if not artist:
        return {"error": "Artist is required"}, 400

    if background and _scan_is_running():
        return _cached_missing_payload(artist, scan_guarded=True), 200

    # A single on-demand lookup is cheap, but it is still pointless while the
    # MusicBrainz breaker is open — and persisting its empty result would wipe
    # this artist's cached rows.
    if not _musicbrainz_available():
        logger.info(
            "Missing releases lookup served from cache",
            reason="MusicBrainz reported unavailable",
            artist=artist,
        )
        return _cached_missing_payload(artist, musicbrainz_unavailable=True), 200

    try:
        existing_albums = fetch_artist_albums(None, artist)
        artist_mbid = _resolve_artist_mbid(artist)
    except Exception as exc:
        logger.error("get_missing_releases failed", artist=artist, error=str(exc))
        return {"artist": artist, "missing": [], "existing_albums": [], "info": str(exc)}, 500

    existing_norm = {_normalize_release_title(a) for a in existing_albums if a}

    # Normalised titles of tracks the artist already owns — a SINGLE whose
    # track is on a library album is not missing.
    library_track_titles = _library_track_titles(artist)

    if not artist_mbid:
        return {
            "artist": artist,
            "missing": [],
            "existing_albums": existing_albums,
            "info": "No MusicBrainz artist ID stored for this artist. Run a popularity scan first to resolve the MBID.",
        }, 200

    try:
        release_groups, lookup_ok = _fetch_all_musicbrainz_releases(artist_mbid)
    except Exception as exc:
        logger.error("MusicBrainz fetch failed", artist=artist, error=str(exc))
        return {"artist": artist, "missing": [], "existing_albums": existing_albums, "info": str(exc)}, 500

    if not lookup_ok:
        # Do NOT persist: an unsuccessful lookup would delete the cached rows
        # and report the artist as having nothing missing.
        logger.warning(
            "Missing releases not refreshed",
            reason="MusicBrainz lookup did not complete",
            artist=artist,
        )
        payload = _cached_missing_payload(artist, musicbrainz_unavailable=True)
        payload["existing_albums"] = existing_albums
        return payload, 200

    missing_items = _build_missing_release_items(
        release_groups, existing_norm,
        library_track_titles=library_track_titles,
    )

    try:
        _persist_missing_releases(artist, missing_items)
    except Exception as exc:
        logger.error(
            "Could not persist missing releases",
            artist=artist, error=str(exc), exc_info=True,
        )

    return {
        "artist": artist,
        "missing": missing_items,
        "existing_albums": existing_albums,
    }, 200


def _wait_while(
    predicate: Any,
    *,
    wait_seconds: float,
    max_waits: int,
    reason: str,
    should_stop: Any,
) -> bool:
    """Sleep while *predicate* holds. Returns False when it never cleared."""
    waits = 0
    while predicate():
        if should_stop():
            return False
        if waits >= max_waits:
            logger.warning(
                "Missing releases scan gave up waiting",
                reason=reason,
                waited_s=round(waits * wait_seconds, 1),
            )
            return False
        if waits == 0:
            logger.info("Missing releases scan paused", reason=reason)
        waits += 1
        time.sleep(wait_seconds)
    if waits:
        logger.info(
            "Missing releases scan resumed",
            reason=reason,
            waited_s=round(waits * wait_seconds, 1),
        )
    return True


def _run_missing_releases_scan() -> None:
    """Background loop: scan every library artist for missing releases."""
    global _scan_thread
    try:
        from services.scanning.scan_state import (
            clear_stop_request,
            is_stop_requested,
            write_progress_with_current_artist,
        )
        clear_stop_request(_PROGRESS_PATH)

        def _should_stop() -> bool:
            return bool(is_stop_requested(_PROGRESS_PATH))

        try:
            with db_session() as session:
                rows = session.execute(
                    text("""
                        SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS canonical_artist
                        FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
                          AND COALESCE(NULLIF(album_artist, ''), artist) != ''
                        ORDER BY canonical_artist
                    """)
                ).fetchall()
            artists = [str(r[0]) for r in rows if r[0]] if rows else []
        except Exception as exc:
            logger.debug("Artist list fetch failed", error=str(exc))
            artists = []

        total_artists = len(artists)
        logger.info("Starting missing releases scan", total_artists=total_artists)

        try:
            cleaned = _cleanup_imported_releases()
            if cleaned:
                logger.info("Cleaned up imported releases", cleaned_count=cleaned)
        except Exception as exc:
            logger.debug("Cleanup failed", error=str(exc))

        total_missing = 0
        processed = 0
        skipped_unavailable = 0

        def _write_progress(status: str, current_artist: str | None = None) -> None:
            write_progress_with_current_artist(
                _PROGRESS_PATH,
                "missing_releases_scan",
                status == "running",
                current_artist=current_artist,
                extra={
                    "status": status,
                    "processed_artists": processed,
                    "total_artists": total_artists,
                    "total_missing_found": total_missing,
                    "skipped_unavailable": skipped_unavailable,
                    "percent_complete": int((processed / total_artists) * 100) if total_artists else 0,
                },
            )

        for artist in artists:
            if _should_stop():
                logger.info("Stop signal received, exiting gracefully")
                _write_progress("stopped")
                return

            # Yield to the popularity scan. Both walk the whole library and
            # share one 1 req/s MusicBrainz budget; running together drove
            # MusicBrainz to 503 within seconds.
            if not _wait_while(
                _popularity_scan_active,
                wait_seconds=_POPULARITY_WAIT_SECONDS,
                max_waits=_POPULARITY_MAX_WAITS,
                reason="a popularity scan is active",
                should_stop=_should_stop,
            ):
                _write_progress("stopped" if _should_stop() else "paused")
                return

            # Wait out a tripped circuit breaker rather than burning through
            # the remaining artists producing empty results.
            if not _wait_while(
                lambda: not _musicbrainz_available(),
                wait_seconds=_UNAVAILABLE_WAIT_SECONDS,
                max_waits=_UNAVAILABLE_MAX_WAITS,
                reason="MusicBrainz reported unavailable",
                should_stop=_should_stop,
            ):
                _write_progress("stopped" if _should_stop() else "paused")
                return

            processed += 1
            _write_progress("running", current_artist=artist)

            try:
                artist_mbid = _resolve_artist_mbid(artist)
                if not artist_mbid:
                    continue

                existing_albums = fetch_artist_albums(None, artist)
                existing_norm = {_normalize_release_title(a) for a in existing_albums if a}

                # Normalised titles of tracks the artist already owns — a
                # SINGLE whose track is on a library album is not missing.
                library_track_titles = _library_track_titles(artist)

                release_groups, lookup_ok = _fetch_all_musicbrainz_releases(artist_mbid)
                if not lookup_ok:
                    # Leave the cached rows alone; this artist is retried on
                    # the next sweep rather than being emptied now.
                    skipped_unavailable += 1
                    logger.info(
                        "Artist skipped without refreshing cache",
                        reason="MusicBrainz lookup did not complete",
                        artist=artist,
                    )
                    continue

                missing_items = _build_missing_release_items(
                    release_groups, existing_norm,
                    library_track_titles=library_track_titles,
                )
                # Persist even when empty: the lookup SUCCEEDED, so an empty
                # result genuinely means nothing is missing and stale rows
                # should be cleared.
                _persist_missing_releases(artist, missing_items)
                total_missing += len(missing_items)

                # Fill a few cached tracklists for the releases just written,
                # so the artist page's expandable tracklist is served from the
                # DB instead of costing up to three MusicBrainz calls on
                # click. Bounded per artist and halted the moment a popularity
                # scan starts — see the function's docstring for the cost.
                try:
                    backfill_missing_release_tracklists(
                        artist,
                        should_stop=_should_stop,
                    )
                    _write_progress("running", current_artist=artist)
                except Exception as exc:
                    logger.debug(
                        "Tracklist backfill failed",
                        artist=artist,
                        error=str(exc),
                    )
            except Exception as exc:
                logger.error("Error scanning artist", artist=artist, error=str(exc))
                continue
            finally:
                time.sleep(_ARTIST_DELAY_SECONDS)

        _write_progress("complete")
        logger.info(
            "Missing releases scan complete",
            total_missing=total_missing,
            total_artists=total_artists,
            skipped_unavailable=skipped_unavailable,
        )

    except Exception as exc:
        logger.error("Scan failed", error=str(exc), exc_info=True)
        try:
            from services.scanning.scan_state import write_progress_with_current_artist
            write_progress_with_current_artist(
                _PROGRESS_PATH, "missing_releases_scan", False,
                extra={"status": "error", "error": str(exc)},
            )
        except Exception:
            pass
    finally:
        with _scan_lock:
            _scan_thread = None


def start_missing_release_scan(force: bool = False) -> tuple[dict[str, Any], int]:
    """Start the full-library missing-releases scan in the background.

    Refuses to start while a popularity scan is running: both sweep the whole
    library against the same 1 req/s MusicBrainz budget. Pass ``force=True``
    to override.
    """
    global _scan_thread
    with _scan_lock:
        if _scan_thread and _scan_thread.is_alive():
            return {"success": False, "error": "Missing releases scan already running"}, 400

        if not force and _popularity_scan_active():
            logger.info(
                "Missing releases scan not started",
                reason="a popularity scan is already active",
            )
            return {
                "success": False,
                "error": (
                    "A popularity scan is already running. Both scans share the "
                    "MusicBrainz rate limit, so this scan was not started."
                ),
            }, 409

        _scan_thread = threading.Thread(
            target=_run_missing_releases_scan, daemon=True, name="missing-release-scan"
        )
        _scan_thread.start()
    return {"success": True, "message": "Missing releases scan started"}, 200


def import_release(artist: str, release_id: str, title: str) -> tuple[dict[str, Any], int]:
    """Import a missing release as placeholder track records."""
    artist = str(artist or "").strip()
    release_id = str(release_id or "").strip()
    title = str(title or "").strip()

    if not artist or not release_id or not title:
        return {"error": "Artist, release_id, and title are required"}, 400

    is_mb_uuid = bool(re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        release_id, re.IGNORECASE,
    ))

    release_year = ""
    media: list[dict[str, Any]] = []

    if is_mb_uuid:
        if not _musicbrainz_available():
            return {"error": "MusicBrainz is currently unavailable; try again shortly"}, 503
        client = get_shared_mb_client()
        data, _resolved = _resolve_mb_release(client, release_id)
        if not data:
            return {"error": "Release not found on MusicBrainz"}, 404
        release_year = str(data.get("date") or "")[:4]
        media = data.get("media") or []
    else:
        try:
            from api_clients.discogs import DiscogsClient
            from helpers.config_helpers import get_config as _get_cfg
            _token = (_get_cfg().get("api_integrations", {}).get("discogs", {}) or {}).get("token") or ""
            client = DiscogsClient(token=_token)
            data = client.get_release(release_id)
        except Exception as exc:
            logger.debug("Discogs release fetch failed", release_id=release_id, error=str(exc))
            data = None

        if not data:
            return {"error": "Release not found on Discogs"}, 404

        release_year = str(data.get("year") or "")
        media = [{"tracks": [
            {"title": t.get("title", ""), "duration": t.get("duration"), "position": t.get("position")}
            for t in (data.get("tracklist") or [])
        ]}]

    if not media or not any((d.get("tracks") or []) for d in media):
        return {"error": "No media found"}, 400

    from db.repositories.popularity_repository import save_to_db

    count = 0
    for disc_idx, disc in enumerate(media, start=1):
        disc_number = disc.get("position", disc_idx)
        for track_idx, track in enumerate(disc.get("tracks", []), start=1):
            recording = track.get("recording", {})
            track_title = recording.get("title") or track.get("title") or "Unknown"
            duration = track.get("length") or recording.get("length")
            if isinstance(duration, str) and duration.isdigit():
                duration = int(duration)

            mbid = recording.get("id", "")
            track_record = {
                "id": mbid or f"{release_id}_{disc_number}_{track_idx}",
                "title": track_title,
                "artist": artist,
                "album": title,
                "track_number": track_idx,
                "disc_number": disc_number,
                "duration": duration,
                "year": release_year,
                "mbid": mbid,
                "writer": "[]",
                "score": 0.0,
                "spotify_score": 0,
                "lastfm_score": 0,
                "age_score": 0,
                "genres": "[]",
                "file_path": None,
                "stars": 0,
                "last_scanned": datetime.now().isoformat(),
            }
            save_to_db(track_record)
            count += 1

    try:
        with db_session() as session:
            session.execute(
                text("""
                    DELETE FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist) AND release_id = :release_id
                """),
                {"artist": artist, "release_id": release_id},
            )
    except Exception as exc:
        logger.debug("Could not clear imported release from cache", error=str(exc))

    return {"success": True, "tracks_imported": count, "message": f"Imported {count} tracks from '{title}'"}, 200


def _resolve_mb_release(client: Any, mb_id: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve a MusicBrainz release OR release-group MBID to a release payload."""
    try:
        data = client.get_release(mb_id, inc="recordings")
        if data and data.get("id"):
            return data, str(data["id"])
    except Exception:
        pass

    try:
        release_search = client.get(
            "release",
            params={"release-group": mb_id, "limit": 1, "fmt": "json"},
            timeout=15.0,
        )
        releases = (release_search or {}).get("releases") or []
        if not releases:
            return None, ""

        release_mbid = str(releases[0].get("id") or "")
        if not release_mbid:
            return None, ""

        data = client.get_release(release_mbid, inc="recordings")
        return (data, release_mbid) if data else (None, "")
    except Exception as exc:
        logger.debug("Release-group resolution failed", mb_id=mb_id, error=str(exc))
        return None, ""


def scan_all_missing_releases(force: bool = False) -> tuple[dict[str, Any], int]:
    """Scan all artists for missing releases in the background."""
    return start_missing_release_scan(force=force)


def add_artist(artist: str) -> tuple[dict[str, Any], int]:
    """Add an artist to the database by creating a placeholder record."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    try:
        with db_session() as session:
            # Every other writer in the codebase populates `id` as well as
            # `name`; inserting `name` alone fails when `id` is NOT NULL.
            session.execute(
                text(
                    "INSERT INTO artists (id, name) VALUES (:artist, :artist) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"artist": artist},
            )
        return {"success": True, "message": f"Artist '{artist}' added"}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500
