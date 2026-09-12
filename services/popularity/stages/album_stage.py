"""Album enrichment/statistics stage.

Orchestrates album-level enrichment during a popularity scan while delegating
To existing services.enrichment.* and services.metadata.* modules.

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
  Corroborated by the local album or track titles before they are adopted.
  An uncorroborated "+live" classification previously renamed every studio
  Track on the album to "... (Live)" and wrote that to the audio files.
- Per-album re-entrancy guard so two pipelines cannot enrich the same album
  Concurrently.
- Per-row SAVEPOINTs in write loops, so one failed row cannot abort the whole
  PostgreSQL transaction and silently drop every remaining update.
- Release MBID resolution is skipped when no track actually needs one.
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
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, TypeVar

import httpx
import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from db.utils import row_get
from services.catalog.album_classification_service import (
    detect_live_album_type,
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
    if spotify_type:
        spotify_lower = spotify_type.casefold()
        if spotify_lower == "compilation" or "+compilation" in spotify_lower or "(compilation)" in spotify_lower:
            return "album+compilation"
    if "soundtrack" in album_lower:
        return "album+soundtrack"
    if any(re.search(pattern, album_lower) for pattern in _LIVE_ALBUM_PATTERNS):
        return "album+live"
    if "+remix" in album_lower or "(remix)" in album_lower:
        return "album+remix"
    return "album"


def _album_title_suggests_live(album: str) -> bool:
    album_lower = (album or "").casefold().strip()
    return any(re.search(pattern, album_lower) for pattern in _LIVE_ALBUM_PATTERNS)


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

    try:
        from db.repositories.metadata import fetch_album_art_blob
        cached = _call_with_heartbeat(
            "album_art.cache",
            fetch_album_art_blob,
            artist=artist,
            album=album,
            log_context=context,
        )
        blob = cached[0] if isinstance(cached, (tuple, list)) and cached else None
        if blob:
            Logger.info("[ENRICH] album art cache hit", source="cached", **context)
            return "cached"
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
    result: dict[str, Any] = {"country": None, "bio": None, "image_url": None}
    context = {"artist": artist}

    with _log_section("artist_metadata.database_read", **context):
        with db_session() as session:
            existing = session.execute(
                text("SELECT country, bio, image_url FROM artists WHERE name = :artist"),
                {"artist": artist},
            ).mappings().first()
        if existing:
            result.update(
                country=existing.get("country") or None,
                bio=existing.get("bio") or None,
                image_url=existing.get("image_url") or None,
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

    if not any(result.values()):
        Logger.info("[ENRICH] artist metadata persistence skipped", reason="no values resolved", **context)
    else:
        try:
            with _log_section("artist_metadata.database_write", **context):
                with db_session() as session:
                    session.execute(
                        text("""
                            INSERT INTO artists (id, name, country, bio, image_url)
                            VALUES (:artist, :artist, :country, :bio, :image_url)
                            ON CONFLICT (name) DO UPDATE SET
                                country = COALESCE(excluded.country, artists.country),
                                bio = COALESCE(excluded.bio, artists.bio),
                                image_url = COALESCE(excluded.image_url, artists.image_url)
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
                            "lf": json.dumps(result["lastfm"]) if result["lastfm"] else None,
                            "lb": json.dumps(result["listenbrainz"]) if result["listenbrainz"] else None,
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


def _lookup_musicbrainz_album_type(artist: str, album: str) -> tuple[str | None, str | None]:
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
) -> tuple[str, str | None, str | None]:
    context = {"artist": artist, "album": album}
    detected = _detect_album_type(artist, album, album_artist, spotify_type)
    Logger.info("[ENRICH] local album type detected", detected_type=detected, **context)

    mb_type, release_group_mbid = _lookup_musicbrainz_album_type(artist, album)
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

        if detected == "album" or mb_type in {"single", "ep"}:
            detected = mb_type

    Logger.info(
        "[ENRICH] album type resolved",
        detected_type=detected,
        musicbrainz_type=mb_type,
        musicbrainz_type_raw=original_mb_type,
        release_group_mbid=release_group_mbid,
        **context,
    )
    return detected, mb_type, release_group_mbid


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
        Logger.info("[ENRICH] release MBID persisted", rows_updated=rows_updated, release_mbid=release_mbid, **context)
    except Exception as exc:
        Logger.exception("[ENRICH] release MBID propagation failed", error=_safe_error(exc), **context)


def _genre_values(label: str, mb_genres_raw: Any, genres_raw: Any) -> tuple[str, str]:
    mb_list = [str(value).strip() for value in _json_list(mb_genres_raw) if str(value).strip()]
    if label.casefold() not in {value.casefold() for value in mb_list}:
        mb_list.insert(0, label)
    genres = [value.strip() for value in str(genres_raw or "").split(",") if value.strip()]
    if label.casefold() not in {value.casefold() for value in genres}:
        genres.insert(0, label)
    return json.dumps(mb_list), ", ".join(genres)


def _inject_album_genre(
    track_id: str,
    label: str,
    mb_genres_raw: Any,
    genres_raw: Any,
    *,
    session: Any | None = None,
) -> None:
    mb_json, genres_csv = _genre_values(label, mb_genres_raw, genres_raw)
    params = {"mb": mb_json, "genres": genres_csv, "track_id": str(track_id)}
    statement = text(
        "UPDATE tracks SET musicbrainz_genres = :mb, genres = :genres WHERE id = :track_id"
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
                    {"tags": json.dumps(names), "artist": artist},
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
        detected, _, release_group_mbid = _resolve_album_type(
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
                    new_title = title
                    # Strict validation for suffix at end of title
                    has_suffix = bool(re.search(rf"[\(\[]{re.escape(label)}[^)\]]*[\)\]]\s*$", title, re.IGNORECASE))
                    if not is_live_or_unplugged_track_title(title) and not has_suffix:
                        new_title = f"{title} ({label})"
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
                    except Exception as exc:
                        failed += 1
                        Logger.warning("[ENRICH] remix track tagging failed", track_id=track_id, error=_safe_error(exc), **context)
        Logger.info("[ENRICH] remix tagging result", attempted=attempted, rows_updated=updated, failed=failed, **context)


_LIVE_SUFFIX_RE = re.compile(r"\s*[\(\[](?:Live|Acoustic)[^)\]]*[\)\]]\s*$", re.IGNORECASE)


def strip_live_acoustic_suffix(title: str) -> str:
    return _LIVE_SUFFIX_RE.sub("", title or "").strip()


def _drop_live_genres_from_json(raw: Any) -> str | None:
    if not raw:
        return None
    parsed = _json_list(raw)
    kept = [value for value in parsed if str(value).strip().casefold() not in {"live", "acoustic"}]
    return json.dumps(kept) if kept != parsed else None


def _drop_live_genres_from_csv(raw: Any) -> str | None:
    if not raw:
        return None
    parts = [value.strip() for value in str(raw).split(",") if value.strip()]
    kept = [value for value in parts if value.casefold() not in {"live", "acoustic"}]
    return ", ".join(kept) if kept != parts else None


def revert_track_live_state(track_id: str) -> bool:
    context = {"track_id": str(track_id)}
    Logger.info("[ENRICH] live-state revert started", **context)
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
                Logger.info(
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
                        musicbrainz_genres = COALESCE(:mb, musicbrainz_genres),
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
                else:
                    Logger.warning("[ENRICH] live-state file tag write skipped", reason="file does not exist", file_path=resolved, **context)
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


def _run_full_enrichment(
    artist: str,
    album: str,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]],
    detected_type: str,
    options: dict[str, Any],
    discogs_token: str | None,
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
            Logger.info("[ENRICH] release-country backfill result", country=metadata["country"], rows_updated=rows_updated, **context)
        except Exception as exc:
            Logger.exception("[ENRICH] release-country backfill failed", error=_safe_error(exc), **context)
    else:
        Logger.info("[ENRICH] release-country backfill skipped", reason="country unavailable", **context)

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
        Logger.warning(
            "[ENRICH] detected album type downgraded before tagging",
            original_type=detected_type,
            downgraded_type="album",
            **context,
        )
        detected_type = "album"

    metadata, similar = _run_full_enrichment(
        artist,
        album,
        album_context,
        album_tracks,
        detected_type,
        options,
        _get_discogs_token(),
    )
    extra_context: dict[str, Any] = {}
    if metadata.get("country"):
        extra_context["artist_country"] = metadata["country"]
    if similar.get("lastfm"):
        extra_context["similar_artists_lastfm"] = similar["lastfm"]
    if similar.get("listenbrainz"):
        extra_context["similar_artists_listenbrainz"] = similar["listenbrainz"]
    Logger.info("[ENRICH] enrich_album_extras completed", artist=artist, album=album, extra_context_keys=sorted(extra_context))
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

    Logger.info(
        "[ENRICH] album scan started",
        track_count=len(album_tracks),
        stat_eligible_track_count=len(stat_eligible_tracks or []),
        singles_pass=singles_pass,
        popularity_pass=popularity_pass,
        defer_full_enrichment=defer_full,
        **context,
    )

    if not artist or not album:
        Logger.warning("[ENRICH] album scan has incomplete identity", artist_present=bool(artist), album_present=bool(album), **context)

    def _result(detected: str, heterogeneous: bool) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        if metadata.get("country"):
            extras["artist_country"] = metadata["country"]
        if similar.get("lastfm"):
            extras["similar_artists_lastfm"] = similar["lastfm"]
        if similar.get("listenbrainz"):
            extras["similar_artists_listenbrainz"] = similar["listenbrainz"]
        Logger.info(
            "[ENRICH] album scan completed",
            elapsed_s=round(time.monotonic() - scan_start, 3),
            detected_type=detected,
            heterogeneous=heterogeneous,
            metadata_country=metadata.get("country"),
            has_bio=bool(metadata.get("bio")),
            has_image=bool(metadata.get("image_url")),
            lastfm_similar_count=len(similar.get("lastfm") or []),
            listenbrainz_similar_count=len(similar.get("listenbrainz") or []),
            extra_context_keys=sorted(extras),
            **context,
        )
        return {
            "album_row": album_row,
            "album_context": {**album_context, **extras},
            "stat_eligible_tracks": stat_eligible_tracks,
            "detected_album_type": detected,
            "is_heterogeneous": heterogeneous,
            "similar_artists": similar,
            "artist_metadata": metadata,
        }

    with _album_scan_guard(artist, album) as acquired:
        if not acquired:
            Logger.warning(
                "[ENRICH] album scan skipped",
                reason="another enrichment pass holds this album",
                **context,
            )
            return _result(detected_type, is_heterogeneous)

        try:
            if popularity_pass:
                with _log_section("scan.album_type.local_detection", **context):
                    detected_type = _detect_album_type(
                        artist,
                        album,
                        album_artist or None,
                        spotify_type or None,
                    )
                Logger.info("[ENRICH] local album type detected", detected_type=detected_type, **context)
                is_heterogeneous = any(marker in detected_type.casefold() for marker in _HETEROGENEOUS_MARKERS)
                Logger.info(
                    "[ENRICH] MusicBrainz album-type lookup skipped",
                    reason="popularity-only pass",
                    detected_type=detected_type,
                    heterogeneous=is_heterogeneous,
                    **context,
                )
            else:
                with _log_section("scan.album_type.resolve", **context):
                    detected_type, mb_type, release_group_mbid = _resolve_album_type(
                        artist,
                        album,
                        album_artist or None,
                        spotify_type or None,
                        album_tracks,
                    )

                is_heterogeneous = any(marker in detected_type.casefold() for marker in _HETEROGENEOUS_MARKERS)
                Logger.info(
                    "[ENRICH] album type finalised",
                    detected_type=detected_type,
                    musicbrainz_type=mb_type,
                    release_group_mbid=release_group_mbid,
                    heterogeneous=is_heterogeneous,
                    **context,
                )

                with _log_section("scan.album_type.persist", **context):
                    _persist_album_type_to_tracks(
                        artist,
                        album,
                        album_tracks,
                        detected_type,
                        release_group_mbid,
                    )

                if "+compilation" in detected_type.casefold() or "+soundtrack" in detected_type.casefold():
                    try:
                        with _log_section("scan.compilation_flag.persist", **context):
                            with db_session() as session:
                                result = session.execute(
                                    text(
                                        "UPDATE tracks SET is_compilation = 1 "
                                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                                        "AND album = :album AND COALESCE(is_compilation, 0) = 0"
                                    ),
                                    {"artist": artist, "album": album},
                                )
                                rows_updated = result.rowcount
                        Logger.info("[ENRICH] compilation flag persistence result", rows_updated=rows_updated, **context)
                    except Exception as exc:
                        Logger.exception("[ENRICH] compilation flag persistence failed", error=_safe_error(exc), **context)
                else:
                    Logger.info("[ENRICH] compilation flag persistence skipped", reason="album is not compilation/soundtrack", **context)

            discogs_token = _get_discogs_token()
            if popularity_pass or singles_pass:
                Logger.info(
                    "[ENRICH] full enrichment skipped",
                    reason="popularity-only pass" if popularity_pass else "singles pass",
                    **context,
                )
                if singles_pass:
                    with _log_section("scan.singles.musicbrainz_artist_id", **context):
                        _fetch_musicbrainz_artist_id(artist)
            elif defer_full:
                Logger.info("[ENRICH] full enrichment deferred", **context)
                with _log_section("scan.deferred.musicbrainz_artist_id", **context):
                    _fetch_musicbrainz_artist_id(artist)
            else:
                with _log_section("scan.full_enrichment", **context):
                    metadata, similar = _run_full_enrichment(
                        artist,
                        album,
                        album_context,
                        album_tracks,
                        detected_type,
                        options,
                        discogs_token,
                    )

        except Exception as exc:
            Logger.exception(
                "[ENRICH] album scan failed",
                elapsed_s=round(time.monotonic() - scan_start, 3),
                error=_safe_error(exc),
                **context,
            )
            detected_type = detected_type or "album"
            is_heterogeneous = False
            metadata = {"country": None, "bio": None, "image_url": None}
            similar = {"lastfm": [], "listenbrainz": []}

        return _result(detected_type, is_heterogeneous)
