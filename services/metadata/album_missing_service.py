"""Album detail/comparison services — missing tracks, title mismatches, library tracks.

Extracted from the old monolithic app.py.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    fetch_musicbrainz_release_metadata,
)
from api_clients.musicbrainz_http import escape_lucene_special_chars
from helpers.normalization_service import (
    file_version_variant_key,
    normalize_title_for_compare,
    normalize_title_for_lucene_query,
)

logger = structlog.get_logger(__name__)


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

    return [
        dict(r)
        for r in rows
        if _album_key(str(r.get("album") or "")) == album_key
    ]


def _queued_coverage(
    artist: str, album: str,
) -> tuple[dict[str, str], dict[tuple[int, str], str]]:
    """Queue rows that may deliver THIS album's tracks, keyed to their status.

    Returns ``(title_key -> status, (disc, track_number) -> status)``.

    ⚠️ ATTRIBUTION. The query is scoped to the ARTIST and the album is matched
    in Python, which on a **Various Artists** album spans the whole library —
    so a row whose ``album`` is empty used to slip past the
    ``if q_album and ...`` guard and grant coverage to EVERY album by that
    artist.  A row that does not name an album cannot be attributed to one, so
    it now contributes nothing; re-queueing such a track is harmless because
    ``find_blocking_queue_item`` de-duplicates by identity.
    """
    title_keys: dict[str, str] = {}
    positions: dict[tuple[int, str], str] = {}
    try:
        from db.engine import db_session as _q_session
        from sqlalchemy import text as _q_text

        _album_norm = _album_key(album)
        with _q_session() as session:
            q_rows = session.execute(
                _q_text("""
                    SELECT title, track_number, disc_number, status, album
                    FROM download_queue
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND status IN ('queued', 'searching', 'downloading',
                                     'processing', 'moving', 'imported',
                                     'in_collection', 'matched', 'completed')
                """),
                {"artist": artist},
            ).mappings().all() or []
        for q in q_rows:
            # Match the queue row's album against the requested album, tolerating
            # a leading year prefix ("2024 - 樂-STAR" ≈ "樂-STAR").
            q_album = str(q.get("album") or "").strip()
            if not q_album or _album_key(q_album) != _album_norm:
                continue
            status = str(q.get("status") or "queued")
            qt = q.get("title") or ""
            if qt:
                title_keys.setdefault(_title_match_key(qt), status)
            qtn = str(q.get("track_number") or "").strip()
            qd = int(q.get("disc_number") or 1) if q.get("disc_number") not in (None, "", "0", 0) else 1
            if qtn:
                positions.setdefault((qd, qtn), status)
    except Exception as _qexc:
        logger.debug("Download-queue coverage check failed", error=str(_qexc))
    return title_keys, positions


#: Queue statuses that mean the track has been DELIVERED (or is already owned),
#: so listing it as missing would be wrong: the file is in the library, or is
#: seconds away from being imported there.
_DELIVERED_QUEUE_STATUSES = frozenset(
    {"imported", "completed", "matched", "in_collection"}
)


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
                "SELECT id, title, track_number, disc_number, mbid, album FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            ),
            {"artist": artist},
        ).mappings().all()
        library_rows = _rows_in_album(library_rows)
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

    mb_tracks = mb_release.get("tracks", [])
    mb_total = len(mb_tracks)

    lib_norm = set()
    lib_by_position: dict[tuple[int, str], str] = {}
    for r in library_rows:
        title = r.get("title") or ""
        if title:
            lib_norm.add(_title_match_key(title))
        disc = int(r.get("disc_number") or 1) if r.get("disc_number") not in (None, "", "0", 0) else 1
        tn = str(r.get("track_number") or "").strip()
        if tn:
            lib_by_position[(disc, tn)] = title or ""

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
    queued_titles, queued_positions = _queued_coverage(artist, album)

    missing = []
    #: Why the rest of the release is NOT listed — one dict, four gates.
    #: Without it a track dropped by a gate is indistinguishable from one that
    #: never existed (the reported "1-9, 12, 13 render but 10 and 11 absent").
    excluded: dict[str, int] = {"in_library": 0, "queued": 0}
    for mt in mb_tracks:
        mb_title = mt.get("title", "")
        if not mb_title:
            continue
        norm = _title_match_key(mb_title)
        mb_disc = int(mt.get("disc_number") or 1)
        mb_num = str(mt.get("track_number") or "").strip()

        position_occupied = bool(mb_num and (mb_disc, mb_num) in lib_by_position)
        if position_occupied or norm in lib_norm:
            # ⚠️ Position counts even when the TITLE differs: a wrong track
            # sitting at disc 1 / track 10 makes 10 look present. That is the
            # gate most likely to have hidden the reported pair.
            excluded["in_library"] += 1
            continue

        queue_status = queued_titles.get(norm) or (
            queued_positions.get((mb_disc, mb_num)) if mb_num else None
        )
        if queue_status and queue_status in _DELIVERED_QUEUE_STATUSES:
            excluded["queued"] += 1
            continue

        entry = {
            "title": mb_title,
            "track_number": mt.get("track_number"),
            "disc_number": mt.get("disc_number", 1),
            "recording_mbid": mt.get("recording_mbid"),
            "track_artist": mt.get("artist") or artist,
            "year": mb_release.get("release_year") or mb_release.get("year"),
            "release_id": mb_mbid,
            "duration": mt.get("duration"),
        }
        if queue_status:
            entry["queue_status"] = queue_status
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
        "mb_total": mb_total,
        "library_count": library_count,
        # WHY the rest of the release is not listed, so the arithmetic is
        # self-checking: len(visible) + sum(excluded.values()) == mb_total.
        "excluded": excluded,
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
    album_key = _album_key(album)
    with db_session() as session:
        rows = session.execute(
            text(
                "SELECT id, album, title, track_number, disc_number, file_path, "
                "COALESCE(NULLIF(artist, ''), '—') AS track_artist "
                "FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            ),
            {"artist": artist},
        ).mappings().all()

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        if _album_key(str(row.get("album") or "")) != album_key:
            continue
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
        })

    duplicates.sort(key=lambda g: (g["disc_number"], g["track_number"], g["title"].casefold()))
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

    Falls back to the derivation-free path: it only reads the local tracklist to
    honour the download-queue coverage rule, so a track the queue has already
    DELIVERED is never re-listed as missing.  A track it has not delivered yet
    (queued / searching / downloading) IS listed — it carries no
    ``queue_status`` here because this path only decides what to persist, but
    the row survives a reload instead of vanishing with the queue stall.

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

    # Queue coverage — shared with ``get_missing_tracks`` so the two paths can
    # never disagree about what "handled" means.  Only a DELIVERED row is
    # skipped here: an undelivered one (queued / searching / downloading) is
    # kept, so the track survives a page reload instead of vanishing the
    # moment the user reloads after a lookup.
    queued_titles, queued_positions = _queued_coverage(artist, album)

    missing: list[dict[str, Any]] = []
    for entry in comparison_rows:
        if entry.get("matched"):
            continue
        title = _as_text(entry.get("mb_title"))
        if not title:
            continue
        mb_disc = int(entry.get("mb_disc_number") or 1)
        mb_num = str(entry.get("mb_track_number") or "").strip()
        norm = _title_match_key(title)

        queue_status = queued_titles.get(norm) or (
            queued_positions.get((mb_disc, mb_num)) if mb_num else None
        )
        if queue_status and queue_status in _DELIVERED_QUEUE_STATUSES:
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
                    "       release_id, recording_mbid, duration "
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
                    "SELECT COUNT(*) FROM tracks "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "  AND LOWER(COALESCE(album, '')) = LOWER(:album)"
                ),
                {"artist": artist, "album": album},
            ).scalar() or 0

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
    except Exception as exc:
        logger.error(
            "Get persisted missing tracks failed",
            artist=artist, album=album, error=str(exc),
        )
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": 0}

    missing = [dict(r) for r in rows]
    return {
        "missing_tracks": missing,
        "missing_count": len(missing),
        "mb_total": int(library_count) + len(missing),
        "library_count": int(library_count),
        # Only the gate this path can actually KNOW about — the others need a
        # MusicBrainz release, and a page load must stay a pure DB read.
        "excluded": {"rejected": int(dismissed)},
    }


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
                "SELECT id, title, track_number, disc_number, ignored "
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

        inserted = 0
        for m in missing:
            key = _missing_row_key(m)
            if key in existing_keys:
                continue

            tn = str(m.get("track_number") or "").strip()
            disc = int(m.get("disc_number") or 1)
            title = str(m.get("title") or "").strip()
            if not title:
                continue

            session.execute(
                text("""
                    INSERT INTO missing_album_tracks
                        (artist_name, album_name, title, track_number, disc_number,
                         track_artist, year, release_id, recording_mbid, duration)
                    VALUES
                        (:artist, :album, :title, :track_number, :disc_number,
                         :track_artist, :year, :release_id, :recording_mbid, :duration)
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
                },
            )
            existing_keys.add(key)
            inserted += 1

        session.commit()

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
