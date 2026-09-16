"""TheAudioDB API client.
Provides access to artist/album metadata, artwork, and genres.
"""

from __future__ import annotations

import logging
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)

DEFAULT_API_KEY = "123"


class AudioDbClient:
    """TheAudioDB wrapper for artist/album artwork, biography and genres."""

    def __init__(self, api_key: str = DEFAULT_API_KEY, http_session=None, enabled: bool = True):
        self.api_key = api_key or DEFAULT_API_KEY
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://www.theaudiodb.com/api/v1/json"

    def _get(self, endpoint: str, params: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        """GET an AudioDB endpoint and return a JSON dict."""
        if not self.enabled or not self.api_key:
            return {}
        url = f"{self.base_url}/{self.api_key}/{endpoint}"
        response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def search_artist(self, artist_name: str, timeout: float = 10.0) -> dict[str, Any] | None:
        """Return first TheAudioDB artist match."""
        if not artist_name:
            return None
        try:
            data = self._get("search.php", {"s": artist_name}, timeout=timeout)
            artists = data.get("artists")
            if isinstance(artists, list) and artists:
                return artists[0] if isinstance(artists[0], dict) else None
        except Exception as exc:
            logger.debug("AudioDB artist search failed for %s: %s", artist_name, exc)
        return None

    def search_album(self, artist_name: str, album_name: str, timeout: float = 10.0) -> dict[str, Any] | None:
        """Return first TheAudioDB album match."""
        if not artist_name or not album_name:
            return None
        try:
            data = self._get("searchalbum.php", {"s": artist_name, "a": album_name}, timeout=timeout)
            albums = data.get("album")
            if isinstance(albums, list) and albums:
                return albums[0] if isinstance(albums[0], dict) else None
        except Exception as exc:
            logger.debug("AudioDB album search failed for %s - %s: %s", artist_name, album_name, exc)
        return None

    def get_artist_fanart(self, artist_name: str, timeout: float = 10.0) -> str | None:
        """Return best available artist image URL."""
        artist = self.search_artist(artist_name, timeout=timeout)
        if not artist:
            return None
        return (
            artist.get("strArtistFanart")
            or artist.get("strArtistBanner")
            or artist.get("strArtistLogo")
            or artist.get("strArtistThumb")
            or None
        )

    def get_artist_biography(self, artist_name: str, timeout: float = 10.0) -> str | None:
        """Return English artist biography where available."""
        artist = self.search_artist(artist_name, timeout=timeout)
        if not artist:
            return None
        return artist.get("strBiographyEN") or artist.get("strBiography") or None

    def get_album_artwork(self, artist_name: str, album_name: str, timeout: float = 10.0) -> str | None:
        """Return album artwork URL where available."""
        album = self.search_album(artist_name, album_name, timeout=timeout)
        if not album:
            return None
        return album.get("strAlbumThumb") or album.get("strAlbumCDart") or None

def get_artist_genres(self, artist_name: str, timeout: float = 10.0) -> list[str]:
        """Return primary artist genre and style as a list for compatibility."""
        artist = self.search_artist(artist_name, timeout=timeout)
        if not artist:
            return []
            
        genres = []
        
        # Grab the broad genre (e.g., "Metal")
        primary_genre = artist.get("strGenre")
        if primary_genre and primary_genre.strip():
            genres.append(primary_genre.strip())
            
        # Grab the granular style (e.g., "Metalcore")
        style = artist.get("strStyle")
        if style and style.strip():
            genres.append(style.strip())
            
        return list(dict.fromkeys(genres)) # Deduplicate just in case


_audiodb_client: AudioDbClient | None = None


def _get_audiodb_client(api_key: str = DEFAULT_API_KEY, enabled: bool = True) -> AudioDbClient:
    """Return process-local default AudioDB client for compatibility wrappers."""
    global _audiodb_client
    if _audiodb_client is None or _audiodb_client.api_key != (api_key or DEFAULT_API_KEY):
        _audiodb_client = AudioDbClient(api_key=api_key or DEFAULT_API_KEY, enabled=enabled)
    return _audiodb_client


def get_artist_fanart(artist_name: str, api_key: str = DEFAULT_API_KEY, enabled: bool = True) -> str | None:
    return _get_audiodb_client(api_key, enabled).get_artist_fanart(artist_name)


def get_artist_biography(artist_name: str, api_key: str = DEFAULT_API_KEY, enabled: bool = True) -> str | None:
    return _get_audiodb_client(api_key, enabled).get_artist_biography(artist_name)


def get_album_artwork(artist_name: str, album_name: str, api_key: str = DEFAULT_API_KEY, enabled: bool = True) -> str | None:
    return _get_audiodb_client(api_key, enabled).get_album_artwork(artist_name, album_name)


def get_audiodb_genres(artist: str, api_key: str = DEFAULT_API_KEY, enabled: bool = True) -> list[str]:
    return _get_audiodb_client(api_key, enabled).get_artist_genres(artist)
