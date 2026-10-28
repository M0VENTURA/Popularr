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
from helpers.normalization_service import has_mastering_marker, strip_diacritics
from services.popularity.popularity_matching import (
    get_primary_artist_preserve_case,
    normalize_for_aggregation,
)
from services.popularity.popularity_sources import (
    extract_recording_mbid,
    get_listenbrainz_batch_for_tracks,
)

logger = structlog.get_logger(__name__)

#: Fallback when ``features.prefetch_cache_days`` is unset.
CACHE_FRESH_HOURS = 24 * 7


def _prefetch_cache_hours() -> float:
    """Effective prefetch freshness window in hours, from Config.

    ``features.prefetch_cache_days`` (default 7) is shared with the release
    cache so both prefetches can be aligned with the Scan Behaviour windows.
    ``0`` means "always re-fetch" (never fresh), matching the skip-window
    convention.
    """
    try:
        from helpers.config_helpers import get_feature

        days = get_feature("prefetch_cache_days", 7)
        return max(0.0, float(days or 0)) * 24.0
    except Exception:
        return float(CACHE_FRESH_HOURS)

#: Last.fm ``artist.getTopTracks`` is a SINGLE call per artist, and every
#: title it returns is answered from that one call for the rest of the scan
#: (audit G2). Tracks OUTSIDE the map fall to the deep-cut path —
#: search-aggregation + track.getInfo + variant lookups, 3-6 Last.fm calls
#: EACH per TTL. Widened from 200: the extra titles cost nothing per artist
#: and cover the deep cuts of most discographies. Last.fm may cap lower; a
#: shorter answer is simply a smaller map, never an error.
_LF_TOP_TRACKS_LIMIT = 500  # 1 week

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


def clear_prefetch_memory_caches() -> None:
    """Drop the per-process artist.getTopTracks maps.

    A FORCED scan must not answer from a map built by an earlier pass: that map
    may predate a provider-side change (or our own remaster matching fix), and
    the whole point of a forced refresh is to replace a stale match.
    """
    with _CACHE_LOCK:
        _lf_top_tracks_cache.clear()
        _lf_top_tracks_titles.clear()
        _lf_top_tracks_tags.clear()


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
        hours = _prefetch_cache_hours()
        if hours <= 0:
            return False
        return age < hours * 3600
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
    _mbid = str(row.get("recording_mbid") or "").strip()
    if _mbid:
        # The album-tracklist identity (audit G3): lets the runner skip the
        # per-album ListenBrainz tracklist lookup when the mapping is already
        # on the row.
        entry["recording_mbid"] = _mbid
    _rel_title = str(row.get("release_track_title") or "").strip()
    if _rel_title:
        # The RELEASE's title at the position the identity was resolved from:
        # the runner corroborates it against the LOCAL title before trusting
        # the cached identity (a row cached from a DIFFERENT edition of the
        # album must be re-resolved, not re-applied).
        entry["release_track_title"] = _rel_title
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
                
        top_tracks = lastfm_client.get_artist_top_tracks(primary, limit=_LF_TOP_TRACKS_LIMIT) or []
    except Exception as exc:
        logger.debug("artist.getTopTracks failed", artist=artist, error=str(exc))
        return {}
        
    out: dict[str, dict[str, int]] = {}
    titles: dict[str, str] = {}
    tags: dict[str, str] = {}
    acc: dict[str, dict[str, int]] = {}
    best: dict[str, tuple[int, str]] = {}
    # Remaster rows are the SAME performance as the plain title. Summing them
    # into the plain row doubles the audience (e.g. 400k + 400k = 800k), so a
    # remaster row competes with the plain total on strength instead.
    remaster_best: dict[str, tuple[int, int, str]] = {}
    remaster_tags: dict[str, str] = {}
    plain_playcount_seen: dict[str, bool] = {}
    remaster_playcount_seen: dict[str, bool] = {}

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

        if has_mastering_marker(str(name)):
            previous = remaster_best.get(key)
            if previous is None or (listeners, playcount) > (previous[0], previous[1]):
                remaster_best[key] = (listeners, playcount, str(name))
                remaster_playcount_seen[key] = "playcount" in track
                _tags = _extract_top_tags(track)
                if _tags:
                    remaster_tags[key] = _tags
            continue

        if "playcount" in track:
            plain_playcount_seen[key] = True
        entry = acc.setdefault(key, {"lastfm_listeners": 0, "lastfm_playcount": 0})
        entry["lastfm_listeners"] += listeners
        entry["lastfm_playcount"] += playcount

        if key not in best or listeners > best[key][0]:
            best[key] = (listeners, name)
            _tags = _extract_top_tags(track)
            if _tags:
                tags[key] = _tags

    for key in set(acc) | set(remaster_best):
        plain = acc.get(key)
        remaster = remaster_best.get(key)
        if plain is not None and remaster is not None:
            plain_strength = (plain["lastfm_listeners"], plain["lastfm_playcount"])
            remaster_strength = (remaster[0], remaster[1])
            if remaster_strength > plain_strength:
                out[key] = {
                    "name": remaster[2],
                    "lastfm_listeners": remaster[0],
                    "lastfm_playcount": remaster[1],
                    "_playcount_available": bool(
                        remaster_playcount_seen.get(key, False)
                    ),
                }
                titles[key] = remaster[2]
                if remaster_tags.get(key):
                    tags[key] = remaster_tags[key]
                continue
        if plain is not None:
            out[key] = dict(plain)
            out[key]["name"] = best.get(key, (0, key))[1]
            out[key]["_playcount_available"] = bool(
                plain_playcount_seen.get(key, False)
            )
            titles[key] = best.get(key, (0, key))[1]
            continue
        if remaster is not None:
            out[key] = {
                "name": remaster[2],
                "lastfm_listeners": remaster[0],
                "lastfm_playcount": remaster[1],
                "_playcount_available": bool(
                    remaster_playcount_seen.get(key, False)
                ),
            }
            titles[key] = remaster[2]
            if remaster_tags.get(key):
                tags[key] = remaster_tags[key]

    with _CACHE_LOCK:
        _lf_top_tracks_cache[cache_key] = out
        _lf_top_tracks_titles[cache_key] = titles
        _lf_top_tracks_tags[cache_key] = tags

    return out


def _norm(title: str) -> str:
    return normalize_for_aggregation(title or "")


def get_artist_top_tracks_map(lastfm_client: Any, artist: str) -> dict[str, dict[str, int]]:
    return _lf_top_tracks_map(lastfm_client, artist)


#: Stored form of ``isrc`` that this lookup does NOT need to handle: the track
#: stage normalises a bracketed list to a bare code and writes it back, so a
#: scanned row already holds ``USRC17607839``. A row that has never been
#: scanned may still hold ``["USRC17607839"]``, and the LIKE arm below is what
#: catches those.
def get_recording_identity_popularity(
    tracks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Popularity for these tracks, COPIED from other rows of the same recording.

    ⭐ WHY THE COPY IS SOUND. An ISRC identifies a specific performance, and
    ``recording_mbid`` is the same identity under another name. So for a given
    recording:

    * **ListenBrainz** counts are held against the RECORDING MBID — the same
      number for every album that carries it. Not a heuristic: it is the same
      key.
    * **Last.fm** counts are held against one artist+title track page, so the
      same recording yields the same page and the same listeners.

    Reported: *"if another copy of the same track is found, can it copy the data
    from the other track on the database rather than doing a new lookup"*. Two
    copies reach this point as two INDEPENDENT lookups today, because every
    cache in this module is keyed by ``(artist, title)``:

    * the same recording with a different title spelling on the two albums
      ("Song" vs "Song (feat. X)") keys two rows;
    * a compilation credits "Various Artists" while the artist's own album
      credits the band, and the track stage's prefetch is skipped for a
      compilation artist ENTIRELY.

    ⚠️ WHAT IS **NOT** COPIED, deliberately:

    * ``is_single`` and any score. A single verdict is a fact about a RELEASE,
      not a recording — a track can be a single off one album and a deep cut on
      a compilation. ``finalise_stage._sync_isrc_popularity`` already handles
      the score/is_single side with its own rules; copying them here would
      duplicate (and pre-empt) that decision.
    * a zero count. The caller's branches treat a falsy count as "no data" and
      fall through to the lookup, so returning zeros would change nothing —
      except that the entry would exist. Only rows with a real count are
      offered, so the entry's presence means "this is answered".

    The LENGTH is not consulted either: the identity already established that
    this is the same performance, and the length guard exists to decide identity,
    not to re-litigate it.

    Returns entries keyed by the normalised title of each REQUESTED track, shaped
    like ``prefetched_popularity`` so the per-track paths use them without a
    branch of their own.
    """
    if not tracks:
        return {}

    # Collect the identities this album can be answered FROM. Normalised the same
    # way the track stage normalises before storing, so the two agree.
    codes: list[str] = []
    mbids: list[str] = []
    for track in tracks:
        code = _normalize_isrc_code(track.get("isrc"))
        if code:
            codes.append(code)
        mbid = str(track.get("recording_mbid") or track.get("mbid") or "").strip()
        if mbid:
            mbids.append(mbid)
    codes = sorted(set(codes))
    mbids = sorted(set(mbids))
    if not codes and not mbids:
        return {}

    rows: list[dict[str, Any]] = []
    try:
        from sqlalchemy import bindparam, text as _text

        from db.engine import db_session

        # ⚠️ `IN` with an EXPANDING bindparam, not `= ANY(...)`: ANY is
        # PostgreSQL-only and the test suite runs on SQLite (same house pattern
        # the playlist and track queries use).
        #
        # The LIKE arm catches a row whose ``isrc`` still holds the bracketed
        # list form (``["USRC17607839"]``) because it has never been through a
        # scan. It is a substring match, so it cannot use an index — it is here
        # for correctness, and the equality arm plus the new
        # ``idx_tracks_isrc`` carry the common case.
        #
        # ⚠️ Each arm is added ONLY when it has values. An empty expanding
        # bindparam renders ``IN (NULL)`` and is harmless, but an empty
        # ``(" OR ".join([]))`` leaves a bare ``()`` in the WHERE clause, which
        # is a SYNTAX ERROR — reached whenever a track has a recording MBID and
        # no ISRC, which is common.
        conditions: list[str] = []
        params: dict[str, Any] = {}
        if mbids:
            conditions.append("recording_mbid IN :mbids")
            params["mbids"] = mbids
        if codes:
            conditions.append("isrc IN :codes")
            params["codes"] = codes
            like_arm = " OR ".join(f"isrc LIKE :like{i}" for i in range(len(codes)))
            conditions.append(
                f"(isrc IS NOT NULL AND isrc <> '' AND ({like_arm}))"
            )
            for index, code in enumerate(codes):
                params[f"like{index}"] = f"%{code}%"

        statement = f"""
            SELECT CAST(id AS TEXT) AS id, title, artist, album, isrc, recording_mbid,
                   lastfm_listeners, lastfm_playcount,
                   listenbrainz_listens, listenbrainz_users,
                   lastfm_tags, COALESCE(is_live, 0) AS is_live
            FROM tracks
            WHERE {" OR ".join(conditions)}
        """

        bindings = []
        if mbids:
            bindings.append(bindparam("mbids", expanding=True))
        if codes:
            bindings.append(bindparam("codes", expanding=True))
        bound = _text(statement).bindparams(*bindings)
        with db_session() as session:
            rows = [dict(r._mapping) for r in (session.execute(bound, params).fetchall() or [])]
    except Exception as exc:
        # Never fatal: this is an optimisation. A failure means the normal
        # per-track lookup runs, which is what happened before this existed.
        logger.debug("Recording-identity popularity lookup failed", error=str(exc))
        return {}

    if not rows:
        return {}

    # Index the siblings by BOTH identities, keeping the strongest row per key.
    # "Strongest" = most Last.fm listeners, then most ListenBrainz listens, which
    # matches how the cache read above already breaks ties between variant rows.
    by_code: dict[str, dict[str, Any]] = {}
    by_mbid: dict[str, dict[str, Any]] = {}
    for row in rows:
        # ⚠️ EVERY code on the row, not just the first. A row may legitimately
        # list several ISRCs for one recording (a re-release keeps the original
        # and adds its own), and ``normalize_isrc`` returns only the first in
        # that case — which would leave the row unreachable by the others.
        for code in _isrc_codes_in(row.get("isrc")):
            existing = by_code.get(code)
            if existing is None or _count_strength(row) > _count_strength(existing):
                by_code[code] = row
        mbid = str(row.get("recording_mbid") or "").strip()
        if mbid:
            existing = by_mbid.get(mbid)
            if existing is None or _count_strength(row) > _count_strength(existing):
                by_mbid[mbid] = row

    out: dict[str, dict[str, Any]] = {}
    for track in tracks:
        title = track.get("title")
        if not title:
            continue
        key = normalize_for_aggregation(str(title))
        if not key or key in out:
            continue

        sibling = None
        code = _normalize_isrc_code(track.get("isrc"))
        if code:
            sibling = by_code.get(code)
        if sibling is None:
            mbid = str(track.get("recording_mbid") or track.get("mbid") or "").strip()
            if mbid:
                sibling = by_mbid.get(mbid)
        if sibling is None:
            continue

        # A track must not answer from ITSELF — that is a re-read of the row's own
        # stored counts, which the caller already has.
        if str(sibling.get("id") or "") == str(track.get("id") or ""):
            continue

        # ⭐⭐ TITLE CORROBORATION. An ISRC identifies a performance, but a WRONG
        # ISRC is common in the wild: measured on Arion — "Life Is Not
        # Beautiful", where three bonus tracks all carried the lead single's code
        # (FIRAN1800001) and inherited its 42k-listener row. Matching on the
        # identity ALONE made each one answer from the hit.
        #
        # The comparison is on ``normalize_for_aggregation`` keys, which fold
        # "(feat. X)" / "(Remastered)" / version markers — so a genuine variant
        # spelling of the SAME song still corroborates, while a different song
        # that merely shares a bad code cannot.
        sibling_key = normalize_for_aggregation(str(sibling.get("title") or ""))
        if sibling_key != key:
            continue

        entry = _sibling_entry(sibling)
        if entry:
            out[key] = entry

    return out


def _normalize_isrc_code(value: Any) -> str:
    """Bare 12-character ISRC code, or ``""``.

    Delegates to ``normalize_isrc`` so the code this file matches on is spelled
    exactly the way the track stage stores it.
    """
    if not value:
        return ""
    try:
        from helpers.normalization_service import normalize_isrc

        return normalize_isrc(value)
    except Exception:
        return ""


def _isrc_codes_in(value: Any) -> list[str]:
    """EVERY 12-character ISRC code present in a stored value.

    Used for the SIBLING side of the lookup, where a row listing several codes
    must be reachable by each of them. ``normalize_isrc`` deliberately returns
    only the first code, which is the right contract for the track stage (one
    code to store) and the wrong one for an index.
    """
    if not value:
        return []
    try:
        import re

        if isinstance(value, (list, tuple, set)):
            value = " ".join(str(v) for v in value)
        compact = re.sub(r"[^0-9A-Za-z]+", "", str(value)).upper()
        return sorted({m.group(0) for m in re.finditer(r"[A-Z]{2}[0-9A-Z]{3}[0-9]{7}", compact)})
    except Exception:
        return []


def _count_strength(row: dict[str, Any]) -> tuple[int, int]:
    return (
        int(row.get("lastfm_listeners") or 0),
        int(row.get("listenbrainz_listens") or 0),
    )


def _sibling_entry(row: dict[str, Any]) -> dict[str, Any]:
    """A prefetch-shaped entry from a sibling row, or ``{}`` when it holds nothing.

    Only NON-ZERO counts are offered: the caller's branches fall through to the
    lookup on a falsy count, so an entry carrying zeros would be pure overhead —
    and, worse, would look like an answer to anything that only checks presence.
    """
    lf = int(row.get("lastfm_listeners") or 0)
    lb = int(row.get("listenbrainz_listens") or 0)
    if not lf and not lb:
        return {}

    entry: dict[str, Any] = {
        "lastfm_listeners": lf,
        "lastfm_playcount": int(row.get("lastfm_playcount") or 0),
        "listenbrainz_listens": lb,
        "listenbrainz_users": int(row.get("listenbrainz_users") or 0),
        "source": "isrc_sibling",
        # The identity, so the track stage can SKIP resolving ISRC -> recording
        # over the network (a shared 1 req/s MusicBrainz budget).
        "recording_mbid": str(row.get("recording_mbid") or "").strip() or None,
    }
    tags = row.get("lastfm_tags")
    if tags:
        entry["lastfm_tags"] = tags
    return entry


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
        if force:
            # A forced pass must re-query the provider, not reuse a map built by
            # an earlier pass. This is the "stale/bad match survives a forced
            # refresh" path.
            with _CACHE_LOCK:
                _lf_top_tracks_cache.pop(_lf_cache_key, None)
                _lf_top_tracks_titles.pop(_lf_cache_key, None)
                _lf_top_tracks_tags.pop(_lf_cache_key, None)
            try:
                from services.popularity.popularity_sources import (
                    invalidate_artist_provider_cache,
                )

                invalidate_artist_provider_cache(artist)
            except Exception as exc:
                logger.debug("Provider cache invalidation failed", artist=artist, error=str(exc))

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
