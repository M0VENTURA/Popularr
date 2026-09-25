"""Navidrome artist import service.

Core service for importing artist/album/track data from Navidrome into
the Popularr database. Coordinates between Navidrome API calls, metadata
processing, and database persistence.

Key Functions:
    - scan_artist_to_db(): Full artist import pipeline (API fetch, metadata
      extraction, DB upsert).
    - fetch_artist_albums(): Get all albums for an artist from Navidrome.
    - fetch_album_tracks(): Get all tracks for an album from Navidrome.

Architecture:
    Uses ``db.repositories.tracks.upsert_track_payload`` for PostgreSQL-safe
    writes. Compatible with both old-style direct calls and new repository
    path. Cleanup hooks run after import to maintain data integrity.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

from api_clients.navidrome import NavidromeClient
from db.repositories.tracks import upsert_track_payload, upsert_tracks_bulk
from sqlalchemy import text
from db.engine import db_session
from helpers.logging_config import log_unified
from helpers.normalization_service import strip_album_edition_marker
from helpers.text_utils import _clean_artist_name_for_storage
from services.scanning.cleanup import (
    cleanup_empty_artist_dirs,
    cleanup_stale_album_tracks_if_needed,
    cleanup_stale_artist_tracks_if_needed,
    normalize_existing_artist_rows_safe,
    sanitize_artist_rows_safe,
)
from services.scanning.filters import should_skip_album, should_skip_cached_album
from services.scanning.metadata_extractor import extract_track_metadata
from services.scanning.scan_state import is_stop_requested
from services.scanning.payload_builder import build_track_payload

VA_ALBUM_ARTIST_VARIANTS = frozenset({
    "various artists", "various", "v/a", "va", "compilation", "original soundtrack",
})


def _get_fallback_client() -> NavidromeClient | None:
    """Return a properly configured Navidrome client from the scan service.

    Returns None when Navidrome is not configured so callers can degrade
    gracefully instead of crashing or producing malformed requests.
    """
    try:
        from services.scanning.navidrome_scan_service import get_nav_client
        return get_nav_client()
    except RuntimeError:
        return None
    except Exception:
        return None


def fetch_artist_albums(artist_id: str, client: NavidromeClient | None = None) -> list[dict[str, Any]]:
    """Fetch artist albums using an explicit client, legacy start.py helper, or fallback client."""
    if client is not None:
        return client.fetch_artist_albums(artist_id) or []

    try:
        from start import fetch_artist_albums as existing_fetch_artist_albums
        return existing_fetch_artist_albums(artist_id) or []
    except Exception:
        fb = _get_fallback_client()
        if fb:
            return fb.fetch_artist_albums(artist_id) or []
        return []


def fetch_album_tracks(album_id: str, client: NavidromeClient | None = None) -> dict[str, Any]:
    """Fetch album tracks using an explicit client, legacy start.py helper, or fallback client."""
    if client is not None:
        return client.fetch_album_tracks(album_id) or {}

    try:
        from start import fetch_album_tracks as existing_fetch_album_tracks
        return existing_fetch_album_tracks(album_id) or {}
    except Exception:
        fb = _get_fallback_client()
        if fb:
            return fb.fetch_album_tracks(album_id) or {}
        return {}


def save_to_db(track_payload: dict[str, Any]) -> None:
    """Persist a track payload through the PostgreSQL-safe tracks repository."""
    upsert_track_payload(track_payload)


def detect_live_album(album_name: str) -> dict[str, bool]:
    """Return lightweight live/unplugged album context flags."""
    from services.catalog.album_classification_service import (
        is_live_or_alternate_album,
        detect_live_album_type,
    )
    is_live = is_live_or_alternate_album(album_name)
    album_type = detect_live_album_type(album_name)
    return {"is_live": is_live, "is_unplugged": album_type == "acoustic"}


def compute_artist_album_diff(
    artist_name: str,
    nav_albums: list[dict[str, Any]],
) -> tuple[bool, set[str], set[str]]:
    """Compare pre-fetched Navidrome albums against current DB album names/counts.

    Returns ``(skip_artist, changed_albums, removed_albums)``.
    """
    nav_names: set[str] = set()
    nav_counts: dict[str, int] = {}
    for album in nav_albums:
        name = strip_album_edition_marker(album.get("name") or "")
        if name:
            nav_names.add(name)
            nav_counts[name] = int(album.get("songCount", 0) or 0)

    db_names: set[str] = set()
    db_counts: dict[str, int] = {}
    db_raw_names: set[str] = set()

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT album, COUNT(*) as track_count
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND album IS NOT NULL AND TRIM(album) <> ''
                    GROUP BY album
                """),
                {"artist": artist_name},
            )
            for row in result.fetchall() or []:
                album_name = str(row[0])
                count = int(row[1] or 0)
                if album_name:
                    db_raw_names.add(album_name)
                    stripped_db = strip_album_edition_marker(album_name)
                    db_names.add(stripped_db)
                    db_counts[stripped_db] = db_counts.get(stripped_db, 0) + count
    except Exception as exc:
        logger.debug(
            "[NAVIDROME_SCAN] Could not query DB albums",
            artist=artist_name,
            error=str(exc),
        )
        return False, set(), set()

    changed = nav_names.symmetric_difference(db_names)
    for album in nav_names & db_names:
        if nav_counts.get(album, 0) != db_counts.get(album, 0):
            changed.add(album)

    for raw_name in db_raw_names - nav_names:
        if strip_album_edition_marker(raw_name) in nav_names:
            changed.add(strip_album_edition_marker(raw_name))

    removed = {db_name for db_name in db_names if db_name not in nav_names}
    return (not changed), changed, removed


def album_artist_from_sources(
    *,
    api_album_artist: str,
    album_artist_from_artist_view: str,
    canonical_artist_name: str,
    album_name: str,
    existing_album_artists: dict[str, str],
) -> str:
    """Resolve album_artist while preserving existing Various Artists-style values."""
    album_artist_value = (
        _clean_artist_name_for_storage(api_album_artist or album_artist_from_artist_view or canonical_artist_name)
        or canonical_artist_name
    )

    existing_album_artist = existing_album_artists.get(album_name, "")
    existing_is_va = existing_album_artist.lower().strip() in VA_ALBUM_ARTIST_VARIANTS
    new_is_va = album_artist_value.lower().strip() in VA_ALBUM_ARTIST_VARIANTS

    if existing_album_artist and existing_is_va and not new_is_va:
        return existing_album_artist

    return album_artist_value


def fetch_album_tracks_safe(
    *,
    album_id: str,
    album_name: str,
    client: NavidromeClient | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch tracks for one album without failing the whole artist scan."""
    try:
        album_data = fetch_album_tracks(album_id, client=client) or {}
        return album_data.get("tracks", []) or [], album_data.get("artist", "") or ""
    except Exception as err:
        logger.debug(
            "Failed to fetch tracks for album",
            album=album_name,
            error=str(err),
        )
        return [], ""


def extract_and_backfill_track_metadata(
    *,
    navi_client: Any,
    track: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Extract Navidrome metadata and writer JSON."""
    get_song = getattr(navi_client, "get_song", None)
    extracted = extract_track_metadata(track, get_song=get_song)
    writer_json = extracted.get("writer", "[]") or "[]"
    return extracted, writer_json


def prefetch_artist_state(*, canonical_artist_name: str) -> dict[str, Any]:
    """Read existing DB state needed for one artist scan."""
    existing_track_ids: set[str] = set()
    existing_album_tracks: dict[str, set[str]] = {}
    existing_album_artists: dict[str, str] = {}

    with db_session() as session:
        result = session.execute(
            text("""
                SELECT id, album, album_artist
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
            """),
            {"artist": canonical_artist_name},
        )

        for row in result.fetchall() or []:
            track_id = row[0]
            album = row[1]
            album_artist = row[2]

            if track_id:
                existing_track_ids.add(str(track_id))
            if album and track_id:
                existing_album_tracks.setdefault(strip_album_edition_marker(str(album)), set()).add(str(track_id))
            if album and album_artist:
                existing_album_artists[strip_album_edition_marker(str(album))] = str(album_artist)

    return {
        "existing_track_ids": existing_track_ids,
        "navidrome_track_ids": set(),
        "existing_album_tracks": existing_album_tracks,
        "existing_album_artists": existing_album_artists,
        "albums_needing_reimport": set(),
    }


def scan_artist_to_db(
    artist_name: str,
    artist_id: str,
    verbose: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    processed_artists: int = 0,
    total_artists: int = 0,
    album_filter: str | None = None,
    progress_file: str | None = None,
    progress_scan_type: str | None = None,
    diff_mode: bool = False,
    client: NavidromeClient | None = None,
):
    """Scan one Navidrome artist and upsert local PostgreSQL track rows efficiently."""
    log_unified(f"[NAVIDROME_IMPORT] Importing artist: {artist_name} (artist_id={artist_id or 'none'}, force={force}, processed={processed_artists})")

    if not artist_id:
        logger.warning("[NAVIDROME_SCAN] No artist_id", artist=artist_name)
        return None

    canonical_artist_name = _clean_artist_name_for_storage(artist_name) or artist_name
    active_client = client

    try:
        _phase_started = time.monotonic()
        state = prefetch_artist_state(canonical_artist_name=canonical_artist_name)

        existing_track_ids = state["existing_track_ids"]
        navidrome_track_ids = state["navidrome_track_ids"]
        existing_album_tracks = state["existing_album_tracks"]
        existing_album_artists = state["existing_album_artists"]
        albums_needing_reimport = state["albums_needing_reimport"]

        # The import preamble below is silent by design (it only logs when it
        # actually changes rows), so a large catalogue made the dashboard show
        # "Step 1/3: Navidrome import ..." and then nothing for minutes — which
        # looks identical to a hung scan.  Emit a phase line per step so the
        # operator can tell a slow import from a stuck one.
        log_unified(
            f"[NAVIDROME_IMPORT] Prefetch complete for '{artist_name}' in "
            f"{time.monotonic() - _phase_started:.1f}s — "
            f"{len(existing_track_ids)} known track(s), "
            f"{len(existing_album_tracks)} known album(s) in DB"
        )

        _phase_started = time.monotonic()
        normalize_existing_artist_rows_safe(
            artist_name=artist_name,
            canonical_artist_name=canonical_artist_name,
        )
        sanitize_artist_rows_safe(canonical_artist_name=canonical_artist_name)
        log_unified(
            f"[NAVIDROME_IMPORT] Normalised local rows for '{artist_name}' in "
            f"{time.monotonic() - _phase_started:.1f}s"
        )

        # Single fetch for artist albums from Navidrome
        _phase_started = time.monotonic()
        albums = fetch_artist_albums(artist_id, client=active_client) or []
        log_unified(
            f"[NAVIDROME_IMPORT] Fetched {len(albums)} album(s) for '{artist_name}' "
            f"from Navidrome in {time.monotonic() - _phase_started:.1f}s"
        )

        if not albums:
            log_unified(
                f"Navidrome Import - {artist_name}: Navidrome returned no albums — "
                "skipping import and stale-track cleanup (existing local tracks "
                "preserved). Check Navidrome connectivity / artist mapping."
            )
            logger.warning(
                "[NAVIDROME_SCAN] fetch_artist_albums returned empty — skipping import + cleanup to avoid deleting existing tracks",
                artist=artist_name,
            )
            return None

        changed_album_names: set[str] | None = None
        removed_album_names: set[str] = set()

        if diff_mode and not force and not album_filter and not filter_missing:
            skip_artist, changed_album_names, removed_album_names = compute_artist_album_diff(
                canonical_artist_name,
                albums,
            )
            if skip_artist:
                return {"changed": False, "changed_albums": 0}

        _album_name_counts: dict[str, int] = {}
        for _album in albums:
            _name = strip_album_edition_marker(str(_album.get("name") or "").strip())
            if _name:
                _album_name_counts[_name] = _album_name_counts.get(_name, 0) + 1
        duplicate_album_names = {
            _name for _name, _count in _album_name_counts.items() if _count > 1
        }

        # Album names that collide once edition markers are stripped share ONE
        # cached id set (``existing_album_tracks`` is keyed by the stripped
        # name).  Cleaning each entry individually would make the colliding
        # entries delete each other's tracks.  Skipping the cleanup entirely —
        # the previous behaviour — stranded genuinely deleted tracks in these
        # albums forever, so the cleanup is DEFERRED and run once over the
        # UNION of every colliding entry's Navidrome ids.
        #
        # The union is only trustworthy when EVERY entry of that name had its
        # tracks fetched: an entry skipped before the fetch contributes no ids,
        # and running the union anyway would delete that entry's live tracks.
        duplicate_entry_totals: dict[str, int] = {
            _name: _count
            for _name, _count in _album_name_counts.items()
            if _name in duplicate_album_names
        }
        duplicate_fetched_counts: dict[str, int] = {}
        duplicate_nav_ids: dict[str, set[str]] = {}
        duplicate_cached_ids: dict[str, set[str]] = {}

        navi_client = active_client or _get_fallback_client()

        _albums_matched_filter = 0
        for album_index, album in enumerate(albums, 1):
            album_name = strip_album_edition_marker(album.get("name") or "")

            if progress_file and is_stop_requested(progress_file):
                log_unified(f"Navidrome Import - Stop requested — import halted for '{artist_name}'")
                break

            if should_skip_album(
                album_name=album_name,
                album_filter=album_filter,
                filter_missing=filter_missing,
                albums_needing_reimport=albums_needing_reimport,
                diff_mode=diff_mode,
                changed_album_names=changed_album_names,
            ):
                continue

            album_id = album.get("id")
            if not album_id:
                continue

            _albums_matched_filter += 1
            logger.info(
                "Importing album",
                album_index=album_index,
                album_total=len(albums),
                album=album_name,
            )
            log_unified(f"Navidrome Import - {artist_name} - Album {album_index}/{len(albums)}: {album_name}")

            album_context = detect_live_album(album_name)
            tracks, api_album_artist = fetch_album_tracks_safe(
                album_id=album_id,
                album_name=album_name,
                client=active_client,
            )

            for track in tracks:
                if track.get("id"):
                    navidrome_track_ids.add(track.get("id"))

            # Record this entry's ids for the deferred colliding-name cleanup.
            # Counted straight after the fetch — before any skip decision — so
            # an entry skipped as "cached" still contributes its live ids.
            if album_name in duplicate_entry_totals:
                duplicate_fetched_counts[album_name] = (
                    duplicate_fetched_counts.get(album_name, 0) + 1
                )
                duplicate_nav_ids.setdefault(album_name, set()).update(
                    track.get("id") for track in tracks if track.get("id")
                )

            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            if album_name in duplicate_entry_totals:
                duplicate_cached_ids[album_name] = cached_ids_for_album
            album_needs_reimport = album_name in albums_needing_reimport

            if should_skip_cached_album(
                artist_name=artist_name,
                album_name=album_name,
                tracks=tracks,
                cached_ids_for_album=cached_ids_for_album,
                force=force,
                album_needs_reimport=album_needs_reimport,
                verbose=verbose,
            ):
                continue

            album_artist_value = album_artist_from_sources(
                api_album_artist=api_album_artist,
                album_artist_from_artist_view=album.get("artist", "") or "",
                canonical_artist_name=canonical_artist_name,
                album_name=album_name,
                existing_album_artists=existing_album_artists,
            )

            album_mbids_seen: set[str] = set()
            _album_payloads: list[dict[str, Any]] = []

            for track in tracks:
                if not track.get("id"):
                    continue

                extracted, writer_json = extract_and_backfill_track_metadata(
                    navi_client=navi_client,
                    track=track,
                )

                payload = build_track_payload(
                    track=track,
                    extracted=extracted,
                    album_name=album_name,
                    album_artist_value=album_artist_value,
                    album_context=album_context,
                    canonical_artist_name=canonical_artist_name,
                    writer_json=writer_json,
                    is_new_track=track.get("id") not in cached_ids_for_album,
                )

                album_mbid = str(payload.get("musicbrainz_album_mbid") or "").strip()
                if album_mbid:
                    album_mbids_seen.add(album_mbid)

                _album_payloads.append(payload)

            if _album_payloads:
                try:
                    # ⚠️ The return value is CHECKED — see the note in
                    # scan_stage_runner. A refused batch must not pass silently.
                    if not upsert_tracks_bulk(_album_payloads):
                        logger.warning(
                            "[NAVIDROME_SCAN] Bulk track persist reported FAILURES — "
                            "some rows were refused by the database",
                            artist=artist_name,
                            album=album_name,
                            count=len(_album_payloads),
                        )
                except Exception as exc:
                    logger.warning(
                        "[NAVIDROME_SCAN] Bulk track persist failed",
                        artist=artist_name,
                        album=album_name,
                        error=str(exc),
                    )

            if len(album_mbids_seen) > 1:
                logger.warning(
                    "[NAVIDROME_SCAN] Album MBID inconsistency",
                    artist=artist_name,
                    album=album_name,
                    mbid_count=len(album_mbids_seen),
                )

            if diff_mode and album_name not in duplicate_album_names:
                cleanup_stale_album_tracks_if_needed(
                    artist_name=artist_name,
                    album_name=album_name,
                    cached_ids_for_album=cached_ids_for_album,
                    navidrome_tracks=tracks,
                )

        # Deferred cleanup for names that collide once edition markers are
        # stripped.  Runs only when every entry of the name was fetched, so the
        # union genuinely represents everything Navidrome still holds.
        if diff_mode:
            for _dup_name in sorted(duplicate_album_names):
                if duplicate_fetched_counts.get(_dup_name, 0) != duplicate_entry_totals.get(_dup_name, 0):
                    logger.debug(
                        "Skipping colliding-album cleanup - not every entry was fetched",
                        artist=artist_name,
                        album=_dup_name,
                        fetched=duplicate_fetched_counts.get(_dup_name, 0),
                        total=duplicate_entry_totals.get(_dup_name, 0),
                    )
                    continue
                cleanup_stale_album_tracks_if_needed(
                    artist_name=artist_name,
                    album_name=_dup_name,
                    cached_ids_for_album=duplicate_cached_ids.get(_dup_name, set()),
                    navidrome_tracks=[
                        {"id": track_id}
                        for track_id in duplicate_nav_ids.get(_dup_name, set())
                    ],
                )

        # An album_filter that matches nothing used to return silently, so the
        # callers (album pipeline step 1/3) moved on to the popularity scan
        # with no import having happened and no explanation in the log.
        if album_filter and not filter_missing and _albums_matched_filter == 0:
            log_unified(
                f"[NAVIDROME_IMPORT] No album matched the filter '{album_filter}' "
                f"for '{artist_name}' — Navidrome returned {len(albums)} album(s), "
                "none of which matched by name. Nothing was imported."
            )
            logger.warning(
                "[NAVIDROME_IMPORT] Album filter matched no Navidrome album",
                artist=artist_name,
                album_filter=album_filter,
                navidrome_album_count=len(albums),
            )

        if not filter_missing and not album_filter and not diff_mode:
            if navidrome_track_ids:
                cleanup_stale_artist_tracks_if_needed(
                    artist_name=artist_name,
                    existing_track_ids=existing_track_ids,
                    navidrome_track_ids=navidrome_track_ids,
                )
            else:
                log_unified(
                    f"Navidrome Import - {artist_name}: no tracks returned for any "
                    "album — skipping stale-track cleanup (existing local tracks "
                    "preserved). Check Navidrome connectivity."
                )
            cleanup_empty_artist_dirs(
                artist_name=artist_name,
                canonical_artist_name=canonical_artist_name,
            )

        if diff_mode:
            for removed_album in removed_album_names:
                removed_cached_ids = existing_album_tracks.get(removed_album, set())
                if removed_cached_ids:
                    cleanup_stale_album_tracks_if_needed(
                        artist_name=artist_name,
                        album_name=removed_album,
                        cached_ids_for_album=removed_cached_ids,
                        navidrome_tracks=[],
                    )

        if diff_mode and changed_album_names is not None:
            return {"changed": True, "changed_albums": len(changed_album_names)}

        return None

    except Exception:
        logger.exception("scan_artist_to_db failed", artist=artist_name)
        raise


def pre_import_sync_album_artists(artist_id: str | None = None) -> dict[str, Any]:
    """Compatibility placeholder for album-artist pre-sync."""
    return {
        "success": True,
        "unique_album_artists": 0,
        "new_artists_created": 0,
        "existing_artists": 0,
        "sync_time_ms": 0,
        "new_artists": [],
    }
