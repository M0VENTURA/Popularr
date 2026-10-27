"""Report tracks whose stored Last.fm listener count contradicts itself.

Why this exists
---------------
Last.fm's listener count is a property of ``(artist, title)``, NOT of a release:
the API does not distinguish a live version from a studio one. So two rows in
``tracks`` carrying the same artist and title MUST hold the same listener count.

Where they do not, one of the two was matched to something else. Measured on
this collection: **1,924 groups** conflict, with a median spread of **9.5x**,
a 90th percentile of **188x**, and a maximum of **234,688x** — e.g. Jonathan
Groff's "Into the Unknown" stored as 469,376 listeners on one row and 2 on
another, from the same album and year.

Those values are also durable: ``should_freeze_track`` skips any track that is
2+ years old and already carries a score, so a bad number written by an older
matcher is never re-queried. Measured: 79% of the low side of a conflict pair is
frozen. This report is how you find them and scope a targeted
``force`` refresh instead of forcing the whole collection.

⚠️ Read-only. Nothing here writes to the database.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import text

import structlog

from db.engine import db_session

logger = structlog.get_logger(__name__)

__all__ = [
    "build_listener_contradictions",
    "implicated_albums",
    "contradiction_key",
]

#: A listener count below this is treated as noise rather than evidence. A
#: genuine release with 0-2 Last.fm listeners is possible; comparing it to a
#: 400,000-listener row is still meaningful, so the floor is only used to
#: avoid dividing by zero.
_MIN_DIVISOR = 1

_TRAILING_BRACKET = re.compile(r"\s*\([^)]*\)\s*$")


def contradiction_key(title: str) -> str:
    """Normalise a title for cross-album comparison.

    Trailing parentheticals are stripped because a compilation often carries
    "(Live)" / "(Remastered)" / a country tag on the same underlying song, and
    Last.fm's count for ``(artist, title)`` ignores them. This deliberately
    mirrors how the popularity sources normalise before matching.
    """
    value = str(title or "").strip()
    value = _TRAILING_BRACKET.sub("", value)
    return " ".join(value.casefold().split())


def _listener_row(row: Any) -> dict[str, Any]:
    m = row._mapping if hasattr(row, "_mapping") else row

    def _get(k: str, d: Any = None) -> Any:
        try:
            return m.get(k, d)
        except Exception:
            return d

    return {
        "track_id": str(_get("id") or ""),
        "title": str(_get("title") or ""),
        "artist": str(_get("artist") or ""),
        "album": str(_get("album") or ""),
        "year": str(_get("year") or ""),
        "lastfm_listeners": int(_get("lastfm_listeners") or 0),
        "lastfm_playcount": int(_get("lastfm_playcount") or 0),
        "stars": int(_get("stars") or 0),
        "popularity_frozen": bool(_get("popularity_frozen")),
    }


def build_listener_contradictions(
    *,
    min_ratio: float = 3.0,
    limit_groups: int | None = None,
) -> dict[str, Any]:
    """Find ``(artist, title)`` groups carrying conflicting listener counts.

    Args:
        min_ratio: Only report a group when its highest count is at least this
            many times its lowest. 3.0 is the default because a small spread
            can be legitimate (Last.fm merging two near-identical entries).
        limit_groups: Cap the number of groups returned (the summary counts are
            always computed over the full set).

    Returns:
        ``{"groups": [...], "summary": {...}, "albums": [...]}`` where
        ``albums`` is the deduplicated ``(artist, album)`` list a targeted
        refresh would need to cover.
    """
    try:
        ratio = max(1.0, float(min_ratio))
    except (TypeError, ValueError):
        ratio = 3.0

    with db_session() as session:
        rows = session.execute(
            text(
                "SELECT id, title, artist, album, year, "
                "COALESCE(lastfm_listeners, 0) AS lastfm_listeners, "
                "COALESCE(lastfm_playcount, 0) AS lastfm_playcount, "
                "COALESCE(stars, star_rating, 0) AS stars, "
                "COALESCE(popularity_frozen, FALSE) AS popularity_frozen "
                "FROM tracks "
                "WHERE COALESCE(lastfm_listeners, 0) > 0 "
                "  AND COALESCE(album, '') <> ''"
            )
        ).fetchall() or []

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = _listener_row(row)
        key = (item["artist"].casefold().strip(), contradiction_key(item["title"]))
        if key[0] and key[1]:
            grouped[key].append(item)

    groups: list[dict[str, Any]] = []
    total_rows_in_conflict = 0
    frozen_low = 0
    for (artist_key, title_key), items in grouped.items():
        counts = {i["lastfm_listeners"] for i in items}
        if len(counts) <= 1:
            continue
        low = min(items, key=lambda i: i["lastfm_listeners"])
        high = max(items, key=lambda i: i["lastfm_listeners"])
        spread = high["lastfm_listeners"] / max(_MIN_DIVISOR, low["lastfm_listeners"])
        if spread < ratio:
            continue

        total_rows_in_conflict += len(items)
        if low["popularity_frozen"]:
            frozen_low += 1

        groups.append({
            "artist": high["artist"] or low["artist"],
            "title": high["title"] or low["title"],
            "title_key": title_key,
            "spread": round(spread, 1),
            "distinct_counts": len(counts),
            "rows": sorted(
                (
                    {
                        "track_id": i["track_id"],
                        "album": i["album"],
                        "year": i["year"],
                        "lastfm_listeners": i["lastfm_listeners"],
                        "lastfm_playcount": i["lastfm_playcount"],
                        "stars": i["stars"],
                        "popularity_frozen": i["popularity_frozen"],
                    }
                    for i in items
                ),
                key=lambda x: -x["lastfm_listeners"],
            ),
        })

    groups.sort(key=lambda g: -g["spread"])

    album_keys: set[tuple[str, str]] = set()
    for group in groups:
        for row in group["rows"]:
            if row["album"]:
                album_keys.add((group["artist"], row["album"]))

    summary = {
        "groups": len(groups),
        "rows_in_conflict": total_rows_in_conflict,
        "groups_with_frozen_low_side": frozen_low,
        "implicated_albums": len(album_keys),
        "min_ratio": ratio,
        "scanned_rows": len(rows),
    }

    if limit_groups:
        groups = groups[: int(limit_groups)]

    logger.info("Listener contradiction report built", **summary)

    return {
        "groups": groups,
        "summary": summary,
        "albums": [
            {"artist": a, "album": al} for a, al in sorted(album_keys)
        ],
    }


def implicated_albums(*, min_ratio: float = 3.0) -> list[tuple[str, str]]:
    """The ``(artist, album)`` pairs a targeted force-refresh must cover."""
    report = build_listener_contradictions(min_ratio=min_ratio)
    return [(a["artist"], a["album"]) for a in report["albums"]]
