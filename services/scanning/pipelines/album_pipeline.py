"""Album-level scan pipeline.

Extracted from the large routes/scans.py file. This keeps the route handler
small while preserving the original workflow:

1. Resolve the Navidrome artist ID where possible.
2. Import Navidrome metadata for the album.
3. Run metadata enrichment.
4. Run popularity scoring.
5. Trigger album type detection when available.
"""

from __future__ import annotations

import threading

import structlog

from db.repositories.scan_repository import lookup_artist_id, lookup_track_artist_count
from helpers.logging_config import log_unified
from services.scanning.navidrome_import import scan_artist_to_db

logger = structlog.get_logger(__name__)

# In-process guard: prevents the same album pipeline from running twice
# concurrently (double form submits, dashboard + album page triggers). Two
# overlapping runs double the Last.fm/ListenBrainz API load per track, which
# triggers rate limits and yields inconsistent listener data.
_pipeline_lock = threading.Lock()
_running_albums: set[tuple[str, str]] = set()


def _try_claim(artist_name: str, album_name: str) -> bool:
    """Claim the album for this pipeline run. False if already running."""
    global _running_albums
    key = (artist_name.strip().lower(), album_name.strip().lower())
    with _pipeline_lock:
        if key in _running_albums:
            return False
        _running_albums.add(key)
        return True


def _release(artist_name: str, album_name: str) -> None:
    global _running_albums
    key = (artist_name.strip().lower(), album_name.strip().lower())
    with _pipeline_lock:
        _running_albums.discard(key)


def _maybe_auto_detect_album_type(artist_name: str, album_name: str) -> None:
    """Run album type detection via the current stage service.

    The legacy root ``album_type_detector`` module no longer exists; album
    type detection now lives in ``services.popularity.stages.album_stage``
    (``ensure_album_type``).  The popularity scan (step 2) already detects
    and persists the type for processed albums, so this is a lightweight
    safety net for albums that were skipped (timestamp/no-change skips).
    """
    try:
        from db.repositories.library import get_tracks_for_album
        from services.popularity.stages.album_stage import ensure_album_type

        tracks = get_tracks_for_album(artist_name, album_name) or []
        if not tracks:
            return
        _mb_types = {
            str(t.get("musicbrainz_albumtype") or "").strip()
            for t in tracks
            if str(t.get("musicbrainz_albumtype") or "").strip()
        }
        _sp_types = {
            str(t.get("spotify_album_type") or "").strip()
            for t in tracks
            if str(t.get("spotify_album_type") or "").strip()
        }
        album_row = {
            "artist": artist_name,
            "album": album_name,
            "album_artist": artist_name,
            "spotify_album_type": next(iter(_sp_types)) if len(_sp_types) == 1 else None,
            "musicbrainz_album_type": next(iter(_mb_types)) if len(_mb_types) == 1 else None,
            "tracks": tracks,
        }
        detected = ensure_album_type(album_row)
        if detected:
            log_unified(
                f"💿 Album type detected for '{artist_name} - {album_name}': {detected}"
            )
    except Exception as exc:
        logger.debug(
            "Album type detection skipped",
            artist=artist_name,
            album=album_name,
            error=str(exc),
        )


def run_album_pipeline(artist_name: str, album_name: str, force: bool = False) -> None:
    """Run the complete scan pipeline for one album."""
    album_display = f"{artist_name} - {album_name}"

    if not _try_claim(artist_name, album_name):
        log_unified(f"⏭️ Album scan already running for: {album_display} — skipping duplicate trigger")
        return

    try:
        log_unified(f"💿 Album scan pipeline started for: {album_display}")

        artist_id = lookup_artist_id(artist_name)

        if not artist_id:
            try:
                # Rebuild artist index using NavidromeClient directly
                from api_clients.navidrome import NavidromeClient
                from helpers.config_helpers import get_navidrome_config
                
                nav_config = get_navidrome_config()
                if nav_config:
                    nav_client = NavidromeClient(
                        base_url=nav_config.get("base_url"),
                        username=nav_config.get("user"),
                        password=nav_config.get("pass"),
                    )
                    artist_index = nav_client.build_artist_index()
                    artist_data = (artist_index or {}).get(artist_name, {})
                    artist_id = artist_data.get("id") if isinstance(artist_data, dict) else None
            except Exception as exc:
                logger.debug(
                    "Could not rebuild artist index",
                    artist=artist_name,
                    error=str(exc),
                )

        if artist_id:
            log_unified(f"Step 1/3: Navidrome import for album '{album_display}'")
            _import_result = scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=True,
                force=force,
                album_filter=album_name,
            )
            if isinstance(_import_result, dict) and (
                _import_result.get("deferred")
                or _import_result.get("success") is False
            ):
                log_unified(
                    f"⚠️ Album scan for '{album_display}' did not complete: "
                    f"{_import_result.get('reason') or 'remote scan not verified'}"
                )
                return
        else:
            track_count = lookup_track_artist_count(artist_name)
            if track_count == 0:
                log_unified(f"❌ Scan aborted: no tracks found for '{album_display}'")
                return
            log_unified(f"Navidrome import skipped for '{album_display}' because only local track rows were found")

        from services.popularity.pipeline import run_popularity_scan as popularity_scan

        # Popularity scoring for THIS album only (album-filtered), so a
        # single-album scan does not walk the artist's entire catalogue.
        # Artist-wide scans are still available from the dashboard.
        # NOTE: the combined pass already performs the metadata enrichment
        # (album type / art / artist metadata / similar artists), and an
        # album-filtered scan never hits the timestamp skip — so a separate
        # metadata-only pre-pass here was pure duplicate work (double API
        # calls for art/bio/similar/last.fm per album).  Removed.
        log_unified(f"Step 2/3: Popularity scan for album '{album_display}'")
        popularity_result = popularity_scan(
            verbose=True,
            force=force,
            artist_filter=artist_name,
            album_filter=album_name,
            progress_file="popularity_scan",
        )

        # ⭐ The album-filtered popularity pass ALWAYS runs this album (the skip
        # windows are bypassed for an explicit album filter), and its album
        # stage resolves and persists the album type. The safety net is kept
        # only for the case where the pass processed nothing, so it no longer
        # re-reads and re-detects an album the pass just handled.
        _processed = 0
        if isinstance(popularity_result, dict):
            try:
                _processed = int(popularity_result.get("albums_processed") or 0)
            except (TypeError, ValueError):
                _processed = 0
        if _processed <= 0:
            log_unified(
                f"Step 3/3: Popularity pass processed no albums — running album-type "
                f"detection for '{album_display}'"
            )
            _maybe_auto_detect_album_type(artist_name, album_name)
        else:
            log_unified(
                f"Step 3/3: Album type already resolved by the popularity pass for "
                f"'{album_display}' — no second detection"
            )

        log_unified(f"✅ Scan complete for album '{album_display}'")

    except Exception as exc:
        log_unified(f"❌ Album scan failed for {album_display}: {exc}")
        logger.exception(
            "Album pipeline failed",
            album=album_display,
            error=str(exc),
        )
        raise
    finally:
        _release(artist_name, album_name)
