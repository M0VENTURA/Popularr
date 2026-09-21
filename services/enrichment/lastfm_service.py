"""Last.fm enrichment service.

This module owns Last.fm application behaviour:
- safer multi-artist candidate handling
- featured-artist stripping
- track/artist match scoring
- recommendation cache policy
- album/title-track/single interpretation

Raw HTTP is handled by ``api_clients.lastfm_http``.

Rebuilt with the following corrections:

- Separate re-entrant locks for the client and service singletons. The
  previous single non-reentrant lock deadlocked when
  ``get_lastfm_track_info()`` constructed a service that then requested the
  shared client on the same thread.
- An empty or missing API key can no longer evict a working singleton.
- ``album.getInfo`` payloads are cached per instance, removing the two to
  three duplicate requests previously issued per single check.
- The ``track.search`` fallback now preserves tags, keeps playcount and
  listener counts distinct, and no longer sums listeners across every
  matching result.
- ``check_track_as_single()`` no longer accepts EP-sized releases as singles.
- Configuration is read lazily so config-editor changes apply without a
  restart, via ``reset_lastfm_config_cache()``.
- ``RecommendationCache`` is a process-wide singleton to prevent concurrent
  instances clobbering each other's writes.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import structlog

from api_clients.lastfm_http import LastFmHttpClient
from helpers.config_helpers import get_lastfm_config
from helpers.normalization_service import (
    FEAT_SUFFIX_RE,
    strip_cover_attribution,
    strip_diacritics,
    strip_featured_guest_suffix,
)

logger = structlog.get_logger(__name__)
logging.getLogger(__name__).setLevel(logging.DEBUG)

# =============================================================================
# CONFIGURATION (lazily read so config edits apply without a restart)
# =============================================================================

_CONFIG_LOCK = threading.Lock()
_LASTFM_CONFIG_CACHE: dict[str, Any] | None = None

_CONFIG_DEFAULTS: dict[str, Any] = {
    "CACHE_TTL_HOURS": 24,
}


def get_config_value(key: str, default: Any = None) -> Any:
    """Return a Last.fm config value, loading configuration on first use."""
    global _LASTFM_CONFIG_CACHE

    with _CONFIG_LOCK:
        if _LASTFM_CONFIG_CACHE is None:
            try:
                _LASTFM_CONFIG_CACHE = dict(get_lastfm_config() or {})
            except Exception as exc:
                logger.debug("Last.fm config load failed", error=str(exc))
                _LASTFM_CONFIG_CACHE = {}
        config = _LASTFM_CONFIG_CACHE

    if key in config:
        return config[key]
    if default is not None:
        return default
    return _CONFIG_DEFAULTS.get(key)


def reset_lastfm_config_cache() -> None:
    """Clear the cached configuration. Call after saving configuration."""
    global _LASTFM_CONFIG_CACHE
    with _CONFIG_LOCK:
        _LASTFM_CONFIG_CACHE = None


# =============================================================================
# THREAD-SAFE SINGLETONS
# =============================================================================

_CLIENT_INIT_LOCK = threading.RLock()
_SERVICE_INIT_LOCK = threading.RLock()

_SHARED_LF_CLIENT: LastFmHttpClient | None = None
_lastfm_service: "LastFmService | None" = None


def _is_placeholder_key(api_key: str | None) -> bool:
    return str(api_key or "").strip().lower() in (
        "",
        "your_lastfm_api_key",
        "your_api_key",
        "<your_api_key>",
        "placeholder",
    )


def get_shared_lastfm_client(api_key: str) -> LastFmHttpClient:
    """Return the process-wide shared LastFmHttpClient singleton.

    An empty or placeholder key never replaces an existing working client.
    """
    global _SHARED_LF_CLIENT

    if _is_placeholder_key(api_key) and _SHARED_LF_CLIENT is not None:
        return _SHARED_LF_CLIENT

    client = _SHARED_LF_CLIENT
    if client is not None and getattr(client, "api_key", None) == api_key:
        return client

    with _CLIENT_INIT_LOCK:
        client = _SHARED_LF_CLIENT
        if client is None or getattr(client, "api_key", None) != api_key:
            if _is_placeholder_key(api_key) and client is not None:
                return client
            _SHARED_LF_CLIENT = LastFmHttpClient(api_key=api_key)
        return _SHARED_LF_CLIENT


def _sanitize_release_name(album_name: str) -> str:
    """Strips '(Topshelf Edition)', '[Deluxe Version]', etc. for exact API matches."""
    if not album_name:
        return ""
    cleaned = re.sub(
        r"\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]",
        "",
        album_name,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned if cleaned else album_name


# =============================================================================
# RECOMMENDATION CACHE
# =============================================================================

class RecommendationCache:
    """Simple JSON cache for Last.fm recommendation payloads."""

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = (
            Path(cache_dir) if cache_dir else Path.home() / ".cache" / "popularr"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "lastfm_recommendations.json"
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                if not self.cache_file.exists():
                    logger.debug("Recommendation cache miss (no file)", key=key)
                    return None
                cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
                entry = cache.get(key)
                if not entry:
                    logger.debug("Recommendation cache miss (key not found)", key=key)
                    return None
                ttl_hours = float(get_config_value("CACHE_TTL_HOURS", 24) or 24)
                age_hours = (time.time() - float(entry.get("timestamp") or 0)) / 3600
                if age_hours > ttl_hours:
                    logger.debug("Recommendation cache expired", key=key, age_hours=round(age_hours, 2), ttl=ttl_hours)
                    cache.pop(key, None)
                    self._save_unsafe(cache)
                    return None
                logger.debug("Recommendation cache hit", key=key, age_hours=round(age_hours, 2))
                return entry.get("data")
            except Exception as exc:
                logger.debug("Recommendation cache read failed", key=key, error=str(exc))
                return None

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            try:
                cache = (
                    json.loads(self.cache_file.read_text(encoding="utf-8"))
                    if self.cache_file.exists()
                    else {}
                )
                cache[key] = {"data": value, "timestamp": time.time()}
                self._save_unsafe(cache)
                logger.debug("Recommendation cache updated", key=key)
            except Exception as exc:
                logger.debug("Recommendation cache write failed", key=key, error=str(exc))

    def _save_unsafe(self, cache: dict[str, Any]) -> None:
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp.replace(self.cache_file)


_RECOMMENDATION_CACHE: RecommendationCache | None = None
_RECOMMENDATION_CACHE_LOCK = threading.RLock()


def get_recommendation_cache() -> RecommendationCache:
    global _RECOMMENDATION_CACHE
    if _RECOMMENDATION_CACHE is not None:
        return _RECOMMENDATION_CACHE
    with _RECOMMENDATION_CACHE_LOCK:
        if _RECOMMENDATION_CACHE is None:
            _RECOMMENDATION_CACHE = RecommendationCache()
        return _RECOMMENDATION_CACHE


# =============================================================================
# SERVICE
# =============================================================================

class LastFmService:
    """Application-level Last.fm behaviour."""

    def __init__(
        self,
        api_key: str,
        username: str | None = None,
        http_client: LastFmHttpClient | None = None,
        db_connection: Any = None,
    ):
        self.api_key = api_key or ""
        self.username = username
        self.http = http_client or get_shared_lastfm_client(self.api_key)
        self.cache = get_recommendation_cache()
        self.db_connection = db_connection
        self.mb_client = None
        self._album_info_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._album_cache_lock = threading.Lock()

    # -- text helpers -----------------------------------------------------

    @staticmethod
    def clean_spaces(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def extract_artist_name(artist_field: Any) -> str:
        if isinstance(artist_field, str):
            return artist_field.strip()
        if isinstance(artist_field, dict):
            name = (artist_field.get("name") or artist_field.get("#text") or "").strip()
            if name:
                return name
            nested = artist_field.get("artist")
            if isinstance(nested, dict):
                return (nested.get("name") or nested.get("#text") or "").strip()
            if isinstance(nested, str):
                return nested.strip()
        return ""

    @classmethod
    def strip_featured_artist(cls, artist: str) -> str:
        if not artist:
            return ""
        return cls.clean_spaces(FEAT_SUFFIX_RE.split(artist, maxsplit=1)[0])

    @classmethod
    def normalize_artist_for_compare(cls, artist: str) -> str:
        if not artist:
            return ""
        value = cls.clean_spaces(artist).lower()
        value = FEAT_SUFFIX_RE.split(value, maxsplit=1)[0]
        value = re.sub(
            r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*",
            " & ",
            value,
            flags=re.IGNORECASE,
        )
        # Fold diacritics BEFORE comparing.  Last.fm's catalogue indexes an
        # accented artist under its ASCII spelling ("Lïve" -> "Live",
        # "Motörhead" -> "Motorhead"), so a comparison that keeps the accent
        # scores the correct global row at 0 and the caller discards it as a
        # mismatch — which is how a track with millions of global listeners
        # ended up scored from a few hundred.
        value = strip_diacritics(value)
        return cls.clean_spaces(value)

    @classmethod
    def _strip_bracketed_content(cls, value: str) -> str:
        return re.sub(r"\s*[\[\(][^\]\)]*[\]\)]", "", value or "").strip()

    @classmethod
    def build_artist_lookup_candidates(cls, artist: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = cls.clean_spaces(value)
            key = value.lower()
            if value and key not in seen:
                candidates.append(value)
                seen.add(key)

        original = cls.clean_spaces(artist)
        primary = cls.strip_featured_artist(artist)
        add(original)
        add(primary)
        # The ASCII-folded spelling is queried as its own candidate: providers
        # that index the canonical form ("Live") return nothing at all for the
        # accented query, so a folded retry is the difference between the
        # global catalogue and an empty result.
        add(strip_diacritics(original))
        add(strip_diacritics(primary))
        add(
            re.sub(
                r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*",
                " & ",
                primary,
                flags=re.IGNORECASE,
            )
        )

        no_brackets = cls._strip_bracketed_content(artist)
        if no_brackets:
            add(no_brackets)
            add(strip_diacritics(no_brackets))
            no_brackets_primary = cls.strip_featured_artist(no_brackets)
            if no_brackets_primary:
                add(no_brackets_primary)
                add(strip_diacritics(no_brackets_primary))

        return candidates

    @classmethod
    def artist_match_score(cls, query_artist: str, returned_artist: str) -> int:
        q_raw = cls.clean_spaces(query_artist).lower()
        r_raw = cls.clean_spaces(returned_artist).lower()
        q_norm = cls.normalize_artist_for_compare(query_artist)
        r_norm = cls.normalize_artist_for_compare(returned_artist)
        q_primary = cls.strip_featured_artist(query_artist).lower()
        r_primary = cls.strip_featured_artist(returned_artist).lower()

        if q_raw and r_raw and q_raw == r_raw:
            return 100
        if q_norm and r_norm and q_norm == r_norm:
            return 90
        if q_primary and r_primary and q_primary == r_primary:
            return 70
        if q_primary and r_raw and q_primary == r_raw:
            return 60
        if q_norm and r_raw and q_norm == r_raw:
            return 60
        return 0

    # -- artist top tracks ------------------------------------------------

    def get_artist_top_tracks(self, artist: str, limit: int = 100) -> list[dict[str, Any]]:
        if not self.api_key or not artist:
            return []

        # This is the single source of an artist's global listener counts, so a
        # lookup that silently returns nothing costs EVERY track on the album
        # its real popularity.  Last.fm indexes an accented artist under its
        # ASCII spelling ("Lïve" -> "Live"), so retry the folded spelling when
        # the verbatim query yields nothing ("autocorrect" does not cover it).
        _spellings: list[str] = []
        for _candidate in (artist, strip_diacritics(artist)):
            _clean = self.clean_spaces(_candidate)
            if _clean and _clean not in _spellings:
                _spellings.append(_clean)

        for _index, _spelling in enumerate(_spellings):
            try:
                data = self.http.get_json(
                    "artist.getTopTracks",
                    artist=_spelling,
                    limit=max(1, min(int(limit), 200)),
                )
                tracks = (data.get("toptracks") or {}).get("track") or []
                if isinstance(tracks, dict):
                    tracks = [tracks]
                usable = [t for t in tracks if isinstance(t, dict)]
                if usable:
                    if _index:
                        logger.info(
                            "artist.getTopTracks resolved via folded spelling",
                            queried=artist, matched_spelling=_spelling, tracks=len(usable),
                        )
                    return usable
            except Exception as exc:
                logger.debug(
                    "Artist top tracks failed", artist=_spelling, error=str(exc)
                )
        return []

    # -- track info -------------------------------------------------------

    def _get_track_info_once(
        self,
        artist: str,
        title: str,
        track_mbid: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"autocorrect": 1}
        if track_mbid:
            params["mbid"] = track_mbid
        else:
            params["artist"] = artist
            params["track"] = title

        try:
            data = self.http.get_json("track.getInfo", **params)
            if not isinstance(data, dict) or "error" in data:
                return self._empty_track_info(artist, title)

            track = data.get("track", {}) or {}
            if not isinstance(track, dict):
                return self._empty_track_info(artist, title)

            returned_artist = self.extract_artist_name(track.get("artist"))
            album_title = ""
            if isinstance(track.get("album"), dict):
                album_title = track["album"].get("title") or track["album"].get("name") or ""

            return {
                "track_play": int(track.get("playcount", 0) or 0),
                "listeners": int(track.get("listeners", 0) or 0),
                "toptags": track.get("toptags", {}) or {},
                "lookup_artist": artist,
                "returned_artist": returned_artist,
                "track_name": track.get("name", title),
                "url": track.get("url", ""),
                "album": album_title,
            }
        except Exception as exc:
            logger.debug(
                "track.getInfo failed", artist=artist, track=title, error=str(exc)
            )
            return self._empty_track_info(artist, title)

    @staticmethod
    def _empty_track_info(artist: str, title: str) -> dict[str, Any]:
        return {
            "track_play": 0,
            "listeners": 0,
            "toptags": {},
            "lookup_artist": artist,
            "returned_artist": "",
            "track_name": title,
            "url": "",
            "album": "",
        }

    def get_track_info(
        self,
        artist: str,
        title: str,
        track_mbid: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            return self._normalise_track_result(
                self._empty_track_info(artist, title), artist
            )

        title = strip_cover_attribution(title) or title

        best_result: dict[str, Any] | None = None
        best_tuple = (-1, -1, -1)

        if track_mbid:
            mbid_result = self._get_track_info_once(artist, title, track_mbid=track_mbid)
            best_result = mbid_result
            best_tuple = (
                self.artist_match_score(artist, mbid_result.get("returned_artist", "")),
                int(mbid_result.get("listeners", 0) or 0),
                int(mbid_result.get("track_play", 0) or 0),
            )
            if best_tuple[0] >= 90 and (best_tuple[1] > 0 or best_tuple[2] > 0):
                logger.debug("Track info resolved via MBID", artist=artist, track=title, mbid=track_mbid, listeners=best_tuple[1])
                return self._normalise_track_result(best_result, artist)

        for candidate_artist in self.build_artist_lookup_candidates(artist):
            candidate = self._get_track_info_once(candidate_artist, title)
            artist_score = self.artist_match_score(
                artist, candidate.get("returned_artist", "")
            )

            if artist_score < 60:
                continue

            candidate_tuple = (
                artist_score,
                int(candidate.get("listeners", 0) or 0),
                int(candidate.get("track_play", 0) or 0),
            )

            if candidate_tuple > best_tuple:
                best_tuple = candidate_tuple
                best_result = candidate
                logger.debug("New best track candidate found", query_artist=artist, candidate_artist=candidate_artist, score=artist_score, listeners=candidate.get("listeners", 0))

        if best_result is None or (
            int(best_result.get("listeners", 0) or 0) == 0
            and int(best_result.get("track_play", 0) or 0) == 0
        ):
            logger.debug("Zero listeners found via getInfo, falling back to track.search", artist=artist, track=title)
            fallback = self._track_info_from_search(artist, title)
            if fallback is not None:
                best_result = fallback

        return self._normalise_track_result(
            best_result or self._empty_track_info(artist, title), artist
        )

    def _track_info_from_search(self, artist: str, title: str) -> dict[str, Any] | None:
        search_results = self.search_track(artist, title, limit=20)
        if not search_results:
            return None

        try:
            from services.popularity.popularity_matching import (
                normalize_for_aggregation as _nfa,
            )

            target = _nfa(title)
        except Exception:
            _nfa = None  # type: ignore[assignment]
            target = None

        best: dict[str, Any] | None = None
        best_listeners = 0
        seen: set[str] = set()

        for track in search_results:
            track_name = str(track.get("name") or "")
            if target is not None and _nfa is not None and _nfa(track_name) != target:
                continue

            url = str(track.get("url") or "").strip()
            dedupe_key = (
                url or f"{track_name.lower()}|{track.get('artist', '')}".strip().lower()
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            listeners = int(track.get("listeners", 0) or 0)
            if listeners > best_listeners:
                best_listeners = listeners
                best = track

        if best is None or best_listeners <= 0:
            logger.debug("Fallback track.search found no viable candidates", artist=artist, track=title)
            return None

        best_artist = str(best.get("artist") or "")
        best_name = str(best.get("name") or title)
        logger.debug("Fallback track.search winner selected", query_artist=artist, query_title=title, matched_artist=best_artist, matched_title=best_name, listeners=best_listeners)

        detail = self._get_track_info_once(best_artist or artist, best_name)
        toptags = detail.get("toptags") or {}
        playcount = int(detail.get("track_play", 0) or 0)
        listeners = int(detail.get("listeners", 0) or 0) or best_listeners

        return {
            "track_play": playcount,
            "listeners": listeners,
            "toptags": toptags,
            "lookup_artist": artist,
            "returned_artist": best_artist,
            "track_name": best_name,
            "url": str(best.get("url") or ""),
            "album": detail.get("album", ""),
        }

    @staticmethod
    def _normalise_track_result(
        result: dict[str, Any], fallback_artist: str
    ) -> dict[str, Any]:
        return {
            "track_play": int(result.get("track_play", 0) or 0),
            "listeners": int(result.get("listeners", 0) or 0),
            "toptags": result.get("toptags", {}) or {},
            "lookup_artist": result.get("lookup_artist", fallback_artist),
            "returned_artist": result.get("returned_artist", ""),
            "track_name": result.get("track_name", ""),
            "url": result.get("url", ""),
            "album": result.get("album", ""),
        }

    def get_track_tags(
        self,
        artist: str,
        title: str,
        track_mbid: str | None = None,
        limit: int = 15,
    ) -> list[str]:
        """Return Last.fm track-level tag names for genre aggregation."""
        info = self.get_track_info(artist, title, track_mbid=track_mbid)
        toptags = info.get("toptags") or {}
        tags = toptags.get("tag") if isinstance(toptags, dict) else toptags
        if isinstance(tags, dict):
            tags = [tags]
        if not isinstance(tags, list):
            return []

        names: list[str] = []
        for tag in tags:
            if isinstance(tag, dict):
                name = str(tag.get("name") or "").strip()
            else:
                name = str(tag or "").strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= max(1, int(limit)):
                break
        return names

    # -- searches ---------------------------------------------------------

    def search_track(self, artist: str, title: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.api_key:
            return []
        try:
            data = self.http.get_json(
                "track.search", artist=artist, track=title, limit=limit
            )
            tracks = (
                data.get("results", {}).get("trackmatches", {}).get("track", [])
                if isinstance(data, dict)
                else []
            )
            if isinstance(tracks, dict):
                tracks = [tracks]
            output = []
            for track in tracks or []:
                if not isinstance(track, dict):
                    continue
                track_artist = self.extract_artist_name(track.get("artist"))
                score = self.artist_match_score(artist, track_artist)
                if score >= 60:
                    output.append(
                        {
                            "name": track.get("name", ""),
                            "artist": track_artist,
                            "listeners": int(track.get("listeners", 0) or 0),
                            "url": track.get("url", ""),
                            "_score": score,
                        }
                    )

            output.sort(key=lambda item: (item["_score"], item["listeners"]), reverse=True)
            for item in output:
                item.pop("_score", None)
            return output
        except Exception as exc:
            logger.debug(
                "track.search failed", artist=artist, track=title, error=str(exc)
            )
            return []

    def search_album(
        self, album: str, artist: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            return []
        try:
            clean_album = _sanitize_release_name(album)
            data = self.http.get_json(
                "album.search", album=clean_album, limit=max(1, min(limit, 100))
            )
            albums = (
                data.get("results", {}).get("albummatches", {}).get("album", [])
                if isinstance(data, dict)
                else []
            )
            if isinstance(albums, dict):
                albums = [albums]
            output = []
            for item in albums or []:
                if not isinstance(item, dict):
                    continue
                item_artist = self.extract_artist_name(item.get("artist"))
                if artist and self.artist_match_score(artist, item_artist) < 60:
                    continue
                output.append(item)
            return output
        except Exception as exc:
            logger.debug("album.search failed", album=album, error=str(exc))
            return []

    # -- album info (cached) ----------------------------------------------

    def _album_get_info(self, artist: str, album: str) -> dict[str, Any]:
        if not self.api_key or not album:
            return {}

        cache_key = (
            self.clean_spaces(artist).lower(),
            self.clean_spaces(album).lower(),
        )
        with self._album_cache_lock:
            cached = self._album_info_cache.get(cache_key)
        if cached is not None:
            logger.debug("Album info cache hit", artist=artist, album=album)
            return cached

        clean_album = _sanitize_release_name(album)
        payload_out: dict[str, Any] = {}
        lookup_succeeded = False

        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                payload = self.http.get_json(
                    "album.getInfo", artist=lookup_artist, album=clean_album
                )
                lookup_succeeded = True
                album_payload = payload.get("album") if isinstance(payload, dict) else None
                if isinstance(album_payload, dict):
                    album_payload["_lookup_artist"] = lookup_artist
                    payload_out = album_payload
                    logger.debug("Album info live fetch succeeded", artist=artist, lookup_artist=lookup_artist, album=clean_album)
                    break
            except Exception as exc:
                logger.debug(
                    "album.getInfo failed",
                    lookup_artist=lookup_artist,
                    album=album,
                    error=str(exc),
                )

        if lookup_succeeded:
            with self._album_cache_lock:
                self._album_info_cache[cache_key] = payload_out

        return payload_out

    @staticmethod
    def _album_track_list(album_data: dict[str, Any]) -> list[dict[str, Any]]:
        tracks = album_data.get("tracks", {}) if isinstance(album_data, dict) else {}
        if isinstance(tracks, dict):
            track_data = tracks.get("track", [])
            if isinstance(track_data, dict):
                return [track_data]
            if isinstance(track_data, list):
                return [t for t in track_data if isinstance(t, dict)]
        if isinstance(tracks, list):
            return [t for t in tracks if isinstance(t, dict)]
        return []

    def get_album_track_count(self, artist: str, album: str) -> int:
        return len(self._album_track_list(self._album_get_info(artist, album)))

    def has_title_track(self, artist: str, album: str) -> bool:
        album_data = self._album_get_info(artist, album)
        if not album_data:
            return False
        album_name = str(album_data.get("name") or album).lower().strip()
        for track in self._album_track_list(album_data):
            if str(track.get("name") or "").lower().strip() == album_name:
                return True
        return False

    @staticmethod
    def _is_genuine_release(album_data: dict[str, Any]) -> bool:
        if album_data.get("mbid"):
            return True
        released = str(album_data.get("releasedate") or "").strip()
        if released and released.lower() not in ("14 jun 2005, 00:00", "14 jun 2005"):
            return True
        wiki = album_data.get("wiki")
        if isinstance(wiki, dict) and str(wiki.get("published") or "").strip():
            return True
        return False

    MAX_SINGLE_TRACK_COUNT = 3

    _ALBUM_SINGLE_MARKER_RE = re.compile(
        r"\b(?:single|radio\s+edit|single\s+version)\b", re.IGNORECASE
    )

    def check_track_as_single(self, artist: str, track_title: str) -> bool:
        album_data = self._album_get_info(artist, track_title)
        if not album_data:
            logger.debug("Rejected single: Could not fetch album data", artist=artist, track=track_title)
            return False

        album_name = str(album_data.get("name") or "").lower().strip()
        track_name = str(track_title or "").lower().strip()

        if strip_featured_guest_suffix(album_name) != strip_featured_guest_suffix(track_name):
            logger.debug("Rejected single: Name mismatch", artist=artist, track=track_title, album_name=album_name)
            return False

        returned_artist = self.extract_artist_name(album_data.get("artist"))
        if not returned_artist or self.artist_match_score(artist, returned_artist) < 90:
            logger.debug("Rejected single: Artist match score too low", query_artist=artist, returned_artist=returned_artist, track=track_title)
            return False

        if not self._is_genuine_release(album_data):
            logger.debug("Rejected single: Not a genuine release", artist=artist, track=track_title)
            return False

        count = self.get_album_track_count(artist, track_title)
        if count <= 0:
            logger.debug("Rejected single: Track count is zero or missing", artist=artist, track=track_title)
            return False
        if count <= self.MAX_SINGLE_TRACK_COUNT:
            logger.debug("Accepted single: Track count within limits", artist=artist, track=track_title, count=count)
            return True

        has_marker = bool(self._ALBUM_SINGLE_MARKER_RE.search(str(album_data.get("name") or "")))
        if has_marker:
            logger.debug("Accepted single: Found explicit single marker despite track count", artist=artist, track=track_title, count=count, album_name=album_data.get("name"))
            return True
        else:
            logger.debug("Rejected single: Exceeds track limit without explicit single marker", artist=artist, track=track_title, count=count, album_name=album_data.get("name"))
            return False

    # -- temporal / artist ------------------------------------------------

    def get_track_temporal_data(
        self,
        artist: str,
        title: str,
        track_mbid: str | None = None,
    ) -> dict[str, Any]:
        info = self.get_track_info(artist, title, track_mbid=track_mbid)
        return {
            "all_time_listeners": int(info.get("listeners", 0) or 0),
            "all_time_playcount": int(info.get("track_play", 0) or 0),
            "7day_listeners": None,
            "365day_listeners": None,
            "momentum_score": 1.0,
            "popularity_trend": "unknown",
            "data_source": "standard_api_only" if self.api_key else "unavailable",
        }

    def get_artist_info(self, artist: str) -> dict[str, Any]:
        if not self.api_key:
            return {"bio": "", "bio_text": "", "image": "", "similar": []}

        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                payload = self.http.get_json("artist.getInfo", artist=lookup_artist)
                data = payload.get("artist", {}) if isinstance(payload, dict) else {}
                if not data:
                    continue

                image_url = ""
                if isinstance(data.get("image"), list):
                    for image in reversed(data["image"]):
                        if isinstance(image, dict) and image.get("#text"):
                            image_url = image.get("#text", "")
                            break

                bio = data.get("bio", {}) if isinstance(data.get("bio"), dict) else {}
                return {
                    "bio": bio.get("content", ""),
                    "bio_text": bio.get("summary", "") or bio.get("content", ""),
                    "image": image_url,
                    "similar": [],
                }
            except Exception as exc:
                logger.debug(
                    "artist.getInfo failed", lookup_artist=lookup_artist, error=str(exc)
                )

        return {"bio": "", "bio_text": "", "image": "", "similar": []}

    def get_artist_top_tags(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.api_key or not artist:
            return []
        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                data = self.http.get_json(
                    "artist.getTopTags",
                    artist=lookup_artist,
                    limit=max(1, min(100, limit)),
                )
                tags = (
                    data.get("toptags", {}).get("tag", []) if isinstance(data, dict) else []
                )
                if isinstance(tags, dict):
                    tags = [tags]

                result = [
                    {
                        "name": tag.get("name", ""),
                        "count": int(tag.get("count", 0) or 0),
                    }
                    for tag in tags or []
                    if isinstance(tag, dict) and tag.get("name")
                ]
                if result:
                    return result
            except Exception as exc:
                logger.debug(
                    "artist.getTopTags failed",
                    lookup_artist=lookup_artist,
                    error=str(exc),
                )
        return []

    def get_similar_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.api_key or not artist:
            return []

        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                data = self.http.get_json(
                    "artist.getSimilar",
                    artist=lookup_artist,
                    limit=max(1, min(int(limit), 100)),
                )
                if not isinstance(data, dict) or "error" in data:
                    continue

                similar_artists = (data.get("similarartists") or {}).get("artist", [])
                if isinstance(similar_artists, dict):
                    similar_artists = [similar_artists]

                result: list[dict[str, Any]] = []
                for artist_obj in similar_artists or []:
                    if not isinstance(artist_obj, dict):
                        continue
                    name = artist_obj.get("name", "")
                    if not name:
                        continue
                    try:
                        match = float(artist_obj.get("match", 0.0))
                    except Exception:
                        match = 0.0
                    result.append({"name": name, "match": match})

                if result:
                    return result
            except Exception as exc:
                logger.debug(
                    "similar artists failed", lookup_artist=lookup_artist, error=str(exc)
                )
        return []

    # -- recommendations --------------------------------------------------

    def get_recommendations(self) -> dict[str, list[dict[str, Any]]]:
        if not self.api_key:
            return {"artists": [], "albums": [], "tracks": []}

        cache_key = (
            f"recommendations_{self.username or 'global'}"
            if not self.db_connection
            else None
        )

        if cache_key:
            cached = self.cache.get(cache_key)
            if cached:
                return cached

        result = {
            "artists": self._get_recommended_artists(),
            "albums": self._get_recommended_albums(),
            "tracks": self._get_recommended_tracks(),
        }

        if cache_key:
            self.cache.set(cache_key, result)
        return result

    def _get_recommended_artists(self) -> list[dict[str, Any]]:
        method = "user.getTopArtists" if self.username else "chart.getTopArtists"
        kwargs = (
            {"user": self.username, "limit": 20, "period": "6month"}
            if self.username
            else {"limit": 20}
        )
        try:
            response = self.http.request(method, **kwargs)
            response.raise_for_status()
            data = response.json()
            artists = (
                data.get("topartists", {}).get("artist", [])
                if self.username
                else data.get("artists", {}).get("artist", [])
            )
            return [
                {
                    "name": item.get("name", ""),
                    "listeners": item.get("listeners", 0),
                    "match": 1.0,
                    "playcount": item.get("playcount", 0),
                    "url": item.get("url", ""),
                }
                for item in artists or []
                if isinstance(item, dict)
            ][:20]
        except Exception as exc:
            logger.debug("Recommended artists failed", error=str(exc))
            return []

    def _get_recommended_albums(self) -> list[dict[str, Any]]:
        if not self.username:
            return []
        try:
            response = self.http.request(
                "user.getTopAlbums", user=self.username, limit=12, period="6month"
            )
            response.raise_for_status()
            albums = response.json().get("topalbums", {}).get("album", [])
            return [
                {
                    "name": item.get("name", ""),
                    "artist": self.extract_artist_name(item.get("artist")),
                    "playcount": item.get("playcount", 0),
                    "url": item.get("url", ""),
                    "similarity": 1.0,
                }
                for item in albums or []
                if isinstance(item, dict)
            ][:12]
        except Exception as exc:
            logger.debug("Recommended albums failed", error=str(exc))
            return []

    def _get_recommended_tracks(self) -> list[dict[str, Any]]:
        method = "user.getTopTracks" if self.username else "chart.getTopTracks"
        kwargs = (
            {"user": self.username, "limit": 20, "period": "6month"}
            if self.username
            else {"limit": 20}
        )
        try:
            response = self.http.request(method, **kwargs)
            response.raise_for_status()
            tracks = response.json().get("toptracks", {}).get("track", [])
            return [
                {
                    "name": item.get("name", ""),
                    "artist": self.extract_artist_name(item.get("artist")),
                    "playcount": item.get("playcount", 0),
                    "url": item.get("url", ""),
                    "similarity": 1.0,
                }
                for item in tracks or []
                if isinstance(item, dict)
            ][:20]
        except Exception as exc:
            logger.debug("Recommended tracks failed", error=str(exc))
            return []


# =============================================================================
# BRIDGE FUNCTIONS
# =============================================================================

def _config_api_key() -> str:
    try:
        from helpers.config_helpers import get_config

        cfg = get_config() or {}
        return str(
            (cfg.get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "")
            or ""
        )
    except Exception:
        return ""


def get_shared_lastfm_service(api_key: str = "") -> LastFmService:
    global _lastfm_service

    api_key = api_key or _config_api_key()

    service = _lastfm_service
    if service is not None and (
        _is_placeholder_key(api_key) or getattr(service, "api_key", None) == api_key
    ):
        return service

    with _SERVICE_INIT_LOCK:
        service = _lastfm_service
        if service is not None and (
            _is_placeholder_key(api_key) or getattr(service, "api_key", None) == api_key
        ):
            return service
        _lastfm_service = LastFmService(api_key)
        return _lastfm_service


def get_lastfm_track_info(artist: str, title: str, api_key: str = "") -> dict[str, Any]:
    return get_shared_lastfm_service(api_key).get_track_info(artist, title)


def get_lastfm_track_tags(
    artist: str,
    title: str,
    api_key: str = "",
    limit: int = 15,
) -> list[str]:
    return get_shared_lastfm_service(api_key).get_track_tags(artist, title, limit=limit)


def get_lastfm_recommendations(
    api_key: str,
    username: str | None = None,
    db_connection: Any = None,
) -> dict[str, Any]:
    return LastFmService(
        api_key, username=username, db_connection=db_connection
    ).get_recommendations()
