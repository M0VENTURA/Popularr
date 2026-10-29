"""CSV exports of the popularity data — one artist, one album, or the library.

Backs the "Download Popularity Report" actions on the artist and album pages
and the collection-wide export on the Config page: the full popularity row set
— Last.fm listeners/playcount, ListenBrainz listens/users, both source scores,
the combined score, the star rating and the single-detection verdict — so the
numbers can be sorted and filtered in a spreadsheet instead of read off a
screen. It also carries ``last_scanned`` (the newest Last.fm/ListenBrainz fetch
for the row) so a reader can tell whether the numbers are current or stale.

All three scopes share ONE query and ONE ordering rule. The album report is a
subset of the artist report at the same ordering, and the artist report is a
subset of the collection report, so a row appears in the same relative order in
every file; two copies of the SQL would eventually disagree about a column or a
sort key, which is worse than no report at all.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from sqlalchemy import text

from db.engine import db_session

#: Columns of the report, in file order. ``rank`` and ``last_scanned`` are
#: computed after the query; every other name is a real ``tracks`` column
#: (checked against ``db.schema.COLUMN_REGISTRY`` — selecting one that does not
#: exist would 500).
POPULARITY_REPORT_COLUMNS: tuple[str, ...] = (
    "rank",
    "title",
    "artist",
    "album",
    "disc_number",
    "track_number",
    "year",
    "raw_score",
    "final_score",
    "popularity",
    "stars",
    "duration",
    "lastfm_listeners",
    "lastfm_playcount",
    "lastfm_playcount_available",
    "lastfm_score",
    "listenbrainz_listens",
    "listenbrainz_users",
    "listenbrainz_score",
    "exclude_from_stats",
    "is_single",
    "single_confidence",
    "single_confidence_score",
    "is_live",
    "popularity_frozen",
    # Data freshness. ``last_scanned`` is the NEWEST of the two per-source
    # fetch stamps (computed below, because GREATEST/max differs between
    # PostgreSQL and the SQLite leg); the individual stamps follow so a stale
    # column can be traced to Last.fm or ListenBrainz.  Blank means the row
    # predates those timestamps / was never source-refreshed — deliberately NOT
    # ``updated_at`` (a metadata edit would make stale popularity data look
    # fresh) and not ``tracks.last_scanned`` (that legacy column is no longer
    # written by the pipeline).
    "last_scanned",
    "lastfm_last_updated",
    "listenbrainz_last_updated",
)

#: Most popular first. ``raw_score`` is the PRE-REMAP blend (migration 016):
#: the weighted Last.fm/ListenBrainz mixture BEFORE
#: ``apply_album_relative_popularity`` normalised it to the album's own band.
#: ``final_score`` is that normalised value, so it is only comparable WITHIN an
#: album — it is what let the top cut of a live album (a few thousand
#: listeners) outrank the artist's biggest hit (a million listeners) in this
#: report. The report promises "most popular first", so the cross-album number
#: leads; ``final_score`` (and the page's own fallback chain) only serve rows
#: written before ``raw_score`` existed.
#:
#: The SAME chain orders the album-scoped report: within one album the two
#: values are monotonic in each other (the remap is order-preserving), so the
#: album file reads out in the same relative order as the artist file.
_POPULARITY_REPORT_SELECT = """
    SELECT title, artist, album, disc_number, track_number, year,
           raw_score, final_score, popularity, stars, duration,
           lastfm_listeners, lastfm_playcount, lastfm_playcount_available,
           lastfm_score,
           listenbrainz_listens, listenbrainz_users, listenbrainz_score,
           exclude_from_stats,
           is_single, single_confidence, single_confidence_score,
           is_live, popularity_frozen,
           lastfm_last_updated, listenbrainz_last_updated
    FROM tracks
    WHERE {where}
    ORDER BY COALESCE(NULLIF(raw_score, 0), final_score, popularity, stars, 0) DESC,
             LOWER(COALESCE(title, ''))
"""

#: Artists are keyed by ``COALESCE(album_artist, artist)`` everywhere else in
#: the app, so the report uses the same key — a compilation track filed under
#: its album artist belongs to that artist's report.
_ARTIST_PREDICATE = "LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"

#: Album titles are NOT unique across artists ("Greatest Hits" exists a
#: thousand times over), which is why the album scope always carries the
#: artist predicate as well.
_ALBUM_PREDICATE = "LOWER(COALESCE(album, '')) = LOWER(:album)"


def _scope(*, artist: str | None, album: str | None) -> tuple[str, dict[str, Any]]:
    """WHERE clause + bound parameters for one scope.

    ``artist=None`` means the WHOLE COLLECTION (the Config page's export). That
    is expressed as ``1 = 1`` rather than by omitting the WHERE clause, so the
    statement text has one shape and the ordering rule is provably the same in
    every scope.

    Built as text rather than ``(:album IS NULL OR ...)`` on purpose: a bare
    untyped parameter compared against ``NULL`` makes PostgreSQL guess at the
    parameter's type, and the guess is not always ``text``.
    """
    if not artist:
        return "1 = 1", {}

    params: dict[str, Any] = {"artist": artist}
    predicate = _ARTIST_PREDICATE
    if album:
        predicate = f"{predicate} AND {_ALBUM_PREDICATE}"
        params["album"] = album
    return predicate, params


def _newest_timestamp(*values: Any) -> str:
    """The newest of several timestamp values, as text ('' when none).

    Both PostgreSQL drivers and SQLite can hand back ``datetime`` OR ISO text
    for the same column, so the values are compared as their string form —
    ISO-8601 and SQL timestamps both sort correctly that way.  Using SQL
    ``GREATEST``/``max`` instead would need a dialect fork for no benefit: the
    row set is already in memory.
    """
    newest = ""
    for value in values:
        if value is None:
            continue
        text_value = str(value).strip()
        if text_value and text_value > newest:
            newest = text_value
    return newest


def _filename_part(value: str, fallback: str) -> str:
    """Strip everything a filesystem or a Content-Disposition header dislikes."""
    return re.sub(r"[^A-Za-z0-9 _-]", "", str(value or "")).strip() or fallback


def build_popularity_report(*, artist: str | None = None, album: str | None = None) -> str:
    """CSV text for one artist (or one album), or the whole collection.

    ``artist=None`` → every track in ``tracks``. That is what the Config page's
    "Export Popularity Data" button downloads, and it is the only scope whose
    size is not bounded by one artist, so it is also the one that most needs to
    stay OFF the event loop.

    BLOCKING — the caller must run it OFF the event loop
    (``await asyncio.to_thread(...)``), because it reads every track in scope.
    """
    predicate, params = _scope(artist=artist, album=album)

    with db_session() as session:
        rows = (
            session.execute(
                text(_POPULARITY_REPORT_SELECT.format(where=predicate)), params
            )
            .mappings()
            .all()
        )

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(POPULARITY_REPORT_COLUMNS)
    for position, row in enumerate(rows, start=1):
        last_scanned = _newest_timestamp(
            row.get("lastfm_last_updated"),
            row.get("listenbrainz_last_updated"),
        )
        values = [
            last_scanned if column == "last_scanned" else row.get(column)
            for column in POPULARITY_REPORT_COLUMNS[1:]
        ]
        # ``None`` becomes an empty cell; everything else is written as stored.
        writer.writerow([position] + values)
    return buffer.getvalue()


def build_collection_popularity_report() -> str:
    """CSV of EVERY track in the library, most popular first.

    A thin wrapper rather than a second query: the collection file must sort
    exactly like an artist file so a row keeps its relative position when the
    two are compared side by side.
    """
    return build_popularity_report()


def popularity_report_filename(*, artist: str | None = None, album: str | None = None) -> str:
    """Attachment name for one scope; the album, when given, is part of it."""
    if not artist:
        return "Popularr - collection popularity report.csv"
    safe_artist = _filename_part(artist, "artist")
    if album:
        safe_album = _filename_part(album, "album")
        return f"{safe_artist} - {safe_album} - popularity report.csv"
    return f"{safe_artist} - popularity report.csv"
