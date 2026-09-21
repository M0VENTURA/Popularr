"""Album art enrichment service.

Replaces helper-style album_art_manager responsibilities with a service that
composes API clients/enrichment and repository/file-tag layers without raw HTTP leakage.
"""

from __future__ import annotations

import base64 as _b64
import os
import re
import time as _time
from typing import Any, Optional, Tuple

import httpx
from quart import Response
import structlog
from sqlalchemy import text

from api_clients.coverartarchive import (
    get_release_front_image_bytes,
    get_release_group_front_image_bytes,
)
from api_clients.discogs_http import DiscogsHttpClient
from api_clients.musicbrainz_http import MusicBrainzHttpClient
from db.engine import db_session
from db.repositories.metadata import fetch_album_art_blob, fetch_album_art_record
from db.utils import row_get
from helpers.normalization_service import (
    normalize_album,
    normalize_artist,
)

logger = structlog.get_logger(__name__)

# Negative-result cache for the Navidrome art lookup
_navidrome_art_miss_cache: dict[tuple[str, str], float] = {}
_NAVIDROME_MISS_TTL_SECONDS = 6 * 3600

# Dedicated isolated HTTP client for non-blocking art downloads to prevent pool starvation
_art_client = httpx.Client(
    timeout=httpx.Timeout(10.0, connect=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
    follow_redirects=True,
)


def _sanitize_release_name(album_name: str) -> str:
    """Strips '(Topshelf Edition)', '[Deluxe Version]', etc. for exact API matches."""
    if not album_name:
        return ""
    cleaned = re.sub(
        r'\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]', 
        '', 
        album_name, 
        flags=re.IGNORECASE
    ).strip()
    return cleaned if cleaned else album_name


def save_album_art_to_db(
    artist_name: str,
    album_name: str,
    image_data: bytes,
    source: str = "unknown",
    mime_type: str = "image/jpeg",
) -> bool:
    if not image_data:
        return False
    try:
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO album_art
                    (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
                    VALUES (:artist_name, :album_name, :image_data, :mime_type, :source, CURRENT_TIMESTAMP)
                    ON CONFLICT (artist_name, album_name)
                    DO UPDATE SET
                        image_data = EXCLUDED.image_data,
                        image_mime_type = EXCLUDED.image_mime_type,
                        source = EXCLUDED.source,
                        downloaded_at = EXCLUDED.downloaded_at
                """),
                {
                    "artist_name": artist_name,
                    "album_name": album_name,
                    "image_data": image_data,
                    "mime_type": mime_type,
                    "source": source,
                },
            )
        return True
    except Exception as exc:
        logger.debug("Failed to save album art to database", error=str(exc))
        return False


def fetch_album_art_from_musicbrainz(artist_name: str, album_name: str) -> bytes | None:
    """Fetch cover art cleanly via MusicBrainz release-group and Cover Art Archive client."""
    clean_album = _sanitize_release_name(album_name)
    try:
        musicbrainz = MusicBrainzHttpClient()
        release_groups = musicbrainz.search_release_groups(f'release:"{clean_album}" AND artist:"{artist_name}"', limit=1)
        if not release_groups:
            return None
        release_group_mbid = release_groups[0].get("id")
        if not release_group_mbid:
            return None

        # Prefer a CONCRETE release's Cover Art Archive art (per-release
        # front art is populated more often than the release-group's).  Browse
        # the group's releases and try each; fall back to the group front.
        try:
            releases = musicbrainz.browse_releases_for_group(release_group_mbid, inc="", limit=5)
            for rel in releases or []:
                rel_id = rel.get("id")
                if not rel_id:
                    continue
                cover = get_release_front_image_bytes(rel_id)
                if cover:
                    logger.info("Fetched MusicBrainz/CAA art from release", release_id=rel_id, artist=artist_name, album=album_name)
                    return cover
        except Exception as exc:
            logger.debug("CAA release-browse art fetch failed", error=str(exc))

        return get_release_group_front_image_bytes(release_group_mbid)
    except Exception as exc:
        logger.debug("Failed to fetch album art from MusicBrainz/CAA", error=str(exc))
        return None


def fetch_album_art_from_itunes(
    artist_name: str,
    album_name: str,
) -> bytes | None:
    """Fetch album art from the iTunes / Apple Music API."""
    if not artist_name or not album_name:
        return None

    clean_album = _sanitize_release_name(album_name)
    try:
        headers = {"User-Agent": "Popularr/1.0"}
        search_url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist_name} {clean_album}",
            "entity": "album",
            "limit": 5,
        }

        response = _art_client.get(
            search_url,
            params=params,
            headers=headers,
        )
        response.raise_for_status()

        data = response.json()
        results = data.get("results") or []

        if not results:
            logger.debug("iTunes: No results found", artist=artist_name, album=album_name)
            return None

        wanted_artist = normalize_artist(artist_name)
        wanted_album = normalize_album(clean_album)

        for result in results:
            result_artist = normalize_artist(result.get("artistName", ""))
            result_album = normalize_album(result.get("collectionName", ""))

            if result_artist == wanted_artist and result_album == wanted_album:
                artwork_url = result.get("artworkUrl100", "")
                if not artwork_url:
                    continue

                artwork_url = artwork_url.replace("100x100", "1000x1000")
                logger.info("iTunes: Found exact match", artist=artist_name, album=album_name)

                art_response = _art_client.get(artwork_url, headers=headers)
                if art_response.status_code == 200:
                    return art_response.content

        artwork_url = results[0].get("artworkUrl100", "")
        if artwork_url:
            artwork_url = artwork_url.replace("100x100", "1000x1000")
            logger.debug("iTunes: Using fallback result", artist=artist_name, album=album_name)

            art_response = _art_client.get(artwork_url, headers=headers)
            if art_response.status_code == 200:
                return art_response.content

    except Exception as exc:
        logger.debug("Failed to fetch album art from iTunes", error=str(exc))

    return None


def fetch_album_art_from_discogs(artist_name: str, album_name: str, token: str) -> bytes | None:
    """Fetch cover art cleanly using the low-level DiscogsHttpClient."""
    if not token:
        return None
        
    clean_album = _sanitize_release_name(album_name)
    try:
        discogs = DiscogsHttpClient(token=token)
        results = discogs.search_database({"q": f"{artist_name} {clean_album}", "type": "release", "per_page": 1})

        if not results or not results[0].get("cover_url"):
            return None

        img_url = results[0]["cover_url"]
        resp = _art_client.get(img_url)

        if resp.status_code == 200:
            return resp.content
    except Exception as exc:
        logger.debug("Failed to fetch album art from Discogs", error=str(exc))
    return None


def fetch_album_art_from_navidrome(artist_name: str, album_name: str) -> bytes | None:
    """Pull album art from Navidrome's own library (Subsonic ``getCoverArt``).

    This is the FIRST source every caller must try. The art Navidrome already
    serves for the album the user owns is by definition the right cover, and it
    costs one request to the local server instead of a lookup against MusicBrainz
    / Cover Art Archive / Discogs / AudioDB.

    Two steps, cheapest first:

    1. **A song id from our own ``tracks`` table.** ``tracks.id`` IS the
       Navidrome/Subsonic song id (the importer stores it verbatim — see
       ``services/scanning/payload_builder.py``), and ``getCoverArt`` accepts a
       song id, so ONE request returns the album's art with no search. This also
       replaces the old library-wide ``client.search(...)`` fuzzy lookup, which
       was expensive enough that the whole call had to be guarded.
    2. Only when that yields nothing, the previous fallback: search Navidrome for
       the album and use the ALBUM id.

    The local-library guard is now CASE-INSENSITIVE, matching every other
    album-scoped lookup in the app. It used to compare ``artist``/``album`` with
    ``=`` while the rest of the code uses ``LOWER(...)``, so an album whose stored
    casing differed was reported as "not in local library" and Navidrome was
    skipped entirely — one of the reasons the online sources were reached first.
    """
    # GUARD: only for albums that ARE in the local library. Never run a
    # Navidrome lookup for a missing release (the artist page lists those too).
    local_song_id = ""
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT id FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "  AND LOWER(COALESCE(album, '')) = LOWER(:album) "
                    "  AND COALESCE(id, '') <> '' "
                    "LIMIT 1"
                ),
                {"artist": artist_name, "album": album_name},
            ).fetchone()
            if not row:
                logger.debug("Skipping Navidrome art search (album not in local library)", artist=artist_name, album=album_name)
                return None
            local_song_id = str(row[0] or "").strip()
    except Exception as exc:
        logger.debug("Navidrome guard check failed", error=str(exc))
        pass # Fail open to prevent regressions

    try:
        from helpers.config_helpers import get_config
        from api_clients.navidrome import NavidromeClient

        cfg = get_config()
        users = cfg.get("navidrome_users") or []
        if not users:
            return None
            
        first = users[0]
        base_url = str(first.get("base_url") or "").rstrip("/")
        username = str(first.get("user") or "")
        password = str(first.get("pass") or "")
        if not all([base_url, username, password]):
            return None

        cache_key = (normalize_artist(artist_name), normalize_album(album_name))
        last_miss = _navidrome_art_miss_cache.get(cache_key)
        if last_miss and (_time.time() - last_miss) < _NAVIDROME_MISS_TTL_SECONDS:
            return None

        client = NavidromeClient(
            base_url=base_url,
            username=username,
            password=password,
        )

        # ---- Step 1: the song id we already hold. One request, no search. ----
        if local_song_id:
            data = client.get_cover_art_bytes(local_song_id, size=600)
            if data:
                logger.debug(
                    "Fetched album art from Navidrome (library song id)",
                    artist=artist_name, album=album_name, song_id=local_song_id,
                )
                return data

        # ---- Step 2: locate the album on the server and use its album id. ----
        wanted_artist = normalize_artist(artist_name)
        wanted_album = normalize_album(album_name)
        album_id = None

        try:
            result = client.search(
                f"{artist_name} {album_name}",
                artist_count=0,
                album_count=10,
                song_count=0,
            )
            for candidate in result.get("albums") or []:
                candidate_artist = normalize_artist(candidate.get("artist") or "")
                candidate_album = normalize_album(
                    candidate.get("name") or candidate.get("album") or ""
                )
                if candidate_artist == wanted_artist and candidate_album == wanted_album:
                    album_id = candidate.get("id")
                    break
        except Exception as exc:
            logger.debug("Failed to locate album in Navidrome", artist=artist_name, album=album_name, error=str(exc))

        if not album_id:
            _navidrome_art_miss_cache[cache_key] = _time.time()
            return None

        data = client.get_cover_art_bytes(album_id, size=600)
        if data:
            logger.debug("Fetched album art from Navidrome", artist=artist_name, album=album_name)
            return data
            
        _navidrome_art_miss_cache[cache_key] = _time.time()
        return None
    except Exception as exc:
        logger.debug("Failed to fetch album art from Navidrome", error=str(exc))
        return None


def fetch_album_art_from_audiodb(artist_name: str, album_name: str) -> bytes | None:
    """Fetch album art from TheAudioDB as an additional fallback source."""
    clean_album = _sanitize_release_name(album_name)
    try:
        url = "https://theaudiodb.com/api/v1/json/2/searchalbum.php"
        resp = _art_client.get(url, params={"s": artist_name, "a": clean_album})
        if resp.status_code != 200:
            return None
            
        data = resp.json()
        albums = data.get("album", [])
        if not albums:
            return None
            
        art_url = albums[0].get("strAlbumThumb") or albums[0].get("strAlbumCDart")
        if not art_url:
            return None
            
        img_resp = _art_client.get(art_url)
        if img_resp.status_code == 200:
            logger.debug("Fetched album art from AudioDB", artist=artist_name, album=album_name)
            return img_resp.content
        return None
    except Exception as exc:
        logger.debug("Failed to fetch album art from AudioDB", error=str(exc))
        return None


def apply_album_art_to_tracks(artist_name: str, album_name: str, image_data: bytes, mime_type: str = "image/jpeg") -> int:
    if not image_data:
        return 0
        
    rows = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist_name AND album = :album_name
                """),
                {"artist_name": artist_name, "album_name": album_name},
            )
            rows = result.fetchall() or []
    except Exception as exc:
        logger.debug("Failed to query tracks for album-art apply", error=str(exc))
        rows = []

    from services.metadata.tag_file_service import resolve_music_file_path, write_tags_to_file

    updated = 0
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            file_path = str(mapping.get("file_path") or "").strip()
        else:
            # Legacy tuple row: (id, file_path).
            file_path = str(row[1] or "").strip() if len(row) > 1 else ""
        if not file_path:
            continue
        resolved = resolve_music_file_path(file_path) or file_path
        if not os.path.exists(resolved):
            continue
        if write_tags_to_file(resolved, {"cover_art_data": image_data, "cover_art_mime": mime_type}):
            updated += 1
    return updated


def get_album_art_placeholder_svg(size: int = 300) -> Response:
    """Generate an SVG placeholder."""
    try:
        size = int(size)
        size = max(10, min(1000, size))
    except (ValueError, TypeError):
        size = 300
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" role="img">
        <rect fill="#2a2a2a" width="{size}" height="{size}"/>
        <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Album Art</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')


# Album-art sources that are ALREADY the best available copy: Navidrome's own
# art, and the two the USER chose (an upload, or a URL they supplied). Everything
# else came from a provider.
_ART_SOURCES_ALREADY_BEST = frozenset({"navidrome", "upload", "url"})


def navidrome_art_may_replace(source: str) -> bool:
    """May Navidrome's ``getCoverArt`` replace the art already stored?

    YES for anything a provider supplied (``musicbrainz``, ``discogs``,
    ``audiodb``, ``itunes``, ``missing_releases``, ``unknown``, ``""``); NO for
    Navidrome's own art and for the two user-chosen sources.

    WHY this exists: every art path checks the DB cache BEFORE anything else and
    returned the cached blob, so an album that had once collected Cover Art
    Archive (or Discogs/AudioDB) art could never pick up the library's own cover
    — the reported "currently it looks online for it, but it should first use the
    coverart from Navidrome". The cache stays authoritative for the user's own
    choices; a provider-sourced picture is upgradable.
    """
    return str(source or "").strip().lower() not in _ART_SOURCES_ALREADY_BEST


def get_or_fetch_album_art(artist: str, album: str, discogs_token: str = "") -> tuple[bytes | None, str | None]:
    """Orchestrates DB retrieval, API fetching, and DB caching.

    Order: stored art (unless a provider supplied it) → Navidrome → MusicBrainz
    / Cover Art Archive → Discogs → AudioDB. Navidrome is asked BEFORE the online
    providers because the album the user owns is already in Navidrome, so its
    ``getCoverArt`` is both the correct cover and a local request.
    """
    data, mime, cached_source = fetch_album_art_record(artist=artist, album=album)
    if data and not navidrome_art_may_replace(cached_source):
        return data, mime

    # FAST PATH for Missing Releases: Check the missing_releases table for a cached cover URL
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT cover_art_url FROM missing_releases WHERE LOWER(artist) = LOWER(:artist) AND LOWER(title) = LOWER(:album) LIMIT 1"),
                {"artist": artist, "album": album}
            ).fetchone()
            if row and row[0] and str(row[0]).strip():
                url = str(row[0]).strip()
                resp = _art_client.get(url)
                if resp.status_code == 200:
                    save_album_art_to_db(artist, album, resp.content, source="missing_releases")
                    return resp.content, "image/jpeg"
    except Exception as exc:
        logger.debug("Missing release art shortcut failed", error=str(exc))

    data = fetch_album_art_from_navidrome(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="navidrome")
        return data, "image/jpeg"

    # Navidrome has no copy of this album: whatever we already hold STANDS,
    # rather than re-downloading the same provider art it came from (``data``
    # here is still the stored blob from the read at the top of this function).
    if data:
        return data, mime or "image/jpeg"

    data = fetch_album_art_from_musicbrainz(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="musicbrainz")
        return data, "image/jpeg"

    data = fetch_album_art_from_discogs(artist, album, token=discogs_token)
    if data:
        save_album_art_to_db(artist, album, data, source="discogs")
        return data, "image/jpeg"

    data = fetch_album_art_from_audiodb(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="audiodb")
        return data, "image/jpeg"

    return None, None


def download_and_save_album_art(artist: str, album: str, image_data: bytes, source: str = "unknown") -> bool:
    if not image_data:
        return False
    save_album_art_to_db(artist, album, image_data, source=source)
    count = apply_album_art_to_tracks(artist, album, image_data)
    return count > 0


def search_album_art_external(artist: str, album: str, source: str = "musicbrainz") -> tuple[dict, int]:
    """Search for album art from the specified external source."""
    try:
        from helpers.config_helpers import get_config
        _cfg = get_config() or {}
        _discogs_cfg = (_cfg.get("api_integrations") or {}).get("discogs") or {}
        _discogs_token = str(_discogs_cfg.get("token") or "").strip()
    except Exception:
        _discogs_token = ""

    def _mb_search() -> bytes | None:
        data = fetch_album_art_from_musicbrainz(artist, album)
        if data:
            return data
        return None

    sources = {
        "musicbrainz": ("MusicBrainz", lambda: _mb_search()),
        "discogs": ("Discogs", lambda: fetch_album_art_from_discogs(artist, album, token=_discogs_token)),
        "applemusic": ("Apple Music", lambda: fetch_album_art_from_itunes(artist, album)),
        "itunes": ("Apple Music", lambda: fetch_album_art_from_itunes(artist, album)),
        "audiodb": ("AudioDB", lambda: fetch_album_art_from_audiodb(artist, album)),
    }
    entry = sources.get(source)
    if not entry:
        return {"success": False, "error": f"Unknown source: {source}"}, 400

    label, fn = entry
    data = fn()
    if not data:
        return (
            {
                "success": False,
                "error": "No album art found",
                "images": [],
            },
            404,
        )

    data_url = f"data:image/jpeg;base64,{_b64.b64encode(data).decode('ascii')}"
    return (
        {
            "success": True,
            "images": [
                {
                    "url": data_url,
                    "title": album,
                    "artist": artist,
                    "source": label,
                }
            ],
        },
        200,
    )


def set_album_art_from_url(artist: str, album: str, image_url: str) -> dict[str, Any]:
    """Download image from URL and save to database + embed into track files."""
    try:
        if str(image_url or "").startswith("data:"):
            header, _, b64_payload = image_url.partition(",")
            mime_type = header[5:].split(";")[0] or "image/jpeg"
            try:
                image_data = _b64.b64decode(b64_payload)
            except Exception:
                return {"success": False, "error": "Invalid data URL payload"}
        else:
            resp = _art_client.get(image_url)
            if resp.status_code != 200:
                return {"success": False, "error": "Failed to download image"}
            image_data = resp.content
            mime_type = resp.headers.get("content-type", "image/jpeg")

        saved = save_album_art_to_db(artist, album, image_data, source="url", mime_type=mime_type)
        if saved:
            files_updated = 0
            try:
                files_updated = apply_album_art_to_tracks(artist, album, image_data, mime_type)
            except Exception as exc:
                logger.debug("URL art embed failed", artist=artist, album=album, error=str(exc))
            return {
                "success": True,
                "message": "Album art saved from URL",
                "files_updated": files_updated,
            }
        return {"success": False, "error": "Failed to save album art"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def set_album_art_from_upload(artist: str, album: str, image_data: bytes, mime_type: str) -> dict[str, Any]:
    """Save uploaded image data to database + embed into track files."""
    try:
        saved = save_album_art_to_db(artist, album, image_data, source="upload", mime_type=mime_type)
        if not saved:
            return {"success": False, "error": "Failed to save album art"}
        files_updated = 0
        try:
            files_updated = apply_album_art_to_tracks(artist, album, image_data, mime_type)
        except Exception as exc:
            logger.debug("Upload art embed failed", artist=artist, album=album, error=str(exc))
        return {
            "success": True,
            "message": "Album art saved from upload",
            "files_updated": files_updated,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


__all__ = [
    "fetch_album_art_blob",
    "save_album_art_to_db",
    "apply_album_art_to_tracks",
    "fetch_album_art_from_musicbrainz",
    "fetch_album_art_from_discogs",
    "fetch_album_art_from_audiodb",
    "fetch_album_art_from_navidrome",
    "get_or_fetch_album_art",
    "navidrome_art_may_replace",
    "download_and_save_album_art",
    "get_album_art_placeholder_svg",
]
