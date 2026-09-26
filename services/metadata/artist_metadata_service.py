"""Artist metadata service — consolidated from the old monolithic app.py.

This module owns artist-level metadata routes that don't fit neatly into
the existing correction/scan services. Functions are module-level so routes
can ``import metadata_service as metadata``.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.enrichment.artist_bio_service import get_artist_biography
from services.enrichment.musicbrainz_service import get_shared_mb_client

# ⚠️ Module-level, NOT a local import inside one function.  ``lookup_ids``
# builds a Lucene query with this helper, and it used to be imported only
# inside ``genre_recommendations`` — so in ``lookup_ids`` the name was
# undefined and raised NameError, which the surrounding
# ``except Exception`` swallowed at DEBUG level.  The MusicBrainz half of the
# artist-ID lookup therefore ALWAYS failed and silently returned an empty
# musicbrainz_artist_id, with nothing in the log to show why.
from api_clients.musicbrainz_http import escape_lucene_special_chars

logger = structlog.get_logger(__name__)


def cleanup_false_positive_missing(artist: str) -> tuple[dict[str, Any], int]:
    """Remove false-positive missing releases for an artist."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(:artist) AND title IN "
                    "(SELECT DISTINCT album FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist))"
                ),
                {"artist": artist},
            )
            removed = result.rowcount or 0
        return {"success": True, "removed_count": removed, "artist": artist}, 200
    except Exception as exc:
        logger.error("Cleanup false-positive missing failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def get_artist_bio(artist: str) -> tuple[dict[str, Any], int]:
    """Get artist biography from DB cache or Wikidata fallback."""
    if not artist:
        return {"success": False, "error": "name required"}, 400
    bio = ""
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT bio FROM artists WHERE name = :artist"),
                {"artist": artist},
            ).mappings().first()
        bio = str((row or {}).get("bio") or "").strip()
    except Exception:
        bio = ""
    if bio:
        return {"success": True, "bio": bio, "source": "database"}, 200
    try:
        bio = get_artist_biography(artist) or ""
    except Exception:
        bio = ""
    return {"success": True, "bio": bio, "source": "wikidata" if bio else "none"}, 200


def get_singles_count(artist: str) -> tuple[dict[str, Any], int]:
    """Get count of singles for an artist."""
    if not artist:
        return {"success": False, "error": "name required"}, 400
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT COUNT(*) FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND COALESCE(is_single, FALSE) = TRUE "
                    "AND LOWER(COALESCE(single_confidence, '')) = 'high'"
                ),
                {"artist": artist},
            ).fetchone()
        count = int(row[0]) if row else 0
        return {"success": True, "count": count}, 200
    except Exception as exc:
        logger.error("Get singles count failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def get_covered_by(artist: str) -> tuple[dict[str, Any], int]:
    """Get covers of this artist's songs in the library."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, title, artist, album FROM tracks "
                    "WHERE original_cover_artist ILIKE :artist AND LOWER(COALESCE(album_artist, artist)) != LOWER(:artist)"
                ),
                {"artist": artist},
            ).mappings().all()
        covers = [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "artist": r.get("artist"),
                "album": r.get("album"),
            }
            for r in rows
        ]
        return {"success": True, "covers": covers, "total": len(covers)}, 200
    except Exception as exc:
        logger.error("Get covered by failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def artist_favourite(request: Any) -> tuple[dict[str, Any], int]:
    """Check/add/remove artist favourite via bookmarks table."""
    artist = (request.args.get("artist") or (request.json or {}).get("artist") or "").strip()
    if not artist:
        return {"success": False, "error": "artist required"}, 400
        
    with db_session() as session:
        if request.method == "GET":
            row = session.execute(
                text("SELECT 1 FROM bookmarks WHERE type = 'artist_favourite' AND LOWER(name) = LOWER(:artist) LIMIT 1"),
                {"artist": artist},
            ).fetchone()
            return {"success": True, "is_favourite": row is not None}, 200
        elif request.method == "POST":
            session.execute(
                text("INSERT INTO bookmarks (type, name) VALUES ('artist_favourite', :artist) ON CONFLICT DO NOTHING"),
                {"artist": artist},
            )
            return {"success": True, "is_favourite": True}, 200
        elif request.method == "DELETE":
            session.execute(
                text("DELETE FROM bookmarks WHERE type = 'artist_favourite' AND LOWER(name) = LOWER(:artist)"),
                {"artist": artist},
            )
            return {"success": True, "is_favourite": False}, 200
        return {"success": False, "error": "Unsupported method"}, 405


# Thread-safe in-process cache for artist images
_artist_image_cache: dict[str, tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()
_ARTIST_IMAGE_CACHE_TTL_SECONDS = 6 * 3600
# ⚠️ Deliberately SHORT (1 min), unlike the positive TTL above.
#
# A "no image" answer is only true until the artist's next SCAN writes
# ``artists.image_url`` — and the user's requirement is that an artist image
# appears once that scan runs ("show an empty spot until it's scanned in").
# A long negative TTL breaks exactly that: the page would keep serving the
# cached emptiness for the rest of the window even though the scan had already
# filled the image. This cache is per-process (each hypercorn worker has its
# own), so there is no reliable hook to invalidate the other workers when a
# scan lands — a short window is what bounds the staleness instead.
# Long enough to protect the DB from a burst of pagination requests, short
# enough that a completed scan shows up on the next reload.
_ARTIST_IMAGE_NEGATIVE_TTL_SECONDS = 60
_ARTIST_IMAGE_CACHE_MAX_ENTRIES = 5000


def get_artist_image(artist: str) -> tuple[dict[str, Any], int]:
    """Get the artist's image URL from the DATABASE ONLY.

    Resolution order (all local):

    1. ``artists.image_url`` — written by the SCAN
       (``album_stage._fetch_artist_metadata`` resolves it once per artist per
       scan, together with country/bio/members).
    2. ``artist_images.image_url`` — a manually chosen image.
    3. the artist's own ALBUM art from ``album_art``, served through the
       existing ``/api/album/<artist>/<album>/art`` endpoint — "use an album
       image when the artist has none".

    Returns ``image_url: ""`` when none of those exist, so an artist that has
    not been scanned yet shows an EMPTY spot.

    ⚠️ There is deliberately NO AudioDB (or any other network) fallback here.
    This is read on every artists-page load, once per artist, and the shared
    clients throttle by reserving a future slot and sleeping — so an unprefetched
    artist made a page load queue behind whatever else held the budget (the same
    class of freeze the artist page itself had). The image is the SCAN's job; a
    page must only read what the scan already resolved.
    """
    if not artist:
        return {"success": False, "error": "name required"}, 400

    def _is_valid_url(url: str) -> bool:
        # A same-origin PATH is valid too: the album-art fallback below is served
        # by this app's own ``/api/album/<artist>/<album>/art`` route.
        return bool(url) and url.startswith(("http://", "https://", "/"))

    key = artist.strip().lower()
    if not key:
        return {"success": False, "error": "name required"}, 400

    with _CACHE_LOCK:
        cached = _artist_image_cache.get(key)
        if cached:
            img_url, fetched_at = cached
            ttl = _ARTIST_IMAGE_CACHE_TTL_SECONDS if _is_valid_url(img_url) else _ARTIST_IMAGE_NEGATIVE_TTL_SECONDS
            if (time.monotonic() - fetched_at) < ttl:
                return ({"success": True, "image_url": img_url}, 200) if _is_valid_url(img_url) else (
                    {"success": False, "error": "No image", "image_url": ""}, 200
                )

    url = ""
    try:
        with db_session() as session:
            try:
                row = session.execute(
                    text(
                        "SELECT image_url FROM artists WHERE LOWER(name) = LOWER(:artist) "
                        "AND image_url IS NOT NULL AND image_url != '' "
                        "ORDER BY CASE WHEN name = :artist THEN 0 ELSE 1 END LIMIT 1"
                    ),
                    {"artist": artist},
                ).mappings().first()
                if row:
                    url = str(row.get("image_url") or "").strip()
            except Exception:
                url = ""

            if not _is_valid_url(url):
                url = ""
                try:
                    img_row = session.execute(
                        text(
                            "SELECT image_url FROM artist_images WHERE LOWER(artist_name) = LOWER(:artist) "
                            "AND image_url IS NOT NULL AND image_url != '' LIMIT 1"
                        ),
                        {"artist": artist},
                    ).mappings().first()
                    if img_row:
                        url = str(img_row.get("image_url") or "").strip()
                except Exception:
                    url = ""

            # Fallback 3: the artist's own ALBUM art, straight from the local
            # ``album_art`` table (filled by the album-art pipeline during the
            # scan) and served by this app's album-art route. Used when the
            # artist has no image of its own — no network involved.
            if not _is_valid_url(url):
                try:
                    art_row = session.execute(
                        text(
                            "SELECT album_name FROM album_art "
                            "WHERE LOWER(artist_name) = LOWER(:artist) "
                            "  AND image_data IS NOT NULL "
                            "ORDER BY downloaded_at DESC NULLS LAST, LOWER(album_name) ASC "
                            "LIMIT 1"
                        ),
                        {"artist": artist},
                    ).mappings().first()
                    _album_for_art = str((art_row or {}).get("album_name") or "").strip()
                    if _album_for_art:
                        from urllib.parse import quote as _quote

                        url = "/api/album/{artist}/{album}/art".format(
                            artist=_quote(artist, safe=""),
                            album=_quote(_album_for_art, safe=""),
                        )
                        logger.debug(
                            "Artist image served from album art",
                            artist=artist,
                            album=_album_for_art,
                        )
                except Exception as exc:
                    logger.debug("Album-art fallback failed", artist=artist, error=str(exc))

        with _CACHE_LOCK:
            if _is_valid_url(url):
                _artist_image_cache[key] = (url, time.monotonic())
                return {"success": True, "image_url": url}, 200
            
            _artist_image_cache[key] = ("", time.monotonic())
            if len(_artist_image_cache) > _ARTIST_IMAGE_CACHE_MAX_ENTRIES:
                _artist_image_cache.pop(next(iter(_artist_image_cache)), None)
                
        return {"success": False, "error": "No image", "image_url": ""}, 200
    except Exception as exc:
        logger.error("Get artist image failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc), "image_url": ""}, 200


def search_images(artist: str, source: str) -> tuple[dict[str, Any], int]:
    """Search for artist images from external sources."""
    return {"success": False, "error": "Not yet implemented", "images": []}, 501


def set_image(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Set custom artist image URL."""
    artist = (payload.get("artist") or "").strip()
    url = (payload.get("image_url") or "").strip()
    if not artist or not url:
        return {"success": False, "error": "artist and image_url required"}, 400
    try:
        with db_session() as session:
            session.execute(
                text(
                    "INSERT INTO artists (name, image_url) VALUES (:artist, :url) "
                    "ON CONFLICT (name) DO UPDATE SET image_url = EXCLUDED.image_url"
                ),
                {"artist": artist, "url": url},
            )
        return {"success": True}, 200
    except Exception as exc:
        logger.error("Set image failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def update_ids(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Update artist ID columns on all of an artist's tracks.

    NOTE: ``lastfm_artist_mbid`` / ``spotify_artist_id`` are legacy columns
    that do NOT exist in the current ``tracks`` schema (only
    ``musicbrainz_artistid``, ``musicbrainz_albumartistid`` and
    ``discogs_artist_id`` do).  Writing them previously raised
    "column does not exist" — the MBID from the Edit-Artist-IDs form lands
    in ``musicbrainz_artistid`` instead.
    """
    artist = str(payload.get("artist") or "").strip()
    if not artist:
        return {"success": False, "error": "Missing artist name"}, 400

    updates: list[str] = []
    bind_values: dict[str, Any] = {}
    for idx, (column, key) in enumerate(
        (
            ("musicbrainz_artistid", "musicbrainz_artist_id"),
            ("discogs_artist_id", "discogs_artist_id"),
        )
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            updates.append(f"{column} = :v{idx}")
            bind_values[f"v{idx}"] = value

    if not updates:
        return {"success": False, "error": "No IDs provided"}, 400

    bind_values["artist"] = artist
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    f"UPDATE tracks SET {', '.join(updates)} "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"
                ),
                bind_values,
            )
            rows_updated = result.rowcount or 0
        return {
            "success": True,
            "message": f"Artist IDs updated for {artist}",
            "rows_updated": rows_updated,
            "updated": {
                "musicbrainz_artist_id": str(payload.get("musicbrainz_artist_id") or "").strip() or None,
                "discogs_artist_id": str(payload.get("discogs_artist_id") or "").strip() or None,
            },
        }, 200
    except Exception as exc:
        logger.error("Update IDs failed", artist=artist, error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}, 500


def lookup_ids(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Look up MusicBrainz/Discogs artist IDs and persist them for the artist."""
    artist = str(payload.get("artist") or "").strip()
    if not artist:
        return {"success": False, "error": "Missing artist name"}, 400

    import difflib

    musicbrainz_id = ""
    discogs_id = ""

    # ✅ Use shared MusicBrainz client singleton
    try:
        mb = get_shared_mb_client()
        results = mb.search_artists(f'artist:"{escape_lucene_special_chars(artist)}"', limit=5)
        if results:
            best = max(
                results,
                key=lambda item: difflib.SequenceMatcher(
                    None, artist.lower(), str(item.get("name") or "").lower()
                ).ratio(),
            )
            musicbrainz_id = str(best.get("id") or "").strip()
    except Exception as exc:
        logger.debug("MusicBrainz artist lookup failed", artist=artist, error=str(exc))

    try:
        from helpers.config_helpers import get_config
        from api_clients.discogs_http import DiscogsHttpClient
        cfg = get_config() or {}
        token = str((cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token") or "").strip()
        if token:
            dc = DiscogsHttpClient(token=token)
            results = dc.search_database({"q": artist, "type": "artist", "per_page": 5})
            if results:
                best = max(
                    results,
                    key=lambda item: difflib.SequenceMatcher(
                        None, artist.lower(), str(item.get("title") or "").lower()
                    ).ratio(),
                )
                discogs_id = str(best.get("id") or "").strip()
    except Exception as exc:
        logger.debug("Discogs artist lookup failed", artist=artist, error=str(exc))

    if not musicbrainz_id and not discogs_id:
        return {"success": False, "error": "No IDs found from external lookup"}, 404

    updates: list[str] = []
    bind_values: dict[str, Any] = {}
    _bind_idx = 0
    if musicbrainz_id:
        # ``lastfm_artist_mbid`` is a legacy column that does NOT exist in
        # the current schema — the MBID is stored in ``musicbrainz_artistid``.
        updates.append(f"musicbrainz_artistid = :v{_bind_idx}")
        bind_values[f"v{_bind_idx}"] = musicbrainz_id
        _bind_idx += 1
    if discogs_id:
        updates.append(f"discogs_artist_id = :v{_bind_idx}")
        bind_values[f"v{_bind_idx}"] = discogs_id
    bind_values["artist"] = artist

    try:
        with db_session() as session:
            result = session.execute(
                text(
                    f"UPDATE tracks SET {', '.join(updates)} "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"
                ),
                bind_values,
            )
            rows_updated = result.rowcount or 0
        return {
            "success": True,
            "artist": artist,
            "musicbrainz_artist_id": musicbrainz_id or None,
            "discogs_artist_id": discogs_id or None,
            "rows_updated": rows_updated,
        }, 200
    except Exception as exc:
        logger.error("Lookup IDs failed", artist=artist, error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}, 500


def _catalogue_artist_names(conn: Any, names: list[str]) -> set[str]:
    unique = sorted({n.strip() for n in names if n and n.strip()})
    if not unique:
        return set()
    try:
        with db_session() as session:
            placeholders = ", ".join(f":n{i}" for i in range(len(unique)))
            rows = session.execute(
                text(
                    "SELECT DISTINCT LOWER(COALESCE(NULLIF(album_artist, ''), artist)) "
                    f"FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN ({placeholders})"
                ),
                {f"n{i}": n for i, n in enumerate(unique)},
            ).fetchall() or []
        found = set()
        for r in rows:
            value = r[0]
            if value:
                found.add(str(value).lower())
        return found
    except Exception as exc:
        logger.debug("Failed to resolve catalogue artists", error=str(exc))
        return set()


def _norm_artist_key(name: str) -> str:
    """Punctuation/case-tolerant artist key for in-collection matching.

    Last.fm / ListenBrainz similar-artist names frequently differ from the
    stored library name by punctuation, "The" prefixes, or whitespace
    ("Beatles, The" vs "The Beatles" vs "beatles").  Comparing normalised
    keys makes the similar-artists section correctly identify artists that
    are ALREADY in the collection instead of re-suggesting them.
    """
    value = str(name or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\bthe\b", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _annotate_similar_artist(entries: list[Any], in_collection: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    # Pre-compute normalised keys of owned artists once.
    owned_keys = {_norm_artist_key(n) for n in in_collection}
    for entry in entries or []:
        if isinstance(entry, str):
            name = entry.strip()
            annotated: dict[str, Any] = {"name": name, "match": 0.0}
        elif isinstance(entry, dict):
            annotated = dict(entry)
            name = str(annotated.get("name") or "").strip()
        else:
            continue
        if not name:
            continue
        annotated["name"] = name
        annotated.setdefault("match", 0.0)
        key = _norm_artist_key(name)
        annotated["in_collection"] = bool(key and key in owned_keys)
        result.append(annotated)
    return result


def get_similar_artists(artist: str, args: Any) -> tuple[dict[str, Any], int]:
    """Get similar artists from DB cache."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    sources: dict[str, list[Any]] = {"lastfm": [], "listenbrainz": []}
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated "
                    "FROM artists WHERE name = :artist"
                ),
                {"artist": artist},
            ).mappings().first()
        if row:
            import json
            lf_raw = str(row.get("similar_artists_lastfm") or "") if row else ""
            lb_raw = str(row.get("similar_artists_listenbrainz") or "") if row else ""
            if lf_raw:
                try:
                    sources["lastfm"] = json.loads(lf_raw) if isinstance(json.loads(lf_raw), list) else []
                except Exception:
                    pass
            if lb_raw:
                try:
                    sources["listenbrainz"] = json.loads(lb_raw) if isinstance(json.loads(lb_raw), list) else []
                except Exception:
                    pass
        names = []
        for entries in sources.values():
            for entry in entries:
                if isinstance(entry, str):
                    names.append(entry)
                elif isinstance(entry, dict):
                    names.append(str(entry.get("name") or ""))
        in_collection = _catalogue_artist_names(None, names)
        for source in sources:
            sources[source] = _annotate_similar_artist(sources[source], in_collection)
        return {"success": True, "similar_artists": sources}, 200
    except Exception as exc:
        logger.error("Get similar artists failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def get_compilations(artist: str) -> tuple[dict[str, Any], int]:
    """Get compilation appearances for an artist."""
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT album, album_artist, COUNT(*) as count FROM tracks "
                    "WHERE LOWER(artist) = LOWER(:artist) AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) != LOWER(:artist) "
                    "GROUP BY album, album_artist ORDER BY album"
                ),
                {"artist": artist},
            ).mappings().all()
        compilations = [
            {
                "album": r.get("album"),
                "album_artist": r.get("album_artist"),
                "track_count": int(r.get("count", 0) or 0),
            }
            for r in rows
        ]
        return {"success": True, "compilations": compilations}, 200
    except Exception as exc:
        logger.error("Get compilations failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def get_main_tracks(artist: str) -> tuple[dict[str, Any], int]:
    """Get main tracks for an artist."""
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, title, album, stars FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist ORDER BY stars DESC NULLS LAST LIMIT 50"
                ),
                {"artist": artist},
            ).mappings().all()
        tracks = [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "album": r.get("album"),
                "stars": float(r.get("stars") or 0),
            }
            for r in rows
        ]
        return {"success": True, "tracks": tracks}, 200
    except Exception as exc:
        logger.error("Get main tracks failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def get_artist_members_cached(artist: str) -> list[dict[str, Any]]:
    """An artist's line-up, from the DATABASE ONLY.

    Populated by the SCAN — ``album_stage._fetch_artist_metadata`` reads
    MusicBrainz once per artist per scan, honouring the same freshness window
    this function used to apply. It is deliberately NOT resolved here any more:
    resolving it made the artist page call MusicBrainz on EVERY load
    (``search_artists`` + ``get_artist_members``), and because the shared
    client throttles at ~1 req/s by *reserving* a future slot, a page load
    during a scan queued behind it for tens of seconds and stalled the whole
    hypercorn worker. A page must be a read-only view of what the scan has
    already resolved.

    Returns ``[]`` when the scan has not run for this artist yet — the page
    simply renders no line-up until it has.
    """
    import json

    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT members FROM artists WHERE name = :artist"),
                {"artist": artist},
            ).mappings().first()

        raw = str((row or {}).get("members") or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception as exc:
        logger.debug("Get artist members failed", artist=artist, error=str(exc))
        return []


def get_stats(artist: str) -> tuple[dict[str, Any], int]:
    """Get statistics for an artist."""
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT COUNT(*) as tracks, COUNT(DISTINCT album) as albums, "
                    "AVG(stars) as avg_stars, SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star "
                    "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"
                ),
                {"artist": artist},
            ).mappings().first()
        if not row:
            return {"success": False, "error": "Artist not found"}, 404
        stats = {
            "track_count": int(row.get("tracks") or 0),
            "album_count": int(row.get("albums") or 0),
            "avg_stars": float(row.get("avg_stars") or 0),
            "five_star_count": int(row.get("five_star") or 0),
        }
        return {"success": True, **stats}, 200
    except Exception as exc:
        logger.error("Get stats failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def apply_genres(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Apply genres to all tracks by an artist.

    ⚠️ Reports what actually happened. The tag write was wrapped in a bare
    ``except Exception: pass`` and the response said ``success: True, updated: N``
    unconditionally — so a run where EVERY file tag write failed (an unresolved
    path, a read-only mount, the tagging master toggle off) still told the user
    the genres were applied. The count was the number of DB rows it looped over,
    not the number it wrote.
    """
    from services.metadata.tag_file_service import (
        resolve_music_file_path,
        write_tags_to_file,
    )
    artist = (payload.get("artist") or "").strip()
    genres = payload.get("genres", [])
    if not artist or not genres:
        return {"success": False, "error": "artist and genres required"}, 400
    try:
        files_written = 0
        files_failed = 0
        with db_session() as session:
            rows = session.execute(
                text("SELECT id, file_path FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"),
                {"artist": artist},
            ).mappings().all()
            genre_str = " \\ ".join(genres) if isinstance(genres, list) else str(genres)
            updated = 0
            for r in rows:
                track_id = r.get("id")
                fp = r.get("file_path")
                session.execute(
                    text("UPDATE tracks SET genres = :genre_str WHERE id = :track_id"),
                    {"genre_str": genre_str, "track_id": track_id},
                )
                updated += 1
                if not (fp and str(fp).strip()):
                    files_failed += 1
                    continue
                # The DB may hold a path relative to the music root; the writer
                # needs an absolute, existing path or it silently returns False.
                resolved = resolve_music_file_path(str(fp))
                if not resolved:
                    files_failed += 1
                    logger.warning(
                        "Apply genres: audio file not resolvable",
                        artist=artist, track_id=track_id, file_path=str(fp),
                    )
                    continue
                try:
                    if write_tags_to_file(resolved, {"genre": genres}):
                        files_written += 1
                    else:
                        files_failed += 1
                except Exception as tag_exc:
                    files_failed += 1
                    logger.warning(
                        "Apply genres: file tag write failed",
                        artist=artist, track_id=track_id, error=str(tag_exc),
                    )
        return {
            "success": updated > 0,
            "updated": updated,
            "files_written": files_written,
            "files_failed": files_failed,
        }, 200 if updated else 500
    except Exception as exc:
        logger.error("Apply genres failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def genre_recommendations(artist: str) -> tuple[dict[str, Any], int]:
    """Get genre recommendations for an artist from MusicBrainz."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    try:
        from api_clients.musicbrainz_http import escape_lucene_special_chars
        # ✅ Use shared MusicBrainz client singleton
        mb = get_shared_mb_client()
        artists = mb.search_artists(
            f'artist:"{escape_lucene_special_chars(artist)}"',
            limit=1,
        )
        if not artists:
            return {"success": True, "genres": []}, 200
        tags = artists[0].get("tags", [])
        genres = [{"name": t.get("name"), "count": t.get("count")} for t in tags]
        return {"success": True, "genres": genres}, 200
    except Exception as exc:
        logger.error("Genre recommendations failed", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def genre_management(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Handle genre management save operations."""
    return {"success": False, "error": "Not yet implemented"}, 501
