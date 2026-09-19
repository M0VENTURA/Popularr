"""Navidrome scanning service helpers.

Coordinates multiple Navidrome API calls for scan workflow operations.
Intentionally separate from ``api_clients.navidrome`` (raw HTTP layer).

Key Functions:
    - fetch_all_tracks_concurrently(): Parallel fetching of all tracks
      from Navidrome using ThreadPoolExecutor.
    - Other scan coordination helpers for batch processing.

Architecture:
    These are scan workflow helpers, not HTTP endpoint wrappers. They
    compose multiple API calls and handle coordination concerns like
    pagination, concurrency, and error handling.
"""

from __future__ import annotations

import concurrent.futures
import time
from typing import Any

import structlog

from api_clients.navidrome import NavidromeClient
from helpers.logging_config import log_unified

logger = structlog.get_logger(__name__)


from helpers.config_helpers import get_scan_pipeline_config
_scan_cfg = get_scan_pipeline_config()


def fetch_all_tracks_concurrently(
    client: NavidromeClient,
    total_tracks: int,
    page_size: int | None = None,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    if page_size is None:
        page_size = _scan_cfg["page_size"]
    if max_workers is None:
        max_workers = _scan_cfg["max_workers"]
    """Fetch all songs from Navidrome concurrently when get_songs exists.

    This function is defensive because not every client implementation has a
    ``get_songs`` method. When unavailable, it logs and returns an empty list.
    """
    if not hasattr(client, "get_songs"):
        logger.warning("NavidromeClient.get_songs is not available; concurrent fetch skipped")
        return []

    offsets = range(0, int(total_tracks or 0), int(page_size or 500))
    all_tracks: list[dict[str, Any]] = []

    def fetch_page(offset: int):
        return client.get_songs(offset=offset, size=page_size)  # type: ignore[attr-defined]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_offset = {executor.submit(fetch_page, offset): offset for offset in offsets}

        for future in concurrent.futures.as_completed(future_to_offset):
            offset = future_to_offset[future]
            try:
                page_data = future.result()
                if page_data:
                    all_tracks.extend(page_data)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch Navidrome page",
                    offset=offset,
                    error=str(exc),
                )

    return all_tracks


def build_artist_index_from_albums(client: NavidromeClient, page_size: int = 500) -> dict[str, dict[str, Any]]:
    """Build a scan-oriented artist index from the album list.

    Album-derived artists are more relevant for import workflows than the raw
    getArtists tree because the scanner imports albums and tracks.
    """
    albums = client.get_albums(artist_id=None, page_size=page_size)
    if not albums:
        return {}

    artist_map: dict[str, dict[str, Any]] = {}

    for album in albums:
        artist_name = str(album.get("artist") or "").strip()
        artist_id = str(album.get("artistId") or "").strip()

        if not artist_name or not artist_id:
            continue

        if artist_name not in artist_map:
            artist_map[artist_name] = {
                "id": artist_id,
                "album_count": 0,
                "track_count": 0,
                "last_updated": None,
            }

        artist_map[artist_name]["album_count"] += 1
        artist_map[artist_name]["track_count"] += int(album.get("songCount", 0) or 0)

    log_unified(f"Built album-derived Navidrome index for {len(artist_map)} artists")
    return artist_map


def build_artist_index(client: NavidromeClient) -> dict[str, dict[str, Any]]:
    """Build artist index using album list first, then getArtists fallback."""
    artist_map = build_artist_index_from_albums(client, page_size=500)
    if artist_map:
        return artist_map

    fallback: dict[str, dict[str, Any]] = {}

    for artist in client.get_artists():
        artist_id = artist.get("id")
        artist_name = artist.get("name")

        if artist_id and artist_name:
            fallback[str(artist_name)] = {
                "id": artist_id,
                "album_count": int(artist.get("albumCount", 0) or 0),
                "track_count": 0,
                "last_updated": None,
            }

    log_unified(f"Built fallback Navidrome index for {len(fallback)} artists")
    return fallback


# -------------------------------------------------------------------------
# Delta-scan helpers (only import what changed)
# -------------------------------------------------------------------------

# `getAlbumList2` list types that Navidrome actually implements. Navidrome
# rejects Subsonic's `recentlyAdded` with
# `code=0 message="type 'recentlyAdded' not implemented"`, which the client
# logs as a WARNING on every delta scan. `newest` is ordered by `created`
# descending, so it already covers "albums added since <timestamp>"; the
# per-album `created`/`updated` filter below does the rest.
_DELTA_LIST_TYPES = ("newest",)
_DELTA_MAX_PAGES = 5


def _album_sort_ts(album: dict[str, Any]) -> float:
    """Return a sortable timestamp for an album (newest first)."""
    raw = album.get("created") or album.get("updated") or ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def fetch_changed_albums(
    client: NavidromeClient,
    since_ts: Any = None,
    *,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch only recently added/changed albums from Navidrome.

    Pages ``getAlbumList2`` with the ``newest`` list type (deduped by album id)
    rather than crawling the full library. When
    ``since_ts`` is provided the returned list is filtered to albums whose
    ``created``/``updated`` timestamp is at or after that time; otherwise the
    head of the lists is treated as "recent".

    Returns:
        A list of album dicts (id, artist, artistId, songCount, ...).
    """
    if page_size is None:
        page_size = _scan_cfg["page_size"]
    if max_pages is None:
        max_pages = _DELTA_MAX_PAGES

    since_epoch = _coerce_epoch(since_ts)
    seen: set[str] = set()
    albums: list[dict[str, Any]] = []

    for list_type in _DELTA_LIST_TYPES:
        offset = 0
        for _ in range(max(1, int(max_pages))):
            page = client.get_album_list2_page(
                list_type=list_type,
                size=page_size,
                offset=offset,
            )
            if not page:
                break

            for album in page:
                album_id = str(album.get("id") or "")
                if not album_id or album_id in seen:
                    continue
                if since_epoch is not None:
                    ts = _album_sort_ts(album)
                    if ts and ts < since_epoch:
                        continue
                seen.add(album_id)
                albums.append(album)

            if len(page) < page_size:
                break
            offset += page_size

    log_unified(f"Delta album fetch returned {len(albums)} changed albums")
    return albums


def fetch_changed_songs(
    client: NavidromeClient,
    since_ts: Any = None,
    *,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Best-effort fetch of songs modified after ``since_ts``.

    Uses the OpenSubsonic ``modified`` filter on ``getSongs``. Navidrome may
    ignore or reject the parameter on older versions — in that case this
    returns an empty list and callers should fall back to the album delta.
    """
    if since_ts is None:
        return []

    if page_size is None:
        page_size = _scan_cfg["page_size"]
    if max_pages is None:
        max_pages = _DELTA_MAX_PAGES

    songs: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max(1, int(max_pages))):
        try:
            page = client.get_songs(offset=offset, size=page_size, modified=since_ts)
        except Exception as exc:
            logger.debug(
                "Delta song fetch failed (server may not support `modified`)",
                error=str(exc),
            )
            break
        if not page:
            break
        songs.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    if songs:
        log_unified(f"Delta song fetch returned {len(songs)} changed songs")
    else:
        logger.debug("Delta song fetch returned no songs (modified filter likely unsupported)")
    return songs


def build_delta_artist_index(
    client: NavidromeClient,
    since_ts: Any = None,
    *,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Build an artist index containing ONLY artists with changed content.

    Delta sources:
    1. ``getIndexes?ifModifiedSince`` — the authoritative Subsonic delta
       signal.  Navidrome returns the FULL album-artist index whenever a
       library scan completed after ``since_ts`` (i.e. anything changed —
       INCLUDING new songs added to existing albums) and an empty index
       otherwise.  Album-list deltas are ordered by ``created`` and silently
       miss new tracks dropped into existing albums, so this source is what
       makes those changes visible.
    2. Recently added/changed albums (``getAlbumList2`` newest/recentlyAdded)
       — supplementary; catches new albums on servers with different
       ``getIndexes`` semantics.
    3. Songs modified since ``since_ts`` (``getSongs?modified=``, best-effort;
       Navidrome itself does not implement ``getSongs``).

    Returns an artist map with the same shape as ``build_artist_index``
    (``{name: {id, album_count, track_count, last_updated}}``). When no delta
    can be determined (e.g. server ignores ``modified`` and no recent albums
    exist) this returns ``{}`` so callers can fall back to a full scan.
    """
    artist_map: dict[str, dict[str, Any]] = {}

    # 1) getIndexes?ifModifiedSince — Navidrome returns the full album-artist
    #    index when any scan completed after the cutoff, so an artist whose
    #    EXISTING album gained new songs becomes a candidate again.
    try:
        indexes = client.get_indexes(if_modified_since=since_ts) or {}
        for group in indexes.get("index", []) or []:
            for artist in group.get("artist", []) or []:
                artist_name = str(artist.get("name") or "").strip()
                artist_id = str(artist.get("id") or "").strip()
                if not artist_name or not artist_id:
                    continue
                artist_map.setdefault(artist_name, {
                    "id": artist_id,
                    "album_count": 0,
                    "track_count": 0,
                    "last_updated": None,
                })
        if artist_map:
            log_unified(
                f"Delta artist index: {len(artist_map)} artists from getIndexes(ifModifiedSince)"
            )
    except Exception as exc:
        logger.debug("Delta getIndexes failed — relying on album/song deltas", error=str(exc))

    changed_albums = fetch_changed_albums(
        client,
        since_ts,
        page_size=page_size,
        max_pages=max_pages,
    )
    for album in changed_albums:
        artist_name = str(album.get("artist") or "").strip()
        artist_id = str(album.get("artistId") or "").strip()
        if not artist_name or not artist_id:
            continue
        entry = artist_map.setdefault(artist_name, {
            "id": artist_id,
            "album_count": 0,
            "track_count": 0,
            "last_updated": None,
        })
        entry["album_count"] += 1
        entry["track_count"] += int(album.get("songCount", 0) or 0)

    changed_songs = fetch_changed_songs(
        client,
        since_ts,
        page_size=page_size,
        max_pages=max_pages,
    )
    for song in changed_songs:
        artist_name = str(song.get("artist") or "").strip()
        artist_id = str(song.get("artistId") or "").strip()
        if not artist_name or not artist_id:
            continue
        entry = artist_map.setdefault(artist_name, {
            "id": artist_id,
            "album_count": 0,
            "track_count": 0,
            "last_updated": None,
        })
        entry["track_count"] += 1

    log_unified(f"Delta artist index: {len(artist_map)} artists with changed content")
    return artist_map


def _coerce_epoch(value: Any) -> float | None:
    """Coerce a timestamp (epoch / ISO string / datetime) to epoch seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except (TypeError, ValueError):
            return None
    try:
        return float(value.timestamp())
    except (AttributeError, TypeError):
        return None


def get_library_stats(client: NavidromeClient, cache_seconds: int = 3600) -> dict[str, int]:
    """Return cached library stats derived from the scan-oriented artist index."""
    now = time.time()

    if client._stats_cache and now - client._last_stats_time < cache_seconds:
        return client._stats_cache  # type: ignore[return-value]

    try:
        artist_map = build_artist_index(client)
        total_albums = sum(int(info.get("album_count", 0) or 0) for info in artist_map.values())
        total_tracks = sum(int(info.get("track_count", 0) or 0) for info in artist_map.values())

        client._stats_cache = {
            "total_albums": total_albums,
            "total_tracks": total_tracks,
            "total_songs": total_tracks,
        }
        client._last_stats_time = now
        return client._stats_cache  # type: ignore[return-value]

    except Exception as exc:
        logger.error("Failed to get Navidrome library stats", error=str(exc), exc_info=True)
        return {"total_albums": 0, "total_tracks": 0, "total_songs": 0}
