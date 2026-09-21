"""Bulk popularity cache service.

Fetches Last.fm and ListenBrainz popularity for ALL tracks of an artist in a
small number of API calls (``artist.getTopTracks`` + batched ListenBrainz
lookups) and persists them into ``track_popularity_cache``.

Non-forced scans read from this cache instead of making per-track API calls,
which avoids rate limiting and keeps scores consistent between runs.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import structlog

from db.repositories import popularity_cache as cache_repo
from helpers.normalization_service import strip_diacritics
from services.popularity.popularity_matching import (
    get_primary_artist_preserve_case,
    normalize_for_aggregation,
)
from services.popularity.popularity_sources import (
    extract_recording_mbid,
    get_listenbrainz_batch_for_tracks,
)

logger = structlog.get_logger(__name__)

CACHE_FRESH_HOURS = 24 * 7  # 1 week

# =============================================================================
# THREAD-SAFE MEMORY CACHES
# =============================================================================

_CACHE_LOCK = threading.Lock()

# One artist.getTopTracks call per artist per process — albums of the same
# artist share the result instead of repeating the API call.
_lf_top_tracks_cache: dict[str, dict[str, dict[str, int]]] = {}

# Original (cased) track titles for the same map.
_lf_top_tracks_titles: dict[str, dict[str, str]] = {}

# Last.fm top-tags for the same map.
_lf_top_tracks_tags: dict[str, dict[str, str]] = {}


def _is_fresh(row: dict[str, Any]) -> bool:
    """Return True when a cached row is fresh enough to reuse."""
    try:
        updated = row.get("updated_at")
        if not updated:
            return False
        # Postgres returns naive datetimes for TIMESTAMP columns.
        if isinstance(updated, datetime):
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            else:
                updated = updated.astimezone(timezone.utc)
        else:
            return False
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age < CACHE_FRESH_HOURS * 3600
    except Exception:
        return False


def _row_to_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a cache row into an entry dict, or {} when all-zero."""
    entry = {
        "lastfm_listeners": int(row.get("lastfm_listeners") or 0),
        "lastfm_playcount": int(row.get("lastfm_playcount") or 0),
        "listenbrainz_listens": int(row.get("listenbrainz_listens") or 0),
        "listenbrainz_users": int(row.get("listenbrainz_users") or 0),
    }
    _source = str(row.get("source") or "")
    if not any(entry.values()) and _source != "album_tracklist":
        return {}
        
    if _source == "album_tracklist":
        # Release-first entries are authoritative
        entry["_album_tracklist"] = True
        entry["source"] = "album_tracklist"
        
    _tags = row.get("lastfm_tags")
    if _tags:
        entry["lastfm_tags"] = _tags
    return entry


def _get_lastfm_client() -> Any:
    """Build a LastFmClient from config, or None when no API key is set."""
    try:
        from helpers.config_helpers import get_config
        cfg = get_config().get("api_integrations", {}).get("lastfm", {})
        api_key = cfg.get("api_key", "")
        if api_key:
            from api_clients.lastfm import LastFmClient
            return LastFmClient(api_key)
    except Exception as exc:
        logger.debug("LastFmClient build failed", error=str(exc))
    return None


def _extract_top_tags(track: dict[str, Any]) -> str:
    """Extract the top Last.fm tag names for one top-tracks entry."""
    try:
        import json as _json
        raw = (track.get("toptags") or {}).get("tag") or []
        names: list[str] = []
        seen: set[str] = set()
        for tag in raw:
            if isinstance(tag, dict):
                name = str(tag.get("name") or "").strip()
            else:
                name = str(tag or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(name)
        return _json.dumps(names[:15], ensure_ascii=False) if names else ""
    except Exception:
        return ""


def _lf_top_tracks_map(lastfm_client: Any, artist: str) -> dict[str, dict[str, int]]:
    """One ``artist.getTopTracks`` call -> {normalized title: listeners/playcount}."""
    try:
        primary = get_primary_artist_preserve_case(artist) or artist
        # Folded so an accented spelling ("Lïve") and its ASCII form ("Live")
        # resolve to the SAME cache entry rather than fetching twice.
        cache_key = strip_diacritics(primary).casefold().strip() or artist
        
        with _CACHE_LOCK:
            if cache_key in _lf_top_tracks_cache:
                return _lf_top_tracks_cache[cache_key]
                
        top_tracks = lastfm_client.get_artist_top_tracks(primary, limit=200) or []
    except Exception as exc:
        logger.debug("artist.getTopTracks failed", artist=artist, error=str(exc))
        return {}
        
    out: dict[str, dict[str, int]] = {}
    titles: dict[str, str] = {}
    tags: dict[str, str] = {}
    acc: dict[str, dict[str, int]] = {}
    best: dict[str, tuple[int, str]] = {}
    
    for track in top_tracks:
        if not isinstance(track, dict):
            continue
        name = track.get("name")
        if not name:
            continue
        key = _norm(name)
        if not key:
            continue
            
        listeners = int(track.get("listeners") or 0)
        playcount = int(track.get("playcount") or 0)
        
        entry = acc.setdefault(key, {"lastfm_listeners": 0, "lastfm_playcount": 0})
        entry["lastfm_listeners"] += listeners
        entry["lastfm_playcount"] += playcount
        
        if key not in best or listeners > best[key][0]:
            best[key] = (listeners, name)
            _tags = _extract_top_tags(track)
            if _tags:
                tags[key] = _tags
                
    for key, counts in acc.items():
        out[key] = dict(counts)
        titles[key] = best.get(key, (0, key))[1]
        
    with _CACHE_LOCK:
        _lf_top_tracks_cache[cache_key] = out
        _lf_top_tracks_titles[cache_key] = titles
        _lf_top_tracks_tags[cache_key] = tags
        
    return out


def _norm(title: str) -> str:
    return normalize_for_aggregation(title or "")


def get_artist_top_tracks_map(lastfm_client: Any, artist: str) -> dict[str, dict[str, int]]:
    return _lf_top_tracks_map(lastfm_client, artist)


def prefetch_artist_popularity(
    artist: str,
    tracks: list[dict[str, Any]],
    lastfm_client: Any = None,
    lb_data: dict[str, dict[str, int | None]] | None = None,
    force: bool = False,
    cache_full_catalogue: bool = False,
) -> dict[str, dict[str, Any]]:
    """Populate the cache for an artist's tracks and return fresh entries."""
    if not artist or not tracks:
        return {}

    titles = [t.get("title") for t in tracks if t.get("title")]
    if not titles:
        return {}

    if lastfm_client is None:
        lastfm_client = _get_lastfm_client()

    cached = cache_repo.get_cached_popularity_for_titles(artist, titles)
    cached_by_norm: dict[str, dict[str, Any]] = {}
    
    for _row in cached.values():
        _key = _norm(str(_row.get("title") or ""))
        if not _key:
            continue
        _existing = cached_by_norm.get(_key)
        if _existing is None:
            cached_by_norm[_key] = _row
        elif int(_row.get("lastfm_listeners") or 0) > int(_existing.get("lastfm_listeners") or 0):
            cached_by_norm[_key] = _row
            
    cached = cached_by_norm
    entries: dict[str, dict[str, Any]] = {}

    # 1. Quick check: seed from fresh cached values
    if not force:
        for title, row in cached.items():
            entry = _row_to_entry(row)
            if entry and _is_fresh(row):
                entries[title] = entry

    def _missing(field: str) -> bool:
        return any(
            not (entries.get(_norm(t.get("title"))) or {}).get(field)
            for t in tracks if t.get("title")
        )

    _lf_cache_key = strip_diacritics(
        get_primary_artist_preserve_case(artist) or artist
    ).casefold().strip() or artist

    # 2. Last.fm: one bulk call, only when some title lacks LF data.
    if lastfm_client is not None and _missing("lastfm_listeners"):
        lf_map = _lf_top_tracks_map(lastfm_client, artist)
        
        with _CACHE_LOCK:
            lf_tags = _lf_top_tracks_tags.get(_lf_cache_key) or {}
            
        for track in tracks:
            title = track.get("title")
            if not title:
                continue
            key = _norm(title)
            if key not in entries and key in lf_map:
                entries[key] = dict(lf_map[key])
                if key in lf_tags:
                    entries[key]["lastfm_tags"] = lf_tags[key]

        if cache_full_catalogue and not force and not cached:
            with _CACHE_LOCK:
                title_by_key_lf = _lf_top_tracks_titles.get(_lf_cache_key) or {}
                
            for key, counts in lf_map.items():
                if key in entries:
                    continue
                entries[key] = dict(counts)
            logger.info("Cached full catalogue", artist=artist, titles_count=len(lf_map), reason="no prior data")

    # 3. ListenBrainz: batch fill, only for titles lacking LB data.
    lb_needed = [
        track for track in tracks
        if track.get("title")
        and not (entries.get(_norm(track["title"])) or {}).get("listenbrainz_listens")
        and (entries.get(_norm(track["title"])) or {}).get("source") != "album_tracklist"
    ]
    
    if lb_needed:
        batch = lb_data if lb_data is not None else get_listenbrainz_batch_for_tracks(lb_needed)
        for track in lb_needed:
            mbid = extract_recording_mbid(track)
            data = (batch or {}).get(mbid) if mbid else None
            if not data:
                continue
            key = _norm(track["title"])
            entry = entries.setdefault(key, {})
            entry["listenbrainz_listens"] = int(data.get("total_listen_count") or 0)
            entry["listenbrainz_users"] = int(data.get("total_user_count") or 0)

    # 4. Persist ONLY new/changed rows
    title_by_key = {_norm(t["title"]): t["title"] for t in tracks if t.get("title")}
    
    with _CACHE_LOCK:
        lf_titles = _lf_top_tracks_titles.get(_lf_cache_key) or {}
        lf_tags_persist = _lf_top_tracks_tags.get(_lf_cache_key) or {}
        
    rows_to_upsert = []
    for key, entry in entries.items():
        if not entry:
            continue
        prev = cached.get(key) or {}
        entry_lf_listeners = int(entry.get("lastfm_listeners") or 0)
        entry_lf_playcount = int(entry.get("lastfm_playcount") or 0)
        entry_lb_listens = int(entry.get("listenbrainz_listens") or 0)
        entry_lb_users = int(entry.get("listenbrainz_users") or 0)
        entry_tags = str(entry.get("lastfm_tags") or lf_tags_persist.get(key) or "") or ""
        prev_tags = str(prev.get("lastfm_tags") or "") or ""
        
        if (
            int(prev.get("lastfm_listeners") or 0) == entry_lf_listeners
            and int(prev.get("lastfm_playcount") or 0) == entry_lf_playcount
            and int(prev.get("listenbrainz_listens") or 0) == entry_lb_listens
            and int(prev.get("listenbrainz_users") or 0) == entry_lb_users
            and prev_tags == entry_tags
        ):
            continue 
            
        rows_to_upsert.append({
            "artist": artist,
            "title": title_by_key.get(key) or lf_titles.get(key) or key,
            "lastfm_listeners": entry_lf_listeners,
            "lastfm_playcount": entry_lf_playcount,
            "listenbrainz_listens": entry_lb_listens,
            "listenbrainz_users": entry_lb_users,
            "lastfm_tags": entry_tags or None,
            "source": entry.get("source") or "bulk",
        })
        
    if rows_to_upsert:
        try:
            cache_repo.upsert_track_popularity_bulk(rows_to_upsert)
        except Exception as exc:
            logger.debug("Bulk upsert failed", artist=artist, error=str(exc))

    return entries
