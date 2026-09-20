"""Artist release cache service.

Prefetches an artist's releases (albums / EPs / singles) from MusicBrainz and
Discogs into ``artist_release_cache`` so singles detection can match local
tracks against known single releases WITHOUT per-track API searches.

- MusicBrainz: two release-group searches (singles+EPs, then albums).
- Discogs: one artist-releases call (first 100 releases, role=Main), with
  format-based classification (Single / EP / Album).
- Persisted with a 7-day freshness TTL; re-prefetch skips fresh artists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars
from db.engine import db_session
# Imported as a MODULE (not ``from … import fetch_missing_release_tracklist``) so
# the name is resolved at call time: a test can patch the module attribute, and
# a future move of the function does not need this binding updated.
from services.metadata import artist_scan_service

logger = structlog.get_logger(__name__)

CACHE_FRESH_HOURS = 24 * 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_musicbrainz_category(release_group: dict[str, Any]) -> str:
    """Derive the artist-page display category from a MusicBrainz release-group.

    Delegates to ``services.catalog.release_categories`` so this path and the
    in-library classifier cannot disagree — they previously did, which put the
    same release in different sections depending on whether it was owned.

    Returns a canonical category KEY.  Secondary types beyond live/compilation/
    remix (soundtrack, field recording, DJ-mix, mixtape, demo, spokenword,
    interview, audiobook, audio drama, score) now get their own section instead
    of being flattened into Studio Albums.
    """
    from services.catalog.release_categories import category_for_musicbrainz

    primary = release_group.get("primary-type") or release_group.get("primary_type") or ""
    # Search results can carry ``secondary-types`` as a comma-joined STRING
    # ("Live,Compilation"); the registry normalises that (iterating it
    # character-by-character never matched, which is what flattened every
    # live/compilation release into Studio).
    secondary = (
        release_group.get("secondary-types")
        or release_group.get("secondary_types")
        or []
    )
    return category_for_musicbrainz(str(primary), secondary)


def _derive_discogs_category(fmt_tokens: str) -> str:
    """Derive the artist-page display category from Discogs format tokens.

    Discogs format strings carry the type as explicit tokens ("CD, Album, Live",
    "2xLP, Compilation", "CD, Album, Remix").  Mapped onto the canonical
    registry so a Discogs release lands in the same section its MusicBrainz
    equivalent would.
    """
    from services.catalog.release_categories import category_for_musicbrainz

    tokens = set((fmt_tokens or "").lower().replace(",", " ").split())
    # Discogs spells it "DJ Mix" and "Mixtape" where MusicBrainz uses "DJ-mix"
    # and "Mixtape/Street"; map onto MusicBrainz's vocabulary before delegating.
    secondary: list[str] = []
    if "compilation" in tokens:
        secondary.append("compilation")
    if "soundtrack" in tokens:
        secondary.append("soundtrack")
    if "live" in tokens:
        secondary.append("live")
    if "remix" in tokens:
        secondary.append("remix")
    if "mixtape" in tokens or "street" in tokens:
        secondary.append("mixtape/street")
    if "dj" in tokens or "dj-mix" in tokens or "djmix" in tokens:
        secondary.append("dj-mix")
    if "spokenword" in tokens or "spoken word" in tokens:
        secondary.append("spokenword")

    if "ep" in tokens:
        return "ep"
    if "single" in tokens:
        return "single"
    return category_for_musicbrainz("album", secondary)


def _fallback_release_category(title: str) -> str:
    """Title-based category for cache rows persisted without one.

    Rows written before the ``category`` column existed (or from sources that
    did not expose a secondary type) only carry ``release_type`` — a live
    album / compilation / remix stored as plain ``album`` would otherwise be
    flattened into the Studio Albums bucket on the artist page.

    Returns a canonical category KEY; the registry owns the labels.  An
    unrecognisable title stays in Studio, which is the correct default when
    there is genuinely no signal to go on.
    """
    from services.catalog.release_categories import STUDIO_KEY

    try:
        from services.catalog.album_classification_service import (
            detect_greatest_hits_album,
            is_live_album_enhanced,
        )
    except Exception:
        detect_greatest_hits_album = None
        is_live_album_enhanced = None

    text = (title or "").lower()
    if (is_live_album_enhanced and is_live_album_enhanced(title)) or (
        "unplugged" in text or "in concert" in text
    ):
        return "live_album"
    if "compilation" in text or (
        detect_greatest_hits_album and detect_greatest_hits_album(title, "")
    ):
        return "compilation"
    if "soundtrack" in text or "ost" in text:
        return "soundtrack"
    if "remix" in text:
        return "remix_album"
    return STUDIO_KEY


def _cache_has_source(artist: str, source: str) -> bool:
    """True when the artist's release cache holds rows from ``source``."""
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "SELECT 1 FROM artist_release_cache "
                    "WHERE LOWER(artist) = LOWER(:artist) AND source = :source LIMIT 1"
                ),
                {"artist": artist, "source": source},
            )
            return result.fetchone() is not None
    except Exception:
        return False


def _artist_cache_fresh(artist: str) -> bool:
    """True when the artist was cached within the TTL."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT MAX(updated_at) AS latest FROM artist_release_cache WHERE LOWER(artist) = LOWER(:artist)"),
                {"artist": artist},
            )
            row = result.fetchone()
        if not row or not row[0]:
            return False
        latest = row[0]
        if isinstance(latest, datetime):
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            else:
                latest = latest.astimezone(timezone.utc)
            return (datetime.now(timezone.utc) - latest).total_seconds() < CACHE_FRESH_HOURS * 3600
        return False
    except Exception:
        return False


def get_cached_artist_release_rows(artist: str, source: str = "discogs") -> list[dict[str, Any]] | None:
    """Fresh ``artist_release_cache`` rows for the artist, or None.

    None means the cache is absent or stale (the caller should fetch the
    API). An empty list means fresh rows exist but none carry ``source``.
    Used by ``DiscogsService._get_artist_releases`` so single detection
    reuses the runner's prefetched artist page instead of re-fetching all
    Discogs pages per process.
    """
    if not _artist_cache_fresh(artist):
        return None
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, title, release_type, source, release_id, year, is_promo, category
                    FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist) AND source = :source
                """),
                {"artist": artist, "source": source},
            )
            return [dict(r) for r in result.mappings().all()]
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Row read failed",
            artist=artist,
            error=str(exc),
        )
        return None


def upsert_artist_release_rows(artist: str, releases: list[dict[str, Any]]) -> None:
    """Persist Discogs release dicts (artist-releases shape) to the cache.

    Converts the in-memory release shape (``format`` list, ``id``) to the
    cache row shape (``release_type``, ``release_id``, ``is_promo``) using
    the same format-token rules as ``_fetch_discogs_releases`` — so rows
    written here and rows written by the prefetch classify identically, and
    ``DiscogsService`` can safely consume either on the next scan.
    """
    try:
        from services.enrichment.discogs_service import (
            ALBUM_FORMAT_TOKENS,
            SINGLE_FORMAT_TOKENS,
            release_format_key,
        )
    except Exception:
        return
    rows: list[dict[str, Any]] = []
    for rel in releases or []:
        if str(rel.get("role") or "Main").strip().lower() != "main":
            continue
        title = str(rel.get("title") or "").strip()
        if not title:
            continue
        fmt_tokens = release_format_key(rel.get("format")).split()
        if ALBUM_FORMAT_TOKENS.intersection(fmt_tokens):
            rtype = "album"
        elif SINGLE_FORMAT_TOKENS.intersection(fmt_tokens):
            rtype = "single"
        else:
            rtype = "album"
        year = rel.get("year")
        if not isinstance(year, int) or year <= 0:
            year = None
        rows.append({
            "artist": artist,
            "title": title,
            "rtype": rtype,
            "category": _derive_discogs_category(release_format_key(rel.get("format"))),
            "source": "discogs",
            "release_id": str(rel.get("id") or "").strip() or None,
            "year": year,
            "is_promo": "promo" in fmt_tokens,
        })
    if rows:
        _upsert_releases(artist, rows)


def _upsert_releases(artist: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        with db_session() as session:
            for row in rows:
                params = {
                    "artist": artist,
                    "title": row.get("title"),
                    "rtype": row.get("rtype") or row.get("release_type", "album"),
                    "category": row.get("category", "Album"),
                    "source": row.get("source", "musicbrainz"),
                    "release_id": row.get("release_id"),
                    "year": row.get("year"),
                    "is_promo": bool(row.get("is_promo", False)),
                }
                session.execute(
                    text("""
                        INSERT INTO artist_release_cache
                            (artist, title, release_type, category, source, release_id, year, is_promo, updated_at)
                        VALUES (:artist, :title, :rtype, :category, :source, :release_id, :year, :is_promo, CURRENT_TIMESTAMP)
                        ON CONFLICT (artist, title, source) DO UPDATE SET
                            release_type = EXCLUDED.release_type,
                            category = EXCLUDED.category,
                            release_id = EXCLUDED.release_id,
                            year = EXCLUDED.year,
                            is_promo = EXCLUDED.is_promo,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    params,
                )
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Upsert failed",
            artist=artist,
            error=str(exc),
        )


def _fetch_musicbrainz_releases(artist: str) -> list[dict[str, Any]]:
    """Release-groups for singles/EPs and albums (two throttled calls)."""
    try:
        client = MusicBrainzHttpClient(enabled=True)
        escaped = escape_lucene_special_chars(artist)
        out: list[dict[str, Any]] = []
        queries = {
            "single": f'artist:"{escaped}" AND (primarytype:single OR primarytype:ep)',
            "album": f'artist:"{escaped}" AND primarytype:album',
        }
        for query in queries.values():
            for rg in client.search_release_groups(query, limit=25) or []:
                title = str(rg.get("title") or "").strip()
                primary = str(rg.get("primary-type") or "").lower()
                if not title or primary not in ("album", "ep", "single"):
                    continue
                year = None
                fdate = str(rg.get("first-release-date") or "")
                if len(fdate) >= 4 and fdate[:4].isdigit():
                    year = int(fdate[:4])
                out.append({
                    "title": title,
                    "release_type": primary,
                    "category": _derive_musicbrainz_category(rg),
                    "source": "musicbrainz",
                    "release_id": str(rg.get("id") or "").strip() or None,
                    "year": year,
                })
        return out
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] MB fetch failed",
            artist=artist,
            error=str(exc),
        )
        return []


def _fetch_discogs_releases(artist: str, discogs_artist_id: str) -> list[dict[str, Any]]:
    """One Discogs artist-releases call; classify by format (role=Main only)."""
    try:
        from api_clients.discogs_http import DiscogsHttpClient
        from helpers.config_helpers import get_config
        from services.enrichment.discogs_service import (
            ALBUM_FORMAT_TOKENS,
            SINGLE_FORMAT_TOKENS,
            release_format_key,
        )
        token = ""
        try:
            api_cfg: dict[str, Any] = get_config().get("api_integrations") or {}
            discogs_cfg: dict[str, Any] = api_cfg.get("discogs") or {}
            token = str(discogs_cfg.get("token") or "")
        except Exception:
            token = ""
        if not token or token.lower() in ("your_discogs_token", "your_token", "placeholder"):
            return []
        client = DiscogsHttpClient(token=token)
        # All pages — a single page of 100 can miss older singles of
        # catalogue-heavy artists (Discogs caps pages at 100 releases).
        releases = client.get_artist_releases_all(discogs_artist_id, max_pages=10) or []
        # Same master-format resolution the in-memory single-detection path
        # uses — format-less master rows would otherwise classify as albums.
        from services.enrichment.discogs_service import resolve_master_formats
        resolve_master_formats(releases, client)
        out: list[dict[str, Any]] = []
        for rel in releases:
            if str(rel.get("role") or "Main").strip().lower() != "main":
                continue
            title = str(rel.get("title") or "").strip()
            if not title:
                continue
            fmt_tokens = release_format_key(rel.get("format")).split()
            if ALBUM_FORMAT_TOKENS.intersection(fmt_tokens):
                rtype = "album"
            elif SINGLE_FORMAT_TOKENS.intersection(fmt_tokens):
                rtype = "single"
            else:
                rtype = "album"
            year = None
            raw_year = rel.get("year")
            if isinstance(raw_year, int) and raw_year > 0:
                year = raw_year
            out.append({
                "title": title,
                "release_type": rtype,
                "category": _derive_discogs_category(release_format_key(rel.get("format"))),
                "source": "discogs",
                "release_id": str(rel.get("id") or "").strip() or None,
                "year": year,
                "is_promo": "promo" in fmt_tokens,
            })
        return out
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Discogs fetch failed",
            artist=artist,
            error=str(exc),
        )
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prefetch_artist_releases(artist: str, discogs_artist_id: str = "") -> dict[str, Any]:
    """Pull an artist's releases into ``artist_release_cache``.

    Skips artists refreshed within the TTL. Returns per-source counts.
    """
    if not artist:
        return {"musicbrainz": 0, "discogs": 0}
    if _artist_cache_fresh(artist) and (_cache_has_source(artist, "discogs") or not discogs_artist_id):
        return {"musicbrainz": 0, "discogs": 0, "skipped": "fresh"}

    mb_rows = _fetch_musicbrainz_releases(artist)
    _upsert_releases(artist, mb_rows)

    discogs_rows: list[dict[str, Any]] = []
    if discogs_artist_id:
        discogs_rows = _fetch_discogs_releases(artist, discogs_artist_id)
        _upsert_releases(artist, discogs_rows)

    if mb_rows or discogs_rows:
        logger.info(
            "[RELEASE_CACHE] Artist releases cached",
            artist=artist,
            mb_count=len(mb_rows),
            discogs_count=len(discogs_rows),
        )
    return {"musicbrainz": len(mb_rows), "discogs": len(discogs_rows)}


def get_artist_single_titles(artist: str, source: str | None = None) -> set[str]:
    """Single/EP titles known for the artist, lowercased.

    ``source``: ``"musicbrainz"``, ``"discogs"``, or None for both.
    """
    try:
        source_clause = ""
        params: dict[str, Any] = {"artist": artist}
        if source:
            source_clause = " AND source = :source"
            params["source"] = source
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT DISTINCT title FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND release_type IN ('single', 'ep')
                      {source_clause}
                """),
                params,
            )
            return {str(r[0]).strip().lower() for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


def get_artist_promo_titles(artist: str, source: str = "discogs") -> set[str]:
    """Promotional single/EP titles known for the artist, lowercased."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT title FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND source = :source
                      AND release_type IN ('single', 'ep')
                      AND COALESCE(is_promo, FALSE) = TRUE
                """),
                {"artist": artist, "source": source},
            )
            return {str(r[0]).strip().lower() for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Missing-releases gap detection + tracklists (cache-driven)
# ---------------------------------------------------------------------------

def _norm_release_title(title: str) -> str:
    """Normalize a release title for library-vs-cache comparison."""
    import re as _re
    return _re.sub(r"\s+", " ", (title or "").strip().lower())


def _library_albums(artist: str) -> set[str]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT album FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album IS NOT NULL AND TRIM(album) <> ''
                """),
                {"artist": artist},
            )
            return {_norm_release_title(r[0]) for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


def _library_track_titles(artist: str) -> set[str]:
    """Normalised titles of every track by the artist already in the library."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT title FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND title IS NOT NULL AND TRIM(title) <> ''
                """),
                {"artist": artist},
            )
            return {_norm_release_title(r[0]) for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


def refresh_missing_releases_for_artist(artist: str) -> dict[str, Any]:
    """Compare the cached artist releases against the library and persist gaps."""
    if not artist:
        return {"missing": 0}
    library = _library_albums(artist)
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT title, release_type, category, year, release_id, source
                    FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND source = 'musicbrainz'
                """),
                {"artist": artist},
            )
            cached = [dict(r) for r in result.mappings().all()]
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Missing-releases read failed",
            artist=artist,
            error=str(exc),
        )
        return {"missing": 0}

    seen: set[str] = set()
    existing_categories: dict[str, str] = {}
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT title, category FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND category IS NOT NULL AND TRIM(category) <> ''
                """),
                {"artist": artist},
            )
            for row in result.fetchall() or []:
                t = str(row[0] or "").strip()
                cat = str(row[1] or "").strip()
                if t and cat:
                    existing_categories[_norm_release_title(t)] = cat
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Existing missing-releases categories read failed",
            artist=artist,
            error=str(exc),
        )

    missing_rows: list[dict[str, Any]] = []
    library_tracks = _library_track_titles(artist)
    for row in cached:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        norm = _norm_release_title(title)
        if norm in library or norm in seen:
            continue
        seen.add(norm)
        rtype = str(row.get("release_type") or "album").lower()
        year = row.get("year")
        if rtype == "single":
            if norm in library_tracks:
                continue
            category = "Single"
        elif rtype == "ep":
            category = "EP"
        else:
            category = str(row.get("category") or "").strip()
            if not category:
                category = _fallback_release_category(title)
            if category.lower() == "album":
                existing = existing_categories.get(norm)
                if existing and existing.lower() != "album":
                    category = existing
        missing_rows.append({
            "release_id": str(row.get("release_id") or "").strip() or f"{artist}-{norm}",
            "title": title,
            "primary_type": rtype,
            "first_release_date": str(year) if isinstance(year, int) else None,
            "category": category,
            "source": str(row.get("source") or "musicbrainz"),
        })

    try:
        with db_session() as session:
            existing = session.execute(
                text("""
                    SELECT id, title FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                """),
                {"artist": artist},
            )
            replace_ids: list[int] = []
            for row in existing.fetchall() or []:
                rid, rtitle = row[0], str(row[1] or "")
                if not rtitle:
                    continue
                norm = _norm_release_title(rtitle)
                if norm in seen or norm in library or norm in library_tracks:
                    replace_ids.append(rid)
            for rid in replace_ids:
                session.execute(
                    text("DELETE FROM missing_releases WHERE id = :id"),
                    {"id": rid},
                )
            for item in missing_rows:
                session.execute(
                    text("""
                        INSERT INTO missing_releases
                            (artist, release_id, title, primary_type, first_release_date,
                             category, last_checked)
                        VALUES (:artist, :release_id, :title, :primary_type, :fdate,
                                :category, CURRENT_TIMESTAMP)
                    """),
                    {**item, "artist": artist, "fdate": item.get("first_release_date")},
                )
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Missing-releases persist failed",
            artist=artist,
            error=str(exc),
        )

    return {"missing": len(missing_rows)}


def populate_missing_release_tracklists(artist: str, limit: int = 5) -> dict[str, Any]:
    """Fetch tracklists for missing releases and store them for queue matching."""
    if not artist:
        return {"fetched": 0}

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT release_id, title, primary_type, category
                    FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND (tracklist IS NULL OR tracklist = '[]')
                    ORDER BY last_checked ASC NULLS FIRST
                    LIMIT :limit
                """),
                {"artist": artist, "limit": limit},
            )
            missing = [dict(r) for r in result.mappings().all()]
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Missing-releases query failed",
            artist=artist,
            error=str(exc),
        )
        return {"fetched": 0}

    if not missing:
        return {"fetched": 0}

    fetched = 0
    for row in missing:
        release_id = str(row.get("release_id") or "").strip()
        if not release_id:
            continue
        # Delegated to the artist-scan service, which owns the missing-releases
        # table and knows that ``release_id`` holds a release-GROUP id.
        #
        # This loop used to call ``get_release(release_id, inc="recordings")``
        # directly on a freshly-constructed MusicBrainzHttpClient. Two problems,
        # both silent:
        #   1. a release-GROUP id 404s ``get_release``, so EVERY fetch raised,
        #      was caught, and left the row's tracklist NULL — the cache could
        #      never fill; and
        #   2. the raw client bypassed ``get_shared_mb_client()``, so these
        #      requests carried their own HTTP session (they still shared the
        #      module-level 1 req/s throttle, but nothing else).
        # ``fetch_missing_release_tracklist`` resolves the group via
        # ``_resolve_mb_release``, caches the result, and uses the shared client.
        titles = artist_scan_service.fetch_missing_release_tracklist(release_id, artist)
        if titles:
            fetched += 1

    return {"fetched": fetched}
