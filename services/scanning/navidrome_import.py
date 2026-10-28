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

import os
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

from api_clients.navidrome import NavidromeClient
from db.repositories.tracks import upsert_track_payload, upsert_tracks_bulk
from sqlalchemy import text
from db.engine import db_session
from helpers.logging_config import log_unified
from helpers.normalization_service import (
    strip_album_edition_marker,
    strip_appended_album_version,
)
from helpers.text_utils import _clean_artist_name_for_storage
from services.scanning.cleanup import (
    cleanup_empty_artist_dirs,
    cleanup_stale_album_tracks_if_needed,
    cleanup_stale_artist_tracks_if_needed,
    normalize_existing_artist_rows_safe,
    sanitize_artist_rows_safe,
)
from services.scanning.filters import should_skip_album, should_skip_cached_album
from services.scanning.metadata_extractor import extract_album_metadata, extract_track_metadata
from services.scanning.reconciliation import (
    adopt_navidrome_track_id,
    build_existing_indexes,
    db_album_fingerprint,
    find_adoptable_track_id,
    invalidate_track_id_caches,
    local_authority_conflicts,
    local_authority_values_match,
    mark_navidrome_import_confirmed,
    mark_navidrome_import_seen,
    navidrome_album_fingerprint,
    path_keys,
)
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


def clean_navidrome_album_name(album: dict[str, Any]) -> str:
    """Return the album name as Popularr should STORE it, from a Navidrome AlbumID3.

    Navidrome's Subsonic API never hands out a bare album title: ``Album.FullName()``
    appends the ``albumversion`` tag, into which Navidrome folds Picard's
    ``musicbrainz_albumcomment`` (the MusicBrainz release DISAMBIGUATION), so
    "17: Greatest Hits" (disambiguation "1999 Album") arrives as
    "17: Greatest Hits (1999 Album)". The edition-keyword stripper cannot catch that
    free-text comment, and writing it into ``tracks.album`` glues a subtitle onto
    every track of the album on every sync.

    The version is removed FIRST (it is the OUTERMOST suffix; Navidrome appends it
    last) by matching the value the album object carries in its own ``version`` field,
    then the usual edition markers are stripped. The version itself is not lost:
    ``extract_album_metadata`` stores it in the ``albumversion`` column.
    """
    raw_name = str(album.get("name") or "")
    return strip_album_edition_marker(
        strip_appended_album_version(raw_name, album.get("version"))
    )


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
        name = clean_navidrome_album_name(album)
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


def _merge_release_tags_from_file(
    extracted: dict[str, Any],
    track: dict[str, Any],
) -> None:
    """Fill fields Navidrome holds but never echoes from the FILE itself.

    The Subsonic ``Child``/``AlbumID3`` structs are a fixed field set:
    ``osChildFromMediaFile`` exposes title, ids, ISRC, replaygain, genres/moods,
    participants, works and movements — and nothing else. Navidrome reads
    barcode, catalog number, media, script, release country/status, language,
    record label, release type and the track/disc totals into
    ``model.MediaFile.Tags`` and keeps them there.

    So importing an album whose files were tagged elsewhere (a Soulseek
    download, a Picard-tagged rip) produced a row whose album page was blank
    for exactly those fields — reported as *"the album didn't pick up the tags
    on import from Navidrome"*. The file is local and already carries them.

    Navidrome's own answer always WINS: a key that came off the wire is never
    replaced, so this only ever fills a gap. Failures are silent — an
    unreadable file degrades to "Navidrome had nothing to say", which is the
    behaviour every other field already has.
    """
    raw_path = str(track.get("path") or extracted.get("file_path") or "").strip()
    if not raw_path:
        return

    resolved = ""
    try:
        from services.metadata.tag_file_service import resolve_music_file_path

        resolved = resolve_music_file_path(raw_path) or ""
    except Exception as exc:
        logger.debug(
            "Release-tag readback: path could not be resolved",
            path=raw_path, error=str(exc),
        )
    if not resolved:
        # Navidrome hands back an ABSOLUTE path when ``ReportRealPath`` is on,
        # and ``resolve_music_file_path`` returns None for anything missing.
        try:
            if os.path.isfile(raw_path):
                resolved = raw_path
        except (OSError, ValueError):
            resolved = ""
    if not resolved:
        return

    try:
        from helpers.metadata_reader import read_release_tag_values

        file_values = read_release_tag_values(resolved)
    except Exception as exc:
        logger.debug(
            "Release-tag readback failed", path=resolved, error=str(exc),
        )
        return

    for field, value in file_values.items():
        if str(extracted.get(field) or "").strip():
            continue  # the wire already answered
        extracted[field] = value


def extract_and_backfill_track_metadata(
    *,
    navi_client: Any,
    track: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Extract Navidrome metadata, file release tags, and writer JSON."""
    get_song = getattr(navi_client, "get_song", None)
    extracted = extract_track_metadata(track, get_song=get_song)
    _merge_release_tags_from_file(extracted, track)
    writer_json = extracted.get("writer", "[]") or "[]"
    return extracted, writer_json


def prefetch_artist_state(*, canonical_artist_name: str) -> dict[str, Any]:
    """Read existing DB state needed for one artist scan.

    Also flags albums whose stored rows are missing one of the CRITICAL
    fields the Navidrome response carries (duration, track number, year,
    file path) so the ``missing`` scan mode re-imports them.

    ⚠️ This set used to be returned EMPTY unconditionally — the old system
    built it (``old_system/deprecated/navidrome_import.py``) but the port
    dropped that half, so ``filter_missing=True`` hit
    ``should_skip_album``'s "not in albums_needing_reimport" branch and
    skipped EVERY album: the missing-fields mode silently imported nothing.
    """
    existing_track_ids: set[str] = set()
    existing_album_tracks: dict[str, set[str]] = {}
    existing_album_rows: dict[str, list[dict[str, Any]]] = {}
    existing_album_artists: dict[str, str] = {}
    albums_needing_reimport: set[str] = set()
    all_rows: list[dict[str, Any]] = []

    def _missing(value: Any) -> bool:
        """NULL / empty / non-positive numeric = the field never landed."""
        if value is None:
            return True
        text = str(value).strip()
        if text == "":
            return True
        try:
            return float(text) <= 0
        except ValueError:
            return False

    with db_session() as session:
        result = session.execute(
            text("""
                SELECT id, album, album_artist, duration, track_number, year, file_path,
                       title, artist, disc_number, musicbrainz_trackid, recording_mbid,
                       mbid, navidrome_genres,
                       local_tags_written_at, metadata_dirty_for_navidrome,
                       navidrome_imported_at
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
            """),
            {"artist": canonical_artist_name},
        )

        for row in result.fetchall() or []:
            mapping = dict(row._mapping) if hasattr(row, "_mapping") else {}
            if not mapping:
                continue
            track_id = mapping.get("id")
            album = mapping.get("album")
            album_artist = mapping.get("album_artist")
            row_dict = {
                "id": str(track_id) if track_id else "",
                "album": str(album or ""),
                "album_artist": str(album_artist or ""),
                "title": mapping.get("title") or "",
                "artist": mapping.get("artist") or "",
                "duration": mapping.get("duration"),
                "track_number": mapping.get("track_number"),
                "disc_number": mapping.get("disc_number"),
                "year": mapping.get("year"),
                "file_path": mapping.get("file_path") or "",
                "musicbrainz_trackid": mapping.get("musicbrainz_trackid") or "",
                "recording_mbid": mapping.get("recording_mbid") or "",
                "mbid": mapping.get("mbid") or "",
                "navidrome_genres": mapping.get("navidrome_genres") or "",
                "local_tags_written_at": mapping.get("local_tags_written_at"),
                "metadata_dirty_for_navidrome": bool(
                    mapping.get("metadata_dirty_for_navidrome")
                ),
                "navidrome_imported_at": mapping.get("navidrome_imported_at"),
            }
            all_rows.append(row_dict)

            if track_id:
                existing_track_ids.add(str(track_id))
            if album and track_id:
                album_key = strip_album_edition_marker(str(album))
                existing_album_tracks.setdefault(album_key, set()).add(str(track_id))
                existing_album_rows.setdefault(album_key, []).append(row_dict)
            if album and album_artist:
                existing_album_artists[strip_album_edition_marker(str(album))] = str(album_artist)
            # Any critical field still empty → this album needs a re-import
            # when ``filter_missing`` is on (all four ARE on the wire, so a
            # re-import genuinely fills them).
            if album and any(
                _missing(row_dict.get(field))
                for field in ("duration", "track_number", "year", "file_path")
            ):
                albums_needing_reimport.add(strip_album_edition_marker(str(album)))

    existing_album_fingerprints = {
        album_key: db_album_fingerprint(rows)
        for album_key, rows in existing_album_rows.items()
    }
    existing_album_dirty = {
        album_key: any(bool(row.get("metadata_dirty_for_navidrome")) for row in rows)
        for album_key, rows in existing_album_rows.items()
    }
    by_path, by_mbid = build_existing_indexes(all_rows)

    return {
        "existing_track_ids": existing_track_ids,
        "navidrome_track_ids": set(),
        "existing_album_tracks": existing_album_tracks,
        "existing_album_rows": existing_album_rows,
        "existing_album_artists": existing_album_artists,
        "existing_album_fingerprints": existing_album_fingerprints,
        "existing_album_dirty": existing_album_dirty,
        "existing_by_path": by_path,
        "existing_by_mbid": by_mbid,
        "known_rows_by_id": {row["id"]: row for row in all_rows if row.get("id")},
        "albums_needing_reimport": albums_needing_reimport,
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
    allow_cleanup: bool = True,
    freshness_verified: bool | None = None,
):
    """Scan one Navidrome artist and upsert local PostgreSQL track rows efficiently."""
    log_unified(f"[NAVIDROME_IMPORT] Importing artist: {artist_name} (artist_id={artist_id or 'none'}, force={force}, processed={processed_artists})")

    if not artist_id:
        logger.warning("[NAVIDROME_SCAN] No artist_id", artist=artist_name)
        return None

    canonical_artist_name = _clean_artist_name_for_storage(artist_name) or artist_name
    active_client = client

    # Direct callers (album page, targeted pipeline, library sync) do not go
    # through run_navidrome_import_scan's freshness gate. Prove the remote
    # state before reading it. A client without scan capability is legacy/test
    # only; the verified import pipeline always passes freshness_verified=True.
    if freshness_verified is None:
        _freshness_client = client or _get_fallback_client()
        if _freshness_client is not None and hasattr(
            _freshness_client, "trigger_and_wait_for_scan"
        ):
            from services.scanning.pipelines.navidrome_pipeline import (
                sync_remote_navidrome_before_import,
            )
            from services.scanning.scan_state import get_scan_checkpoint_path

            if not sync_remote_navidrome_before_import(
                _freshness_client,
                checkpoint_file=get_scan_checkpoint_path("navidrome_scan"),
                use_dirty_state=True,
            ):
                log_unified(
                    f"Navidrome Import - DEFERRED for '{artist_name}' "
                    "(remote scan not verified); local metadata preserved"
                )
                return {"deferred": True, "reason": "remote scan not verified"}

    try:
        _phase_started = time.monotonic()
        state = prefetch_artist_state(canonical_artist_name=canonical_artist_name)

        existing_track_ids = state["existing_track_ids"]
        navidrome_track_ids = state["navidrome_track_ids"]
        existing_album_tracks = state["existing_album_tracks"]
        existing_album_rows = state["existing_album_rows"]
        existing_album_artists = state["existing_album_artists"]
        albums_needing_reimport = state["albums_needing_reimport"]
        existing_album_fingerprints = state["existing_album_fingerprints"]
        existing_album_dirty = state["existing_album_dirty"]
        existing_by_path = state["existing_by_path"]
        existing_by_mbid = state["existing_by_mbid"]
        known_rows_by_id = state["known_rows_by_id"]
        _adopted_ids = False
        _metadata_conflict_count = 0
        _ambiguous_old_ids: set[str] = set()

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
            if existing_track_ids:
                return {
                    "success": False,
                    "changed": False,
                    "reason": "no_albums_for_known_artist",
                    "metadata_conflicts": 0,
                }
            return {
                "success": True,
                "changed": False,
                "reason": "empty_artist",
                "metadata_conflicts": 0,
            }

        changed_album_names: set[str] | None = None
        removed_album_names: set[str] = set()

        if diff_mode and not force and not album_filter and not filter_missing:
            skip_artist, changed_album_names, removed_album_names = compute_artist_album_diff(
                canonical_artist_name,
                albums,
            )
            if skip_artist:
                return {"changed": False, "changed_albums": 0}

            # A DB album that differs from a Navidrome album ONLY by an
            # appended edition/version marker is the SAME album under a new
            # name — a re-home, not a removal. Navidrome keeps the same song
            # ids across a rename, so those ids are still alive under the new
            # name and must never be deleted by the removed-album cleanup (they
            # are keyed by the OLD name, which now looks "removed"). A rename
            # that is not in `changed_album_names` is skipped by the album loop
            # and so never reaches `navidrome_track_ids` — seed the live set
            # here, from the DB ids of any album that reduces to a nav name
            # under the version/edition strippers.
            cleaned_nav_names = set(clean_navidrome_album_name(a) for a in albums)
            for _db_raw, _db_ids in existing_album_tracks.items():
                if not _db_ids:
                    continue
                _cleaned = strip_appended_album_version(
                    strip_album_edition_marker(_db_raw), None
                )
                if _cleaned in cleaned_nav_names:
                    navidrome_track_ids |= _db_ids

        _album_name_counts: dict[str, int] = {}
        for _album in albums:
            _name = clean_navidrome_album_name(_album)
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
        # Set False if a stop request breaks the loop: the live-id set below
        # would then be INCOMPLETE (albums never fetched), and running the
        # removed-album cleanup off it would delete tracks that are still in
        # Navidrome. A partial import must never declare an album removed.
        _album_loop_completed = True
        for album_index, album in enumerate(albums, 1):
            album_name = clean_navidrome_album_name(album)

            if progress_file and is_stop_requested(progress_file):
                log_unified(f"Navidrome Import - Stop requested — import halted for '{artist_name}'")
                _album_loop_completed = False
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

            # ── Navidrome ID reconciliation ───────────────────────────────
            # Navidrome can re-encode IDs (0.64) or re-adopt a row for the same
            # file. Importing the new ID as a brand-new row would leave the old
            # row stale and lose every Popularr-owned enrichment. Adopt only
            # on strong identity evidence: exact file path, or MBID + title +
            # artist corroboration. Never by title alone.
            album_artist_value = album_artist_from_sources(
                api_album_artist=api_album_artist,
                album_artist_from_artist_view=album.get("artist", "") or "",
                canonical_artist_name=canonical_artist_name,
                album_name=album_name,
                existing_album_artists=existing_album_artists,
            )
            for track in tracks:
                new_id = str(track.get("id") or "").strip()
                if not new_id or new_id in existing_track_ids:
                    continue
                match = find_adoptable_track_id(
                    track,
                    album_name=album_name,
                    album_artist=album_artist_value,
                    by_path=existing_by_path,
                    by_mbid=existing_by_mbid,
                )
                if not match:
                    # If several existing rows share this exact file path, do
                    # not guess which one is the new ID. Preserve those rows
                    # from stale cleanup and leave the ambiguity for review.
                    _path_matches: set[str] = set()
                    for _key in path_keys(track.get("path") or track.get("file_path")):
                        for _old in existing_by_path.get(_key, []):
                            if _old and _old != new_id:
                                _path_matches.add(str(_old))
                    if len(_path_matches) > 1:
                        _ambiguous_old_ids.update(_path_matches)
                        logger.warning(
                            "[NAVIDROME_IMPORT] ambiguous ID reconciliation — "
                            "preserving existing rows instead of deleting them",
                            new_id=new_id,
                            candidates=sorted(_path_matches)[:8],
                            album=album_name,
                        )
                    _mbid = str(
                        track.get("musicBrainzId")
                        or track.get("musicbrainz_trackid")
                        or track.get("recording_mbid")
                        or track.get("mbid")
                        or ""
                    ).strip().casefold()
                    if _mbid:
                        _mbid_candidates = {
                            str(row.get("id"))
                            for row in existing_by_mbid.get(_mbid, [])
                            if row.get("id") and str(row.get("id")) != new_id
                        }
                        if _mbid_candidates:
                            _ambiguous_old_ids.update(_mbid_candidates)
                    continue
                old_id, match_reason = match
                if not adopt_navidrome_track_id(old_id, new_id):
                    # Adoption failed (e.g. the new ID already exists). Do not
                    # let stale cleanup delete a row we could not reconcile.
                    _ambiguous_old_ids.add(str(old_id))
                    continue
                _adopted_ids = True
                existing_track_ids.discard(old_id)
                existing_track_ids.add(new_id)
                for _ids in existing_album_tracks.values():
                    if old_id in _ids:
                        _ids.discard(old_id)
                        _ids.add(new_id)
                _row = known_rows_by_id.pop(old_id, None)
                if _row is not None:
                    _row["id"] = new_id
                    known_rows_by_id[new_id] = _row
                for _path_key, _ids in existing_by_path.items():
                    if old_id in _ids:
                        existing_by_path[_path_key] = [
                            new_id if value == old_id else value for value in _ids
                        ]
                logger.info(
                    "[NAVIDROME_IMPORT] track ID adopted",
                    old_id=old_id, new_id=new_id, match=match_reason,
                    artist=artist_name, album=album_name,
                )
                log_unified(
                    f"[NAVIDROME_IMPORT] Adopted changed Navidrome ID for "
                    f"'{album_name}' ({match_reason}): {old_id} → {new_id}"
                )

            if album_name in existing_album_rows:
                existing_album_fingerprints[album_name] = db_album_fingerprint(
                    existing_album_rows[album_name]
                )

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

            _incoming_fingerprint = navidrome_album_fingerprint(
                tracks, album_name=album_name, album_artist=album_artist_value,
            )
            _cached_fingerprint = existing_album_fingerprints.get(album_name)
            _fingerprint_equal = bool(
                _cached_fingerprint
                and _incoming_fingerprint
                and _cached_fingerprint == _incoming_fingerprint
            )

            if should_skip_cached_album(
                artist_name=artist_name,
                album_name=album_name,
                tracks=tracks,
                cached_ids_for_album=cached_ids_for_album,
                force=force,
                album_needs_reimport=album_needs_reimport,
                verbose=verbose,
                metadata_fingerprint_equal=_fingerprint_equal,
                album_metadata_dirty=existing_album_dirty.get(album_name, False),
            ):
                continue

            album_mbids_seen: set[str] = set()
            _album_payloads: list[dict[str, Any]] = []
            _confirmed_ids: list[str] = []
            _seen_ids: list[str] = []
            _dirty_conflicts: list[tuple[str, list[str]]] = []

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
                    album_track_count=len(tracks),
                    album_name=album_name,
                    album_artist_value=album_artist_value,
                    album_context=album_context,
                    canonical_artist_name=canonical_artist_name,
                    writer_json=writer_json,
                    is_new_track=track.get("id") not in cached_ids_for_album,
                    # Navidrome carries the release MBID on the ALBUM object
                    # (`musicBrainzId` on AlbumID3 = mf.MbzAlbumID), not on
                    # the song children — without it every track payload
                    # carried "" and the upsert wiped the stored value.
                    album_mbid=str(album.get("musicBrainzId") or "").strip(),
                    # Record labels / release type / original dates: the
                    # album object carries them, the song children never do.
                    album_tags=extract_album_metadata(album),
                )

                album_mbid = str(payload.get("musicbrainz_album_mbid") or "").strip()
                if album_mbid:
                    album_mbids_seen.add(album_mbid)

                _tid = str(payload.get("id") or "").strip()
                _existing = known_rows_by_id.get(_tid)
                if _existing and bool(_existing.get("metadata_dirty_for_navidrome")):
                    _conflicts = local_authority_conflicts(_existing, payload)
                    if _conflicts:
                        _dirty_conflicts.append((_tid, _conflicts))
                    else:
                        _confirmed_ids.append(_tid)
                elif _tid:
                    _seen_ids.append(_tid)

                _album_payloads.append(payload)

            for _tid, _fields in _dirty_conflicts[:5]:
                _row = known_rows_by_id.get(_tid) or {}
                logger.warning(
                    "[NAVIDROME_IMPORT] metadata conflict — local correction preserved",
                    artist=artist_name,
                    album=album_name,
                    track=_row.get("title") or _tid,
                    fields=",".join(sorted(_fields)),
                    winner="existing",
                    reason="local correction newer than last verified import",
                )
            _metadata_conflict_count += len(_dirty_conflicts)

            if _album_payloads:
                try:
                    # ⚠️ The return value is CHECKED — see the note in
                    # scan_stage_runner. A refused batch must not pass silently.
                    _bulk_ok = bool(upsert_tracks_bulk(_album_payloads))
                    if not _bulk_ok:
                        logger.warning(
                            "[NAVIDROME_SCAN] Bulk track persist reported FAILURES — "
                            "some rows were refused by the database",
                            artist=artist_name,
                            album=album_name,
                            count=len(_album_payloads),
                        )
                    else:
                        # Only after the transaction committed: rows whose
                        # incoming values agreed with the local correction are
                        # no longer dirty; ordinary rows record the verified
                        # import time. Conflicting dirty rows keep their
                        # authority (and their old navidrome_imported_at).
                        if _confirmed_ids:
                            mark_navidrome_import_confirmed(_confirmed_ids)
                        if _seen_ids:
                            mark_navidrome_import_seen(_seen_ids)
                        for _tid in _confirmed_ids:
                            _row = known_rows_by_id.get(_tid)
                            if _row is not None:
                                _row["metadata_dirty_for_navidrome"] = False
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
                    live_track_ids=_ambiguous_old_ids or None,
                    allow_cleanup=allow_cleanup,
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
                    live_track_ids=_ambiguous_old_ids or None,
                    allow_cleanup=allow_cleanup,
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

        # ⚠️ The filter is TOLERANT on purpose (``album_names_match``), so it
        # can match MORE THAN ONE album: asking for "Greatest Hits" matches
        # both "17: Greatest Hits" and "18: Greatest Hits". Every match IS
        # imported, because that is what the request named — but the ambiguity
        # is stated rather than left as a silent surprise, since the user
        # clicked a single album.
        if album_filter and _albums_matched_filter > 1:
            log_unified(
                f"[NAVIDROME_IMPORT] The album filter '{album_filter}' matched "
                f"{_albums_matched_filter} Navidrome albums for '{artist_name}' "
                "— all of them were imported."
            )
            logger.warning(
                "[NAVIDROME_IMPORT] Album filter matched multiple Navidrome albums",
                artist=artist_name,
                album_filter=album_filter,
                matched=_albums_matched_filter,
            )

        if not filter_missing and not album_filter and not diff_mode:
            if navidrome_track_ids:
                cleanup_stale_artist_tracks_if_needed(
                    artist_name=artist_name,
                    existing_track_ids=existing_track_ids,
                    navidrome_track_ids=navidrome_track_ids | _ambiguous_old_ids,
                    allow_cleanup=allow_cleanup,
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

        if diff_mode and _album_loop_completed:
            for removed_album in removed_album_names:
                removed_cached_ids = existing_album_tracks.get(removed_album, set())
                if removed_cached_ids:
                    cleanup_stale_album_tracks_if_needed(
                        artist_name=artist_name,
                        album_name=removed_album,
                        cached_ids_for_album=removed_cached_ids,
                        navidrome_tracks=[],
                        # A renamed album keeps its track ids under the NEW
                        # name; they are alive, so they must not be deleted
                        # just because the old NAME is gone.
                        live_track_ids=navidrome_track_ids | _ambiguous_old_ids,
                        allow_cleanup=allow_cleanup,
                    )

        # ⚠️ THIS IMPORT MAY HAVE JUST REWRITTEN ``tracks.id`` — the id IS the
        # Navidrome song id, so a 0.64 re-encoding or a re-adopted row changes
        # it. The genre / Top-Tracks playlists are built from a CACHED snapshot
        # of those rows (``_GENRE_ROWS_CACHE``, 120s), and nothing else in the
        # import path cleared it: a finalise inside that window pushed the ids
        # the import had just replaced, and the sync reported "stored a
        # different number of tracks than requested" AFTER a clean re-import —
        # the warning telling the operator to re-run the import they had
        # already run.
        try:
            from services.popularity.stages.finalise_stage import (
                invalidate_genre_playlist_rows,
            )

            invalidate_genre_playlist_rows()
        except Exception as _inv_exc:
            logger.debug(
                "Genre playlist row cache invalidation skipped",
                error=str(_inv_exc),
            )

        if _adopted_ids:
            # The cache above is the one that carries Navidrome song ids. The
            # dedicated helper also covers future ID-bearing caches.
            invalidate_track_id_caches()
            try:
                from services.playlists.song_id_reconciliation import (
                    clear_playlist_song_rejections,
                    server_key,
                )

                clear_playlist_song_rejections(server_key(navi_client))
            except Exception as exc:
                logger.debug("Playlist rejection retry reset failed", error=str(exc))
            logger.info(
                "[NAVIDROME_IMPORT] ID adoption invalidated ID-bearing caches",
                artist=artist_name,
            )

        if diff_mode and changed_album_names is not None:
            return {
                "changed": True,
                "changed_albums": len(changed_album_names),
                "metadata_conflicts": _metadata_conflict_count,
            }

        return {
            "changed": True,
            "changed_albums": len(changed_album_names or []),
            "metadata_conflicts": _metadata_conflict_count,
        }

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
