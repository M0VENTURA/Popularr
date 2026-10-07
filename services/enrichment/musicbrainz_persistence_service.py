"""MusicBrainz persistence helpers.

These functions intentionally sit outside ``api_clients`` because they mutate
the local database. They bridge MusicBrainz enrichment and repository writes.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import bindparam, text

from api_clients.musicbrainz_http import MUSICBRAINZ_UUID_RE, escape_lucene_special_chars
from services.enrichment.musicbrainz_service import get_shared_mb_client
from helpers.normalization_service import strip_featured_artist
from db.engine import db_session

logger = structlog.get_logger(__name__)


def _build_artist_credit_string(artist_credit: list[Any]) -> str:
    parts = []
    for entry in artist_credit:
        if isinstance(entry, dict):
            name = entry.get("name", "")
            parts.append(name + entry.get("joinphrase", ""))
    return "".join(parts).strip()


def _has_valid_artist_mbid(value: Any) -> bool:
    """True when *value* is a non-empty MusicBrainz UUID."""
    raw = str(value or "").strip()
    return bool(raw) and bool(MUSICBRAINZ_UUID_RE.match(raw))


def lookup_and_save_artist_mbid(artist: str, db_connection: Any = None) -> str:
    """Lookup an artist MBID and update matching track rows missing artist MBIDs.

    ``db_connection`` is kept for backward compatibility — the writes run on
    their own SQLAlchemy session via QueuePool.
    """
    if not artist:
        return ""

    lookup_artist = strip_featured_artist(artist)
    
    # ✅ USE THE SHARED CLIENT: Ensures we respect global rate limits and reuse connections
    mb_client = get_shared_mb_client()

    try:
        candidates = mb_client.search_artists(f'artist:"{escape_lucene_special_chars(lookup_artist)}"', limit=10)
        if not candidates:
            return ""

        best_candidate, best_score = None, -1
        for candidate in candidates:
            score = 0
            name = candidate.get("name", "")
            if name.lower() == lookup_artist.lower():
                score += 100
            elif lookup_artist.lower() in name.lower():
                score += 50
            else:
                continue

            artist_type = (candidate.get("type") or "").lower()
            if artist_type == "group":
                score += 25
            elif artist_type != "person":
                score += 10

            if candidate.get("disambiguation"):
                score -= 10
            if candidate.get("life-span", {}) and not candidate.get("life-span", {}).get("ended"):
                score += 5

            if score > best_score:
                best_score = score
                best_candidate = candidate

        if not best_candidate or best_score < 0:
            return ""

        mbid = best_candidate.get("id", "")
        if not mbid:
            return ""

        with db_session() as session:
            to_fix = [
                row.get("id")
                for row in session.execute(
                    # Case-insensitive: the scan context's artist name and the
                    # stored value differ only in casing ("Afi" vs "AFI"), and
                    # an exact match returned NOTHING — so the artist MBID the
                    # lookup had just found was never written to any row.
                    text("SELECT id, musicbrainz_artistid FROM tracks WHERE LOWER(artist) = LOWER(:artist)"),
                    {"artist": artist},
                ).mappings().all()
                if not _has_valid_artist_mbid(row.get("musicbrainz_artistid"))
            ]

            feat_re = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
            for row in session.execute(
                text("SELECT id, artist, musicbrainz_artistid FROM tracks WHERE LOWER(artist) LIKE LOWER(:pattern)"),
                {"pattern": f"{artist} %"},
            ).mappings().all():
                if feat_re.search(str(row.get("artist") or "")):
                    if not _has_valid_artist_mbid(row.get("musicbrainz_artistid")):
                        to_fix.append(row.get("id"))

            to_fix = list(dict.fromkeys(to_fix))
            if to_fix:
                for index in range(0, len(to_fix), 500):
                    # Expanding bind: a raw tuple only expands under psycopg2 —
                    # on SQLite it rendered ``IN ?`` and the whole save raised
                    # a syntax error. The expansion is identical on Postgres.
                    chunk = list(to_fix[index:index + 500])

                    session.execute(
                        text("""
                            UPDATE tracks SET
                                musicbrainz_artistid = CASE
                                    WHEN musicbrainz_artistid IS NULL OR musicbrainz_artistid = '' THEN :mbid
                                    ELSE musicbrainz_artistid 
                                END
                            WHERE id IN :ids
                        """).bindparams(bindparam("ids", expanding=True)),
                        {"mbid": mbid, "ids": chunk},
                    )
                    
        return mbid
        
    except Exception as exc:
        logger.debug("Artist MBID lookup and save failed", artist=artist, error=str(exc))
        return ""
