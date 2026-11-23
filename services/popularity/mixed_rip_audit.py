"""Find albums that are really several rips wearing one MusicBrainz release id.

⭐ WHY THIS EXISTS

Popularr and Navidrome can disagree about where one album ends. When a folder is
a mix of two rips, the files share a ``musicbrainz_albumid`` but disagree about
tags Navidrome may use for identity instead — most often ``releasedate``:

    15. Here to Stay (T Ray's mix)   releasedate: 2002-06-19
    01. Here to Stay                 releasedate: (missing)

If a Navidrome install uses a non-default album PID rule, or has not full-scanned
since the retag, those become two albums there while Popularr (which prefers the
MBID) treats them as one. The audible result is a split album that the library
"does not have".

This audit lists the releases where more than one distinct value of a
tag is present WITHIN one MBID, so the folders likely to split are known before
someone opens Navidrome's Get Info to find out why.

The tags checked are the identity-relevant ones from the reported case:
``releasedate`` (Navidrome's PID includes it), ``originaldate``, ``media``, plus
``album`` itself — one release whose files disagree about its own title is
already suspect even though the MBID now holds it together.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.popularity.album_identity import album_mbid

logger = structlog.get_logger(__name__)

#: Columns compared within one MBID. ``releasedate`` leads because a Navidrome
#: album PID can include it, which is the reported split.
_MIXED_RIP_COLUMNS: tuple[str, ...] = (
    "releasedate",
    "originaldate",
    "media",
    "album",
)

_MIXED_RIP_SELECT = """
    SELECT musicbrainz_albumid,
           artist,
           album_artist,
           releasedate,
           originaldate,
           media,
           album,
           COUNT(*) AS tracks
    FROM tracks
    WHERE COALESCE(musicbrainz_albumid, '') <> ''
      AND COALESCE(NULLIF(popularity, 0), NULLIF(final_score, 0)) IS NOT NULL
    GROUP BY musicbrainz_albumid, artist, album_artist,
             releasedate, originaldate, media, album
"""


def find_mixed_rip_albums(limit: int = 200) -> list[dict[str, Any]]:
    """Releases whose tracks disagree about identity-relevant tags.

    Returns one entry per MusicBrainz release id that carries more than one
    distinct value for at least one column, most-split first::

        {"musicbrainz_albumid": "...", "artist": "Korn",
         "albums": ["Here to Stay", "Here to Stay (T Ray's mix)"],
         "tracks": 15,
         "conflicts": {"releasedate": ["2002-06-19", ""]}}

    An empty list means every MBID has internally consistent tags.
    """
    with db_session() as session:
        rows = (
            session.execute(text(_MIXED_RIP_SELECT)).mappings().all()
        )

    # Group the (already tag-distinct) rows back under their release id.
    by_mbid: dict[str, dict[str, Any]] = {}
    for row in rows:
        mbid = album_mbid(row)
        if not mbid:
            continue
        entry = by_mbid.setdefault(
            mbid,
            {
                "musicbrainz_albumid": mbid,
                "artist": str(row.get("album_artist") or row.get("artist") or "").strip(),
                "albums": [],
                "tracks": 0,
                "conflicts": {},
                "_values": {name: set() for name in _MIXED_RIP_COLUMNS},
            },
        )
        entry["tracks"] += int(row.get("tracks") or 0)
        album_title = str(row.get("album") or "").strip()
        if album_title and album_title not in entry["albums"]:
            entry["albums"].append(album_title)
        for name in _MIXED_RIP_COLUMNS:
            entry["_values"][name].add(str(row.get(name) or "").strip())

    mixed: list[dict[str, Any]] = []
    for entry in by_mbid.values():
        conflicts = {
            name: sorted(values)
            for name, values in entry.pop("_values").items()
            # more than one DISTINCT value within one release = a mixed rip
            if len(values) > 1
        }
        if conflicts:
            entry["conflicts"] = conflicts
            mixed.append(entry)

    # Most conflicts first, then biggest; a stable order keeps diffs readable.
    mixed.sort(key=lambda e: (-len(e["conflicts"]), -e["tracks"], e["artist"], e["musicbrainz_albumid"]))
    if limit:
        mixed = mixed[: int(limit)]
    logger.info("Mixed-rip audit complete", releases=len(by_mbid), mixed=len(mixed))
    return mixed
