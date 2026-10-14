"""Compatibility facade for Discogs.

The split version keeps low-level HTTP in ``api_clients.discogs_http`` and
moves enrichment/business rules to ``services.enrichment.discogs_service``.
"""

from __future__ import annotations

from typing import Any

from api_clients.discogs_http import DiscogsHttpClient
from services.enrichment.discogs_service import (
    DiscogsService,
    get_discogs_artist_biography,
    get_discogs_genres,
    has_discogs_video,
    is_discogs_single,
)


class DiscogsClient:
    """Backward-compatible Discogs client facade."""

    def __init__(self, token: str, http_session: Any = None, enabled: bool = True):
        self.token = token or ""
        self.enabled = enabled
        self.http = DiscogsHttpClient(token=token, http_session=http_session, enabled=enabled)
        self.service = DiscogsService(token=token, http_client=self.http, enabled=enabled)
        self.session = self.http.session
        self.base_url = self.http.base_url
        self.headers = self.http.headers

    def get_comprehensive_metadata(self, title: str, artist: str, duration: float | None = None, timeout: float = 10.0) -> dict[str, Any] | None:
        return self.service.get_comprehensive_metadata(title, artist, duration=duration, timeout=timeout)

    def search_releases(self, query: str, limit: int = 5, timeout: float = 10.0) -> list[dict[str, Any]]:
        return self.service.search_releases(query, limit=limit, timeout=timeout)

    def is_single(self, title: str, artist: str, album_context: dict[str, Any] | None = None, timeout: float = 10.0) -> bool:
        return self.service.is_single(title, artist, album_context=album_context, timeout=timeout)

    def get_single_release_year(self, title: str, artist: str, timeout: float = 10.0) -> int | None:
        return self.service.get_single_release_year(title, artist, timeout=timeout)

    def has_official_video(self, title: str, artist: str, timeout: float = 10.0) -> bool:
        return self.service.has_official_video(title, artist, timeout=timeout)

    def get_artist_id(self, artist: str, albums: list[str] | None = None, timeout: float = 10.0) -> str | None:
        return self.service.get_artist_id(artist, albums=albums, timeout=timeout)

    def get_genres(self, title: str, artist: str, timeout: float = 10.0) -> list[str]:
        return self.service.get_genres(title, artist, timeout=timeout)

    def get_release(self, release_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
        return self.service.get_release(release_id, timeout=timeout)

    def get_release_genres_by_id(self, release_id: str, timeout: float = 10.0) -> list[dict[str, str]]:
        return self.service.get_release_genres_by_id(release_id, timeout=timeout)

    def get_artist_biography(self, artist: str, timeout: float = 10.0) -> dict[str, Any]:
        return self.service.get_artist_biography(artist, timeout=timeout)

    def get_artist_biography_by_id(self, artist_id: str, timeout: float = 10.0) -> dict[str, Any]:
        return self.service.get_artist_biography_by_id(artist_id, timeout=timeout)


_discogs_client: DiscogsClient | None = None


def _get_discogs_client(token: str, enabled: bool = True) -> DiscogsClient:
    global _discogs_client
    if _discogs_client is None or _discogs_client.token != token:
        _discogs_client = DiscogsClient(token, enabled=enabled)
    return _discogs_client


def is_discogs_single(title: str, artist: str, album_context: dict[str, Any] | None = None, timeout: float = 10.0, token: str = "", enabled: bool = True) -> bool:
    return _get_discogs_client(token, enabled=enabled).is_single(title, artist, album_context=album_context, timeout=timeout)


def get_discogs_genres(title: str, artist: str, token: str = "", enabled: bool = True, timeout: float = 10.0) -> list[str]:
    return _get_discogs_client(token, enabled=enabled).get_genres(title, artist, timeout=timeout)


def has_discogs_video(title: str, artist: str, token: str = "", enabled: bool = True, timeout: float = 10.0) -> bool:
    return _get_discogs_client(token, enabled=enabled).has_official_video(title, artist, timeout=timeout)


def get_discogs_artist_biography(artist: str, token: str = "", enabled: bool = True, timeout: float = 10.0) -> dict[str, Any]:
    return _get_discogs_client(token, enabled=enabled).get_artist_biography(artist, timeout=timeout)
