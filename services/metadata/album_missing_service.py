"""Album detail/comparison services — missing tracks, title mismatches, library tracks.

Extracted from the old monolithic app.py.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import bindparam, text

from db.engine import db_session
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    fetch_musicbrainz_release_metadata,
    # The ONE track-number rule, shared with both queue matchers: a position
    # is a tie-breaker, never proof.
    _track_number_pairing_allowed,
    _titles_agree_for_pairing,
)
from api_clients.musicbrainz_http import escape_lucene_special_chars
from helpers.normalization_service import (
    file_version_variant_key,
    is_track_artist_placeholder,
    normalize_artist,
    normalize_title_for_compare,
    normalize_title_for_lucene_query,
)
from helpers.track_ordering import album_track_sort_key, leading_int

logger = structlog.get_logger(__name__)


def _file_duration_seconds(file_path: str) -> float | None:
    """The audio file's own length, in seconds, or None.

    ⚠️ The DATABASE duration is NULL for a track that has never finished a
    scan, and the pairing rule treats an unknown length as permissive. That let
    a different recording sitting at the release's track number hide a genuinely
    missing track (reported: "Track 19 is missing but not showing under
    Missing"). Reading the file costs one header parse and only happens when the
    stored length is empty AND the titles already disagree.
    """
    path = str(file_path or "").strip()
    if not path:
        return None
    try:
        from helpers.metadata_reader import read_mp3_metadata
        from helpers.normalization_service import queue_duration_seconds

        meta = read_mp3_metadata(path) or {}
        if meta.get("duration_ms"):
            return queue_duration_seconds(meta.get("duration_ms"))
    except Exception:
        pass
    return None


def _title_match_key(title: str) -> str:
    """Normalise a title for missing-track matching.

    Unlike the old ``[^a-z0-9]+`` strip (which erased ALL Hangul/CJK and
    over-collapsed distinct Korean titles — e.g. "락 (樂) (LALALALA)" and
    "사각지대 (BLIND SPOT)" both became near-empty keys), this preserves
    non-ASCII script characters while stripping punctuation/whitespace and
    normalising case.  Both the MB title and the library title go through
    the SAME key, so Korean / CJK titles match precisely.
    """
    if not title:
        return ""
    import unicodedata as _ud

    value = _ud.normalize("NFKC", str(title)).lower().strip()
    # Keep letters/digits across ALL scripts (incl. Hangul/CJK); drop
    # punctuation, brackets and whitespace.
    value = re.sub(r"[^\w\uAC00-\uD7AF\u4E00-\u9FFF\u3040-\u30FF]+", "", value, flags=re.UNICODE)
    return value.strip()


def _album_key(album: str) -> str:
    """Normalise an album name for scope matching (strip a leading year)."""
    value = str(album or "").strip()
    # "2024 - 樂-STAR" → "樂-STAR"; "2024 樂-STAR" → "樂-STAR".
    value = re.sub(r"^(?:19|20)\d{2}\s*[-–—]?\s+", "", value).strip()
    return value.lower()


def _album_scope_matches(requested: str, stored: str) -> bool:
    """Does *stored* belong to the album named by *requested*?

    Year-tolerant, but not year-BLIND: a leading year may be present on one
    side only (the year is part of the folder name for some libraries and not
    others), yet two DIFFERENT years are two different albums. Without the
    second rule, "2001 - MTV2 Handpicked" and "2002 - MTV2 Handpicked" collapse
    into one scope and a track on both is flagged as a duplicate on a page that
    can only show the requested album's rows.
    """
    requested_text = str(requested or "").strip()
    stored_text = str(stored or "").strip()
    if requested_text.casefold() == stored_text.casefold():
        return True

    requested_year = re.match(r"^((?:19|20)\d{2})\b", requested_text)
    stored_year = re.match(r"^((?:19|20)\d{2})\b", stored_text)
    if (
        requested_year is not None
        and stored_year is not None
        and requested_year.group(1) != stored_year.group(1)
    ):
        return False
    return _album_key(requested_text) == _album_key(stored_text)


def _row_value(row: Any, column: str, position: int | None = None) -> Any:
    """Read ``column`` from a SQLAlchemy RowMapping / Row / dict.

    Rows returned by ``.mappings().all()`` are RowMappings and MUST be indexed
    by COLUMN NAME — ``row[0]`` raises "Could not locate column in row for
    column '0'".  This helper makes that the default while still tolerating a
    plain tuple row, so a caller can never reintroduce the positional-index
    bug by accident.
    """
    if row is None:
        return None
    if hasattr(row, "get"):
        try:
            return row.get(column)
        except Exception:
            pass
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return mapping[column]
        except Exception:
            pass
    if position is not None:
        try:
            return row[position]
        except Exception:
            return None
    return None


def get_library_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    """Get all library tracks for a specific artist/album.

    The album is matched tolerantly: a library album carrying a leading year
    prefix ("2024 - 樂-STAR") still matches the requested album ("樂-STAR").
    """
    album_key = _album_key(album)
    with db_session() as session:
        rows = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, file_path, duration, album, album_artist, artist FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "ORDER BY COALESCE(disc_number, '1'), track_number"
            ),
            {"artist": artist},
        ).mappings().all()

    library = [
        dict(r)
        for r in rows
        if _album_key(str(r.get("album") or "")) == album_key
    ]
    # ``track_number`` is TEXT — ``ORDER BY track_number`` reads 1, 10, 11, 12, 2.
    # Decide the order here with the rule the album page shares.
    library.sort(key=album_track_sort_key)
    return library


def _artist_match_key(value: Any) -> str:
    """Comparison key for an artist credit (case/punctuation/feat-insensitive)."""
    return normalize_artist(str(value or ""))


def _queued_coverage(
    artist: str,
    album: str,
    *,
    track_artists: set[str] | None = None,
) -> tuple[dict[str, str], dict[tuple[int, str], str], dict[tuple[str, str], str], dict[str, str]]:
    """Queue rows that may deliver THIS album's tracks, keyed to their status.

    Returns ``(title_key -> status, (disc, track_number) -> status,
    (artist_key, title_key) -> status, generic_title_key -> status)``.

    ⚠️ IDENTITY IS THE TRACK ARTIST + TITLE, NOT THE ALBUM ARTIST + ALBUM.
    Queue coverage used to require the queue row's ``COALESCE(album_artist,
    artist)`` AND its album to match the page. On a Various Artists compilation
    that missed two legitimate cases, leaving an already-queued track showing
    "Missing" (and its Add button enabled):

    * the row's ``album_artist`` is the track artist (or empty) instead of
      "Various Artists"; and
    * the row was queued from a DIFFERENT release — same performer and song,
      another album.

    ``track_artists`` are the MusicBrainz release's per-track performers, so a
    queue row from another album is still found. Album- and position-based keys
    are kept for same-album rows, where a position is meaningful evidence.

    ⚠️ ATTRIBUTION. A row naming no album contributes no album/position key; it
    can still contribute an artist+title key, which is a statement about the
    track, not about which album it belongs to.
    """
    title_keys: dict[str, str] = {}
    positions: dict[tuple[int, str], str] = {}
    artist_title_keys: dict[tuple[str, str], str] = {}
    generic_title_keys: dict[str, str] = {}

    artists = {
        str(value or "").strip()
        for value in ({artist} | set(track_artists or set()))
        if str(value or "").strip()
    }
    if not artists:
        return title_keys, positions, artist_title_keys, generic_title_keys

    try:
        from db.engine import db_session as _q_session
        from sqlalchemy import text as _q_text

        _album_norm = _album_key(album)
        with _q_session() as session:
            q_rows = session.execute(
                _q_text("""
                    SELECT title, track_number, disc_number, status, album,
                           artist, album_artist
                    FROM download_queue
                    WHERE (
                            LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN :artists
                         OR LOWER(COALESCE(NULLIF(artist, ''), album_artist)) IN :artists
                    )
                      AND status IN ('queued', 'searching', 'downloading',
                                     'processing', 'moving', 'imported',
                                     'in_collection', 'matched', 'completed')
                """).bindparams(bindparam("artists", expanding=True)),
                {"artists": sorted(a.lower() for a in artists)},
            ).mappings().all() or []
        for q in q_rows:
            status = str(q.get("status") or "queued")
            qt = q.get("title") or ""
            if not qt:
                continue
            title_key = _title_match_key(qt)

            # Artist + title: the identity the user asked for. Works across
            # albums, and with either credit column on the queue row.
            for credit in (q.get("artist"), q.get("album_artist")):
                credit_key = _artist_match_key(credit)
                if credit_key:
                    artist_title_keys.setdefault((credit_key, title_key), status)

            # Same-album keys: album title is meaningful, and so is a position.
            q_album = str(q.get("album") or "").strip()
            if not q_album or _album_key(q_album) != _album_norm:
                continue
            title_keys.setdefault(title_key, status)

            # A queue row whose ONLY credits are compilation placeholders
            # ("Various Artists") still tells us the track was queued; its
            # performer was simply never stored. The ALBUM is required here
            # because a title alone cannot attribute an unrelated compilation.
            if (
                is_track_artist_placeholder(q.get("artist"))
                and is_track_artist_placeholder(q.get("album_artist"))
            ):
                generic_title_keys.setdefault(title_key, status)
            qtn = str(q.get("track_number") or "").strip()
            qd = _disc_of(q.get("disc_number"))
            if qtn:
                positions.setdefault((qd, qtn), status)
    except Exception as _qexc:
        logger.debug("Download-queue coverage check failed", error=str(_qexc))
    return title_keys, positions, artist_title_keys, generic_title_keys


#: A track the release labels as a bonus/extra. The title marker is enough on
#: its own; the position rule below handles unlabelled bonus cuts.
_BONUS_TITLE_RE = re.compile(r"\bbonus\b", re.IGNORECASE)


def _disc_of(value: Any) -> int:
    """The numeric disc a tag means, defaulting to 1.

    ``disc_number`` is TEXT, and a "n of m" tag stores ``"2/2"``. A bare
    ``int(...)`` raised ``ValueError`` on exactly those rows — the exception
    escaped ``get_missing_tracks`` and the album's missing list was never
    produced, so a gap on that disc was simply never reported. The album page
    already groups discs through the same tolerant ``leading_int`` (``"2/2"``
    is disc 2), so reading the tag any other way is what made the page show a
    "Disc 2" section whose tracks the missing check could not see.

    A blank or ``0`` disc is disc 1 — the reported "disc 1 and disc 0" split.
    """
    disc = leading_int(value)
    if disc is None or disc < 1:
        return 1
    return disc


def _numeric_track_number(value: Any) -> int | None:
    try:
        number = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def is_bonus_missing_track(
    mb_track: dict[str, Any],
    max_library_position: dict[int, int],
) -> bool:
    """True when a missing MB track is a trailing/bonus track.

    Reported: a compilation's bonus cuts at the END of the release were listed
    as corrections. A missing track is bonus when:

    * its own title says "bonus"; or
    * it sits AFTER the last track the library has on that disc. A gap BEFORE
      the last track is a genuine missing album track, not a bonus.

    ``max_library_position`` is ``{disc: highest numeric track number}`` for the
    tracks the library already has. An album with no numbered rows has no
    positional evidence and falls back to the title marker.
    """
    title = str(mb_track.get("title") or "")
    if _BONUS_TITLE_RE.search(title):
        return True

    try:
        disc = _disc_of(mb_track.get("disc_number"))
    except (TypeError, ValueError):
        disc = 1
    number = _numeric_track_number(mb_track.get("track_number"))
    if number is None:
        return False
    last_present = max_library_position.get(disc)
    return last_present is not None and number > last_present


def _prefer_edition_matching_library(
    mb_release: dict[str, Any],
    *,
    artist: str,
    album: str,
    library_count: int,
) -> dict[str, Any]:
    """An edition of the same release group whose track count matches the library.

    The missing-track list is only meaningful when it is measured against the
    edition the user actually owns. MusicBrainz release groups routinely hold a
    standard and one or more deluxe/bonus versions (Planets: 14 / 16 / 17), and
    the stored release id is whatever ``resolve_release_id`` picked — the
    LARGEST, so a deluxe edition's bonus cuts were reported as missing album
    tracks.

    Returns *mb_release* unchanged unless a DIFFERENT edition matches exactly.
    A tie is broken by preferring the one already stored (i.e. no swap), and any
    failure degrades to the original release rather than losing the list.
    """
    group_mbid = str(mb_release.get("release_group_mbid") or "").strip()
    current_count = len(mb_release.get("tracks") or [])
    if not group_mbid or current_count == library_count:
        return mb_release
    # ``fetch_musicbrainz_release_metadata`` keys the id as ``release_mbid``
    # (not ``id``) — comparing the wrong key would always look like a change.
    current_id = str(
        mb_release.get("release_mbid") or mb_release.get("id") or ""
    ).strip()
    try:
        from services.enrichment.musicbrainz_service import (
            fetch_musicbrainz_release_metadata,
            get_release_group_releases,
        )

        payload = get_release_group_releases(group_mbid, include_track_counts=True)
        releases = [
            rel for rel in (payload.get("releases") or [])
            if str(rel.get("id") or "").strip()
        ]
        exact = [
            rel for rel in releases
            if int(rel.get("track_count") or 0) == int(library_count)
        ]
        if not exact:
            return mb_release
        # Prefer an official release; then the one MB lists first (usually the
        # original). Never swap to a non-official bootleg just for the count.
        official = [
            rel for rel in exact
            if str(rel.get("status") or "").strip().lower() == "official"
        ]
        chosen = (official or exact)[0]
        chosen_id = str(chosen.get("id") or "").strip()
        if not chosen_id or chosen_id == current_id:
            return mb_release
        replacement = fetch_musicbrainz_release_metadata(chosen_id)
        if not replacement or not replacement.get("tracks"):
            return mb_release
        logger.info(
            "Missing-track comparison switched to the edition matching the library",
            artist=artist, album=album,
            stored_tracks=current_count, library_tracks=library_count,
            chosen_release=chosen_id,
        )
        return replacement
    except Exception as exc:
        logger.debug(
            "Edition matching failed — keeping the stored release",
            album=album, error=str(exc),
        )
        return mb_release


def get_missing_tracks(artist: str, album: str, release_mbid: str | None = None) -> dict[str, Any]:
    """Check which tracks are in the MusicBrainz release but missing from the library.

    Computes the missing set from the MusicBrainz release tracklist, persists
    each missing track to ``missing_album_tracks`` (so the list survives page
    refreshes until the track is downloaded or rejected), and returns only the
    tracks that are still missing AND not rejected (``ignored = FALSE``).

    ``release_mbid``: an EXPLICIT release to measure against — the album
    page's Lookup MBID resolves one BEFORE the form is saved, so the stored
    id is still the old release (or empty). When given, it wins over both the
    stored id and the name search. This is the same computation the
    popularity scan runs per album, just against the release the user picked.
    """
    with db_session() as session:
        album_key = _album_key(album)

        def _rows_in_album(rows):
            return [
                dict(r) for r in rows
                if _album_key(str(r.get("album") or "")) == album_key
            ]

        mb_rows = session.execute(
            text(
                "SELECT musicbrainz_album_mbid, album FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != ''"
            ),
            {"artist": artist},
        ).mappings().all()
        mb_row = next(
            (r for r in mb_rows if _album_key(str(r.get("album") or "")) == album_key),
            None,
        )
        # ``mb_row`` is a RowMapping — index by COLUMN NAME, never by integer
        # position (``mb_row[0]`` raised "Could not locate column in row for
        # column '0'").
        mb_mbid = str(_row_value(mb_row, "musicbrainz_album_mbid") or "") if mb_row else None

        library_rows = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, mbid, album, duration, file_path "
                "FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            ),
            {"artist": artist},
        ).mappings().all()
        library_rows = _rows_in_album(library_rows)
        # ⭐ A PLACEHOLDER ROW IS NOT A FILE. Missing-track rows are inserted
        # with no ``file_path`` (and queued rows carry the sentinel). They used
        # to be counted as "in the library" by the title/position gates below,
        # so a genuinely missing track that already had a placeholder row was
        # hidden from the missing list — the reported "track 1 is missing but
        # does not appear under missing releases".
        library_rows = [
            r for r in library_rows
            if "file_path" not in r
            or (
                str(r.get("file_path") or "").strip()
                and "__queued_for_download__" not in str(r.get("file_path") or "")
            )
        ]
        library_count = len(library_rows)

    # ⭐ An explicit release (the album page's Lookup MBID just resolved one)
    # wins over the stored id — and over the name search below — because the
    # stored value is still the OLD release until the user saves the form.
    if release_mbid:
        mb_mbid = str(release_mbid).strip()

    if not mb_mbid and not release_mbid:
        # ✅ Use shared MusicBrainz client singleton instead of raw instantiation
        try:
            query = (
                f'artist:"{escape_lucene_special_chars(artist)}" '
                f'AND release:"{escape_lucene_special_chars(album)}"'
            )
            releases = get_shared_mb_client().search_releases(query, limit=5)
            if releases:
                mb_mbid = releases[0].get("id")
        except Exception as exc:
            logger.debug("MB release search fallback failed", error=str(exc))

    if not mb_mbid:
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": library_count}

    try:
        mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    except Exception as exc:
        logger.debug("MB release metadata fetch failed", release_mbid=mb_mbid, error=str(exc))
        mb_release = None

    if not mb_release:
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": library_count}

    # ⭐⭐ COMPARE AGAINST THE EDITION THE LIBRARY ACTUALLY HOLDS.
    #
    # Reported: "Planets" showed 2 missing tracks (#11, #14) and the release
    # picker offered 17 / 16 / 14-track versions — the library has 14. The
    # missing list was measured against the 17-TRACK DELUXE edition, so its
    # bonus cuts read as genuinely-missing album tracks.
    #
    # Nothing in this path ever asked "which of these editions has the tracks I
    # own?" — and the scan's own choice actively favours the biggest:
    # ``resolve_release_id`` does ``max(..., key=_total_tracks)``. So the stored
    # id IS the deluxe edition for exactly these albums.
    #
    # When an explicit release was requested (the album page's Lookup, or a
    # user pick) that choice is authoritative and is NOT second-guessed. On the
    # scan/refresh path, the group is browsed and an edition whose track count
    # matches the library's wins.
    if not release_mbid and library_count > 0:
        mb_release = _prefer_edition_matching_library(
            mb_release, artist=artist, album=album, library_count=library_count
        )
        mb_tracks = mb_release.get("tracks", [])
    else:
        mb_tracks = mb_release.get("tracks", [])
    mb_total = len(mb_tracks)

    lib_norm = set()
    lib_by_position: dict[tuple[int, str], tuple[str, Any]] = {}
    for r in library_rows:
        title = r.get("title") or ""
        if title:
            lib_norm.add(_title_match_key(title))
        disc = _disc_of(r.get("disc_number"))
        tn = str(r.get("track_number") or "").strip()
        if tn:
            # The DURATION travels with the position so the pairing rule can
            # tell "same recording, differently titled" from "a different song
            # sitting at the same number". The PATH travels too: when the DB
            # has no duration the file's own header can still settle it.
            lib_by_position[(disc, tn)] = (
                title or "",
                r.get("duration"),
                r.get("file_path"),
            )

    # The highest position the library actually has per disc. A missing MB track
    # AFTER it is a trailing/bonus cut; one BEFORE it is a gap in the album.
    max_library_position: dict[int, int] = {}
    for (disc, tn) in lib_by_position:
        number = _numeric_track_number(tn)
        if number is not None:
            max_library_position[disc] = max(
                max_library_position.get(disc, 0), number
            )

    # ── Download-queue coverage ───────────────────────────────────────────
    # Two different questions, deliberately answered differently:
    #
    #   * DELIVERED (imported / completed / matched / in_collection) — the
    #     track is (or is about to be) in the library, so it is NOT missing.
    #     Without this a freshly-downloaded track stayed on the missing list
    #     until a re-scan because the library-match could fail on title or
    #     album normalisation.
    #   * NOT YET DELIVERED (queued / searching / downloading / …) — the user
    #     does NOT have the file, so the track stays on the list and carries
    #     its ``queue_status``.  Hiding it was the reported bug: a queue row
    #     that never progresses (multi-minute searches, a stalled transfer)
    #     removed the track from the page FOREVER — "missing tracks 10 and 11
    #     are still outstanding" — with nothing on screen to explain it.
    # The release's own performers, so queue rows from ANOTHER release (same
    # track artist + title, different album) still count as delivering this one.
    _mb_track_artists = {
        str(mt.get("artist") or "").strip()
        for mt in mb_tracks
        if str(mt.get("artist") or "").strip()
    }
    (
        queued_titles,
        queued_positions,
        queued_artist_titles,
        queued_generic_titles,
    ) = _queued_coverage(artist, album, track_artists=_mb_track_artists)

    # ── Cross-album library ownership ─────────────────────────────────────
    # The SAME track artist + title may already be owned on ANOTHER release
    # (the studio album, a different compilation, a Various Artists album whose
    # album artist differs). It is the same recording, so it is not a
    # re-download — the same rule ``find_library_track`` applies when the Add
    # button checks whether a track is already owned.
    #
    # ⚠️ BUT IT IS STILL MISSING FROM *THIS* ALBUM, and that is the question
    # this function answers. Reported on a Various Artists compilation: a gap on
    # disc 2 was absent from the library while the same recording existed on the
    # band's own album, so this gate excluded it and the missing list came back
    # EMPTY — "Missing tracks on the second disk aren't populating". Owning a
    # song elsewhere is not owning this album's copy of it.
    #
    # The set is therefore only used to ANNOTATE the row (``owned_elsewhere``),
    # which lets the UI say where the recording already lives without pretending
    # the album is complete.
    _cross_album_library: dict[tuple[str, str], str] = {}
    try:
        if _mb_track_artists:
            with db_session() as session:
                _owner_rows = session.execute(
                    text("""
                        SELECT title, artist, album_artist, album
                        FROM tracks
                        WHERE LOWER(COALESCE(NULLIF(artist, ''), album_artist)) IN :artists
                           OR LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN :artists
                    """).bindparams(bindparam("artists", expanding=True)),
                    {"artists": sorted(a.lower() for a in _mb_track_artists)},
                ).mappings().all() or []
            for _owner in _owner_rows:
                _owner_title = _title_match_key(_owner.get("title") or "")
                if not _owner_title:
                    continue
                # A row on THIS album is already handled by the position/title
                # gates above; only another release counts as "elsewhere".
                _owner_album = str(_owner.get("album") or "")
                if _owner_album and _album_key(_owner_album) == _album_key(album):
                    continue
                for _credit in (_owner.get("artist"), _owner.get("album_artist")):
                    _credit_key = _artist_match_key(_credit)
                    if not _credit_key:
                        continue
                    _key = (_credit_key, _owner_title)
                    # Prefer a NAMED owning album: a row with no album is still
                    # "owned elsewhere", but it cannot tell the user where.
                    if _key not in _cross_album_library or not _cross_album_library[_key]:
                        _cross_album_library[_key] = _owner_album
    except Exception as _owner_exc:
        logger.debug("Cross-album library ownership check failed", error=str(_owner_exc))

    # ── Which MB track each TITLE belongs to ──────────────────────────────
    #
    # ⚠️⚠️ A POSITION OCCUPIED BY A DIFFERENT SONG IS NOT EVIDENCE.
    #
    # Reported: "Missing tracks doesn't pick up a track if another track has the
    # wrong track number. A track is track 4, but labelled as one, so it didn't
    # properly load 1 or 4."
    #
    # A mislabelled file takes someone else's slot: track 4's audio tagged as
    # track 1. The release's track 1 then looks present because position 1 is
    # occupied — and the pairing rule below could not tell, because the occupant
    # is a different song with (usually) an unknown length, and an unknown length
    # is deliberately permissive. So the genuinely missing track 1 was never
    # offered, which is the report.
    #
    # The occupant's TITLE already says whose slot it is, so resolve that first:
    # if the row sitting at this number matches a DIFFERENT track of the release,
    # the number belongs to that track, not this one. This is checked before the
    # position is treated as proof, and it does not loosen the pairing rule
    # itself — a correctly numbered album, or a row whose title disagrees with
    # every MB track, behaves exactly as before.
    _mb_positions_by_title: dict[str, set[tuple[int, str]]] = {}
    for _mt in mb_tracks:
        _key = _title_match_key(_mt.get("title") or "")
        if not _key:
            continue
        _mb_positions_by_title.setdefault(_key, set()).add(
            (_disc_of(_mt.get("disc_number")), str(_mt.get("track_number") or "").strip())
        )

    missing = []
    #: Why the rest of the release is NOT listed — one dict, four gates.
    #: Without it a track dropped by a gate is indistinguishable from one that
    #: never existed (the reported "1-9, 12, 13 render but 10 and 11 absent").
    excluded: dict[str, int] = {"in_library": 0}
    for mt in mb_tracks:
        mb_title = mt.get("title", "")
        if not mb_title:
            continue
        norm = _title_match_key(mb_title)
        mb_disc = _disc_of(mt.get("disc_number"))
        mb_num = str(mt.get("track_number") or "").strip()

        position_occupied = bool(mb_num) and (mb_disc, mb_num) in lib_by_position
        if position_occupied:
            # ⚠️ A POSITION IS A TIE-BREAKER, NEVER PROOF — the SAME rule both
            # queue matchers already use. It used to count on its own, so any
            # local rip whose own numbering differed from MusicBrainz's (a
            # double-CD imported disc by disc, a long single-CD rip numbered
            # across the whole disc) made an UNRELATED track at that number
            # claim the MusicBrainz one as present — and a genuinely missing
            # track was never offered. Reported as "not correctly pulling in
            # the missing tracks".
            #
            # Now the name AND the length must both fail to contradict it
            # before a position counts: a title that merely differs in
            # wording still pairs, a different recording with a different
            # length does not, and an unknown length stays permissive so an
            # untagged file is not lost.
            _lib_title, _lib_duration, _lib_path = lib_by_position[(mb_disc, mb_num)]

            # ⚠️⚠️ FIRST: does the occupant belong to ANOTHER track of this
            # release? A mislabelled file takes a slot that is not its own
            # (track 4 tagged as track 1), which made track 1 look present and
            # hid it from the missing list — the reported "a track is track 4,
            # but labelled as one, so it didn't properly load 1 or 4".
            #
            # Answering this does not depend on length at all: the occupant's own
            # title names its real track, and that track already has its own
            # position. So the number belongs to the other track, not this one.
            _occupant_key = _title_match_key(_lib_title)
            _occupant_positions = _mb_positions_by_title.get(_occupant_key, set())
            _belongs_to_another = bool(
                _occupant_key
                and _occupant_key != norm
                and any(
                    position != (mb_disc, mb_num)
                    for position in _occupant_positions
                )
            )

            if _belongs_to_another:
                # The number is taken by a file that names a different song of
                # this release. This track's OWN copy is not here — fall through
                # to the title check and, failing that, report it missing.
                position_occupied = False
                excluded["mislabelled_position"] = (
                    excluded.get("mislabelled_position", 0) + 1
                )
            else:
                if (
                    _lib_duration is None
                    and _lib_path
                    and not _titles_agree_for_pairing(_lib_title, mb_title)
                ):
                    _lib_duration = _file_duration_seconds(str(_lib_path))
                position_occupied = _track_number_pairing_allowed(
                    _lib_title, mb_title, _lib_duration, mt.get("duration"),
                )
        if position_occupied or norm in lib_norm:
            # ⚠️ Position counts even when the TITLE differs: a wrong track
            # sitting at disc 1 / track 10 makes 10 look present. That is the
            # gate most likely to have hidden the reported pair.
            excluded["in_library"] += 1
            continue

        _mb_artist_key = _artist_match_key(mt.get("artist") or artist)
        # Owned on another release (same performer, same song). It is NOT this
        # album's copy, so it is still listed; the album name rides along so the
        # UI can point at the copy the user already has.
        _owned_elsewhere = _cross_album_library.get((_mb_artist_key, norm))

        # Same album first, then TRACK ARTIST + TITLE (which also covers a row
        # queued from a different release), then a compilation row that only
        # stored "Various Artists", then the same-album position.
        _track_artist_key = _artist_match_key(mt.get("artist") or artist)
        queue_status = (
            queued_titles.get(norm)
            or queued_artist_titles.get((_track_artist_key, norm))
            or queued_generic_titles.get(norm)
            or (queued_positions.get((mb_disc, mb_num)) if mb_num else None)
        )
        # ⚠️ A queue status NEVER hides a track — ``in_library`` is the only
        # visibility gate, and the status rides along as an annotation (the UI
        # renders it as a badge). A row marked ``imported``/``completed`` that
        # did not actually land in this album used to be invisible, and an
        # invisible track cannot be edited: *"11 already in the library,
        # 2 delivered not shown … it makes them hard to edit with the correct
        # information."*

        entry = {
            "title": mb_title,
            "track_number": mt.get("track_number"),
            "disc_number": _disc_of(mt.get("disc_number")),
            "recording_mbid": mt.get("recording_mbid"),
            "track_artist": mt.get("artist") or artist,
            "year": mb_release.get("release_year") or mb_release.get("year"),
            "release_id": mb_mbid,
            "duration": mt.get("duration"),
            "is_bonus": is_bonus_missing_track(mt, max_library_position),
        }
        if queue_status:
            entry["queue_status"] = queue_status
        if _owned_elsewhere:
            # An annotation, not a visibility gate: the recording is already on
            # this album name, so the UI can offer a copy instead of a search.
            entry["owned_on_album"] = _owned_elsewhere
        missing.append(entry)

    # Persist missing tracks so they survive refreshes.  A track already
    # present (by title + disc position) is skipped; a track previously
    # rejected (``ignored = TRUE``) is left alone so a soft dismiss is not
    # undone by the next refresh.  NOTE: nothing in the codebase ever CLEARS
    # that flag, so a dismissal is permanent — which is why the response
    # counts it (``excluded['rejected']``) and the page says "N not shown".
    try:
        _persist_missing_tracks(artist, album, missing)
    except Exception as exc:
        logger.warning(
            "Failed to persist missing tracks",
            artist=artist, album=album, error=str(exc),
        )

    # Return only tracks that are still missing AND not rejected.
    rejected_titles = _rejected_missing_titles(artist, album)
    visible = [
        m for m in missing
        if _missing_row_key(m) not in rejected_titles
    ]
    excluded["rejected"] = len(missing) - len(visible)

    return {
        "missing_tracks": visible,
        "missing_count": len(visible),
        "bonus_count": sum(1 for m in visible if m.get("is_bonus")),
        "missing_core_count": sum(1 for m in visible if not m.get("is_bonus")),
        "mb_total": mb_total,
        "library_count": library_count,
        # WHY the rest of the release is not listed, so the arithmetic is
        # self-checking: len(visible) + sum(excluded.values()) == mb_total.
        "excluded": excluded,
    }


def _version_recommendation(
    tracks: list[dict[str, Any]],
    expected_duration: float = 0.0,
) -> dict[str, Any]:
    """Which duplicate copy to keep, or a tie. Never raises.

    ``expected_duration`` is the RELEASE's length for this track, when it is
    known (the album's missing/MB tracklist). Given it, "the correct track
    length" is exact even with only two copies; without it the scorer falls back
    to a majority vote among the copies.

    Advisory only: the scan reads files on disk to rate audio quality, which can
    be slow or unavailable (network mounts, a moved file). A failure degrades to
    "no recommendation" rather than breaking the duplicate list, because the
    BADGE is the primary feature and this is the enhancement.
    """
    try:
        from services.metadata.duplicate_version_service import (
            recommend_duplicate_keeper,
        )

        return recommend_duplicate_keeper(tracks, expected_duration)
    except Exception as exc:
        logger.debug("Duplicate version recommendation failed", error=str(exc))
        return {
            "keep_track_id": "",
            "remove_track_ids": [],
            "confidence": "n/a",
            "reason": "Could not evaluate these copies.",
            "copies": [],
        }


def find_duplicate_tracks(artist: str, album: str) -> dict[str, Any]:
    """Tracks on THIS album that exist more than once (downloaded twice).

    Same duplicate definition the artist-corrections page uses
    (``artist_service``): same ``(title, track_artist, track_number,
    disc_number)`` within the album, at least two rows, and file names that
    do not carry DIFFERENT version keywords (an instrumental cut beside the
    vocal is a variant, not a double download — see
    ``helpers.normalization_service.file_version_variant_key``).

    Returns the groups plus a flat id list so the album page can flag the
    matching tracklist rows by ``data-track-id``.
    """
    # ⚠️ The scope is year-TOLERANT, not year-BLIND. ``_album_key`` alone made
    # two genuinely different releases collide: "2001 - MTV2 Handpicked" and
    # "2002 - MTV2 Handpicked" reduce to the same key, so a track present on
    # both was flagged "Duplicate ×2" on a page that only renders the requested
    # album's rows — a badge with nothing to point at. ``_album_scope_matches``
    # still matches a year on one side only, but keeps different years apart.
    with db_session() as session:
        rows = session.execute(
            text(
                "SELECT id, album, title, track_number, disc_number, file_path, "
                "COALESCE(NULLIF(artist, ''), '—') AS track_artist, "
                # Columns the version recommendation scores on. ``bitrate`` is
                # NOT here because it is not a stored column — file quality is
                # read from the file itself by the shared scorer.
                "duration, stars, recording_mbid, isrc, musicbrainz_albumid, "
                "year, composer, writer, genres, recordlabel, last_scanned "
                "FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            ),
            {"artist": artist},
        ).mappings().all()

    from db.repositories.tracks import dedupe_track_rows_by_file_path

    scoped_rows = []
    for raw in rows:
        row = dict(raw)
        if not _album_scope_matches(album, str(row.get("album") or "")):
            continue
        scoped_rows.append(row)

    # ⭐ TWO ROWS, ONE FILE, IS NOT "DOWNLOADED TWICE".
    #
    # A re-import with changed metadata can leave two rows pointing at the same
    # path. Grouping them by title/position made the album page show the song
    # twice with an identical path and a "Duplicate ×2" badge whose delete
    # action would have removed the file the surviving row still references.
    # Collapse by path first; the extra ROW is for the duplicate-row cleanup.
    scoped_rows = dedupe_track_rows_by_file_path(scoped_rows)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in scoped_rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        key = (
            title,
            str(row.get("track_artist") or ""),
            str(row.get("track_number") or "").strip(),
            str(row.get("disc_number") or "").strip(),
        )
        grouped.setdefault(key, []).append(row)

    duplicates: list[dict[str, Any]] = []
    #: Expected length per normalised title, from the RELEASE's own tracklist
    #: (the missing list). This is the "correct track length" a duplicate
    #: recommendation should be judged against.
    _expected_duration: dict[str, float] = {}
    try:
        with db_session() as session:
            for md in session.execute(
                text(
                    "SELECT title, duration FROM missing_album_tracks "
                    "WHERE LOWER(artist_name) = LOWER(:artist) "
                    "  AND LOWER(album_name) = LOWER(:album) "
                    "  AND COALESCE(ignored, FALSE) = FALSE "
                    "  AND COALESCE(duration, 0) > 0"
                ),
                {"artist": artist, "album": album},
            ).mappings().all():
                key = _title_match_key(str(md.get("title") or ""))
                if key:
                    _expected_duration.setdefault(key, float(md.get("duration") or 0))
    except Exception as exc:
        logger.debug("Expected-duration lookup failed", error=str(exc))

    for (title, track_artist, track_number, disc_number), tracks in grouped.items():
        if len(tracks) < 2:
            continue
        variant_keys = {file_version_variant_key(t.get("file_path")) for t in tracks}
        if len(variant_keys) > 1:
            # Different file-name variants (vocal vs instrumental, …): these
            # are different recordings of one title, not a double download.
            continue
        duplicates.append({
            "title": title,
            "track_artist": track_artist,
            "track_number": track_number,
            "disc_number": disc_number,
            "count": len(tracks),
            "track_ids": [str(t.get("id") or "") for t in tracks],
            # WHICH copy to keep, and why. A duplicate badge that only says
            # "×2" leaves the user to compare durations and tags by eye; the
            # recommendation names the better copy and the reason, and NEVER
            # deletes anything.
            "recommendation": _version_recommendation(
                tracks, _expected_duration.get(_title_match_key(title), 0.0)
            ),
        })

    # ``disc_number``/``track_number`` are TEXT (and stored as TEXT in this
    # dict), so a tuple sort orders "10" before "2" — the shared key makes it
    # numeric, with unnumbered groups after numbered ones.
    duplicates.sort(key=album_track_sort_key)
    flat_ids = [tid for group in duplicates for tid in group["track_ids"]]

    if duplicates:
        logger.debug(
            "Duplicate tracks found on album",
            artist=artist, album=album,
            groups=len(duplicates), rows=len(flat_ids),
        )

    return {
        "duplicates": duplicates,
        "duplicate_track_ids": flat_ids,
        "duplicate_group_count": len(duplicates),
        # Redundant copies beyond the first of each group — the same number
        # the corrections page reports as its duplicate count.
        "duplicate_extra_count": sum(int(g["count"]) - 1 for g in duplicates),
    }


def _as_text(value: Any) -> str:
    """Normalise any scalar to a trimmed string (``None`` -> ``""``)."""
    return "" if value is None else str(value).strip()


def persist_missing_from_comparison(
    artist: str,
    album: str,
    comparison: dict[str, Any],
) -> int:
    """Persist the MISSING tracks from an already-computed comparison.

    ⭐ WHY THIS EXISTS. ``get_missing_tracks`` derives the missing set itself, by
    resolving the release and fetching its metadata — a MusicBrainz release
    SEARCH plus a release fetch when the album has no stored MBID. The album
    page's Compare already HAS the comparison in hand (``compare_musicbrainz_release``
    returns a ``comparison`` list whose unmatched entries are exactly the missing
    tracks), so re-deriving it would pay those calls a second time — and
    MusicBrainz is throttled to 1 req/s, shared with any running scan.

    Falls back to the derivation-free path: it only reads the local tracklist.
    Queue status is consulted nowhere as a reason to hide a track —
    ``in_library`` is the sole visibility gate — because a row marked delivered
    that never landed in this album produced tracks that were neither listed
    nor editable.

    Returns the number of rows written. Raises only for a genuine DB failure;
    the caller decides whether that is fatal (it is not — the comparison the
    user is looking at remains valid).
    """
    comparison_rows = (comparison or {}).get("comparison") or []
    release_id = _as_text(
        (comparison or {}).get("mb_release_mbid")
        or (comparison or {}).get("release_mbid")
    )
    release_year = (
        (comparison or {}).get("mb_original_release_year")
        or (comparison or {}).get("mb_year")
        or ""
    )

    missing: list[dict[str, Any]] = []
    for entry in comparison_rows:
        if entry.get("matched"):
            continue
        title = _as_text(entry.get("mb_title"))
        if not title:
            continue

        missing.append({
            "title": title,
            "track_number": entry.get("mb_track_number"),
            "disc_number": entry.get("mb_disc_number", 1),
            "recording_mbid": entry.get("mb_recording_mbid"),
            "track_artist": entry.get("mb_artist") or artist,
            "year": release_year,
            "release_id": release_id,
            "duration": entry.get("mb_duration"),
        })

    _persist_missing_tracks(artist, album, missing)
    logger.info(
        "Persisted missing tracks from a comparison",
        artist=artist, album=album, missing=len(missing),
    )
    return len(missing)


def import_csv_tracklist(artist: str, album: str, csv_text: str) -> dict[str, Any]:
    """Use a user-uploaded CSV tracklist as the album's missing-track source.

    For a custom album that is not on MusicBrainz/Discogs, the CSV stands in
    for the release: every CSV track the library does not yet have is
    computed and persisted to ``missing_album_tracks`` (the same table the
    MusicBrainz release comparison writes), so the album page's existing
    missing list + "Download Missing Tracks" flow just works.

    Also marks the album ``album_lookup_skip = '1'`` — the user is telling
    the scan "this album has no release to look up", so the scan's MB/Discogs
    album lookups and its own missing-track refresh are skipped for it (the
    refresh would otherwise DELETE these CSV rows as "not in the MB release").

    CSV format (liberal on purpose):

      * a header row is skipped when any cell looks like a field name
        (``title``/``name``/``track``/``track number``/``#``/``artist``/
        ``disc``/``duration``); headers map by name;
      * otherwise POSITIONAL: ``title`` alone, or ``track_number,title``, or
        ``disc,track_number,title`` — trailing columns (``artist``,
        ``duration``) are accepted when present;
      * an Exportify-style ``#5 - Song`` cell is normalised (the ``#`` form
        is treated as the track number).

    Returns ``{"success", "imported", "missing_count", "missing_tracks",
    "library_count", "rows_read", "errors"}``.
    """
    # Stage timings: the import is a handful of DB round trips, so a slow one
    # is a wait (pool checkout / row lock behind a running scan), not CPU. The
    # summary line names which stage paid it instead of leaving the operator
    # with a bare "Request timed out".
    import time as _time

    timings: dict[str, int] = {}
    _t = _time.perf_counter()

    def _lap(name: str) -> None:
        nonlocal _t
        _now = _time.perf_counter()
        timings[f"{name}_ms"] = round((_now - _t) * 1000)
        _t = _now

    rows, errors = _parse_csv_tracklist(csv_text)
    _lap("parse")

    # Reserve the skip flag BEFORE the persist: even a 0-row CSV must stop the
    # scan's MB refresh from wiping the album's existing missing list.
    try:
        from services.metadata.album_lookup_skip_service import set_album_lookup_skip
        set_album_lookup_skip(artist, album, True)
    except Exception as exc:
        logger.debug("Album lookup-skip flag write failed during CSV import", error=str(exc))
    _lap("flag")

    with db_session() as session:
        album_key = _album_key(album)

        def _rows_in_album(rs):
            return [dict(r) for r in rs if _album_key(str(r.get("album") or "")) == album_key]

        library_rows = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, mbid, album, duration FROM tracks "
                "WHERE LOWER(TRIM(COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)))) = LOWER(:artist)"
            ),
            {"artist": artist},
        ).mappings().all()
        library_rows = _rows_in_album(library_rows)
        library_count = len(library_rows)
    _lap("library")

    (
        queued_titles,
        queued_positions,
        queued_artist_titles,
        queued_generic_titles,
    ) = _queued_coverage(
        artist,
        album,
        track_artists={
            str(row.get("artist") or "").strip()
            for row in rows
            if str(row.get("artist") or "").strip()
        },
    )
    _lap("queue")

    lib_norm = {_title_match_key(str(r.get("title") or "")) for r in library_rows if r.get("title")}
    lib_by_position: dict[tuple[int, str], str] = {}
    for r in library_rows:
        disc = _disc_of(r.get("disc_number"))
        tn = str(r.get("track_number") or "").strip()
        if tn:
            lib_by_position[(disc, tn)] = str(r.get("title") or "")

    missing: list[dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        norm = _title_match_key(title)
        tn = str(row.get("track_number") or "").strip()
        disc = _disc_of(row.get("disc_number"))
        if norm in lib_norm:
            continue
        position_key = (disc, tn) if tn else None
        if position_key and position_key in lib_by_position:
            continue
        _track_artist_key = _artist_match_key(row.get("artist") or artist)
        if norm in queued_titles or norm in queued_generic_titles:
            continue
        if (queued_artist_titles.get((_track_artist_key, norm))):
            continue
        if tn:
            qd = disc if disc else 1
            if (qd, tn) in queued_positions:
                continue
        missing.append({
            "title": title,
            "track_number": tn or None,
            "disc_number": disc,
            "track_artist": str(row.get("artist") or artist),
            "year": str(row.get("year") or "") or None,
            "duration": row.get("duration"),
            "release_id": None,
            "recording_mbid": None,
        })
    _lap("match")

    _persist_missing_tracks(artist, album, missing)
    _lap("persist")
    logger.info(
        "Persisted missing tracks from uploaded CSV",
        artist=artist, album=album, rows_read=len(rows), missing=len(missing),
        library_count=library_count, total_ms=sum(timings.values()), **timings,
    )
    return {
        "success": True,
        "imported": len(missing),
        "missing_count": len(missing),
        "missing_tracks": missing,
        "library_count": library_count,
        "rows_read": len(rows),
        "errors": errors,
    }


_CSV_HEADER_CELLS = {
    "title", "name", "track", "track title", "song", "song title",
    "track name", "song name",
    "track number", "track #", "#", "num", "number", "no",
    "artist", "track artist", "disc", "disc number", "disk",
    "duration", "length", "time", "year", "album", "album artist",
}


def _parse_csv_tracklist(csv_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse a liberal CSV tracklist into ``{title, track_number, disc_number,
    artist, year, duration}`` rows. Returns ``(rows, errors)``."""
    import csv as _csv
    import io as _io

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    text = str(csv_text or "")
    if not text.strip():
        return rows, errors

    try:
        reader = list(_csv.reader(_io.StringIO(text)))
    except Exception as exc:
        return rows, [f"could not parse CSV: {exc}"]

    # Skip a leading header row when any cell names a field.
    header_idx: int | None = None
    if reader:
        sample = [str(c or "").strip().lower() for c in (reader[0] or [])]
        if sample and any(c in _CSV_HEADER_CELLS for c in sample):
            header_idx = 0

    col_map: dict[int, str] = {}
    if header_idx is not None:
        for i, cell in enumerate(reader[header_idx]):
            name = " ".join(str(cell or "").strip().lower().split())
            # ⚠️ "TRACK NAME" IS EXPORTIFY'S HEADER and used to map to NOTHING,
            # so an Exportify CSV (the format a user is most likely to have)
            # imported ZERO rows with no error — header detection fired off
            # another cell, ``title`` stayed empty, and every row was skipped
            # silently. "Track" and "Song Name" are the other common spellings.
            if name in (
                "title", "name", "track title", "song", "song title",
                "track name", "song name", "track",
            ):
                col_map[i] = "title"
            elif name in ("artist", "track artist", "artists", "artist name",
                          "artist name(s)"):
                col_map[i] = "artist"
            elif name in ("track number", "track #", "num", "number", "no", "#",
                          "track no", "track no."):
                col_map[i] = "track_number"
            elif name in ("disc", "disc number", "disk", "disc no", "disc no."):
                col_map[i] = "disc_number"
            elif name in ("duration", "length", "time"):
                col_map[i] = "duration"
            elif name == "year":
                col_map[i] = "year"
            # "album"/"album artist" are accepted but deliberately unused: the
            # album identity is THIS page's artist+album, not a per-row value.

    for row_num, raw in enumerate(reader):
        if row_num == header_idx:
            continue
        cells = [str(c or "").strip() for c in (raw or [])]
        if not any(cells):
            continue

        if header_idx is not None:
            entry: dict[str, Any] = {}
            for i, cell in enumerate(cells):
                field = col_map.get(i)
                if not field:
                    continue
                if field == "title":
                    entry["title"] = cell.lstrip("#").strip()
                elif field == "track_number":
                    entry["track_number"] = cell.lstrip("#").strip()
                else:
                    entry[field] = cell
            if entry.get("title"):
                rows.append(_coerce_csv_row(entry))
            continue

        # Positional: title | number,title | disc,number,title | + artist/duration,
        # plus Exportify-style "#N - Title" cells.
        first = cells[0].lstrip("#").strip()
        if not first:
            continue
        # "#5 - Song Five" → (5, "Song Five").
        _hash_split = re.match(r"^(\d+)\s*-\s*(.+)$", first)
        if first.isdigit() and len(cells) >= 2:
            if len(cells) >= 3 and cells[1].isdigit():
                # disc,number,title [, artist]
                entry = {"disc_number": cells[0], "track_number": cells[1], "title": cells[2]}
                if len(cells) >= 4 and cells[3]:
                    entry["artist"] = cells[3]
            else:
                # number,title [, artist] [, duration]
                entry = {"track_number": first, "title": cells[1]}
                if len(cells) >= 3 and cells[2]:
                    if cells[2].isdigit():
                        entry["duration"] = cells[2]
                    else:
                        entry["artist"] = cells[2]
                if len(cells) >= 4 and cells[3] and cells[3].isdigit():
                    entry["duration"] = cells[3]
            rows.append(_coerce_csv_row(entry))
        elif _hash_split:
            entry = {"track_number": _hash_split.group(1), "title": _hash_split.group(2)}
            rows.append(_coerce_csv_row(entry))
        else:
            entry = {"title": first}
            if len(cells) >= 2 and cells[1]:
                # title,artist | title,duration
                if cells[1].isdigit():
                    entry["duration"] = cells[1]
                else:
                    entry["artist"] = cells[1]
            if len(cells) >= 3 and cells[2] and cells[2].isdigit():
                entry["duration"] = cells[2]
            rows.append(_coerce_csv_row(entry))

    return rows, errors


def _coerce_csv_row(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise one parsed CSV row (numbers stripped to plain forms)."""
    tn = str(entry.get("track_number") or "").lstrip("#").strip()
    disc = str(entry.get("disc_number") or "").strip()
    if not disc:
        # A "1-08" style track number splits disc+track.
        m = re.match(r"^(\d+)[-.](\d+)$", tn)
        if m:
            disc, tn = m.group(1), m.group(2)
    entry["track_number"] = tn or None
    entry["disc_number"] = int(disc) if disc.isdigit() else 1
    duration = str(entry.get("duration") or "").strip()
    if duration and not duration.isdigit():
        # "3:45" → seconds; "3:04:15" (h:m:s) supported.
        parts = [p for p in duration.split(":") if p.strip().isdigit()]
        if 1 <= len(parts) <= 3:
            secs = 0
            for p in parts:
                secs = secs * 60 + int(p)
            entry["duration"] = secs
    return entry


def get_missing_tracks_from_db(artist: str, album: str) -> dict[str, Any]:
    """The persisted missing-track list for one album — DATABASE ONLY.

    ``get_missing_tracks`` above resolves the album's MusicBrainz release and
    recomputes the set. That work now belongs to the SCAN (``scan_stage_runner``
    refreshes it per album, where the release metadata is normally already a
    cache hit), because the artist page requests this once PER OWNED ALBUM on
    page load and every call was a MusicBrainz release fetch — plus a release
    SEARCH when the album had no stored MBID. A page must be a read-only view of
    what the scan has resolved.

    ``mb_total`` is not stored per row; it is reported as the library count plus
    the number of missing tracks, so the UI's totals still add up while never
    claiming a release size the database does not know.
    """
    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT title, track_number, disc_number, track_artist, year, "
                    "       release_id, recording_mbid, duration, "
                    "       COALESCE(is_bonus, FALSE) AS is_bonus "
                    "FROM missing_album_tracks "
                    "WHERE LOWER(artist_name) = LOWER(:artist) "
                    "  AND LOWER(album_name) = LOWER(:album) "
                    "  AND COALESCE(ignored, FALSE) = FALSE "
                    "ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, '999')"
                ),
                {"artist": artist, "album": album},
            ).mappings().all()

            library_count = session.execute(
                text(
                    # DISTINCT recordings, not rows: a double download puts two
                    # rows on one album, and the count is shown next to the
                    # release size — so a duplicate reported "N+1 of N" and made
                    # the album look like it had a stray extra track.
                    "SELECT COUNT(*) FROM ("
                    "  SELECT DISTINCT "
                    "    LOWER(TRIM(COALESCE(title, ''))), "
                    "    LOWER(TRIM(COALESCE(artist, ''))), "
                    "    COALESCE(TRIM(CAST(disc_number AS TEXT)), ''), "
                    "    COALESCE(TRIM(CAST(track_number AS TEXT)), '') "
                    "  FROM tracks "
                    "  WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "    AND LOWER(COALESCE(album, '')) = LOWER(:album)"
                    ") AS distinct_tracks"
                ),
                {"artist": artist, "album": album},
            ).scalar() or 0

            # ⭐ QUEUE STATE, READ ON PAGE LOAD.
            #
            # Reported: "when selecting to download a track, it shows a tick …
            # but when the page is refreshed, it doesn't show it's in the
            # queue." The tick was pure DOM state: ``queue_status`` was only
            # ever computed by the live ``refresh=1`` path and never selected
            # here, so a reload always rendered "Missing" again.
            #
            # A queued track is matched by (title, disc, track) within this
            # album — the same identity ``_queued_coverage`` uses — and the row
            # stops showing as queued once the queue row reaches a terminal
            # imported state (it is then a library track, not a missing one).
            queued = _queued_status_for_album(session, artist, album)

            # How many tracks the user dismissed.  The page must be able to
            # say "N more not shown" — otherwise a rejected pair (ignored =
            # TRUE, which NOTHING ever clears) looks identical to a release
            # that never had those tracks.
            dismissed = session.execute(
                text(
                    "SELECT COUNT(*) FROM missing_album_tracks "
                    "WHERE LOWER(artist_name) = LOWER(:artist) "
                    "  AND LOWER(album_name) = LOWER(:album) "
                    "  AND COALESCE(ignored, FALSE) = TRUE"
                ),
                {"artist": artist, "album": album},
            ).scalar() or 0

            # ⚠️ WHAT THE LIBRARY ALREADY HOLDS — the anti-join for the rows
            # below. A downloaded track must stop rendering as missing the
            # moment its `tracks` row exists; see the note at the filter.
            #
            # Defensive on its own: this is a VIEW improvement, and a failure
            # here must degrade to "no anti-join" rather than blank the whole
            # missing list (which the outer handler would do).
            owned_rows: list[Any] = []
            try:
                owned_rows = session.execute(
                    text(
                        "SELECT title, track_number, disc_number FROM tracks "
                        "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                        "  AND LOWER(COALESCE(album, '')) = LOWER(:album) "
                        "  AND COALESCE(file_path, '') NOT LIKE '%__queued_for_download__%'"
                    ),
                    {"artist": artist, "album": album},
                ).mappings().all()
            except Exception as _owned_exc:
                logger.debug(
                    "Missing-track anti-join unavailable",
                    artist=artist, album=album, error=str(_owned_exc),
                )
    except Exception as exc:
        logger.error(
            "Get persisted missing tracks failed",
            artist=artist, album=album, error=str(exc),
        )
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": 0}

    missing = [dict(r) for r in rows]
    # ⚠️⚠️ A DOWNLOADED TRACK MUST STOP SHOWING AS MISSING — IMMEDIATELY.
    #
    # Reported: "When a missing item is downloaded, it doesn't auto heal. I need
    # to manually look up a release again."
    #
    # This function returns the PERSISTED rows verbatim. Rows are only ever
    # deleted by ``_persist_missing_tracks``, which runs from a live recompute —
    # i.e. from a full/metadata scan of the album, or the manual
    # ``refresh=1`` "Lookup MBID". Nothing runs either after an import, so the
    # row outlives the download: the page keeps the yellow badge, and because an
    # ``imported`` queue row deliberately stops matching
    # ``_queued_status_for_album``, the track even loses its "in queue" tick and
    # re-renders as a fresh "Missing" row with a live Download button.
    #
    # The fix is an ANTI-JOIN against ``tracks`` using the SAME identity the
    # persistence layer deletes on (``_missing_row_key``): disc + track number +
    # normalised title, plus the title alone. It needs no MusicBrainz call, so a
    # page load stays a pure DB read.
    #
    # This heals the VIEW; the persisted row is removed by the next recompute
    # (the scan, or the album's Lookup MBID). Until then the two agree, because
    # both are filtered by the same test.
    try:
        _owned_titles: set[str] = set()
        _owned_positions: set[tuple[str, str]] = set()
        for _row in owned_rows:
            _title_key = _title_match_key(str(_row.get("title") or ""))
            if _title_key:
                _owned_titles.add(_title_key)
            _num = str(_row.get("track_number") or "").strip()
            if _num and _title_key:
                _owned_positions.add(
                    (f"{_disc_of(_row.get('disc_number'))}|{_num}", _title_key)
                )
    except Exception:
        _owned_titles, _owned_positions = set(), set()

    if _owned_titles or _owned_positions:
        _kept: list[dict[str, Any]] = []
        _healed = 0
        for _row in missing:
            _num = str(_row.get("track_number") or "").strip()
            _row_title = _title_match_key(str(_row.get("title") or ""))
            _pos_key = f"{_disc_of(_row.get('disc_number'))}|{_num}"
            # Title alone is enough: the release's track and the local copy can
            # legitimately sit at different numbers (a bonus edition, a rip
            # numbered across the whole set), which is the same rule the live
            # matcher applies. The position is a second chance, not a gate.
            if _row_title and _row_title in _owned_titles:
                _healed += 1
                continue
            if _num and (_pos_key, _row_title) in _owned_positions:
                _healed += 1
                continue
            _kept.append(_row)
        if _healed:
            logger.info(
                "Missing-track list healed against the library",
                artist=artist, album=album, healed=_healed, remaining=len(_kept),
            )
        missing = _kept

    # ``track_number`` is TEXT here too: ``COALESCE(track_number, '999')``
    # still compares "10" < "2", which is the reported missing-track order
    # after importing from the album page.
    missing.sort(key=album_track_sort_key)
    for row in missing:
        status = queued.get(_queued_key(row), "")
        if status:
            row["queue_status"] = status
    return {
        "missing_tracks": missing,
        "missing_count": len(missing),
        "bonus_count": sum(1 for m in missing if m.get("is_bonus")),
        "missing_core_count": sum(1 for m in missing if not m.get("is_bonus")),
        "mb_total": int(library_count) + len(missing),
        "library_count": int(library_count),
        # Only the gate this path can actually KNOW about — the others need a
        # MusicBrainz release, and a page load must stay a pure DB read.
        "excluded": {"rejected": int(dismissed)},
    }


def _queued_key(row: Any) -> tuple[str, int, str]:
    """The identity a queue row and a missing row share: (title, disc, track)."""
    if hasattr(row, "get"):
        title = _title_match_key(str(row.get("title") or ""))
        disc = _disc_of(row.get("disc_number"))
        number = str(row.get("track_number") or "").strip()
    else:  # pragma: no cover - defensive
        title, disc, number = "", 1, ""
    return (title, int(disc or 1), number)


#: Terminal queue statuses — the track is IN THE LIBRARY, so it is no longer
#: "queued" from the album page's point of view. Everything else that is not a
#: failure means "in flight".
_QUEUE_TERMINAL_DONE = frozenset({"imported", "in_collection", "completed"})
_QUEUE_FAILED = frozenset({"failed", "removed", "cancelled", "deleted"})


def _queued_status_for_album(
    session: Any, artist: str, album: str,
) -> dict[tuple[str, int, str], str]:
    """``{(title, disc, track): status}`` for this album's in-flight downloads.

    Read on PAGE LOAD so the "download" tick survives a refresh. A row is
    included while it is still in flight (queued / searching / downloading /
    processing / moving / matched / backed_off / pending_*); once it imports it
    is a library track and the page shows it in the tracklist instead, so the
    "in queue" state correctly disappears.
    """
    statuses = sorted(
        {"queued", "searching", "downloading", "processing", "moving", "matched",
         "backed_off", "pending_release", "pending_match", "awaiting_selection",
         "possible_duplicate", "queried", "copy_recommended", "duplicate"}
    )
    placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
    try:
        rows = session.execute(
            text(
                "SELECT title, disc_number, track_number, status "
                "FROM download_queue "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "  AND LOWER(COALESCE(album, '')) = LOWER(:album) "
                f"  AND status IN ({placeholders})"
            ),
            {
                "artist": artist, "album": album,
                **{f"s{i}": s for i, s in enumerate(statuses)},
            },
        ).mappings().all()
    except Exception as exc:
        logger.debug("Album queue-status read failed", artist=artist, album=album, error=str(exc))
        return {}
    out: dict[tuple[str, int, str], str] = {}
    for row in rows:
        key = _queued_key(dict(row))
        status = str(row.get("status") or "").strip()
        if key[0] and status:
            out.setdefault(key, status)
    return out


def _missing_row_key(row: Any) -> tuple[str, int, str]:
    """Build the ``(track_number, disc_number, title_key)`` identity for a row.

    The title component is normalised with ``_title_match_key`` rather than
    compared raw.  Previously the rejected-set used the RAW title while the
    computed set used the raw title too — consistent, but punctuation- and
    case-sensitive, so a rejected "Don't Stop" stopped matching once
    MusicBrainz returned "Don’t Stop" (curly apostrophe) and the track
    reappeared on the missing list despite having been dismissed.
    """
    if hasattr(row, "get"):
        tn = row.get("track_number")
        disc = row.get("disc_number")
        title = row.get("title")
    else:
        tn, disc, title = None, None, None
    return (
        str(tn or "").strip(),
        int(disc or 1),
        _title_match_key(str(title or "")),
    )


def _persist_missing_tracks(artist: str, album: str, missing: list[dict[str, Any]]) -> None:
    """Upsert the computed missing tracks into ``missing_album_tracks``.

    Any previously-persisted row for this artist/album that is NOT in the
    current missing set (e.g. the track has since been downloaded, or the
    release changed) is removed so stale rows never linger.  Rows that were
    rejected (``ignored = TRUE``) are preserved.
    """
    with db_session() as session:
        existing = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, track_artist, ignored, "
                "       COALESCE(is_bonus, FALSE) AS is_bonus "
                "FROM missing_album_tracks "
                "WHERE LOWER(artist_name) = LOWER(:artist) "
                "  AND LOWER(album_name) = LOWER(:album)"
            ),
            {"artist": artist, "album": album},
        ).mappings().all()

        current_keys = {_missing_row_key(m) for m in missing}

        for row in existing:
            if _missing_row_key(row) not in current_keys and not row.get("ignored"):
                session.execute(
                    text("DELETE FROM missing_album_tracks WHERE id = :id"),
                    {"id": row.get("id")},
                )

        # Rows already persisted for this album — including rejected ones —
        # must not be re-inserted.  ``ON CONFLICT DO NOTHING`` without a
        # conflict target only suppresses a genuine constraint violation, so
        # if ``missing_album_tracks`` has no unique index covering
        # (artist, album, title, disc) every scan appended duplicates.
        # Filtering here makes the insert correct regardless of whether that
        # constraint exists.
        existing_keys = {_missing_row_key(row) for row in existing}
        existing_by_key = {_missing_row_key(row): row for row in existing}

        inserted = 0
        refreshed = 0
        for m in missing:
            key = _missing_row_key(m)
            if key in existing_keys:
                # ⚠️ REFRESH the artist even though the row itself survives.
                # This branch used to ``continue`` on sight, so a row written
                # BEFORE the queue started storing the recording's own credit
                # kept its placeholder for ever: the album page read
                # ``track_artist`` and queued the track as "Various Artists"
                # (reported: "missing tracks are still adding various artists
                # to the queue search" — and the report was right that it is
                # the way missing tracks are POPULATED).
                #
                # The computed value wins, with one guard: a placeholder must
                # never overwrite a real performer a previous run stored. MB
                # returns no credit for some recordings, and that fallback is
                # the album artist — losing a good value to it would put the
                # bug back one step.
                _new_artist = str(m.get("track_artist") or "").strip()
                _old_artist = str(
                    existing_by_key[key].get("track_artist") or ""
                ).strip()
                if _new_artist and _new_artist != _old_artist and (
                    not _old_artist
                    or not is_track_artist_placeholder(_new_artist)
                ):
                    session.execute(
                        text(
                            "UPDATE missing_album_tracks "
                            "SET track_artist = :track_artist WHERE id = :id"
                        ),
                        {
                            "track_artist": _new_artist,
                            "id": existing_by_key[key].get("id"),
                        },
                    )
                    refreshed += 1
                _new_bonus = bool(m.get("is_bonus"))
                _old_bonus = bool(existing_by_key[key].get("is_bonus"))
                if _new_bonus != _old_bonus:
                    session.execute(
                        text(
                            "UPDATE missing_album_tracks "
                            "SET is_bonus = :is_bonus WHERE id = :id"
                        ),
                        {
                            "is_bonus": _new_bonus,
                            "id": existing_by_key[key].get("id"),
                        },
                    )
                    refreshed += 1
                continue

            tn = str(m.get("track_number") or "").strip()
            disc = _disc_of(m.get("disc_number"))
            title = str(m.get("title") or "").strip()
            if not title:
                continue

            session.execute(
                text("""
                    INSERT INTO missing_album_tracks
                        (artist_name, album_name, title, track_number, disc_number,
                         track_artist, year, release_id, recording_mbid, duration,
                         is_bonus)
                    VALUES
                        (:artist, :album, :title, :track_number, :disc_number,
                         :track_artist, :year, :release_id, :recording_mbid, :duration,
                         :is_bonus)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "artist": artist,
                    "album": album,
                    "title": title,
                    "track_number": tn or None,
                    "disc_number": disc,
                    "track_artist": m.get("track_artist"),
                    "year": str(m.get("year") or "") or None,
                    "release_id": m.get("release_id"),
                    "recording_mbid": m.get("recording_mbid"),
                    "duration": m.get("duration"),
                    "is_bonus": bool(m.get("is_bonus")),
                },
            )
            existing_keys.add(key)
            inserted += 1

        session.commit()

    if refreshed:
        # One line per album so an operator can see the repair happen rather
        # than wonder why the queue is still searching for "Various Artists"
        # after a scan.
        logger.info(
            "[BACKGROUND_METADATA] Refreshed stale missing-track artists",
            artist=artist,
            album=album,
            refreshed=refreshed,
        )
    if inserted:
        logger.debug(
            "Persisted missing tracks",
            artist=artist, album=album, inserted=inserted, total=len(missing),
        )


def _rejected_missing_titles(artist: str, album: str) -> set[tuple[str, int, str]]:
    """Return the set of ``(track_number, disc_number, title_key)`` rejected rows."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT title, track_number, disc_number
                    FROM missing_album_tracks
                    WHERE LOWER(artist_name) = LOWER(:artist)
                      AND LOWER(album_name) = LOWER(:album)
                      AND ignored = TRUE
                """),
                {"artist": artist, "album": album},
            ).mappings().all()
        return {_missing_row_key(r) for r in rows}
    except Exception as exc:
        logger.debug(
            "Rejected missing-title fetch failed",
            artist=artist, album=album, error=str(exc),
        )
        return set()


def get_title_mismatches(artist: str, album: str) -> dict[str, Any]:
    """Compare library track titles against the full MusicBrainz release tracklist."""
    album_key = _album_key(album)

    with db_session() as session:
        mb_rows = session.execute(
            text(
                "SELECT musicbrainz_album_mbid, album FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != ''"
            ),
            {"artist": artist},
        ).mappings().all()
        mb_rows = [r for r in mb_rows if _album_key(str(r.get("album") or "")) == album_key]

        # FIXED: this was ``mb_rows[0][0]``.  ``.mappings().all()`` returns
        # RowMappings, which are keyed by COLUMN NAME — a positional index
        # raises "Could not locate column in row for column '0'".
        # ``get_missing_tracks`` above already carried a comment warning
        # about exactly this; this function still had the bug, so the title
        # comparison raised on every album that HAD an MBID (the only albums
        # it can actually compare).
        mb_mbid = str(_row_value(mb_rows[0], "musicbrainz_album_mbid") or "") if mb_rows else None

        library_rows = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, duration, album FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            ),
            {"artist": artist},
        ).mappings().all()
        library_rows = [
            dict(r) for r in library_rows
            if _album_key(str(r.get("album") or "")) == album_key
        ]

    if not mb_mbid:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    try:
        mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    except Exception as exc:
        logger.debug(
            "MB release metadata fetch failed",
            release_mbid=mb_mbid, artist=artist, album=album, error=str(exc),
        )
        mb_release = None

    if not mb_release:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    lib_by_tracknum: dict[tuple[Any, Any], dict[str, Any]] = {}
    for r in library_rows:
        tn = r.get("track_number")
        dn = r.get("disc_number") or 1
        try:
            tn_int = int(str(tn).split("/")[0].strip()) if tn else None
        except (ValueError, TypeError):
            tn_int = None
        lib_by_tracknum[(int(dn or 1), tn_int)] = {
            "id": r.get("id"),
            "title": r.get("title"),
            "duration": r.get("duration"),
        }

    mismatches = []
    for mt in mb_release.get("tracks", []):
        mb_title = mt.get("title", "")
        mb_disc = mt.get("disc_number", 1)
        mb_num = mt.get("track_number")
        if not mb_title or mb_num is None:
            continue

        try:
            mb_tn = int(str(mb_num).split("/")[0].strip())
        except (ValueError, TypeError):
            continue

        # The library index is keyed on an INT disc number, so the MB disc
        # must be coerced the same way — a string "1" from the release
        # payload silently missed every lookup.
        try:
            mb_disc_key = int(str(mb_disc).split("/")[0].strip()) if mb_disc not in (None, "") else 1
        except (ValueError, TypeError):
            mb_disc_key = 1

        lib_entry = lib_by_tracknum.get((mb_disc_key, mb_tn))
        if not lib_entry:
            continue

        lib_title = lib_entry.get("title", "")
        if not lib_title:
            continue

        # Unicode-preserving comparison — Korean/CJK titles match
        # precisely instead of being erased by an ASCII-only strip.
        if _title_match_key(lib_title) == _title_match_key(mb_title):
            continue

        # A title differing ONLY by a version/cover marker is not a title
        # mismatch: "(Live)", "(Acoustic)", "(Remix)" and the cover detector's
        # "(Artist Cover)" describe a performance VARIANT the metadata model
        # already stores in dedicated columns, not different wording. This uses
        # the SAME key as the album-page Compare and the Lookup MBID review, so
        # all three agree — a second, disagreeing rule is what would let the
        # sections contradict each other.
        # ⚠️ Falls back to the Unicode-preserving key when the compare key is
        # EMPTY on either side: ``normalize_title_for_compare`` strips to ASCII,
        # and two Hangul titles would both reduce to "" and look identical,
        # silently hiding a real mismatch.
        compare_key_lib = normalize_title_for_compare(lib_title)
        compare_key_mb = normalize_title_for_compare(mb_title)
        if compare_key_lib and compare_key_mb:
            if compare_key_lib == compare_key_mb:
                continue
        elif _title_match_key(lib_title) == _title_match_key(mb_title):
            continue

        dur_tolerance = 5
        lib_dur = lib_entry.get("duration")
        mb_dur = mt.get("duration")
        mismatch_type = "title"
        try:
            if lib_dur and mb_dur and abs(float(lib_dur) - float(mb_dur)) > dur_tolerance:
                mismatch_type = "title_and_length"
        except (TypeError, ValueError):
            # A non-numeric duration is a title-only mismatch, not a crash.
            mismatch_type = "title"

        mismatches.append({
            "track_id": lib_entry["id"],
            "library_title": lib_title,
            "mb_title": mb_title,
            "track_number": mb_num,
            "disc_number": mb_disc,
            "mismatch_type": mismatch_type,
        })

    return {"mismatches": mismatches, "mismatch_count": len(mismatches), "library_count": len(library_rows)}
