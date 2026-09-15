"""Navidrome API client.

This module is intentionally HTTP-focused.

Responsibilities kept here:
- Build Subsonic/Navidrome request parameters.
- Call Navidrome HTTP endpoints.
- Return raw/near-raw API data.
- Playlist/star/scan endpoint wrappers.
- Surface Subsonic-level errors (status="failed") that arrive as HTTP 200,
  per https://www.navidrome.org/docs/developers/subsonic-api/ and the
  underlying Subsonic/OpenSubsonic spec.

Responsibilities moved out:
- Artist-index orchestration -> services.scanning.navidrome_service
- Concurrent multi-page fetching -> services.scanning.navidrome_service
- Track metadata normalization -> services.scanning.metadata_extractor
- DB writes -> db.repositories.tracks / db.repositories.scan_repository

Non-JSON response handling:
    Navidrome falls back to XML when it cannot honour ``f=json`` — which
    happens on some error paths. An XML body therefore may carry
    ``status="failed"``. Treating any non-JSON 2xx body as success reported
    rejected mutations as successful writes, so the status attribute is now
    parsed out of non-JSON bodies before deciding.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any

import structlog

from api_clients.http_utils import create_retry_client

logger = structlog.get_logger(__name__)

# Dedicated session for Navidrome with its OWN connection pool.
navidrome_session = create_retry_client(
    retries=1,
    backoff=0.5,
    status_forcelist=(429, 502, 503, 504),
    timeout=15.0,
)

_DEFAULT_RETRIES = 0
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 410})

_nav_error_log_ts: dict[str, float] = {}
_NAV_ERROR_LOG_COOLDOWN_SECONDS = 60.0

# Subsonic API error codes, per https://subsonic.org/pages/api.jsp and the
# OpenSubsonic clarifications (codes 42-44 are OpenSubsonic additions).
# Every response is wrapped in a `subsonic-response` envelope carrying a
# `status` field ("ok" or "failed") - a "failed" status still arrives over
# HTTP 200, so it must be checked explicitly rather than relying on
# raise_for_status().
_SUBSONIC_ERROR_MESSAGES = {
    0: "Generic error",
    10: "Required parameter is missing",
    20: "Incompatible Subsonic REST protocol version - client must upgrade",
    30: "Incompatible Subsonic REST protocol version - server must upgrade",
    40: "Wrong username or password",
    41: "Token authentication not supported for LDAP users",
    42: "Authentication mechanism not supported by server",
    43: "Conflicting authentication mechanisms provided",
    44: "Invalid API key",
    50: "User is not authorized for the given operation",
    60: "Trial period for the Subsonic server is over",
    70: "The requested data was not found",
}
# Code 70 ("not found") is routine - many callers legitimately probe for
# things that don't exist (e.g. getStarred2 falling back to getStarred).
# Everything else indicates a real problem worth a warning-level log.
_SUBSONIC_NON_ERROR_CODES = frozenset({70})
# Auth-related codes where retrying the exact same request is pointless.
_SUBSONIC_AUTH_ERROR_CODES = frozenset({40, 41, 42, 43, 44, 50})

# Navidrome answers with an XML envelope when it cannot serve JSON. The
# status/error attributes still need to be honoured.
_XML_STATUS_RE = re.compile(r'\bstatus\s*=\s*"([^"]+)"', re.IGNORECASE)
_XML_ERROR_CODE_RE = re.compile(r'\bcode\s*=\s*"(\d+)"', re.IGNORECASE)
_XML_ERROR_MESSAGE_RE = re.compile(r'\bmessage\s*=\s*"([^"]*)"', re.IGNORECASE)


def _md5_hex(value: str) -> str:
    """Return the hex MD5 digest of a string."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _parse_non_json_envelope(body: str) -> dict[str, Any] | None:
    """Extract a Subsonic envelope from a non-JSON (XML) response body.

    Returns a dict shaped like the JSON envelope, or ``None`` when the body
    carries no recognisable ``subsonic-response``.

    This exists because Navidrome returns XML on some error paths even when
    ``f=json`` was requested. Assuming any non-JSON 2xx body meant success
    caused rejected ``updatePlaylist`` calls to be logged as successful
    in-place updates.
    """
    text = (body or "").strip()
    if not text or "subsonic-response" not in text:
        return None

    status_match = _XML_STATUS_RE.search(text)
    if not status_match:
        return None

    envelope: dict[str, Any] = {"status": status_match.group(1).strip().lower()}
    if envelope["status"] == "failed":
        error: dict[str, Any] = {}
        code_match = _XML_ERROR_CODE_RE.search(text)
        if code_match:
            try:
                error["code"] = int(code_match.group(1))
            except (TypeError, ValueError):
                pass
        message_match = _XML_ERROR_MESSAGE_RE.search(text)
        if message_match:
            error["message"] = message_match.group(1)
        envelope["error"] = error
    return envelope


def _coerce_modified_ts(value: Any) -> int | None:
    """Normalise a timestamp into epoch seconds for modified/ifModifiedSince."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (TypeError, ValueError):
            return None
    try:
        return int(value.timestamp())
    except (AttributeError, TypeError):
        return None


def _log_throttled_error(endpoint: str, exc: Exception, prefix: str = "Navidrome request failed") -> None:
    """Throttle repeated failures per endpoint to avoid log spam when offline."""
    # Transient timeouts (server busy mid-scan, connection stall) are expected
    # and non-fatal — log them at WARNING so error.log stays clean of the
    # repeated "timed out" noise (the reported getScanStatus / setRating
    # ReadTimeout spam during Navidrome scans).  Hard failures (auth, 4xx/5xx)
    # stay at ERROR.
    _transient = type(exc).__name__ in (
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "Timeout",
        "ConnectionError",
        "ConnectError",
        "RemoteProtocolError",
    )
    _level = "warning" if _transient else "error"

    _now = time.time()
    _last = _nav_error_log_ts.get(endpoint, 0.0)
    if _now - _last >= _NAV_ERROR_LOG_COOLDOWN_SECONDS:
        _nav_error_log_ts[endpoint] = _now
        getattr(logger, _level)(
            prefix,
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        logger.debug(
            f"{prefix} (suppressed)",
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _log_subsonic_status(result: dict[str, Any], endpoint: str) -> None:
    """Log a Subsonic-level error carried in an HTTP-200 envelope.

    The Subsonic/OpenSubsonic response envelope always has a `status` field
    ("ok" or "failed"); on failure it also carries an `error: {code, message}`
    object. This never raises an HTTP error, so without this check, auth
    failures, authorization errors, and version mismatches look identical to
    "no data found" to every caller that just does `data.get(key, default)`.
    """
    if not isinstance(result, dict) or result.get("status") != "failed":
        return

    error = result.get("error") or {}
    code = error.get("code")
    message = error.get("message") or _SUBSONIC_ERROR_MESSAGES.get(code, "Unknown Subsonic error")

    if code in _SUBSONIC_NON_ERROR_CODES:
        logger.debug("Navidrome: requested data not found", endpoint=endpoint, code=code, message=message)
    else:
        logger.warning("Navidrome API returned a failed status", endpoint=endpoint, code=code, message=message)


class NavidromeClient:
    """HTTP client for Navidrome's Subsonic/OpenSubsonic API."""

    def __init__(self, base_url: str, username: str, password: str, http_session: Any = None, use_token_auth: bool = True):
        self.base_url = str(base_url or "").rstrip("/")
        if self.base_url and not re.match(r"^https?://", self.base_url, re.IGNORECASE):
            self.base_url = "http://" + self.base_url

        self.username = username or ""
        self.password = password or ""
        self.session = http_session or navidrome_session
        self.use_token_auth = use_token_auth
        self._stats_cache: dict[str, Any] | None = None
        self._last_stats_time = 0.0

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------
    def _build_params(self, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "u": self.username,
            "v": "1.16.1",
            "c": "popularr",
            "f": "json",
        }
        if self.use_token_auth and self.password:
            # A fresh salt (>= 6 chars, per spec) is generated for every
            # call - salts must never be reused across requests.
            salt = f"{random.getrandbits(64):016x}"
            params["t"] = _md5_hex(self.password + salt)
            params["s"] = salt
        else:
            params["p"] = self.password
        params.update(kwargs)
        return params

    def _maybe_fallback_to_password_auth(self, result: dict[str, Any], endpoint: str) -> bool:
        """Handle Subsonic error 41 (LDAP-backed servers reject token auth).

        Per Navidrome/Subsonic behaviour, LDAP-backed servers cannot validate
        the salted token scheme and return error 41. The documented recovery
        is to fall back to cleartext/`p=` auth. Returns True if a fallback
        was just applied (caller should retry the request once).
        """
        if not isinstance(result, dict) or result.get("status") != "failed":
            return False
        if not self.use_token_auth or not self.password:
            return False
        error_code = (result.get("error") or {}).get("code")
        if error_code != 41:
            return False

        logger.warning(
            "Navidrome rejected token auth (LDAP-backed server); falling back to password auth",
            endpoint=endpoint,
        )
        self.use_token_auth = False
        return True

    def _get_subsonic_response(self, endpoint: str, *, timeout: int = 30, retries: int = _DEFAULT_RETRIES, **params: Any) -> dict[str, Any]:
        if not self.base_url:
            raise ValueError(
                "Navidrome base_url is empty — Navidrome is not configured. "
                "Complete the setup wizard or check config.yaml navidrome_users."
            )
        url = f"{self.base_url}/rest/{endpoint}"
        last_error: Exception | None = None
        retry_flag = True
        auth_fallback_attempted = False

        for attempt in range(1 + max(0, retries)):
            try:
                response = self.session.get(url, params=self._build_params(**params), timeout=timeout)

                if response.status_code in _RETRYABLE_STATUSES and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome returned retryable HTTP status", endpoint=endpoint, status=response.status_code, retry_in=wait)
                    time.sleep(wait)
                    continue

                if response.status_code in _NON_RETRYABLE_STATUSES:
                    retry_flag = False
                    response.raise_for_status()

                response.raise_for_status()

                # Some Subsonic servers (Navidrome included) return HTTP 200 with
                # an EMPTY body for some endpoints (e.g. updatePlaylist). Treat
                # an empty body as a successful empty response, not an error.
                if not response.content or not response.text.strip():
                    return {}

                try:
                    result = response.json().get("subsonic-response", {}) or {}
                except (json.JSONDecodeError, ValueError):
                    # A 2xx with a non-JSON body is usually an XML envelope.
                    # It can still carry status="failed", so parse it rather
                    # than assuming the call succeeded.
                    parsed = _parse_non_json_envelope(response.text or "")
                    if parsed is None:
                        logger.debug(
                            "Navidrome endpoint returned unrecognised non-JSON 2xx body — treating as empty",
                            endpoint=endpoint,
                            body=(response.text or "")[:120],
                        )
                        return {}
                    logger.debug(
                        "Navidrome endpoint returned an XML envelope",
                        endpoint=endpoint,
                        status=parsed.get("status"),
                    )
                    result = parsed

                if not result:
                    logger.warning("Navidrome returned empty subsonic-response", endpoint=endpoint)
                    return result

                # A "failed" status arrives over HTTP 200, so it must be
                # checked explicitly - raise_for_status() above never catches it.
                if result.get("status") == "failed":
                    if not auth_fallback_attempted and self._maybe_fallback_to_password_auth(result, endpoint):
                        auth_fallback_attempted = True
                        continue  # retry immediately with password auth
                    _log_subsonic_status(result, endpoint)

                return result

            except Exception as exc:
                last_error = exc
                if retry_flag and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome request failed, retrying", endpoint=endpoint, retry_in=wait, error=str(exc))
                    time.sleep(wait)
                    continue

        if last_error:
            # getSongs is known to 404 on newer Navidrome versions. Since the orchestrator
            # safely falls back to get_indexes/get_albums, we demote this to a debug log to
            # stop it from throwing massive ERROR tracebacks into the logs.
            is_404 = getattr(last_error, "response", None) is not None and getattr(last_error.response, "status_code", None) == 404
            if endpoint == "getSongs" and is_404:
                logger.debug(f"Navidrome {endpoint} endpoint returned 404 (expected on newer versions) — returning empty")
            else:
                _log_throttled_error(endpoint, last_error, prefix=f"Navidrome {endpoint} failed after {retries + 1} attempts")
        return {}

    def _post_subsonic_response(self, endpoint: str, *, timeout: int = 60, **params: Any) -> dict[str, Any]:
        _body = self._build_params(**params) or {}
        url = f"{self.base_url}/rest/{endpoint}"
        try:
            response = self.session.post(url, data=_body, timeout=timeout)
            response.raise_for_status()
            # Some Subsonic servers (Navidrome included) return HTTP 200 with an
            # EMPTY body for mutation endpoints like updatePlaylist — an empty
            # body is the success response, not an error. Treat it as ok.
            if not response.content or not response.text.strip():
                return {"status": "ok"}

            try:
                result = response.json().get("subsonic-response", {}) or {}
            except (json.JSONDecodeError, ValueError):
                # Navidrome returns an XML envelope on some error paths even
                # when f=json was requested, and that envelope can say
                # status="failed". Blanket-treating a non-JSON 2xx body as
                # success reported rejected mutations as successful writes,
                # so the status is parsed out before deciding.
                parsed = _parse_non_json_envelope(response.text or "")
                if parsed is None:
                    logger.debug(
                        "Navidrome mutation endpoint returned unrecognised non-JSON 2xx body — treating as success",
                        endpoint=endpoint,
                        body=(response.text or "")[:120],
                    )
                    return {"status": "ok"}
                if parsed.get("status") == "failed":
                    logger.warning(
                        "Navidrome mutation rejected in a non-JSON envelope",
                        endpoint=endpoint,
                        code=(parsed.get("error") or {}).get("code"),
                        message=(parsed.get("error") or {}).get("message"),
                        body=(response.text or "")[:200],
                    )
                result = parsed

            if result.get("status") == "failed":
                if self._maybe_fallback_to_password_auth(result, endpoint):
                    _body = self._build_params(**params)
                    response = self.session.post(url, data=_body, timeout=timeout)
                    response.raise_for_status()
                    try:
                        result = response.json().get("subsonic-response", {}) or {}
                    except (json.JSONDecodeError, ValueError):
                        result = _parse_non_json_envelope(response.text or "") or {"status": "ok"}
                    if result.get("status") == "failed":
                        _log_subsonic_status(result, endpoint)
                else:
                    _log_subsonic_status(result, endpoint)
            return result
        except Exception as exc:
            _log_throttled_error(endpoint, exc, prefix=f"Navidrome {endpoint} POST failed")
            return {}

    # ------------------------------------------------------------------
    # Library read endpoints
    # ------------------------------------------------------------------
    def get_artists(self, artist_ids: list[str] | None = None) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getArtists", timeout=60)
            index_groups = data.get("artists", {}).get("index", []) or []
            filter_ids = set(artist_ids or [])
            artists: list[dict[str, Any]] = []

            for group in index_groups:
                for artist in group.get("artist", []) or []:
                    if filter_ids and artist.get("id") not in filter_ids:
                        continue
                    artists.append(artist)
            return artists
        except Exception as exc:
            logger.error("Failed to fetch artists", error=str(exc))
            return []

    def get_albums(self, artist_id: str | None = None, page_size: int = 200) -> list[dict[str, Any]]:
        if artist_id:
            return self.fetch_artist_albums(artist_id)

        albums: list[dict[str, Any]] = []
        offset = 0
        size = min(int(page_size or 200), 500)
        while True:
            try:
                data = self._get_subsonic_response(
                    "getAlbumList2",
                    timeout=90,
                    type="alphabeticalByName",
                    size=size,
                    offset=offset,
                )
                page = data.get("albumList2", {}).get("album", []) or []
                if not page:
                    break
                albums.extend(page)
                if len(page) < size:
                    break
                offset += size
            except Exception as exc:
                logger.error("Failed to fetch album list", offset=offset, error=str(exc))
                break
        return albums

    def get_album_list2_page(self, list_type: str = "newest", size: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response(
                "getAlbumList2",
                timeout=90,
                type=list_type,
                size=min(int(size or 200), 500),
                offset=offset,
            )
            return data.get("albumList2", {}).get("album", []) or []
        except Exception as exc:
            logger.error("Failed to fetch album list page", list_type=list_type, offset=offset, error=str(exc))
            return []

    def fetch_artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getArtist", timeout=60, id=artist_id)
            return data.get("artist", {}).get("album", []) or []
        except Exception as exc:
            logger.error("Failed to fetch albums for artist", artist_id=artist_id, error=str(exc))
            return []

    def fetch_album_tracks(self, album_id: str) -> dict[str, Any]:
        empty = {"tracks": [], "artist": "", "artistId": "", "name": "", "id": ""}
        try:
            data = self._get_subsonic_response("getAlbum", timeout=60, id=album_id)
            album = data.get("album", {}) or {}
            return {
                "tracks": album.get("song", []) or [],
                "artist": album.get("artist", "") or "",
                "artistId": album.get("artistId", "") or "",
                "name": album.get("name", "") or "",
                "id": album.get("id", "") or "",
            }
        except Exception as exc:
            logger.error("Failed to fetch tracks for album", album_id=album_id, error=str(exc))
            return empty

    def get_song(self, song_id: str) -> dict[str, Any]:
        try:
            data = self._get_subsonic_response("getSong", timeout=10, id=song_id)
            return data.get("song", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch extended song metadata", song_id=song_id, error=str(exc))
            return {}

    def get_songs(self, offset: int = 0, size: int = 500, modified: Any = None) -> list[dict[str, Any]]:
        # NOTE: `getSongs` is not part of the official Subsonic/OpenSubsonic
        # endpoint list (see navidrome.org/docs/developers/subsonic-api) - it
        # appears to be a Navidrome-internal/undocumented endpoint. It works
        # today, but since it isn't part of the published compatibility
        # contract, it could change or disappear in a future Navidrome
        # release without notice. Keep get_indexes()/get_albums() as the
        # documented fallback paths for full-library enumeration.
        try:
            params: dict[str, Any] = {"offset": offset, "size": size}
            ts = _coerce_modified_ts(modified)
            if ts is not None:
                params["modified"] = ts
            data = self._get_subsonic_response("getSongs", timeout=60, **params)
            return data.get("songs", {}).get("song", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch songs", offset=offset, error=str(exc))
            return []

    def get_indexes(self, if_modified_since: Any = None) -> dict[str, Any]:
        try:
            params: dict[str, Any] = {}
            ts = _coerce_modified_ts(if_modified_since)
            if ts is not None:
                # ifModifiedSince is documented as milliseconds since epoch.
                params["ifModifiedSince"] = ts * 1000
            data = self._get_subsonic_response("getIndexes", timeout=60, **params)
            return data.get("indexes", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch indexes", if_modified_since=if_modified_since, error=str(exc))
            return {}

    # ------------------------------------------------------------------
    # Playlist endpoints
    # ------------------------------------------------------------------
    @staticmethod
    def _is_smart_playlist(playlist: dict[str, Any]) -> bool:
        if not isinstance(playlist, dict):
            return False
        if playlist.get("smart") in (True, "true", "True", 1, "1"):
            return True
        if playlist.get("isSmart") in (True, "true", "True", 1, "1"):
            return True
        if playlist.get("criteria"):
            return True
        return str(playlist.get("type") or "").strip().lower() == "smart"

    def fetch_all_playlists(self) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getPlaylists", timeout=30)
            playlists = data.get("playlists", {}).get("playlist", []) or []
            for playlist in playlists:
                playlist["type"] = "smart" if self._is_smart_playlist(playlist) else "regular"
            return playlists
        except Exception as exc:
            logger.error("Failed to fetch playlists", error=str(exc))
            return []

    def fetch_playlist(self, playlist_id: str, timeout: int = 60) -> dict[str, Any]:
        """Fetch one playlist.

        NOTE FOR CALLERS: the Subsonic ``entry`` list is re-exposed as
        ``tracks``. Reading ``entry`` from this return value always misses.

        The default timeout is 60s rather than 30s: fetching a 100-track
        playlist was timing out during change detection, and a timeout here
        is indistinguishable from an empty playlist to naive callers.
        """
        try:
            data = self._get_subsonic_response("getPlaylist", timeout=timeout, id=playlist_id)
            if not data or data.get("status") == "failed":
                return {}
            playlist = data.get("playlist", {}) or {}
            if not playlist:
                return {}
            playlist["type"] = "smart" if self._is_smart_playlist(playlist) else "regular"
            playlist["tracks"] = playlist.pop("entry", []) or []
            return playlist
        except Exception as exc:
            logger.error("Failed to fetch playlist", playlist_id=playlist_id, error=str(exc))
            return {}

    def get_playlist_song_ids(self, playlist_id: str) -> list[str] | None:
        """Return a playlist's current ordered song IDs, or None if unknown.

        ``None`` means the contents could not be read (timeout, error, or an
        unexpected payload) and must NOT be confused with an empty playlist.
        """
        playlist = self.fetch_playlist(playlist_id)
        if not playlist:
            return None
        entries = playlist.get("tracks")
        if entries is None:
            song_count = playlist.get("songCount")
            try:
                if song_count is not None and int(song_count) == 0:
                    return []
            except (TypeError, ValueError):
                pass
            return None
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            return None
        ids = [str((e or {}).get("id") or "").strip() if isinstance(e, dict) else str(e or "").strip() for e in entries]
        return [value for value in ids if value]

    def find_playlist_by_name(self, name: str) -> dict[str, Any] | None:
        wanted = str(name or "").strip().lower()
        for playlist in self.fetch_all_playlists():
            if str(playlist.get("name") or "").strip().lower() == wanted:
                return playlist
        return None

    def delete_playlist(self, playlist_id: str) -> bool:
        try:
            data = self._get_subsonic_response("deletePlaylist", timeout=30, id=playlist_id)
            # Navidrome returns a 2xx with an EMPTY body for deletePlaylist
            # (like other mutation endpoints).  ``_get_subsonic_response``
            # returns {} for that — which is SUCCESS, not failure.  The old
            # ``data.get("status") == "ok"`` check made the dedupe/sweep
            # think every delete failed (so it logged "Could not delete
            # orphaned playlist" and the duplicate stayed in the UI on the
            # next fetch, even though the HTTP delete was accepted).
            if data.get("status") == "failed":
                return False
            return not data or data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to delete playlist", playlist_id=playlist_id, error=str(exc))
            return False

    def update_playlist_public(self, playlist_id: str, public: bool = True) -> bool:
        try:
            data = self._get_subsonic_response(
                "updatePlaylist",
                timeout=30,
                playlistId=playlist_id,
                public="true" if public else "false",
            )
            # Empty-body-success contract (mutation endpoints).
            if data.get("status") == "failed":
                return False
            return not data or data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to update playlist public status", playlist_id=playlist_id, public=public, error=str(exc))
            return False

    def upload_playlist_cover(self, playlist_id: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> bool:
        if not playlist_id or not image_bytes:
            return False
        url = f"{self.base_url}/rest/updatePlaylist"
        try:
            response = self.session.post(
                url,
                params=self._build_params(playlistId=playlist_id),
                files={"coverArt": ("cover.jpg", image_bytes, mime_type)},
                timeout=30,
            )
            response.raise_for_status()
            # Navidrome can return a 2xx with an empty / non-JSON body for
            # mutation endpoints — treat that as success unless the body
            # explicitly reports a failed status.
            if not response.content or not response.text.strip():
                return True
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError):
                parsed = _parse_non_json_envelope(response.text or "")
                if parsed and parsed.get("status") == "failed":
                    _log_subsonic_status(parsed, "updatePlaylist(cover)")
                    return False
                return True
            envelope = data.get("subsonic-response", {}) or {}
            if envelope.get("status") == "failed":
                _log_subsonic_status(envelope, "updatePlaylist(cover)")
            ok = bool(envelope.get("status") == "ok")
            if not ok:
                logger.warning("Playlist cover upload rejected", playlist_id=playlist_id, response=data)
            return ok
        except Exception as exc:
            logger.warning("Playlist cover upload failed", playlist_id=playlist_id, error=str(exc))
            return False

    def rename_playlist(self, playlist_id: str, name: str) -> bool:
        try:
            data = self._get_subsonic_response(
                "updatePlaylist",
                timeout=30,
                playlistId=playlist_id,
                name=name,
            )
            # Same empty-body-success contract as delete_playlist.
            if data.get("status") == "failed":
                return False
            return not data or data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to rename playlist", playlist_id=playlist_id, error=str(exc))
            return False

    def update_playlist_songs(
        self,
        playlist_id: str,
        song_ids: list[str],
        current_count: int | None = None,
    ) -> bool:
        """Replace a playlist's songs in place.

        The replace is expressed as "remove every current index, then add the
        new IDs". Knowing the CURRENT track count is therefore mandatory: if
        the count is wrong or unknown, the removal is skipped and the new IDs
        are APPENDED, silently doubling the playlist.

        ``current_count`` lets a caller that already fetched the playlist pass
        the count in, avoiding a second ``getPlaylist`` round trip. When it is
        not supplied the count is fetched here, and the write is ABORTED if it
        cannot be determined.
        """
        try:
            if current_count is None:
                existing = self.get_playlist_song_ids(playlist_id)
                if existing is None:
                    logger.warning(
                        "Refusing to update playlist songs",
                        playlist_id=playlist_id,
                        reason=(
                            "current track count is unknown; the update would "
                            "append instead of replace and duplicate the playlist"
                        ),
                    )
                    return False
                current_count = len(existing)

            current_count = max(0, int(current_count))
            params: dict[str, Any] = {"playlistId": playlist_id}

            if current_count > 0:
                params["songIndexToRemove"] = list(range(0, current_count))
            if song_ids:
                params["songIdToAdd"] = [str(s) for s in song_ids if str(s or "").strip()]

            data = self._post_subsonic_response("updatePlaylist", timeout=120, **params)
            ok = data.get("status") == "ok"
            if not ok:
                logger.warning(
                    "updatePlaylist songs rejected",
                    playlist_id=playlist_id,
                    removed=current_count,
                    added=len(song_ids or []),
                    response=data,
                )
            return ok
        except Exception as exc:
            logger.error("Failed to update playlist songs", playlist_id=playlist_id, error=str(exc))
            return False

    def create_playlist(self, name: str, song_ids: list[str]) -> dict[str, Any]:
        try:
            params: dict[str, Any] = {
                "name": str(name or ""),
                "public": "true" if public else "false",
            }
            _ids = [str(s) for s in (song_ids or []) if str(s or "").strip()]
            if _ids:
                params["songId"] = _ids
            return self._post_subsonic_response("createPlaylist", timeout=120, **params)
        except Exception as exc:
            logger.error("Failed to create playlist", name=name, error=str(exc))
            return {}

    # ------------------------------------------------------------------
    # Additional Standard Endpoints (Scrobble, GetStarred, etc)
    # ------------------------------------------------------------------
    def get_artist_info(self, artist_id: str) -> dict[str, Any]:
        empty = {"biography": "", "similarArtist": [], "musicBrainzId": "", "lastFmUrl": ""}
        try:
            data = self._get_subsonic_response("getArtistInfo2", timeout=30, id=artist_id)
            info = data.get("artistInfo2", {}) or {}
            return {
                "biography": info.get("biography", "") or "",
                "similarArtist": info.get("similarArtist", []) or [],
                "musicBrainzId": info.get("musicBrainzId", "") or "",
                "lastFmUrl": info.get("lastFmUrl", "") or "",
            }
        except Exception as exc:
            logger.debug("Failed to fetch artist info", artist_id=artist_id, error=str(exc))
            return empty

    def get_starred_items(self) -> dict[str, list[dict[str, Any]]]:
        try:
            data = self._get_subsonic_response("getStarred2", timeout=60)
            starred = data.get("starred2", {}) or {}
            return {
                "tracks": starred.get("song", []) or [],
                "albums": starred.get("album", []) or [],
                "artists": starred.get("artist", []) or [],
            }
        except Exception as exc:
            logger.debug("getStarred2 failed, falling back to getStarred", error=str(exc))

        try:
            data = self._get_subsonic_response("getStarred", timeout=60)
            starred = data.get("starred", {}) or {}
            return {
                "tracks": starred.get("song", []) or [],
                "albums": starred.get("album", []) or [],
                "artists": starred.get("artist", []) or [],
            }
        except Exception as exc:
            logger.error("Failed to fetch starred items", error=str(exc))
            return {"tracks": [], "albums": [], "artists": []}

    def star_track(self, track_id: str) -> bool:
        try:
            data = self._get_subsonic_response("star", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to star track", track_id=track_id, error=str(exc))
            return False

    def unstar_track(self, track_id: str) -> bool:
        try:
            data = self._get_subsonic_response("unstar", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to unstar track", track_id=track_id, error=str(exc))
            return False

    def start_scan(self) -> bool:
        try:
            data = self._get_subsonic_response("startScan", timeout=10)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to start Navidrome scan", error=str(exc))
            return False

    def get_scan_status(self) -> dict[str, Any]:
        try:
            data = self._get_subsonic_response("getScanStatus", timeout=10)
            scan_status = data.get("scanStatus", {}) or {}
            return {
                "success": True,
                "scanning": scan_status.get("scanning", False),
                "count": scan_status.get("count", 0),
                "lastScan": scan_status.get("lastScan"),
                "folderCount": scan_status.get("folderCount"),
            }
        except Exception as exc:
            logger.error("Failed to get Navidrome scan status", error=str(exc))
            return {"success": False, "error": str(exc)}

    def trigger_and_wait_for_scan(
        self,
        *,
        poll_interval_seconds: float = 5.0,
        max_wait_seconds: float = 1800.0,
    ) -> bool:
        """Trigger a Navidrome library rescan and WAIT for it to complete.

        Navidrome's ``startScan`` is asynchronous — it returns immediately
        while the scan runs in the background.  This method initiates the
        scan and then polls ``getScanStatus`` (every ``poll_interval_seconds``,
        up to ``max_wait_seconds``) until ``scanning`` is False, so callers
        can safely proceed to import/read the freshly-updated library.

        The reported issue: remote syncs were fired repeatedly from every
        file-tag write, pausing the server and locking the database.  This
        helper centralises the sync-and-wait so the ONLY automatic sync runs
        once, BEFORE the full Navidrome import, and completes before any
        import work begins.

        Returns True when the scan finished (or was not running at all);
        False on timeout or API failure.
        """
        import time as _time

        try:
            if not self.start_scan():
                logger.warning("Navidrome startScan failed — proceeding without remote sync")
                return False

            # Give Navidrome a moment to flip the scanning flag.
            _time.sleep(min(2.0, poll_interval_seconds))

            deadline = _time.time() + max_wait_seconds
            while _time.time() < deadline:
                try:
                    status = self.get_scan_status()
                except Exception as exc:
                    logger.warning(
                        "Navidrome getScanStatus failed during wait",
                        error=str(exc),
                    )
                    status = {}
                if status.get("success") and not status.get("scanning"):
                    logger.info(
                        "Navidrome remote scan complete",
                        count=status.get("count"),
                        last_scan=status.get("lastScan"),
                    )
                    return True
                _time.sleep(poll_interval_seconds)

            logger.warning(
                "Navidrome remote scan did not finish within timeout",
                max_wait_seconds=max_wait_seconds,
            )
            return False
        except Exception as exc:
            logger.warning("Navidrome trigger_and_wait_for_scan failed", error=str(exc))
            return False

    def ping(self) -> bool:
        try:
            data = self._get_subsonic_response("ping", timeout=10)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.debug("Navidrome ping failed", error=str(exc))
            return False

    def set_rating(self, track_id: str, rating: int) -> bool:
        try:
            rating = max(0, min(5, int(rating)))
            data = self._get_subsonic_response("setRating", timeout=15, id=track_id, rating=rating)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to set rating for track", track_id=track_id, error=str(exc))
            return False

    def scrobble(self, track_id: str, time_stamp: int | None = None, submission: bool = True) -> bool:
        try:
            params: dict[str, Any] = {"id": track_id, "submission": str(submission).lower()}
            if time_stamp is not None:
                params["time"] = time_stamp
            data = self._get_subsonic_response("scrobble", timeout=15, **params)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to scrobble track", track_id=track_id, error=str(exc))
            return False

    def search(self, query: str, artist_count: int = 5, album_count: int = 5, song_count: int = 20) -> dict[str, list[dict[str, Any]]]:
        try:
            data = self._get_subsonic_response(
                "search3",
                timeout=30,
                query=query,
                artistCount=artist_count,
                albumCount=album_count,
                songCount=song_count,
            )
            result = data.get("searchResult3", {}) or {}
            return {
                "artists": result.get("artist", []) or [],
                "albums": result.get("album", []) or [],
                "songs": result.get("song", []) or [],
            }
        except Exception as exc:
            logger.error("Failed to search Navidrome", query=query[:50], error=str(exc))
            return {"artists": [], "albums": [], "songs": []}

    def get_genres(self) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getGenres", timeout=15)
            return data.get("genres", {}).get("genre", []) or []
        except Exception as exc:
            logger.error("Failed to fetch genres", error=str(exc))
            return []

    def get_random_songs(self, size: int = 50, genre: str | None = None) -> list[dict[str, Any]]:
        try:
            params: dict[str, Any] = {"size": max(1, min(500, int(size)))}
            if genre:
                params["genre"] = genre
            data = self._get_subsonic_response("getRandomSongs", timeout=30, **params)
            return data.get("randomSongs", {}).get("song", []) or []
        except Exception as exc:
            logger.error("Failed to fetch random songs", error=str(exc))
            return []

    def get_cover_art_url(self, track_or_album_id: str, size: int = 300) -> str:
        params = self._build_params(id=track_or_album_id, size=size)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}/rest/getCoverArt?{qs}"

    def get_cover_art_bytes(self, track_or_album_id: str, size: int = 600) -> bytes | None:
        try:
            url = self.get_cover_art_url(track_or_album_id, size=size)
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            content_type = resp.headers.get("content-type", "")
            if content_type and not content_type.startswith("image/"):
                return None
            return resp.content or None
        except Exception as exc:
            logger.debug("Failed to fetch cover art bytes", track_or_album_id=track_or_album_id, error=str(exc))
            return None

    def get_stream_url(self, song_id: str, max_bitrate: int | None = None) -> str:
        params = self._build_params(id=song_id)
        if max_bitrate:
            params["maxBitRate"] = max_bitrate
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}/rest/stream?{qs}"

    def get_supported_extensions(self) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getOpenSubsonicExtensions", timeout=10)
            return data.get("openSubsonicExtensions", {}).get("extension", []) or []
        except Exception as exc:
            logger.debug("Could not query OpenSubsonic extensions", error=str(exc))
            return []

    def supports_opensubsonic(self) -> bool:
        exts = self.get_supported_extensions()
        return len(exts) > 0

    # ------------------------------------------------------------------
    # Compatibility forwarding methods
    # ------------------------------------------------------------------
    def build_artist_index(self) -> dict[str, dict[str, Any]]:
        from services.scanning.navidrome_service import build_artist_index
        return build_artist_index(self)

    def get_library_stats(self) -> dict[str, int]:
        from services.scanning.navidrome_service import get_library_stats
        return get_library_stats(self)

    def extract_track_metadata(self, track: dict[str, Any]) -> dict[str, Any]:
        from services.scanning.metadata_extractor import extract_track_metadata
        return extract_track_metadata(track, get_song=self.get_song)
