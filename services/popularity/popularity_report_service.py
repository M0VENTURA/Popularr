"""CSV exports of the popularity data — one artist, one album, or the library.

Backs the "Download Popularity Report" actions on the artist and album pages
and the collection-wide export on the Config page: the full popularity row set
— Last.fm listeners/playcount, ListenBrainz listens/users, both source scores,
the combined score, the star rating and the single-detection verdict — so the
numbers can be sorted and filtered in a spreadsheet instead of read off a
screen. It also carries ``last_scanned`` (the newest Last.fm/ListenBrainz fetch
for the row) so a reader can tell whether the numbers are current or stale, and
the per-source genre fields (ListenBrainz, MusicBrainz, Last.fm, Discogs) plus
the final chosen genre so a reader can see where a track's genre came from.

All three scopes share ONE query and ONE ordering rule. The album report is a
subset of the artist report at the same ordering, and the artist report is a
subset of the collection report, so a row appears in the same relative order in
every file; two copies of the SQL would eventually disagree about a column or a
sort key, which is worse than no report at all.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.popularity_repository import parse_genre_value

logger = structlog.get_logger(__name__)

#: Columns of the report, in file order. ``rank`` and ``last_scanned`` are
#: computed after the query; every other name is a real ``tracks`` column
#: (checked against ``db.schema.COLUMN_REGISTRY`` — selecting one that does not
#: exist would 500).
POPULARITY_REPORT_COLUMNS: tuple[str, ...] = (
    "rank",
    "title",
    "artist",
    # ⚠️ ``album_artist`` IS THE ALBUM'S OWNER, and without it a "this album is
    # filed under the wrong artist" problem is invisible in this file.
    #
    # Reported: "many albums somehow got merged into the Various Artists albums
    # such as Spotify - Essential Alternative". ``artist`` on a compilation is the
    # PER-TRACK performer, so a merged album and a genuine Various-Artists
    # compilation look IDENTICAL in the export — which is exactly why it could not
    # be diagnosed from it. The album-level credit is the column that tells them
    # apart.
    "album_artist",
    "album",
    # The four entity ids every "why did Navidrome split this album?" question
    # needs, plus a one-word integrity flag. See ``_mb_id_diagnostic``.
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "mb_id_diagnostic",
    "disc_number",
    "track_number",
    "year",
    # Per-source genres + the final chosen genre, so a reader can see which
    # provider supplied the genre the app settled on. These are JSONB columns
    # that psycopg hands back as a Python list (and SQLite as JSON/text); they
    # are normalised to a "A; B" cell below rather than written as a list repr.
    "listenbrainz_genres",
    "musicbrainz_genres",
    "lastfm_tags",
    "discogs_genres",
    "genres",
    "raw_score",
    "final_score",
    # A one-word flag for the two score/popularity shapes the audit asked to
    # trace: a famous track with no score, and a score built on no evidence.
    # Blank for ordinary rows.
    "score_diagnostic",
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
    # Which playlists this track currently sits in, so the ranking can be read
    # against where the track actually appears. Sourced from the playlist index
    # (``playlist_entries``) rather than a live Subsonic walk; blank when the
    # index has not been synced or the track is in no playlist.
    "playlists_included_in",
    "exclude_from_stats",
    "is_single",
    "single_confidence",
    "single_confidence_score",
    # ⭐ WHICH SOURCES AGREED. ``is_single`` / ``single_confidence`` say what the
    # detector CONCLUDED; these say why, so the scoring can be judged rather than
    # guessed at. ``single_sources`` is a JSONB list of source names
    # (``["musicbrainz", "discogs"]``) and is normalised to one cell below.
    "single_sources",
    "single_sources_used",
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
    SELECT title, artist, album_artist, album,
           musicbrainz_albumid, musicbrainz_releasegroupid,
           musicbrainz_artistid, musicbrainz_albumartistid,
           disc_number, track_number, year,
           listenbrainz_genres, musicbrainz_genres, lastfm_tags,
           discogs_genres, genres,
           raw_score, final_score, popularity, stars, duration,
           lastfm_listeners, lastfm_playcount, lastfm_playcount_available,
           lastfm_score,
           listenbrainz_listens, listenbrainz_users, listenbrainz_score,
           exclude_from_stats,
           is_single, single_confidence, single_confidence_score,
           single_sources, single_sources_used,
           is_live, popularity_frozen,
           lastfm_last_updated, listenbrainz_last_updated,
           id
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

#: Genre columns are JSONB in PostgreSQL (a Python list off the driver) and
#: JSON/text under SQLite. They are written as one delimited cell — never the
#: Python list repr — so a spreadsheet sees "Rock; Pop" rather than "['Rock',
#: 'Pop']".
_GENRE_COLUMNS: frozenset[str] = frozenset(
    {
        "listenbrainz_genres",
        "musicbrainz_genres",
        "lastfm_tags",
        "discogs_genres",
        "genres",
    }
)


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


def _genre_cell(value: Any) -> str:
    """One CSV cell for a genre column, regardless of its stored shape.

    ``genres`` is TEXT while the provider columns are JSONB; a JSONB column
    arrives as a Python list from psycopg and as JSON text from SQLite, so the
    shared :func:`parse_genre_value` is what keeps both dialects and both column
    types reading out as ``"Rock; Pop"``.
    """
    return "; ".join(parse_genre_value(value))


def ensure_playlist_index() -> dict[str, int]:
    """Make the ``playlists_included_in`` column meaningful before an export.

    ⚠️ WHY THE EXPORT HAS TO DO THIS ITSELF.

    The column reads ``playlist_entries``, which is only populated by
    ``sync_playlist_index`` — and that runs from the scan's FINALISE stage. So on
    a library whose scan is still in progress (or stopped part-way), every row in
    every popularity export came out blank, which reads as "this track is in no
    playlist" rather than "the index has not been synced".

    The sync is incremental: every playlist whose Navidrome ``changed`` stamp has
    not moved is skipped WITHOUT a request, so a settled library costs one
    ``fetch_all_playlists`` call. That is affordable per export and much more
    useful than a silently blank column.

    ⚠️ AND WHY IT MUST PASS NAVIDROME CLIENTS.

    This used to call ``sync_playlist_index()`` with no clients. The sync then
    enumerated ONLY file-backed playlists, saw no errors, and pruned every
    server-backed playlist out of ``playlists``/``playlist_entries``. The export
    therefore wiped the membership it was about to serialise, so every row of
    ``playlists_included_in`` came out blank even when a completed scan had
    indexed the playlists. ``refresh_playlist_index`` in the finalise stage is
    the one refresh path that supplies the server-scoped clients, and the sync
    itself now refuses to prune a no-client (partial) view.

    One definition, called by all three scopes (collection / artist / album), so
    the column can never be populated for one export and blank for another.

    Never raises: the export is the point, and a Navidrome outage must not cost
    the whole file.
    """
    try:
        try:
            # The client factory lives in the finalise stage because playlists
            # are SERVER-scoped and that is where the one-client-per-server
            # dedupe is defined. Import lazily: this runs only for an explicit
            # export, never on a page load.
            from services.popularity.stages.finalise_stage import (
                refresh_playlist_index,
            )

            stats = refresh_playlist_index()
        except Exception as exc:
            # The canonical factory could not be imported. Still refresh the
            # file-backed playlists rather than skipping the pass entirely —
            # safe because a no-client sync no longer prunes.
            logger.debug(
                "Playlist client factory unavailable — indexing file-backed "
                "playlists only",
                error=str(exc),
            )
            from services.playlists.playlist_index_service import (
                sync_playlist_index,
            )

            stats = sync_playlist_index()

        logger.info("Playlist index refreshed for the popularity report", **stats)
        return stats
    except Exception as exc:
        logger.warning(
            "Playlist index refresh before the popularity report failed — "
            "the playlists_included_in column may be blank",
            error=str(exc),
        )
        return {}


#: Columns holding a JSONB list of source names (see ``_source_list_cell``).
_SOURCE_LIST_COLUMNS: frozenset[str] = frozenset({
    "single_sources", "single_sources_used",
})


def _source_list_cell(value: Any) -> str:
    """One CSV cell for a JSONB list of source names.

    ``single_sources`` is stored as a JSON array (``["musicbrainz", "discogs"]``)
    and arrives as a Python list from psycopg, as JSON text from SQLite, and as a
    CSV fragment from an older writer. Writing the raw value would put brackets
    and quotes in a spreadsheet cell and make the column useless for filtering,
    so every shape is flattened to ``"musicbrainz; discogs"``.

    An empty list stays EMPTY rather than becoming ``"[]"`` — "no source agreed"
    is a real answer and must not look like data.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value or text_value in ("[]", "null", "None"):
            return ""
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError):
            # A plain string ("musicbrainz, discogs") or a legacy CSV fragment.
            return text_value
        return _source_list_cell(parsed)
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item or "").strip()]
        return "; ".join(parts)
    return str(value)


_VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
_VA_NAMES = frozenset({"various artists", "variousartist", "va"})


def _mb_id_diagnostic(row: Any, *, album_has_multiple_releases: bool = False) -> str:
    """Flag structurally invalid MusicBrainz IDs on one row.

    * ``album_has_multiple_releases`` — the same artist + album carries more
      than one MUSICBRAINZ_ALBUMID. Navidrome derives albums from release ids,
      so this is the Faun "one folder, two editions" split.
    * ``release_equals_group`` — a Release and a Release Group are different
      entities; the same UUID in both frames is malformed (the Vitro case).
    * ``artist_is_album_artist`` — on a Various Artists album, the per-track
      artist id must be the PERFORMER (Vitro), never the album artist id.
    """
    release = str(row.get("musicbrainz_albumid") or "").strip()
    group = str(row.get("musicbrainz_releasegroupid") or "").strip()
    artist = str(row.get("musicbrainz_artistid") or "").strip()
    album_artist = str(row.get("musicbrainz_albumartistid") or "").strip()
    problems: list[str] = []
    if album_has_multiple_releases:
        problems.append("album_has_multiple_releases")
    if release and group and release.casefold() == group.casefold():
        problems.append("release_equals_group")
    if artist and (
        artist == _VA_MBID
        or (
            album_artist
            and artist.casefold() == album_artist.casefold()
            and (
                str(row.get("album_artist") or "").strip().casefold() in _VA_NAMES
                or album_artist == _VA_MBID
            )
        )
    ):
        problems.append("artist_is_album_artist")
    return "; ".join(problems)


def _score_diagnostic(row: Any) -> str:
    """Flag the two highest-value score/popularity disagreements.

    * ``popular_but_low_score`` — a widely listened studio track scored below
      30. That shape is the signature of a bad release/recording match, a
      missing playcount treated as zero, or a frozen incomplete scan.
    * ``high_score_low_evidence`` — a score of 90+ with almost no listeners on
      either provider: fallback inflation rather than measured popularity.

    Advisory only: the report carries it so the rows can be found, and the
    scoring itself is never changed from here.
    """
    try:
        if bool(row.get("is_live")) or bool(row.get("exclude_from_stats")):
            return ""
        score = float(row.get("final_score") or 0)
        lastfm = float(row.get("lastfm_listeners") or 0)
        listenbrainz = float(row.get("listenbrainz_listens") or 0)
    except (TypeError, ValueError):
        return ""
    if lastfm >= 100_000 and score < 30:
        return "popular_but_low_score"
    if score >= 90 and lastfm < 1_000 and listenbrainz < 100:
        return "high_score_low_evidence"
    return ""


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

    # ⭐ ONE QUERY FOR THE WHOLE FILE, not one per row.
    #
    # The index is optional: an unsynced or absent index yields an empty map and
    # the column reads blank, which is honest — it does not silently claim the
    # track is in no playlist. The failure is contained here so the export
    # itself can never fail because of a convenience column.
    playlists_by_track: dict[str, str] = {}
    try:
        from db.repositories.playlists import playlist_names_by_track_id

        playlists_by_track = playlist_names_by_track_id()
    except Exception as exc:
        logger.debug("Playlist index unavailable for the report", error=str(exc))

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(POPULARITY_REPORT_COLUMNS)

    # ⭐ ONE FOLDER, TWO EDITIONS. Mixing MUSICBRAINZ_ALBUMID values inside one
    # artist+album is what splits the album in Navidrome (reported on Faun's
    # Buch der Balladen: the standard and Limited releases, same group).
    # Computed from the rows already in hand — no second query, no window SQL
    # the report's column parser would trip over.
    _album_release_ids: dict[tuple[str, str], set[str]] = {}
    for _row in rows:
        _key = (
            str(_row.get("album_artist") or _row.get("artist") or "").strip().casefold(),
            str(_row.get("album") or "").strip().casefold(),
        )
        _release = str(_row.get("musicbrainz_albumid") or "").strip()
        if _release:
            _album_release_ids.setdefault(_key, set()).add(_release.casefold())
    _split_albums = {
        key for key, releases in _album_release_ids.items() if len(releases) > 1
    }

    for position, row in enumerate(rows, start=1):
        last_scanned = _newest_timestamp(
            row.get("lastfm_last_updated"),
            row.get("listenbrainz_last_updated"),
        )
        values = []
        for column in POPULARITY_REPORT_COLUMNS[1:]:
            if column == "last_scanned":
                values.append(last_scanned)
            elif column == "score_diagnostic":
                values.append(_score_diagnostic(row))
            elif column == "mb_id_diagnostic":
                _row_key = (
                    str(row.get("album_artist") or row.get("artist") or "").strip().casefold(),
                    str(row.get("album") or "").strip().casefold(),
                )
                values.append(
                    _mb_id_diagnostic(
                        row, album_has_multiple_releases=_row_key in _split_albums,
                    )
                )
            elif column == "playlists_included_in":
                values.append(playlists_by_track.get(str(row.get("id") or ""), ""))
            elif column in _GENRE_COLUMNS:
                values.append(_genre_cell(row.get(column)))
            elif column in _SOURCE_LIST_COLUMNS:
                values.append(_source_list_cell(row.get(column)))
            else:
                values.append(row.get(column))
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
