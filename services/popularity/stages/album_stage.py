"""Album enrichment/statistics stage.

Orchestrates album-level enrichment during a popularity scan while delegating
to existing services.enrichment.* and services.metadata.* modules.

This rebuild adds:
- start, completion, failure, skip and heartbeat logs for every scan section
- provider-level album-art logging
- a single shared heartbeat monitor rather than a thread per call
- defensive result parsing
- UTC-aware cache timestamps
- same-session genre updates to avoid nested write transactions/SQLite locks
- consistent exception logging with elapsed time

Correctness controls added in this revision:
- MusicBrainz secondary types (live/acoustic/remix/compilation) must be
  corroborated by the local album or track titles before they are adopted.
  An uncorroborated "+live" classification previously renamed every studio
  track on the album to "... (Live)" and wrote that to the audio files.
- Per-album re-entrancy guard so two pipelines cannot enrich the same album
  concurrently.
- Per-row SAVEPOINTs in write loops, so one failed row cannot abort the whole
  PostgreSQL transaction and silently drop every remaining update.
- Release MBID resolution is skipped when no track actually needs one.

Correctness control added in THIS revision:
- The corroboration guard above is deliberately conservative about what it
  writes to track TITLES and to the persisted, user-facing album type: an
  uncorroborated "+live"/"+acoustic" classification is downgraded to a plain
  "album" so nothing gets destructively retitled. That downgrade previously
  discarded the raw MusicBrainz classification entirely -- once corroboration
  failed, no field anywhere retained the fact that MusicBrainz itself reported
  the release as live, which meant a downstream consumer that only needs to
  know "is this release live" for a NON-destructive purpose (e.g. gating
  Last.fm's title-based version matching so it doesn't merge a live track's
  listener count with its studio namesake) had no way to find that out for an
  album whose local title/track-title heuristics happened not to corroborate
  it. ``_resolve_album_type`` now also returns the raw, pre-corroboration
  MusicBrainz type alongside the safe/corroborated one, and ``enrich_album``
  surfaces it as ``musicbrainz_secondary_type_raw`` on its result so callers
  that only need a read-only "is this live" signal are not limited by the
  title-corroboration heuristic that (correctly) still guards retitling and
  the persisted album type.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, TypeVar

import httpx
import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from db.utils import row_get
from services.catalog.album_classification_service import (
    classify_compilation_category,
    detect_live_album_type,
    is_live_or_alternate_album,
    is_live_or_unplugged_track_title,
    normalize_primary_release_type,
)
from services.enrichment.album_art_service import (
    fetch_album_art_from_musicbrainz,
    save_album_art_to_db,
)
from services.enrichment.artist_bio_service import get_artist_biography
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    get_shared_mb_service,
)
from helpers.normalization_service import (
    append_annotation_once,
    strip_live_acoustic_suffix,
)
Logger = structlog.get_logger(__name__)
T = TypeVar("T")

_SLOW_CALL_HEARTBEAT_SECONDS = max(
    5.0,
    float(os.getenv("ENRICHMENT_HEARTBEAT_SECONDS", "30")),
)
_MONITOR_TICK_SECONDS = 2.0
_HTTP_TIMEOUT = httpx.Timeout(
    connect=float(os.getenv("ENRICHMENT_HTTP_CONNECT_TIMEOUT", "5")),
    read=float(os.getenv("ENRICHMENT_HTTP_READ_TIMEOUT", "15")),
    write=float(os.getenv("ENRICHMENT_HTTP_WRITE_TIMEOUT", "15")),
    pool=float(os.getenv("ENRICHMENT_HTTP_POOL_TIMEOUT", "5")),
)

_MAX_ARTWORK_BYTES = int(os.getenv("ENRICHMENT_MAX_ARTWORK_BYTES", str(20 * 1024 * 1024)))

# How long a cached MusicBrainz artist roster stays usable. Mirrors the window
# the artist page used to apply itself before this moved into the scan: a
# line-up can change, so the value is refreshed rather than frozen forever.
_ARTIST_MEMBERS_TTL_DAYS = int(os.getenv("ARTIST_MEMBERS_TTL_DAYS", "7"))


def _lookup_artist_members(artist: str, mb_client: Any) -> list[dict[str, Any]]:
    """MusicBrainz members of an artist: search by name, then read relations.

    Kept here (rather than in ``artist_metadata_service``) because the SCAN is
    now the only caller — the artist page reads ``artists.members`` from the DB.
    """
    results = mb_client.search_artists(artist, limit=5)
    if not results:
        return []
    preferred = next(
        (a for a in results if (a.get("type") or "").lower() in {"group", "orchestra", "choir"}),
        results[0],
    )
    artist_mbid = preferred.get("id")
    if not artist_mbid:
        return []
    return mb_client.get_artist_members(artist_mbid) or []


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@contextmanager
def _log_section(section: str, **context: Any) -> Iterator[None]:
    start = time.monotonic()
    Logger.info("[ENRICH] section started", section=section, **context)
    try:
        yield
    except Exception as exc:
        Logger.exception(
            "[ENRICH] section failed",
            section=section,
            elapsed_s=round(time.monotonic() - start, 3),
            error=_safe_error(exc),
            **context,
        )
        raise
    else:
        Logger.info(
            "[ENRICH] section completed",
            section=section,
            elapsed_s=round(time.monotonic() - start, 3),
            **context,
        )


# ---------------------------------------------------------------------------
# Shared heartbeat monitor
# ---------------------------------------------------------------------------

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_INFLIGHT_IDS = itertools.count()
_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_INIT_LOCK = threading.Lock()


def _monitor_loop() -> None:
    while True:
        time.sleep(_MONITOR_TICK_SECONDS)
        now = time.monotonic()
        due: list[dict[str, Any]] = []
        with _INFLIGHT_LOCK:
            for entry in _INFLIGHT.values():
                if now >= entry["next_warn"]:
                    entry["next_warn"] = now + _SLOW_CALL_HEARTBEAT_SECONDS
                    due.append(
                        {
                            "section": entry["section"],
                            "elapsed_s": round(now - entry["started"], 1),
                            "context": entry["context"],
                        }
                    )
        for item in due:
            Logger.warning(
                "[ENRICH] section still running",
                section=item["section"],
                elapsed_s=item["elapsed_s"],
                **item["context"],
            )


def _ensure_monitor() -> None:
    global _MONITOR_THREAD
    if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
        return
    with _MONITOR_INIT_LOCK:
        if _MONITOR_THREAD is None or not _MONITOR_THREAD.is_alive():
            _MONITOR_THREAD = threading.Thread(
                target=_monitor_loop,
                name="enrichment-heartbeat-monitor",
                daemon=True,
            )
            _MONITOR_THREAD.start()


def _call_with_heartbeat(
    section: str,
    func: Callable[..., T],
    *args: Any,
    log_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    context = dict(log_context or {})
    start = time.monotonic()
    _ensure_monitor()
    call_id = next(_INFLIGHT_IDS)
    with _INFLIGHT_LOCK:
        _INFLIGHT[call_id] = {
            "section": section,
            "started": start,
            "context": context,
            "next_warn": start + _SLOW_CALL_HEARTBEAT_SECONDS,
        }

    Logger.info("[ENRICH] call started", section=section, **context)
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        Logger.exception(
            "[ENRICH] call failed",
            section=section,
            elapsed_s=round(time.monotonic() - start, 3),
            error=_safe_error(exc),
            **context,
        )
        raise
    else:
        Logger.info(
            "[ENRICH] call completed",
            section=section,
            elapsed_s=round(time.monotonic() - start, 3),
            **context,
        )
        return result
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(call_id, None)


@contextmanager
def _row_savepoint(session: Any) -> Iterator[None]:
    nested = session.begin_nested()
    try:
        yield
    except Exception:
        try:
            nested.rollback()
        except Exception:
            pass
        raise
    else:
        if nested.is_active:
            nested.commit()


# ---------------------------------------------------------------------------
# Per-album re-entrancy guard
# ---------------------------------------------------------------------------

_ACTIVE_ALBUMS_LOCK = threading.Lock()
_ACTIVE_ALBUMS: dict[tuple[str, str], float] = {}


@contextmanager
def _album_scan_guard(artist: str, album: str) -> Iterator[bool]:
    key = (str(artist or "").casefold().strip(), str(album or "").casefold().strip())
    with _ACTIVE_ALBUMS_LOCK:
        started = _ACTIVE_ALBUMS.get(key)
        if started is not None:
            Logger.warning(
                "[ENRICH] album scan already in progress",
                artist=artist,
                album=album,
                running_for_s=round(time.monotonic() - started, 1),
            )
            acquired = False
        else:
            _ACTIVE_ALBUMS[key] = time.monotonic()
            acquired = True
    try:
        yield acquired
    finally:
        if acquired:
            with _ACTIVE_ALBUMS_LOCK:
                _ACTIVE_ALBUMS.pop(key, None)


def _sanitize_release_name(album_name: str) -> str:
    if not album_name:
        return ""
    cleaned = re.sub(
        r"\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]",
        "",
        album_name,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or album_name


_COMPILATION_ARTISTS = frozenset(
    {"various artists", "various artists –", "various", "compilation", "soundtrack"}
)
_HETEROGENEOUS_MARKERS = (
    "+compilation", "(compilation)", "+soundtrack", "(soundtrack)",
    "+live", "(live)", "+remix", "(remix)", "+spokenword", "(spokenword)",
)

#: Patterns used ONLY by ``_detect_album_type`` to classify a LOCAL album title
#: (no MusicBrainz input). Deliberately narrow: this drives the type written to
#: the DB from the title alone.
#:
#: DO NOT add the corroboration guard's logic here — ``_album_title_suggests_live``
#: used to read this same tuple, which is how the bug below happened. See that
#: function for the split and the reasoning.
_LIVE_ALBUM_PATTERNS = (
    r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
    r"\blive\s+session\b", r"[\(\[]live[\)\]]\s*$",
    r"-\s*live\s*$", r",\s*live\s*$", r"\+\s*live\s*$",
    r"live\s+recording\b", r"live\s+tour\b", r"\bin\s+concert\b",
    r"\bunplugged\b", r"\bacoustic\b",
)

_DESTRUCTIVE_SECONDARY_TYPES = ("+live", "+acoustic", "+remix")
_LIVE_TRACK_CORROBORATION_RATIO = 0.5


def _detect_album_type(
    artist: str,
    album: str,
    album_artist: str | None,
    spotify_type: str | None,
) -> str:
    artist_lower = (artist or "").casefold().strip()
    album_lower = (album or "").casefold().strip()
    album_artist_lower = (album_artist or "").casefold().strip()

    if artist_lower in _COMPILATION_ARTISTS or album_artist_lower in _COMPILATION_ARTISTS:
        return "album+compilation"
        
    # Respect existing rich types from the DB (manual UI edits)
    if spotify_type:
        spotify_lower = spotify_type.casefold()
        if "compilation" in spotify_lower or "+compilation" in spotify_lower:
            return "album+compilation"
        if "+live" in spotify_lower or "live" == spotify_lower:
            return "album+live"
        if "+acoustic" in spotify_lower:
            return "album+acoustic"
        if "+remix" in spotify_lower:
            return "album+remix"
        if "+soundtrack" in spotify_lower:
            return "album+soundtrack"

    if "soundtrack" in album_lower:
        return "album+soundtrack"
    if any(re.search(pattern, album_lower) for pattern in _LIVE_ALBUM_PATTERNS):
        return "album+live"
    if "+remix" in album_lower or "(remix)" in album_lower:
        return "album+remix"
        
    if spotify_type:
        spotify_lower = spotify_type.casefold()
        if spotify_lower in {"single", "ep"}:
            return spotify_lower
            
    return "album"


def _album_title_suggests_live(album: str) -> bool:
    """True when the LOCAL album TITLE corroborates a live/acoustic release."""
    try:
        return is_live_or_alternate_album(album)
    except Exception:
        return any(re.search(pattern, (album or "").casefold()) for pattern in _LIVE_ALBUM_PATTERNS)


def _live_track_ratio(tracks: list[dict[str, Any]]) -> tuple[int, int]:
    titled = [str(track.get("title") or "") for track in tracks or []]
    titled = [title for title in titled if title.strip()]
    if not titled:
        return 0, 0
    live = 0
    for title in titled:
        try:
            if is_live_or_unplugged_track_title(title):
                live += 1
        except Exception:
            continue
    return live, len(titled)


def _mb_type_is_corroborated(
    mb_type: str,
    album: str,
    tracks: list[dict[str, Any]],
    context: dict[str, Any],
) -> bool:
    lower = (mb_type or "").casefold()
    marker = next(
        (value for value in _DESTRUCTIVE_SECONDARY_TYPES if value in lower),
        "",
    )
    if not marker:
        return True

    if marker == "+remix":
        if "remix" in (album or "").casefold():
            return True
        Logger.warning(
            "[ENRICH] MusicBrainz secondary type rejected",
            reason="album title carries no remix marker",
            musicbrainz_type=mb_type,
            **context,
        )
        return False

    if _album_title_suggests_live(album):
        return True

    live_tracks, total_tracks = _live_track_ratio(tracks)
    if total_tracks and (live_tracks / total_tracks) >= _LIVE_TRACK_CORROBORATION_RATIO:
        Logger.info(
            "[ENRICH] MusicBrainz secondary type corroborated by track titles",
            musicbrainz_type=mb_type,
            live_track_count=live_tracks,
            track_count=total_tracks,
            **context,
        )
        return True

    Logger.warning(
        "[ENRICH] MusicBrainz secondary type rejected",
        reason=(
            "neither the album title nor the track titles corroborate a live "
            "or acoustic release; refusing to retitle tracks"
        ),
        musicbrainz_type=mb_type,
        live_track_count=live_tracks,
        track_count=total_tracks,
        **context,
    )
    return False


def _persist_artist_external_ids(artist: str, mbid: str | None = None, discogs_id: str | None = None) -> None:
    if not artist:
        return
    try:
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO artists (id, name, musicbrainz_artistid, discogs_artist_id)
                    VALUES (:name, :name, :mbid, :did)
                    ON CONFLICT (name) DO UPDATE SET
                        musicbrainz_artistid = COALESCE(NULLIF(EXCLUDED.musicbrainz_artistid, ''), artists.musicbrainz_artistid),
                        discogs_artist_id = COALESCE(NULLIF(EXCLUDED.discogs_artist_id, ''), artists.discogs_artist_id)
                """),
                {"name": artist, "mbid": mbid or "", "did": discogs_id or ""}
            )
    except Exception as exc:
        Logger.debug("Artist external Ids persistence failed", artist=artist, error=str(exc))


def _http_get_bytes(url: str, *, section: str, context: dict[str, Any]) -> bytes | None:
    response = _call_with_heartbeat(
        section,
        httpx.get,
        url,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
        log_context=context,
    )
    if response.status_code != 200:
        Logger.info(
            "[ENRICH] artwork HTTP response had no usable image",
            section=section,
            status_code=response.status_code,
            **context,
        )
        return None
    content = response.content or None
    if content and len(content) > _MAX_ARTWORK_BYTES:
        Logger.warning(
            "[ENRICH] artwork rejected",
            reason="response exceeded maximum artwork size",
            section=section,
            byte_count=len(content),
            **context,
        )
        return None
    return content


def _fetch_album_art_with_fallback(
    artist: str,
    album: str,
    discogs_token: str | None = None,
) -> str | None:
    clean_album = _sanitize_release_name(album)
    context = {"artist": artist, "album": album}

    Logger.info("[ENRICH] album-art pipeline started", **context)

    _cached_blob = None
    _cached_source = ""
    try:
        from db.repositories.metadata import fetch_album_art_record

        cached = _call_with_heartbeat(
            "album_art.cache",
            fetch_album_art_record,
            artist=artist,
            album=album,
            log_context=context,
        )
        if isinstance(cached, (tuple, list)) and cached:
            _cached_blob = cached[0]
            _cached_source = str(cached[2] if len(cached) > 2 else "") or ""

        if _cached_blob:
            # Cached art is only "done" when it is Navidrome's own copy or the
            # USER's choice. Art a provider supplied gets first refusal from
            # Navidrome — otherwise the cache made the Navidrome step
            # unreachable for every album that had ever collected CAA/Discogs
            # art, which is the reported "it looks online for it, but it should
            # first use the coverart from Navidrome".
            from services.enrichment.album_art_service import navidrome_art_may_replace

            if not navidrome_art_may_replace(_cached_source):
                Logger.info("[ENRICH] album art cache hit", source=_cached_source or "cached", **context)
                return "cached"
            Logger.info(
                "[ENRICH] album art cache hit (provider art — checking Navidrome first)",
                source=_cached_source or "unknown",
                **context,
            )
        else:
            Logger.info("[ENRICH] album art cache miss", **context)
    except Exception as exc:
        Logger.warning("[ENRICH] album-art cache check failed", error=_safe_error(exc), **context)

    try:
        from services.enrichment.album_art_service import fetch_album_art_from_navidrome
        data = _call_with_heartbeat(
            "album_art.navidrome",
            fetch_album_art_from_navidrome,
            artist,
            album,
            log_context=context,
        )
        if data:
            _call_with_heartbeat(
                "album_art.navidrome.persist",
                save_album_art_to_db,
                artist,
                album,
                data,
                source="navidrome",
                log_context=context,
            )
            return "navidrome"
        Logger.info("[ENRICH] Navidrome returned no album art", **context)
    except Exception as exc:
        Logger.warning("[ENRICH] Navidrome album-art provider failed", error=_safe_error(exc), **context)

    if _cached_blob:
        # Navidrome has no new copy, so the art already stored STANDS. Falling
        # through would re-download the same provider art we already hold (and
        # could replace a good cover with a worse one).
        Logger.info("[ENRICH] album art kept", source=_cached_source or "cached", **context)
        return "cached"

    try:
        data = _call_with_heartbeat(
            "album_art.musicbrainz_caa",
            fetch_album_art_from_musicbrainz,
            artist,
            clean_album,
            log_context=context,
        )
        if data:
            _call_with_heartbeat(
                "album_art.musicbrainz_caa.persist",
                save_album_art_to_db,
                artist,
                album,
                data,
                source="musicbrainz",
                log_context=context,
            )
            return "musicbrainz"
        Logger.info("[ENRICH] MusicBrainz/CAA returned no album art", **context)
    except Exception as exc:
        Logger.warning("[ENRICH] MusicBrainz/CAA album-art provider failed", error=_safe_error(exc), **context)

    try:
        from api_clients.audiodb import get_album_artwork
        art_url = _call_with_heartbeat(
            "album_art.audiodb.lookup",
            get_album_artwork,
            artist,
            clean_album,
            enabled=True,
            log_context=context,
        )
        if art_url:
            data = _http_get_bytes(str(art_url), section="album_art.audiodb.download", context=context)
            if data:
                _call_with_heartbeat(
                    "album_art.audiodb.persist",
                    save_album_art_to_db,
                    artist,
                    album,
                    data,
                    source="audiodb",
                    log_context=context,
                )
                return "audiodb"
        Logger.info("[ENRICH] AudioDB returned no album art", **context)
    except Exception as exc:
        Logger.warning("[ENRICH] AudioDB album-art provider failed", error=_safe_error(exc), **context)

    token_is_valid = bool(
        discogs_token
        and len(discogs_token) >= 10
        and discogs_token.casefold() not in {"your_discogs_token", "your_token", "placeholder"}
    )
    if not token_is_valid:
        Logger.info("[ENRICH] Discogs album-art provider skipped", reason="token unavailable", **context)
    else:
        try:
            from api_clients.discogs_http import DiscogsHttpClient
            client = DiscogsHttpClient(discogs_token)
            results = _call_with_heartbeat(
                "album_art.discogs.lookup",
                client.search_album_release,
                artist,
                clean_album,
                log_context=context,
            ) or []
            cover_url = results[0].get("cover_image") if results and isinstance(results[0], dict) else None
            if cover_url:
                data = _http_get_bytes(str(cover_url), section="album_art.discogs.download", context=context)
                if data:
                    _call_with_heartbeat(
                        "album_art.discogs.persist",
                        save_album_art_to_db,
                        artist,
                        album,
                        data,
                        source="discogs",
                        log_context=context,
                    )
                    return "discogs"
            Logger.info("[ENRICH] Discogs returned no album art", **context)
        except Exception as exc:
            Logger.warning("[ENRICH] Discogs album-art provider failed", error=_safe_error(exc), **context)

    Logger.info("[ENRICH] album-art pipeline completed without artwork", **context)
    return None


def _fetch_artist_metadata(artist: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "country": None, "bio": None, "image_url": None,
        "members": None, "members_last_updated": None,
    }
    context = {"artist": artist}

    with _log_section("artist_metadata.database_read", **context):
        with db_session() as session:
            existing = session.execute(
                text(
                    "SELECT country, bio, image_url, members, members_last_updated "
                    "FROM artists WHERE name = :artist"
                ),
                {"artist": artist},
            ).mappings().first()
        if existing:
            result.update(
                country=existing.get("country") or None,
                bio=existing.get("bio") or None,
                image_url=existing.get("image_url") or None,
                members=existing.get("members") or None,
                members_last_updated=existing.get("members_last_updated") or None,
            )

    if result["bio"]:
        Logger.info("[ENRICH] artist biography lookup skipped", reason="already cached", **context)
    else:
        try:
            bio = _call_with_heartbeat(
                "artist_metadata.biography",
                get_artist_biography,
                artist,
                log_context=context,
            )
            result["bio"] = str(bio) if bio else None
        except Exception as exc:
            Logger.warning("[ENRICH] biography lookup failed", error=_safe_error(exc), **context)

    if result["country"]:
        Logger.info("[ENRICH] artist country lookup skipped", reason="already cached", **context)
    else:
        try:
            mb_client = get_shared_mb_client()
            country = _call_with_heartbeat(
                "artist_metadata.country",
                mb_client.get_artist_country,
                artist,
                log_context=context,
            )
            result["country"] = str(country) if country else None
        except Exception as exc:
            Logger.warning("[ENRICH] country lookup failed", error=_safe_error(exc), **context)

    if result["image_url"]:
        Logger.info("[ENRICH] artist image lookup skipped", reason="already cached", **context)
    else:
        try:
            from api_clients.audiodb import get_artist_fanart
            image = _call_with_heartbeat(
                "artist_metadata.image",
                get_artist_fanart,
                artist,
                enabled=True,
                log_context=context,
            )
            result["image_url"] = str(image) if image else None
        except Exception as exc:
            Logger.warning("[ENRICH] artist image lookup failed", error=_safe_error(exc), **context)

    # ------------------------------------------------------------------
    # Line-up (members).
    #
    # Fetched HERE — once per artist per scan — instead of by the artist page,
    # which called MusicBrainz on EVERY page load and blocked a hypercorn
    # worker behind the shared 1 req/s throttle whenever a scan held the
    # budget (the page then looked frozen for up to 40 s). The page is a
    # read-only view of the scan's work; it never resolves metadata.
    #
    # The freshness window is the one the page used to apply, kept so a
    # line-up change still lands without a manual refresh. A lookup that
    # returns nothing leaves ``members_last_updated`` untouched, so it is
    # retried on the next scan rather than caching the failure.
    # ------------------------------------------------------------------
    _members_fresh = False
    if result.get("members") and result.get("members_last_updated"):
        try:
            _updated = datetime.fromisoformat(
                str(result["members_last_updated"]).replace("Z", "+00:00")
            )
            if _updated.tzinfo is None:
                _updated = _updated.replace(tzinfo=timezone.utc)
            _members_fresh = (datetime.now(timezone.utc) - _updated) < timedelta(
                days=max(1, _ARTIST_MEMBERS_TTL_DAYS)
            )
        except Exception:
            _members_fresh = False

    if _members_fresh:
        Logger.info("[ENRICH] artist members lookup skipped", reason="fresh cache", **context)
    else:
        try:
            members = _call_with_heartbeat(
                "artist_metadata.members",
                _lookup_artist_members,
                artist,
                get_shared_mb_client(),
                log_context=context,
            )
            if members:
                result["members"] = json.dumps(members, ensure_ascii=False)
                result["members_last_updated"] = datetime.now(timezone.utc).isoformat()
            else:
                Logger.info("[ENRICH] artist members lookup returned nothing", **context)
        except Exception as exc:
            Logger.warning("[ENRICH] artist members lookup failed", error=_safe_error(exc), **context)

    if not any(result.values()):
        Logger.info("[ENRICH] artist metadata persistence skipped", reason="no values resolved", **context)
    else:
        try:
            with _log_section("artist_metadata.database_write", **context):
                with db_session() as session:
                    session.execute(
                        text("""
                            INSERT INTO artists (id, name, country, bio, image_url, members, members_last_updated)
                            VALUES (:artist, :artist, :country, :bio, :image_url, :members, :members_last_updated)
                            ON CONFLICT (name) DO UPDATE SET
                                country = COALESCE(excluded.country, artists.country),
                                bio = COALESCE(excluded.bio, artists.bio),
                                image_url = COALESCE(excluded.image_url, artists.image_url),
                                members = COALESCE(excluded.members, artists.members),
                                members_last_updated = COALESCE(excluded.members_last_updated, artists.members_last_updated)
                        """),
                        {"artist": artist, **result},
                    )
        except Exception as exc:
            Logger.warning("[ENRICH] artist metadata persistence failed", error=_safe_error(exc), **context)

    Logger.info(
        "[ENRICH] artist metadata result",
        country=result["country"],
        has_bio=bool(result["bio"]),
        has_image=bool(result["image_url"]),
        **context,
    )
    return result


def _json_list(raw: Any) -> list[Any]:
    if raw in (None, "", "null"):
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, dict):
        return list(raw.values())
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return list(parsed) if isinstance(parsed, (list, tuple)) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _fetch_similar_artists(artist: str, options: dict[str, Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {"lastfm": [], "listenbrainz": []}
    context = {"artist": artist}

    if options.get("singles_only") or options.get("singles_with_missing_popularity"):
        Logger.info("[ENRICH] similar artists skipped", reason="singles pass", **context)
        return result

    try:
        with _log_section("similar_artists.cache_read", **context):
            with db_session() as session:
                cached = session.execute(
                    text("""
                        SELECT similar_artists_lastfm,
                               similar_artists_listenbrainz,
                               similar_artists_last_updated
                        FROM artists WHERE name = :artist
                    """),
                    {"artist": artist},
                ).mappings().first()

        cache_fresh = False
        cache_age_days: int | None = None
        if cached:
            result["lastfm"] = _json_list(row_get(cached, "similar_artists_lastfm"))
            result["listenbrainz"] = _json_list(row_get(cached, "similar_artists_listenbrainz"))
            timestamp_raw = row_get(cached, "similar_artists_last_updated")
            if timestamp_raw:
                try:
                    timestamp = (
                        timestamp_raw
                        if isinstance(timestamp_raw, datetime)
                        else datetime.fromisoformat(str(timestamp_raw).replace("Z", "+00:00"))
                    )
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    cache_age_days = max(0, (datetime.now(timezone.utc) - timestamp).days)
                    cache_fresh = cache_age_days < 90
                except (TypeError, ValueError):
                    Logger.warning(
                        "[ENRICH] invalid similar-artist cache timestamp",
                        timestamp=str(timestamp_raw),
                        **context,
                    )

        lastfm_fresh = cache_fresh and bool(result["lastfm"])
        listenbrainz_fresh = cache_fresh and bool(result["listenbrainz"])
        Logger.info(
            "[ENRICH] similar-artist cache status",
            cache_age_days=cache_age_days,
            lastfm_fresh=lastfm_fresh,
            listenbrainz_fresh=listenbrainz_fresh,
            lastfm_count=len(result["lastfm"]),
            listenbrainz_count=len(result["listenbrainz"]),
            **context,
        )
        if lastfm_fresh and listenbrainz_fresh:
            return result

        from helpers.config_helpers import get_config
        config = get_config()

        if lastfm_fresh:
            Logger.info("[ENRICH] Last.fm similar artists skipped", reason="fresh cache", **context)
        else:
            lastfm_config = config.get("api_integrations", {}).get("lastfm", {})
            if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                try:
                    from api_clients.lastfm import LastFmClient
                    similar = _call_with_heartbeat(
                        "similar_artists.lastfm",
                        LastFmClient(lastfm_config["api_key"]).get_similar_artists,
                        artist,
                        limit=10,
                        log_context=context,
                    ) or []
                    result["lastfm"] = [
                        str(item.get("name"))
                        for item in similar
                        if isinstance(item, dict) and item.get("name")
                    ]
                except Exception as exc:
                    Logger.warning("[ENRICH] Last.fm similar artists failed", error=_safe_error(exc), **context)
            else:
                Logger.info("[ENRICH] Last.fm similar artists skipped", reason="integration disabled or key missing", **context)

        if listenbrainz_fresh:
            Logger.info("[ENRICH] ListenBrainz similar artists skipped", reason="fresh cache", **context)
        else:
            try:
                artist_mbid: str | None = None
                with _log_section("similar_artists.musicbrainz_id_read", **context):
                    with db_session() as session:
                        row = session.execute(
                            text(
                                "SELECT NULLIF(TRIM(musicbrainz_artistid), '') AS mbid "
                                "FROM tracks "
                                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                                "AND COALESCE(NULLIF(TRIM(musicbrainz_artistid), ''), '') <> '' "
                                "LIMIT 1"
                            ),
                            {"artist": artist},
                        ).mappings().first()
                    if row:
                        artist_mbid = row_get(row, "mbid")

                if not artist_mbid:
                    from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
                    artist_mbid = _call_with_heartbeat(
                        "similar_artists.musicbrainz_id_lookup",
                        lookup_and_save_artist_mbid,
                        artist,
                        None,
                        log_context=context,
                    )

                if artist_mbid:
                    from api_clients.listenbrainz import ListenBrainzClient
                    lb_similar = _call_with_heartbeat(
                        "similar_artists.listenbrainz",
                        ListenBrainzClient().get_similar_artists,
                        artist_mbid,
                        limit=10,
                        log_context={**context, "artist_mbid": artist_mbid},
                    ) or []
                    result["listenbrainz"] = [
                        str(item.get("name"))
                        for item in lb_similar
                        if isinstance(item, dict) and item.get("name")
                    ]
                else:
                    Logger.info("[ENRICH] ListenBrainz similar artists skipped", reason="artist MBID unavailable", **context)
            except Exception as exc:
                Logger.warning("[ENRICH] ListenBrainz similar artists failed", error=_safe_error(exc), **context)

        try:
            with _log_section("similar_artists.cache_write", **context):
                with db_session() as session:
                    session.execute(
                        text("""
                            INSERT INTO artists (
                                id, name, similar_artists_lastfm,
                                similar_artists_listenbrainz,
                                similar_artists_last_updated
                            )
                            VALUES (:artist, :artist, :lf, :lb, :updated)
                            ON CONFLICT (name) DO UPDATE SET
                                similar_artists_lastfm = excluded.similar_artists_lastfm,
                                similar_artists_listenbrainz = excluded.similar_artists_listenbrainz,
                                similar_artists_last_updated = excluded.similar_artists_last_updated
                        """),
                        {
                            "artist": artist,
                            "lf": json.dumps(result["lastfm"], ensure_ascii=False) if result["lastfm"] else None,
                            "lb": json.dumps(result["listenbrainz"], ensure_ascii=False) if result["listenbrainz"] else None,
                            "updated": datetime.now(timezone.utc).isoformat(),
                        },
                    )
        except Exception as exc:
            Logger.warning("[ENRICH] similar-artist cache persistence failed", error=_safe_error(exc), **context)
    except Exception as exc:
        Logger.exception("[ENRICH] similar artists pipeline failed", error=_safe_error(exc), **context)

    Logger.info(
        "[ENRICH] similar artists result",
        lastfm_count=len(result["lastfm"]),
        listenbrainz_count=len(result["listenbrainz"]),
        **context,
    )
    return result


_DISCOGS_ID_CACHE_MAX = 2000
_discogs_artist_id_cache: "OrderedDict[str, str]" = OrderedDict()
_discogs_artist_id_lock = threading.Lock()


def clear_stage_caches() -> None:
    with _discogs_artist_id_lock:
        _discogs_artist_id_cache.clear()


def _fetch_discogs_artist_id(artist: str, options: dict[str, Any]) -> None:
    context = {"artist": artist}
    if options.get("singles_only") or options.get("singles_with_missing_popularity"):
        Logger.info("[ENRICH] Discogs artist ID skipped", reason="singles pass", **context)
        return
    if artist.casefold().strip() in _COMPILATION_ARTISTS:
        Logger.info("[ENRICH] Discogs artist ID skipped", reason="compilation artist", **context)
        return

    try:
        from helpers.config_helpers import get_config
        discogs_config = get_config().get("api_integrations", {}).get("discogs", {})
        token = str(discogs_config.get("token") or "")
        if not discogs_config.get("enabled") or token.casefold() in {
            "", "your_discogs_token", "your_token", "placeholder"
        }:
            Logger.info("[ENRICH] Discogs artist ID skipped", reason="integration disabled or token missing", **context)
            return

        cache_key = artist.casefold().strip()
        with _discogs_artist_id_lock:
            discogs_artist_id = _discogs_artist_id_cache.get(cache_key, "")
            if cache_key in _discogs_artist_id_cache:
                _discogs_artist_id_cache.move_to_end(cache_key)

        if discogs_artist_id:
            Logger.info("[ENRICH] Discogs artist ID memory-cache hit", discogs_id=discogs_artist_id, **context)
        else:
            from api_clients.discogs_http import DiscogsHttpClient
            value = _call_with_heartbeat(
                "artist_id.discogs.lookup",
                DiscogsHttpClient(token=token).get_artist_id,
                artist,
                log_context=context,
            )
            discogs_artist_id = str(value or "").strip()
            with _discogs_artist_id_lock:
                _discogs_artist_id_cache[cache_key] = discogs_artist_id
                _discogs_artist_id_cache.move_to_end(cache_key)
                while len(_discogs_artist_id_cache) > _DISCOGS_ID_CACHE_MAX:
                    _discogs_artist_id_cache.popitem(last=False)

        if not discogs_artist_id:
            Logger.info("[ENRICH] Discogs artist ID not found", **context)
            return

        with _log_section("artist_id.discogs.persist", **context):
            with db_session() as session:
                result = session.execute(
                    text(
                        "UPDATE tracks SET discogs_artist_id = :did "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "AND (discogs_artist_id IS NULL OR TRIM(CAST(discogs_artist_id AS TEXT)) = '')"
                    ),
                    {"did": discogs_artist_id, "artist": artist},
                )
                rowcount = result.rowcount

        _persist_artist_external_ids(artist, discogs_id=discogs_artist_id)
        Logger.info("[ENRICH] Discogs artist ID persisted", discogs_id=discogs_artist_id, rows_updated=rowcount, **context)
    except Exception as exc:
        Logger.exception("[ENRICH] Discogs artist ID lookup failed", error=_safe_error(exc), **context)


def _fetch_musicbrainz_artist_id(artist: str) -> None:
    context = {"artist": artist}
    if artist.casefold().strip() in _COMPILATION_ARTISTS:
        Logger.info("[ENRICH] MusicBrainz artist ID skipped", reason="compilation artist", **context)
        return
    try:
        with _log_section("artist_id.musicbrainz.database_read", **context):
            with db_session() as session:
                row = session.execute(
                    text(
                        "SELECT NULLIF(TRIM(musicbrainz_artistid), '') AS mbid "
                        "FROM tracks "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "AND COALESCE(NULLIF(TRIM(musicbrainz_artistid), ''), '') <> '' "
                        "LIMIT 1"
                    ),
                    {"artist": artist},
                ).mappings().first()
        existing_mbid = row_get(row, "mbid") if row else None
        if existing_mbid:
            Logger.info("[ENRICH] MusicBrainz artist ID already present", mbid=existing_mbid, **context)
            _persist_artist_external_ids(artist, mbid=str(existing_mbid))
            return

        from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
        mbid = _call_with_heartbeat(
            "artist_id.musicbrainz.lookup_and_persist",
            lookup_and_save_artist_mbid,
            artist,
            None,
            log_context=context,
        )
        if mbid:
            _persist_artist_external_ids(artist, mbid=str(mbid))

        Logger.info("[ENRICH] MusicBrainz artist ID lookup result", found=bool(mbid), mbid=mbid, **context)
    except Exception as exc:
        Logger.exception("[ENRICH] MusicBrainz artist ID lookup failed", error=_safe_error(exc), **context)


def _fetch_external_genres(artist: str) -> dict[str, list[str]]:
    """Retrieve robust genre data from TheAudioDB and Wikidata."""
    context = {"artist": artist}
    results: dict[str, list[str]] = {}

    # 1. Fetch AudioDB Genres
    try:
        from api_clients.audiodb import get_audiodb_genres
        adb_genres = _call_with_heartbeat(
            "external_genres.audiodb",
            get_audiodb_genres,
            artist,
            log_context=context,
        )
        if adb_genres:
            results["audiodb_genres"] = adb_genres
    except Exception as exc:
        Logger.warning("[ENRICH] AudioDB genres failed", error=_safe_error(exc), **context)

    # 2. Fetch Wikidata Genres
    try:
        artist_mbid: str | None = None
        with _log_section("external_genres.wikidata.mbid_read", **context):
            with db_session() as session:
                row = session.execute(
                    text(
                        "SELECT NULLIF(TRIM(musicbrainz_artistid), '') AS mbid "
                        "FROM tracks "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "AND COALESCE(NULLIF(TRIM(musicbrainz_artistid), ''), '') <> '' "
                        "LIMIT 1"
                    ),
                    {"artist": artist},
                ).mappings().first()
            if row:
                artist_mbid = row_get(row, "mbid")

        if artist_mbid:
            from api_clients.wikidata_http import WikidataHttpClient
            wd_client = WikidataHttpClient()
            qid = _call_with_heartbeat(
                "external_genres.wikidata.qid_lookup",
                wd_client.get_qid_by_musicbrainz_id,
                str(artist_mbid),
                log_context=context,
            )
            if qid:
                entity_data = _call_with_heartbeat(
                    "external_genres.wikidata.entity_fetch",
                    wd_client.get_entity,
                    str(qid),
                    log_context=context,
                )
                
                genre_qids = []
                claims = entity_data.get("claims", {})
                if "P136" in claims:
                    for statement in claims["P136"]:
                        datavalue = statement.get("mainsnak", {}).get("datavalue", {})
                        if datavalue.get("type") == "wikibase-entityid":
                            genre_qids.append(datavalue.get("value", {}).get("id"))
                
                if genre_qids:
                    genre_qids = [g for g in genre_qids if g]
                    resolved_entities = _call_with_heartbeat(
                        "external_genres.wikidata.label_resolve",
                        wd_client.get_entities,
                        genre_qids,
                        log_context=context,
                    )
                    
                    wikidata_genres = []
                    for g_qid in genre_qids:
                        g_ent = resolved_entities.get(g_qid, {})
                        labels = g_ent.get("labels", {})
                        en_label = labels.get("en", {}).get("value")
                        if en_label:
                            wikidata_genres.append(en_label)
                    
                    if wikidata_genres:
                        results["wikidata_genres"] = wikidata_genres
                        Logger.info("[ENRICH] Wikidata genres resolved", count=len(wikidata_genres), **context)
        else:
            Logger.info("[ENRICH] Wikidata genres skipped", reason="artist MBID unavailable", **context)
    except Exception as exc:
        Logger.warning("[ENRICH] Wikidata genres failed", error=_safe_error(exc), **context)

    return results


#: Artist credits that identify a Various Artists compilation on their own.
#: Narrower than ``_COMPILATION_ARTISTS`` on purpose: that set also carries
#: "soundtrack"/"compilation", which `classify_compilation_category` files as
#: SINGLE-ARTIST compilations.  The tracklist gate is scoped to VA because VA is
#: the only case with a free artist-similarity contribution — see
#: `_tracklist_corroborates_release_group`.
_VA_ARTIST_MARKERS = frozenset({
    "various artists", "various artist", "various", "va", "v/a",
})


def _is_various_artists_compilation(
    artist: str,
    album_artist: str | None,
    album: str = "",
    tracks: list[dict[str, Any]] | None = None,
) -> bool:
    """True when this album is a Various Artists compilation.

    Deliberately delegates the multi-artist decision to
    `classify_compilation_category` — the app's established definition, the same
    one that produces ``is_va_compilation`` for the star-rating path — so the
    gate and the scoring code agree about what a VA compilation is rather than
    keeping two subtly different lists.

    The name check runs first so the gate still applies when no track list was
    supplied.  That matters because the name is the actual source of the
    problem: MusicBrainz credits every VA release-group "Various Artists", so a
    local artist of the same name scores a free 1.0 on the artist term.
    """
    names = {
        (artist or "").casefold().strip(),
        (album_artist or "").casefold().strip(),
    }
    if names & _VA_ARTIST_MARKERS:
        return True

    if not tracks:
        return False

    try:
        return classify_compilation_category(
            artist or "", album or "", tracks, album_artist=album_artist
        ) == "va"
    except Exception as exc:  # pragma: no cover - defensive
        Logger.debug(
            "[ENRICH] VA compilation classification failed",
            artist=artist,
            album=album,
            error=_safe_error(exc),
        )
        return False


def _compilation_tracklist_guard_config() -> tuple[bool, float]:
    """``(enabled, floor)`` for the VA-compilation tracklist gate.

    Defaults ON with a 0.6 floor.  Read through ``get_config()`` so the Config
    page (the source of truth for user-editable settings) can disable it or
    loosen the requirement without a code change.
    """
    try:
        from helpers.config_helpers import get_config

        block = (get_config() or {}).get("single_detection") or {}
        if not isinstance(block, dict):
            return True, 0.6
        enabled = bool(block.get("compilation_tracklist_guard", True))
        raw_floor = block.get("compilation_tracklist_floor", 0.6)
        try:
            floor = float(raw_floor)
        except (TypeError, ValueError):
            floor = 0.6
        # A floor outside (0, 1] would either disable the gate silently or
        # reject everything; clamp rather than honour a nonsense value.
        if not 0.0 < floor <= 1.0:
            floor = 0.6
        return enabled, floor
    except Exception as exc:  # pragma: no cover - defensive
        Logger.debug("[ENRICH] tracklist guard config read failed", error=_safe_error(exc))
        return True, 0.6


def _tracklist_corroborates_release_group(
    artist: str,
    album: str,
    album_artist: str | None,
    tracks: list[dict[str, Any]],
    release_group_mbid: str,
    context: dict[str, Any],
) -> bool:
    """Does this release-group's tracklist actually look like the local album?

    Only consulted for Various Artists compilations (see the caller).  The
    reason it exists: every MusicBrainz VA release-group is credited
    "Various Artists", so ``calculate_match_score``'s artist term is a free
    ``1.0`` for a compilation and contributes a flat 0.4 that no other album
    gets.  Against the 0.6 acceptance floor that means only ~0.33 of title
    similarity is required, so generic compilations ("Greatest Hits", "Best
    Of") readily bind to unrelated VA release-groups.  Requiring the tracklists
    to agree is what actually distinguishes a real match.

    Fails CLOSED (returns False) when the tracklist cannot be read: the whole
    point is to prevent a bad bind, so an unverifiable candidate must not be
    accepted.  The cost of a false rejection is only that the album keeps its
    title-heuristic type — nothing is written.
    """
    enabled, floor = _compilation_tracklist_guard_config()
    if not enabled:
        Logger.info(
            "[ENRICH] tracklist guard disabled by config",
            release_group_mbid=release_group_mbid,
            **context,
        )
        return True

    if not tracks:
        # No local tracks to compare against, so the gate cannot distinguish a
        # real match.  Fall back to the pre-existing title-only behaviour
        # rather than rejecting outright, which would be a silent regression
        # for any caller that passes an empty track list.
        Logger.info(
            "[ENRICH] tracklist guard skipped",
            reason="no local tracks supplied",
            **context,
        )
        return True

    from services.enrichment.musicbrainz_service import (
        release_group_tracklist_passes,
    )

    try:
        passed, similarity = _call_with_heartbeat(
            "album_type.tracklist_corroboration",
            release_group_tracklist_passes,
            release_group_mbid,
            tracks,
            floor,
            artist,
            album,
            log_context=context,
        )
    except Exception as exc:
        Logger.warning(
            "[ENRICH] tracklist guard errored; rejecting match",
            release_group_mbid=release_group_mbid,
            error=_safe_error(exc),
            **context,
        )
        return False

    if passed:
        Logger.info(
            "[ENRICH] tracklist corroborated MusicBrainz release-group",
            release_group_mbid=release_group_mbid,
            tracklist_similarity=round(float(similarity), 3),
            tracklist_floor=floor,
            local_track_count=len(tracks),
            **context,
        )
        return True

    Logger.warning(
        "[ENRICH] MusicBrainz release-group rejected by tracklist check",
        reason=(
            "a Various Artists compilation matched on title alone, but its "
            "tracklist does not resemble the local album — refusing to bind "
            "the release-group MBID or adopt its album type"
        ),
        release_group_mbid=release_group_mbid,
        tracklist_similarity=round(float(similarity), 3),
        tracklist_floor=floor,
        local_track_count=len(tracks),
        **context,
    )
    return False


def _lookup_musicbrainz_album_type(
    artist: str,
    album: str,
    album_artist: str | None = None,
    tracks: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str | None]:
    # Signature note: `album_artist` and `tracks` are OPTIONAL so the existing
    # two-argument callers (and the tests that call this directly) keep
    # working.  When they are absent the VA tracklist gate cannot run, and the
    # previous title-only behaviour is preserved.
    clean_album = _sanitize_release_name(album)
    context = {"artist": artist, "album": album, "query_album": clean_album}
    try:
        service = get_shared_mb_service()
        matches = _call_with_heartbeat(
            "album_type.musicbrainz.release_group_search",
            service.search_releasegroup_matches,
            artist,
            clean_album,
            limit=3,
            log_context=context,
        ) or []
        if not matches:
            Logger.info("[ENRICH] MusicBrainz album type had no matches", **context)
            return None, None

        best = matches[0] if isinstance(matches[0], dict) else {}
        score = float(best.get("match_score") or 0)
        if score < 0.6:
            Logger.info("[ENRICH] MusicBrainz album type match rejected", match_score=score, **context)
            return None, None

        # Various Artists compilations need a second, independent signal.
        # `album`/`tracks` are passed so a compilation is still recognised when
        # it is not literally credited "Various Artists" (e.g. a release whose
        # tracks each carry a different artist).
        if _is_various_artists_compilation(artist, album_artist, album, tracks):
            candidate_mbid = str(best.get("id") or "").strip()
            if not candidate_mbid or not _tracklist_corroborates_release_group(
                artist, album, album_artist, tracks or [], candidate_mbid, context
            ):
                return None, None

        primary = str(best.get("primary_type") or "").casefold()
        release_group_mbid = str(best.get("id") or "").strip() or None
        secondary = {
            str(value).casefold()
            for value in (best.get("secondary_types") or [])
            if value
        }
        mapping = {
            "single": "single", "ep": "ep", "album": "album",
            "compilation": "album+compilation", "live": "album+live",
            "remix": "album+remix",
        }

        resolved: str | None
        if primary == "album" or primary not in mapping:
            if "live" in secondary:
                resolved = "album+live"
            elif {"acoustic", "unplugged"} & secondary:
                resolved = "album+acoustic"
            elif "compilation" in secondary:
                resolved = "album+compilation"
            elif "remix" in secondary:
                resolved = "album+remix"
            else:
                resolved = mapping.get(primary)
        else:
            resolved = mapping.get(primary)

        Logger.info(
            "[ENRICH] MusicBrainz album type result",
            primary_type=primary,
            secondary_types=sorted(secondary),
            resolved_type=resolved,
            match_score=score,
            release_group_mbid=release_group_mbid,
            matched_title=best.get("title"),
            **context,
        )
        return resolved, release_group_mbid
    except Exception as exc:
        Logger.exception("[ENRICH] MusicBrainz album-type lookup failed safely", error=_safe_error(exc), **context)
        return None, None


def _resolve_album_type(
    artist: str,
    album: str,
    album_artist: str | None,
    spotify_type: str | None,
    tracks: list[dict[str, Any]],
) -> tuple[str, str | None, str | None, str | None]:
    """Resolve the album type used for persistence/retitling, PLUS the raw
    MusicBrainz secondary type before the corroboration guard downgrades it.

    Returns ``(detected, mb_type, release_group_mbid, mb_type_raw)``:

    - ``detected`` / ``mb_type`` / ``release_group_mbid`` behave exactly as
      before -- ``detected`` is the safe, corroborated type used for display,
      persistence and (elsewhere) track retitling.
    - ``mb_type_raw`` is NEW: it is MusicBrainz's own secondary-type
      classification (e.g. ``"album+live"``), set whenever MusicBrainz
      returned a match, REGARDLESS of whether the corroboration guard below
      accepted or rejected it for the safe/destructive paths. Callers that
      only need a read-only "is this release live" signal -- and are not
      retitling tracks or writing the persisted album type -- should use
      this field instead of ``detected``, since ``detected`` can be silently
      downgraded to a plain "album" for releases MusicBrainz correctly
      flagged as live but whose local title/track-title heuristics didn't
      happen to corroborate.
    """
    context = {"artist": artist, "album": album}
    detected = _detect_album_type(artist, album, album_artist, spotify_type)
    Logger.info("[ENRICH] local album type detected", detected_type=detected, **context)

    # ``tracks`` is passed so the VA-compilation tracklist gate can compare the
    # local tracklist against the candidate release-group.  Without it a VA
    # compilation could bind to an unrelated release-group on a title match
    # alone (the artist term is a free 1.0 for every VA candidate).
    mb_type, release_group_mbid = _lookup_musicbrainz_album_type(
        artist, album, album_artist, tracks
    )
    original_mb_type = mb_type

    if mb_type:
        track_count = len(tracks or [])
        if mb_type in {"single", "ep"} and track_count > 6:
            mb_type = "album"
        elif mb_type == "single" and track_count > 3:
            mb_type = "ep"
        if original_mb_type != mb_type:
            Logger.info(
                "[ENRICH] MusicBrainz album type adjusted by track count",
                original_type=original_mb_type,
                adjusted_type=mb_type,
                track_count=track_count,
                **context,
            )

        if not _mb_type_is_corroborated(mb_type, album, tracks or [], context):
            mb_type = "album" if mb_type.startswith("album") else mb_type

        # Only override if our local detection was a generic album, 
        # or if it's downgrading to single/ep and local wasn't explicitly rich.
        if detected == "album":
            detected = mb_type
        elif mb_type in {"single", "ep"} and "+" not in detected:
            detected = mb_type

    Logger.info(
        "[ENRICH] album type resolved",
        detected_type=detected,
        musicbrainz_type=mb_type,
        musicbrainz_type_raw=original_mb_type,
        release_group_mbid=release_group_mbid,
        **context,
    )
    return detected, mb_type, release_group_mbid, original_mb_type


def _needs_release_mbid(artist: str, album: str) -> bool:
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT 1 FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND album = :album "
                    "AND (musicbrainz_album_mbid IS NULL OR TRIM(musicbrainz_album_mbid) = '') "
                    "LIMIT 1"
                ),
                {"artist": artist, "album": album},
            ).first()
        return bool(row)
    except Exception as exc:
        Logger.warning(
            "[ENRICH] release MBID requirement check failed",
            error=_safe_error(exc),
            artist=artist,
            album=album,
        )
        return True


def _persist_album_type_to_tracks(
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    album_type: str,
    release_group_mbid: str | None,
) -> None:
    context = {"artist": artist, "album": album, "album_type": album_type}
    if not album_type:
        Logger.info("[ENRICH] album type persistence skipped", reason="empty album type", **context)
        return

    primary = normalize_primary_release_type(album_type)
    pending = [
        str(track.get("id"))
        for track in tracks or []
        if track.get("id") and str(track.get("musicbrainz_albumtype") or "") != album_type
    ]
    updated = 0
    if not pending:
        Logger.info(
            "[ENRICH] album type track persistence skipped",
            reason="every track already carries this album type",
            track_count=len(tracks or []),
            **context,
        )
    else:
        with _log_section("album_type.track_persist", track_count=len(pending), **context):
            try:
                with db_session() as session:
                    result = session.execute(
                        text("""
                            UPDATE tracks
                            SET spotify_album_type = :album_type,
                                releasetype = :primary,
                                musicbrainz_albumtype = :album_type
                            WHERE CAST(id AS TEXT) IN :track_ids
                        """).bindparams(bindparam("track_ids", expanding=True)),
                        {
                            "album_type": album_type,
                            "primary": primary,
                            "track_ids": pending,
                        },
                    )
                    updated = result.rowcount or 0
                
                # CRITICAL: Identically mutate the in-memory dictionaries so the 
                # next stage doesn't overwrite these DB changes with its stale load values.
                for track in tracks or []:
                    if str(track.get("id")) in pending:
                        track["spotify_album_type"] = album_type
                        track["releasetype"] = primary
                        track["musicbrainz_albumtype"] = album_type
                        
            except Exception as exc:
                Logger.exception("[ENRICH] album type track update failed", error=_safe_error(exc), **context)
        Logger.info(
            "[ENRICH] album type track persistence result",
            attempted=len(pending),
            rows_updated=updated,
            **context,
        )

    if not release_group_mbid:
        Logger.info("[ENRICH] release-group persistence skipped", reason="release-group MBID unavailable", **context)
        return

    try:
        with _log_section("album_type.release_group_persist", release_group_mbid=release_group_mbid, **context):
            with db_session() as session:
                result = session.execute(
                    text("""
                        UPDATE tracks
                        SET musicbrainz_releasegroupid = :release_group_mbid
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND album = :album
                          AND (musicbrainz_releasegroupid IS NULL OR TRIM(musicbrainz_releasegroupid) = '')
                    """),
                    {"release_group_mbid": release_group_mbid, "artist": artist, "album": album},
                )
                release_group_rows = result.rowcount
            
            # Mutate in memory
            for track in tracks or []:
                if not track.get("musicbrainz_releasegroupid"):
                    track["musicbrainz_releasegroupid"] = release_group_mbid
                    
        Logger.info("[ENRICH] release-group MBID persisted", rows_updated=release_group_rows, release_group_mbid=release_group_mbid, **context)
    except Exception as exc:
        Logger.exception("[ENRICH] release-group MBID propagation failed", error=_safe_error(exc), **context)

    if not _needs_release_mbid(artist, album):
        Logger.info(
            "[ENRICH] release MBID resolution skipped",
            reason="every track already has a release MBID",
            release_group_mbid=release_group_mbid,
            **context,
        )
        return

    release_mbid = ""
    try:
        from services.enrichment.musicbrainz_service import resolve_release_id
        value = _call_with_heartbeat(
            "album_type.musicbrainz.release_resolution",
            resolve_release_id,
            release_group_mbid,
            log_context={**context, "release_group_mbid": release_group_mbid},
        )
        release_mbid = str(value or "").strip()
    except Exception as exc:
        Logger.warning("[ENRICH] release MBID resolution failed", error=_safe_error(exc), release_group_mbid=release_group_mbid, **context)

    if not release_mbid or release_mbid == str(release_group_mbid).strip():
        Logger.info(
            "[ENRICH] release MBID persistence skipped",
            reason="release MBID unavailable or identical to release-group MBID",
            release_mbid=release_mbid or None,
            **context,
        )
        return

    try:
        with _log_section("album_type.release_mbid_persist", release_mbid=release_mbid, **context):
            with db_session() as session:
                result = session.execute(
                    text("""
                        UPDATE tracks
                        SET musicbrainz_album_mbid = :release_mbid,
                            musicbrainz_albumid = :release_mbid
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND album = :album
                          AND (musicbrainz_album_mbid IS NULL OR TRIM(musicbrainz_album_mbid) = '')
                    """),
                    {"release_mbid": release_mbid, "artist": artist, "album": album},
                )
                rows_updated = result.rowcount
            
            # Mutate in memory
            for track in tracks or []:
                if not track.get("musicbrainz_album_mbid"):
                    track["musicbrainz_album_mbid"] = release_mbid
                    track["musicbrainz_albumid"] = release_mbid
                    
        Logger.info("[ENRICH] release MBID persisted", rows_updated=rows_updated, release_mbid=release_mbid, **context)
    except Exception as exc:
        Logger.exception("[ENRICH] release MBID propagation failed", error=_safe_error(exc), **context)


def _resolve_album_release_mbid(artist: str, album: str) -> str:
    """The MusicBrainz RELEASE id already stored for an album, or \"\".

    Read from the tracks table rather than taken as an argument, so the
    extended-metadata fill still works on albums whose release id was resolved
    and persisted on an EARLIER scan (the common case on a rescan, where the
    release-MBID block short-circuits because every track already has one).
    """
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT musicbrainz_album_mbid "
                    "FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "  AND album = :album "
                    "  AND musicbrainz_album_mbid IS NOT NULL "
                    "  AND TRIM(musicbrainz_album_mbid) <> '' "
                    "LIMIT 1"
                ),
                {"artist": artist, "album": album},
            ).first()
        if not row:
            return ""
        return str(row[0] or "").strip()
    except Exception as exc:
        Logger.debug(
            "[ENRICH] release-MBID lookup for extended metadata failed",
            error=_safe_error(exc),
            artist=artist,
            album=album,
        )
        return ""


def _persist_release_extended_fields(
    artist: str,
    album: str,
    release_mbid: str = "",
    tracks: list[dict[str, Any]] | None = None,
) -> int:
    """Fill the album page's Extended Metadata columns from the MB release.

    Writes the SIX album-level release fields onto every track of the album so
    they agree — they describe the RELEASE, not an individual track:

        recordlabel, catalognumber, barcode, releasedate, media, releasecountry

    WHY THIS IS NEEDED: nothing ever populated these. The scan only resolved the
    release MBID, and the extended fields were not even EXTRACTED from
    MusicBrainz (``fetch_musicbrainz_release_metadata`` never requested
    ``labels`` and had no ``label-info`` / ``barcode`` / ``release-events``
    parsing), so the album page's Extended Metadata panel was permanently
    blank.

    ``release_mbid`` is optional: when omitted it is read from the album's
    stored tracks, so a rescan still fills an album whose release id was
    persisted earlier.

    Fill-only: a column that already holds a value is left alone, so a manual
    edit on the album page is never silently clobbered by a rescan. Each column
    is considered separately rather than skipping the whole album when one
    field is already set, so a partially-filled album still gains the rest.

    Returns the number of rows updated.
    """
    release_mbid = str(release_mbid or "").strip() or _resolve_album_release_mbid(
        artist, album
    )
    if not release_mbid:
        Logger.info(
            "[ENRICH] extended-metadata skipped",
            reason="no MusicBrainz release id for the album",
            artist=artist,
            album=album,
        )
        return 0
    context = {"artist": artist, "album": album, "release_mbid": release_mbid}

    try:
        from services.enrichment.musicbrainz_service import (
            fetch_musicbrainz_release_metadata,
        )
        release = fetch_musicbrainz_release_metadata(release_mbid) or {}
    except Exception as exc:
        Logger.warning(
            "[ENRICH] extended-metadata fetch failed",
            error=_safe_error(exc),
            **context,
        )
        return 0

    if not release:
        Logger.info(
            "[ENRICH] extended-metadata fetch returned nothing", **context,
        )
        return 0

    # The six columns this function owns. Named after the actual tracks
    # columns so no mapping table is needed.
    _COLUMNS = (
        "recordlabel", "catalognumber", "barcode",
        "releasedate", "media", "releasecountry",
    )

    values: dict[str, str] = {}
    for column in _COLUMNS:
        raw = release.get(column)
        text_value = str(raw or "").strip()
        if not text_value:
            continue
        try:
            # MappedTrait-safe: ints (e.g. a numeric barcode) must persist as
            # their string form, since the columns are TEXT.
            values[column] = text_value
        except Exception:
            continue

    if not values:
        Logger.info(
            "[ENRICH] extended-metadata empty on the MusicBrainz release",
            **context,
        )
        return 0

    # COALESCE(NULLIF(TRIM(col), ''), '') = '' -> fill only when empty.
    set_clause = ", ".join(
        f"{column} = CASE WHEN COALESCE(NULLIF(TRIM({column}), ''), '') = '' "
        f"THEN :{column} ELSE {column} END"
        for column in values
    )
    params: dict[str, Any] = dict(values)
    params.update({"artist": artist, "album": album})

    try:
        with _log_section("full.release_extended_fields_persist", **context):
            with db_session() as session:
                result = session.execute(
                    text(
                        f"""
                        UPDATE tracks
                        SET {set_clause}
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND album = :album
                        """
                    ),
                    params,
                )
                rows_updated = result.rowcount or 0
                
            # Mutate in memory so the track stage sees and saves the changes.
            for track in tracks or []:
                for column, val in values.items():
                    if not str(track.get(column) or "").strip():
                        track[column] = val
                        
        Logger.info(
            "[ENRICH] extended-metadata persisted",
            rows_updated=rows_updated,
            fields=sorted(values),
            **context,
        )
        return rows_updated
    except Exception as exc:
        Logger.exception(
            "[ENRICH] extended-metadata persistence failed",
            error=_safe_error(exc),
            **context,
        )
        return 0


def _genre_values(label: str, mb_genres_raw: Any, genres_raw: Any) -> tuple[Any, str]:
    mb_list = _json_list(mb_genres_raw)
    mb_list = [str(value).strip() for value in mb_list if str(value).strip()]
    if label.casefold() not in {value.casefold() for value in mb_list}:
        mb_list.insert(0, label)
    
    genres = [value.strip() for value in str(genres_raw or "").split(",") if value.strip()]
    if label.casefold() not in {value.casefold() for value in genres}:
        genres.insert(0, label)
    return mb_list, ", ".join(genres)


def _inject_album_genre(
    track_id: str,
    label: str,
    mb_genres_raw: Any,
    genres_raw: Any,
    *,
    session: Any | None = None,
) -> None:
    mb_json, genres_csv = _genre_values(label, mb_genres_raw, genres_raw)
    params = {"mb": json.dumps(mb_json, ensure_ascii=False), "genres": genres_csv, "track_id": str(track_id)}
    statement = text(
        "UPDATE tracks SET musicbrainz_genres = CAST(:mb AS JSONB), genres = :genres WHERE id = :track_id"
    )
    try:
        if session is not None:
            session.execute(statement, params)
        else:
            with db_session() as owned_session:
                owned_session.execute(statement, params)
    except Exception as exc:
        Logger.warning("[ENRICH] genre label injection failed", track_id=track_id, label=label, error=_safe_error(exc))
        raise


def _fetch_artist_lastfm_tags(artist: str) -> None:
    context = {"artist": artist}
    try:
        with _log_section("artist_tags.lastfm.cache_read", **context):
            with db_session() as session:
                row = session.execute(
                    text("SELECT lastfm_artist_tags FROM artists WHERE name = :artist"),
                    {"artist": artist},
                ).first()
        if row and row[0]:
            Logger.info("[ENRICH] Last.fm artist tags skipped", reason="already cached", **context)
            return

        from helpers.config_helpers import get_config
        lastfm_config = get_config().get("api_integrations", {}).get("lastfm", {})
        api_key = str(lastfm_config.get("api_key") or "")
        if not lastfm_config.get("enabled") or api_key in {
            "", "your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>"
        }:
            Logger.info("[ENRICH] Last.fm artist tags skipped", reason="integration disabled or key missing", **context)
            return

        from api_clients.lastfm import LastFmClient
        tags = _call_with_heartbeat(
            "artist_tags.lastfm.lookup",
            LastFmClient(api_key).get_artist_top_tags,
            artist,
            limit=15,
            log_context=context,
        ) or []
        names = [str(tag.get("name")) for tag in tags if isinstance(tag, dict) and tag.get("name")]
        if not names:
            Logger.info("[ENRICH] Last.fm artist tags returned no values", **context)
            return

        with _log_section("artist_tags.lastfm.persist", tag_count=len(names), **context):
            with db_session() as session:
                result = session.execute(
                    text("UPDATE artists SET lastfm_artist_tags = :tags WHERE name = :artist"),
                    {"tags": json.dumps(names, ensure_ascii=False), "artist": artist},
                )
                rows_updated = result.rowcount
        Logger.info("[ENRICH] Last.fm artist tags persisted", tag_count=len(names), rows_updated=rows_updated, **context)
    except Exception as exc:
        Logger.exception("[ENRICH] Last.fm artist tags failed", error=_safe_error(exc), **context)


def ensure_album_type(album_row: dict[str, Any], options: dict[str, Any] | None = None) -> str | None:
    options = options or {}
    artist = str(album_row.get("artist") or "").strip()
    album = str(album_row.get("album") or "").strip()
    tracks = album_row.get("tracks") or []
    context = {"artist": artist, "album": album}
    Logger.info("[ENRICH] ensure album type started", track_count=len(tracks), **context)

    if not artist or not album:
        Logger.warning("[ENRICH] ensure album type skipped", reason="artist or album missing", **context)
        return None

    stored = {
        str(track.get("musicbrainz_albumtype") or "").strip()
        for track in tracks
        if track.get("id")
    }
    stored.discard("")
    if len(stored) == 1 and not options.get("force"):
        value = next(iter(stored))
        Logger.info("[ENRICH] ensure album type cache hit", detected_type=value, **context)
        return value

    detected: str | None = None
    try:
        detected, _mb_type, release_group_mbid, _mb_type_raw = _resolve_album_type(
            artist,
            album,
            str(album_row.get("album_artist") or "") or None,
            str(album_row.get("spotify_album_type") or "") or None,
            tracks,
        )

        if not detected:
            Logger.warning("[ENRICH] ensure album type produced no type", **context)
            return None

        _persist_album_type_to_tracks(artist, album, tracks, detected, release_group_mbid)
        Logger.info("[ENRICH] ensure album type completed", detected_type=detected, **context)
        return detected
    except Exception as exc:
        Logger.exception("[ENRICH] ensure album type failed", detected_type=detected, error=_safe_error(exc), **context)
        return detected


def _apply_live_remix_album_tagging(
    artist: str,
    album: str,
    album_type: str,
    tracks: list[dict[str, Any]],
) -> None:
    lower = (album_type or "").casefold()
    is_live_album = "+live" in lower or "(live)" in lower or "+acoustic" in lower
    is_remix_album = "+remix" in lower or "(remix)" in lower
    context = {"artist": artist, "album": album, "album_type": album_type}

    Logger.info(
        "[ENRICH] live/remix tagging evaluated",
        is_live_album=is_live_album,
        is_remix_album=is_remix_album,
        track_count=len(tracks or []),
        **context,
    )

    if is_live_album:
        live_type = detect_live_album_type(album, album_type) or "live"
        label = "Acoustic" if live_type == "acoustic" else "Live"
        attempted = 0
        updated = 0
        failed = 0
        with _log_section("album_tagging.live_acoustic", label=label, **context):
            with db_session() as session:
                for track in tracks or []:
                    track_id = track.get("id")
                    title = str(track.get("title") or "")
                    if not track_id or not title:
                        continue
                    already_tagged = (
                        (label == "Live" and bool(track.get("is_live")))
                        or (label == "Acoustic" and bool(track.get("is_acoustic")))
                    )
                    if already_tagged:
                        continue
                    attempted += 1
                    if is_live_or_unplugged_track_title(title):
                        new_title = title
                    else:
                        new_title = append_annotation_once(title, label)
                    try:
                        with _row_savepoint(session):
                            result = session.execute(
                                text("""
                                    UPDATE tracks
                                    SET is_live = :is_live,
                                        is_acoustic = :is_acoustic,
                                        album_context_live = 1,
                                        title = :title
                                    WHERE id = :track_id
                                """),
                                {
                                    "is_live": 1 if label == "Live" else 0,
                                    "is_acoustic": 1 if label == "Acoustic" else 0,
                                    "title": new_title,
                                    "track_id": str(track_id),
                                },
                            )
                            _inject_album_genre(
                                str(track_id),
                                label,
                                track.get("musicbrainz_genres"),
                                track.get("genres"),
                                session=session,
                            )
                        if result.rowcount and result.rowcount > 0:
                            updated += result.rowcount
                            # CRITICAL: Mutate in-memory dictionary.
                            track["is_live"] = 1 if label == "Live" else 0
                            track["is_acoustic"] = 1 if label == "Acoustic" else 0
                            track["album_context_live"] = 1
                            track["title"] = new_title
                            mb_json, genres_csv = _genre_values(label, track.get("musicbrainz_genres"), track.get("genres"))
                            track["musicbrainz_genres"] = mb_json
                            track["genres"] = genres_csv
                    except Exception as exc:
                        failed += 1
                        Logger.warning("[ENRICH] live/acoustic track tagging failed", track_id=track_id, error=_safe_error(exc), **context)
        Logger.info("[ENRICH] live/acoustic tagging result", label=label, attempted=attempted, rows_updated=updated, failed=failed, **context)

    if is_remix_album:
        attempted = 0
        updated = 0
        failed = 0
        with _log_section("album_tagging.remix", **context):
            with db_session() as session:
                for track in tracks or []:
                    track_id = track.get("id")
                    if not track_id:
                        continue
                    attempted += 1
                    try:
                        with _row_savepoint(session):
                            result = session.execute(
                                text("UPDATE tracks SET is_remix = 1 WHERE id = :track_id AND COALESCE(is_remix, 0) = 0"),
                                {"track_id": str(track_id)},
                            )
                            _inject_album_genre(
                                str(track_id),
                                "Remix",
                                track.get("musicbrainz_genres"),
                                track.get("genres"),
                                session=session,
                            )
                        if result.rowcount and result.rowcount > 0:
                            updated += result.rowcount
                            # Mutate in memory.
                            track["is_remix"] = 1
                            mb_json, genres_csv = _genre_values("Remix", track.get("musicbrainz_genres"), track.get("genres"))
                            track["musicbrainz_genres"] = mb_json
                            track["genres"] = genres_csv
                    except Exception as exc:
                        failed += 1
                        Logger.warning("[ENRICH] remix track tagging failed", track_id=track_id, error=_safe_error(exc), **context)
        Logger.info("[ENRICH] remix tagging result", attempted=attempted, rows_updated=updated, failed=failed, **context)


def _drop_live_genres_from_json(raw: Any) -> str | None:
    if not raw:
        return None
    parsed = _json_list(raw)
    kept = [value for value in parsed if str(value).strip().casefold() not in {"live", "acoustic"}]
    return json.dumps(kept, ensure_ascii=False) if kept != parsed else None


def _drop_live_genres_from_csv(raw: Any) -> str | None:
    if not raw:
        return None
    parts = [value.strip() for value in str(raw).split(",") if value.strip()]
    kept = [value for value in parts if value.casefold() not in {"live", "acoustic"}]
    return ", ".join(kept) if kept != parts else None


def track_carries_live_state(track: dict[str, Any]) -> bool:
    """True when a track row MIGHT need a live/acoustic revert."""
    if not isinstance(track, dict):
        return True

    if row_get(track, "is_live") or row_get(track, "is_acoustic") or row_get(track, "album_context_live"):
        return True

    title = str(track.get("title") or "")
    if title and strip_live_acoustic_suffix(title) != title:
        return True

    if _drop_live_genres_from_json(track.get("musicbrainz_genres")) is not None:
        return True
    if _drop_live_genres_from_csv(track.get("genres")) is not None:
        return True

    return False


def revert_track_live_state(track_id: str) -> bool:
    """Undo live/acoustic tagging for one track. Returns True when it changed something."""
    context = {"track_id": str(track_id)}
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT title, file_path, genres, musicbrainz_genres, "
                    "COALESCE(is_live, 0) AS is_live, "
                    "COALESCE(is_acoustic, 0) AS is_acoustic, "
                    "COALESCE(album_context_live, 0) AS album_context_live "
                    "FROM tracks WHERE CAST(id AS TEXT) = :track_id"
                ),
                {"track_id": str(track_id)},
            ).mappings().first()
            if not row:
                Logger.warning("[ENRICH] live-state revert skipped", reason="track not found", **context)
                return False

            old_title = str(row_get(row, "title") or "")
            new_title = strip_live_acoustic_suffix(old_title)
            new_mb = _drop_live_genres_from_json(row_get(row, "musicbrainz_genres"))
            new_genres = _drop_live_genres_from_csv(row_get(row, "genres"))
            flags_set = bool(
                row_get(row, "is_live")
                or row_get(row, "is_acoustic")
                or row_get(row, "album_context_live")
            )

            if (
                new_title == old_title
                and new_mb is None
                and new_genres is None
                and not flags_set
            ):
                Logger.debug(
                    "[ENRICH] live-state revert skipped",
                    reason="track carries no live state to revert",
                    title=old_title,
                    **context,
                )
                return False

            session.execute(
                text("""
                    UPDATE tracks
                    SET title = :title,
                        is_live = 0,
                        is_acoustic = 0,
                        album_context_live = 0,
                        musicbrainz_genres = COALESCE(CAST(:mb AS JSONB), musicbrainz_genres),
                        genres = COALESCE(:genres, genres)
                    WHERE CAST(id AS TEXT) = :track_id
                """),
                {
                    "track_id": str(track_id),
                    "title": new_title or old_title,
                    "mb": new_mb,
                    "genres": new_genres,
                },
            )
            file_path = row_get(row, "file_path")

        if new_title != old_title and file_path:
            try:
                from services.metadata.tag_file_service import update_file_tags
                resolved = str(file_path)
                if not os.path.isabs(resolved):
                    from helpers.config_helpers import get_config
                    music_root = (
                        (get_config().get("music", {}) or {}).get("root")
                        or os.environ.get("MUSIC_ROOT", "/music")
                    )
                    resolved = os.path.join(music_root, resolved)
                if os.path.exists(resolved):
                    tags: dict[str, Any] = {"title": new_title or old_title}
                    if new_genres is not None:
                        tags["genres"] = [value.strip() for value in new_genres.split(",") if value.strip()]
                    _call_with_heartbeat(
                        "live_state.file_tag_write",
                        update_file_tags,
                        resolved,
                        tags,
                        log_context=context,
                    )
            except Exception as exc:
                Logger.warning("[ENRICH] live-state file tag write failed", error=_safe_error(exc), **context)

        Logger.info("[ENRICH] live-state revert completed", old_title=old_title, new_title=new_title or old_title, **context)
        return True
    except Exception as exc:
        Logger.exception("[ENRICH] live-state revert failed", error=_safe_error(exc), **context)
        return False


def _persist_alternate_takes(album_context: dict[str, Any]) -> None:
    alternate_takes = (album_context or {}).get("alternate_takes") or {}
    groups_seen = 0
    attempted = 0
    updated = 0
    failed = 0
    with _log_section("alternate_takes.persist", group_count=len(alternate_takes)):
        with db_session() as session:
            for _, variants in alternate_takes.items():
                if not isinstance(variants, list) or len(variants) < 2:
                    continue
                groups_seen += 1
                base_track = variants[0]
                base_id = base_track.get("id") if isinstance(base_track, dict) else None
                if not base_id:
                    continue
                for variant in variants[1:]:
                    alternate_id = variant.get("id") if isinstance(variant, dict) else None
                    if not alternate_id or str(alternate_id) == str(base_id):
                        continue
                    attempted += 1
                    try:
                        with _row_savepoint(session):
                            result = session.execute(
                                text(
                                    "UPDATE tracks SET alternate_take = 1, base_track_id = :base_id "
                                    "WHERE id = :alternate_id AND COALESCE(alternate_take, 0) = 0"
                                ),
                                {"base_id": str(base_id), "alternate_id": str(alternate_id)},
                            )
                        if result.rowcount and result.rowcount > 0:
                            updated += result.rowcount
                            # Mutate in memory.
                            variant["alternate_take"] = 1
                            variant["base_track_id"] = str(base_id)
                    except Exception as exc:
                        failed += 1
                        Logger.warning("[ENRICH] alternate-take persistence failed", alternate_id=alternate_id, error=_safe_error(exc))
    Logger.info(
        "[ENRICH] alternate-take persistence result",
        groups_seen=groups_seen,
        attempted=attempted,
        rows_updated=updated,
        failed=failed,
    )


def _get_discogs_token() -> str | None:
    try:
        from helpers.config_helpers import get_config
        token = get_config().get("api_integrations", {}).get("discogs", {}).get("token")
        token_text = str(token or "").strip()
        if token_text.casefold() in {"", "your_discogs_token", "your_token", "placeholder"}:
            Logger.info("[ENRICH] Discogs token unavailable")
            return None
        return token_text
    except Exception as exc:
        Logger.warning("[ENRICH] Discogs configuration read failed", error=_safe_error(exc))
        return None


def _correct_soundtrack_album_artist(
    artist: str,
    album: str,
    album_artist: str | None,
    release_group_mbid: str | None,
    album_tracks: list[dict[str, Any]],
    album_context: dict[str, Any],
) -> None:
    # Defensively extract variables if a caller didn't explicitly pass them.
    if album_artist is None and album_tracks:
        album_artist = next((str(t.get("album_artist") or "") for t in album_tracks if t.get("album_artist")), "")
        
    current_album_artist = (album_artist or "").strip()
    if current_album_artist.casefold() != "soundtrack":
        return
        
    if not release_group_mbid and album_tracks:
        release_group_mbid = next((str(t.get("musicbrainz_releasegroupid") or "") for t in album_tracks if t.get("musicbrainz_releasegroupid")), "")
        
    if not release_group_mbid:
        return

    context = {"artist": artist, "album": album, "release_group_mbid": release_group_mbid}
    
    try:
        service = get_shared_mb_service()
        rg_data = _call_with_heartbeat(
            "album_artist.musicbrainz.fetch_credits",
            service.get_release_group_by_id,
            release_group_mbid,
            includes=["artist-credits"],
            log_context=context,
        )
        
        if not rg_data or "artist-credit" not in rg_data:
            return

        mb_credit_name = "".join(
            credit.get("name", "") + credit.get("joinphrase", "")
            for credit in rg_data["artist-credit"]
        ).strip()

        if not mb_credit_name or mb_credit_name.casefold() == "soundtrack":
            return

        # 1. Update the Database
        with _log_section("album_artist.correction.persist", old=current_album_artist, new=mb_credit_name, **context):
            with db_session() as session:
                result = session.execute(
                    text("""
                        UPDATE tracks
                        SET album_artist = :new_album_artist
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND album = :album
                          AND album_artist ILIKE 'soundtrack'
                    """),
                    {
                        "new_album_artist": mb_credit_name,
                        "artist": artist,
                        "album": album
                    },
                )
                rows_updated = result.rowcount
                
        # 2. Update the shared in-memory context so track_stage sees the change
        if album_context:
            album_context["album_artist"] = mb_credit_name
                
        # 3. Update physical files and in-memory track dicts
        from services.metadata.tag_file_service import update_file_tags
        from helpers.config_helpers import get_config
        import os
        
        music_root = ((get_config().get("music", {}) or {}).get("root") or os.environ.get("MUSIC_ROOT", "/music"))
        files_updated = 0
        
        for track in album_tracks:
            # CRITICAL: Mutate the in-memory track dictionary so the track stage 
            # doesn't save the stale "Soundtrack" value back to the DB at the end.
            track["album_artist"] = mb_credit_name
            
            file_path = track.get("file_path")
            if not file_path:
                continue
                
            resolved = str(file_path)
            if not os.path.isabs(resolved):
                resolved = os.path.join(music_root, resolved)
                
            if os.path.exists(resolved):
                try:
                    _call_with_heartbeat(
                        "album_artist.correction.file_tag_write",
                        update_file_tags,
                        resolved,
                        {"album_artist": mb_credit_name},
                        log_context=context,
                    )
                    files_updated += 1
                except Exception as exc:
                    Logger.warning("[ENRICH] Soundtrack file tag write failed", track_id=track.get("id"), error=_safe_error(exc), **context)

        Logger.info(
            "[ENRICH] Soundtrack album_artist corrected", 
            old_artist=current_album_artist, 
            new_artist=mb_credit_name, 
            db_rows_updated=rows_updated, 
            files_updated=files_updated,
            **context
        )
        
    except Exception as exc:
        Logger.warning("[ENRICH] Soundtrack album_artist correction failed", error=_safe_error(exc), **context)


def _run_full_enrichment(
    artist: str,
    album: str,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]],
    detected_type: str,
    options: dict[str, Any],
    discogs_token: str | None,
    album_artist: str | None = None,
    release_group_mbid: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    start = time.monotonic()
    context = {"artist": artist, "album": album, "detected_type": detected_type}
    Logger.info("[ENRICH] full album enrichment started", track_count=len(album_tracks), **context)

    with _log_section("full.album_art", **context):
        art_source = _fetch_album_art_with_fallback(artist, album, discogs_token)
    Logger.info("[ENRICH] album-art result", source=art_source, found=bool(art_source), **context)

    with _log_section("full.artist_metadata", **context):
        metadata = _fetch_artist_metadata(artist)

    with _log_section("full.lastfm_tags", **context):
        _fetch_artist_lastfm_tags(artist)

    # ── Extended Metadata (album page panel) ──────────────────────────────
    # Record label / catalog number / barcode / release date / media format /
    # release country come from the MusicBrainz RELEASE. Nothing populated
    # them before, so the panel was permanently blank.
    #
    # This runs BEFORE the artist-country fallback below so a real release
    # country wins, and the runtime-checkable ``_resolve_album_release_mbid``
    # lookup means it fills an album even when the release id was persisted on
    # an earlier scan.
    with _log_section("full.release_extended_fields", **context):
        _persist_release_extended_fields(artist, album, tracks=album_tracks)

    with _log_section("full.soundtrack_correction", **context):
        _correct_soundtrack_album_artist(artist, album, album_artist, release_group_mbid, album_tracks, album_context)

    if metadata.get("country"):
        try:
            with _log_section("full.release_country_backfill", **context):
                with db_session() as session:
                    result = session.execute(
                        text(
                            "UPDATE tracks SET releasecountry = :country "
                            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                            "AND (releasecountry IS NULL OR TRIM(releasecountry) = '')"
                        ),
                        {"country": metadata["country"], "artist": artist},
                    )
                    rows_updated = result.rowcount
                # Mutate in memory.
                for track in album_tracks:
                    if not track.get("releasecountry"):
                        track["releasecountry"] = metadata["country"]
            Logger.info("[ENRICH] release-country backfill result", country=metadata["country"], rows_updated=rows_updated, **context)
        except Exception as exc:
            Logger.exception("[ENRICH] release-country backfill failed", error=_safe_error(exc), **context)

    with _log_section("full.musicbrainz_artist_id", **context):
        _fetch_musicbrainz_artist_id(artist)

    with _log_section("full.similar_artists", **context):
        similar = _fetch_similar_artists(artist, options)

    with _log_section("full.discogs_artist_id", **context):
        _fetch_discogs_artist_id(artist, options)

    with _log_section("full.live_remix_tagging", **context):
        _apply_live_remix_album_tagging(artist, album, detected_type, album_tracks)

    with _log_section("full.alternate_takes", **context):
        _persist_alternate_takes(album_context)

    Logger.info(
        "[ENRICH] full album enrichment completed",
        total_s=round(time.monotonic() - start, 3),
        art_source=art_source,
        lastfm_similar_count=len(similar.get("lastfm") or []),
        listenbrainz_similar_count=len(similar.get("listenbrainz") or []),
        **context,
    )
    return metadata, similar


def enrich_album_extras(
    *,
    artist: str,
    album: str,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]],
    detected_type: str,
    options: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[Any]], dict[str, Any]]:
    Logger.info("[ENRICH] enrich_album_extras started", artist=artist, album=album)

    context = {"artist": artist, "album": album}
    if not _mb_type_is_corroborated(detected_type, album, album_tracks, context):
        detected_type = "album"

    metadata, similar = _run_full_enrichment(
        artist,
        album,
        album_context,
        album_tracks,
        detected_type,
        options,
        _get_discogs_token(),
        album_artist=None,
        release_group_mbid=None
    )
    
    extra_context: dict[str, Any] = {}
    if metadata.get("country"):
        extra_context["artist_country"] = metadata["country"]
    if similar.get("lastfm"):
        extra_context["similar_artists_lastfm"] = similar["lastfm"]
    if similar.get("listenbrainz"):
        extra_context["similar_artists_listenbrainz"] = similar["listenbrainz"]

    external_genres = _fetch_external_genres(artist)
    if external_genres.get("audiodb_genres"):
        extra_context["audiodb_genres"] = external_genres["audiodb_genres"]
    if external_genres.get("wikidata_genres"):
        extra_context["wikidata_genres"] = external_genres["wikidata_genres"]

    return extra_context, similar, metadata


def enrich_album(
    *,
    album_row: dict[str, Any],
    album_context: dict[str, Any],
    stat_eligible_tracks: list[dict[str, Any]],
    options: dict[str, Any],
) -> dict[str, Any]:
    scan_start = time.monotonic()
    artist = str(album_row.get("artist") or "").strip()
    album = str(album_row.get("album") or "").strip()
    album_artist = str(album_row.get("album_artist") or "").strip()
    spotify_type = str(
        album_row.get("musicbrainz_album_type")
        or album_row.get("spotify_album_type")
        or ""
    ).strip()
    album_tracks = album_row.get("tracks") or []
    context = {"artist": artist, "album": album}

    singles_pass = bool(
        options.get("singles_only")
        or options.get("singles_with_missing_popularity")
        or options.get("singles_detection_only")
    )
    popularity_pass = bool(options.get("popularity_only"))
    defer_full = bool(options.get("defer_full_enrichment"))
    metadata: dict[str, Any] = {"country": None, "bio": None, "image_url": None}
    similar: dict[str, list[Any]] = {"lastfm": [], "listenbrainz": []}
    detected_type = "album"
    is_heterogeneous = False
    mb_type_raw: str | None = None

    def _result(detected: str, heterogeneous: bool) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        if metadata.get("country"):
            extras["artist_country"] = metadata["country"]
        if similar.get("lastfm"):
            extras["similar_artists_lastfm"] = similar["lastfm"]
        if similar.get("listenbrainz"):
            extras["similar_artists_listenbrainz"] = similar["listenbrainz"]
        if mb_type_raw:
            extras["musicbrainz_secondary_type_raw"] = mb_type_raw
        return {
            "album_row": album_row,
            "album_context": {**album_context, **extras},
            "stat_eligible_tracks": stat_eligible_tracks,
            "detected_album_type": detected,
            "musicbrainz_secondary_type_raw": mb_type_raw,
            "is_heterogeneous": heterogeneous,
            "similar_artists": similar,
            "artist_metadata": metadata,
        }

    with _album_scan_guard(artist, album) as acquired:
        if not acquired:
            return _result(detected_type, is_heterogeneous)

        try:
            if popularity_pass:
                detected_type = _detect_album_type(artist, album, album_artist or None, spotify_type or None)
                is_heterogeneous = any(marker in detected_type.casefold() for marker in _HETEROGENEOUS_MARKERS)
            else:
                detected_type, mb_type, release_group_mbid, mb_type_raw = _resolve_album_type(
                    artist, album, album_artist or None, spotify_type or None, album_tracks
                )
                is_heterogeneous = any(marker in detected_type.casefold() for marker in _HETEROGENEOUS_MARKERS)
                _persist_album_type_to_tracks(artist, album, album_tracks, detected_type, release_group_mbid)

                if "+compilation" in detected_type.casefold() or "+soundtrack" in detected_type.casefold():
                    with db_session() as session:
                        session.execute(
                            text(
                                "UPDATE tracks SET is_compilation = 1 "
                                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                                "AND album = :album AND COALESCE(is_compilation, 0) = 0"
                            ),
                            {"artist": artist, "album": album},
                        )
                    for track in album_tracks:
                        if not track.get("is_compilation"):
                            track["is_compilation"] = 1

            discogs_token = _get_discogs_token()
            if not popularity_pass and not singles_pass and not defer_full:
                metadata, similar = _run_full_enrichment(
                    artist, album, album_context, album_tracks, detected_type, options, discogs_token,
                    album_artist=album_artist or None, release_group_mbid=release_group_mbid
                )
        except Exception as exc:
            Logger.exception("[ENRICH] album scan failed", error=_safe_error(exc), **context)

        return _result(detected_type, is_heterogeneous)
