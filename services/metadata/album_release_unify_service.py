"""One physical album must carry ONE MusicBrainz release identity.

A folder assembled track-by-track can contain files matched to different
editions of the same release group (Korn — Untouchables: the Canadian release on
most tracks, a Japanese edition on the bonus remix, and one row whose release id
is actually the release-group UUID). Navidrome derives albums from
``MUSICBRAINZ_ALBUMID``, so that folder splits.

The rules here:

* unify only when every row shares ONE non-empty release group — a folder with
  two groups is two same-named albums and must not be merged;
* a release id equal to the group id is malformed and never a candidate;
* prefer the edition whose MusicBrainz track count matches the local track
  count (this is how a Japanese bonus-track edition wins over the standard one),
  then the id most rows already carry;
* fan the release id and its release-level fields to every row AND file;
* never touch recording ids, titles or positions.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

_RELEASE_FIELDS: tuple[str, ...] = (
    "barcode", "catalognumber", "releasecountry", "media", "recordlabel",
    "releasetype", "releasestatus", "releasedate", "originaldate",
    "originalyear", "disctotal", "tracktotal", "release_title", "release_year",
)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return _norm(value).casefold()


def _load_rows(artist: str, album: str) -> list[dict[str, Any]]:
    with db_session() as session:
        rows = session.execute(
            text(
                "SELECT id, file_path, musicbrainz_album_mbid, "
                "musicbrainz_albumid, musicbrainz_releasegroupid, "
                "disc_number, track_number, "
                + ", ".join(_RELEASE_FIELDS)
                + " FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "AND LOWER(album) = LOWER(:album)"
            ),
            {"artist": artist, "album": album},
        ).mappings().all() or []
    return [dict(row) for row in rows]


def _row_release_id(row: dict[str, Any]) -> str:
    return _norm(row.get("musicbrainz_album_mbid") or row.get("musicbrainz_albumid"))


def _choose_by_track_count(group_mbid: str, local_count: int) -> list[str]:
    """Release ids in *group_mbid* whose track count matches the folder."""
    try:
        from services.enrichment.musicbrainz_service import (
            get_release_group_releases,
        )

        payload = get_release_group_releases(group_mbid, include_track_counts=True)
    except Exception as exc:
        logger.debug("Canonical release lookup failed", group=group_mbid, error=str(exc))
        return []
    releases = payload.get("releases") if isinstance(payload, dict) else payload
    if not isinstance(releases, list):
        return []
    return [
        str(release.get("id") or "").strip()
        for release in releases
        if int(release.get("track_count") or 0) == int(local_count)
        and str(release.get("id") or "").strip()
    ]


def _canonical_release_id(
    rows: list[dict[str, Any]], group_mbid: str, preferred: str = "",
) -> str:
    if preferred and _key(preferred) != _key(group_mbid):
        # An explicit album-page choice is authoritative; this repair must not
        # second-guess the edition the user just selected.
        return preferred
    candidates = [
        rid for rid in (_row_release_id(row) for row in rows)
        if rid and _key(rid) != _key(group_mbid)
    ]
    # Duplicate rows must not inflate the count the edition is matched against.
    positions = {
        (_norm(row.get("disc_number")), _norm(row.get("track_number")))
        for row in rows
        if _norm(row.get("track_number"))
    }
    local_count = len(positions) or len(rows)
    if not candidates:
        # Every row is malformed; only MusicBrainz can name the release.
        exact = _choose_by_track_count(group_mbid, local_count)
        return exact[0] if exact else ""
    counts = Counter(_key(rid) for rid in candidates)
    majority = counts.most_common(1)[0][0]
    # A folder assembled from mixed editions needs the edition that matches the
    # COMPLETE local tracklist (the Japanese bonus-track case), not merely the
    # id most files happened to carry.
    if len(counts) > 1:
        exact = _choose_by_track_count(group_mbid, local_count)
        if exact:
            # Prefer an edition already present in the folder when two share
            # the same track count; otherwise take MusicBrainz's first exact.
            for release_id in exact:
                if _key(release_id) in counts:
                    return release_id
            return exact[0]
    return next(rid for rid in candidates if _key(rid) == majority)


def _release_values(rows: list[dict[str, Any]], canonical_id: str) -> dict[str, Any]:
    """Release-level values for *canonical_id*: a stored row first, else MB."""
    values: dict[str, Any] = {}
    matching = [row for row in rows if _key(_row_release_id(row)) == _key(canonical_id)]
    for field in _RELEASE_FIELDS:
        for row in matching:
            value = _norm(row.get(field))
            if value:
                values[field] = value
                break
    if values:
        return values

    # The chosen edition is not represented by any file yet (e.g. the Japanese
    # bonus-track release). Read its release-level fields from MusicBrainz so
    # the folder gets a coherent barcode/catalogue/country set, not just the id.
    try:
        from services.enrichment.musicbrainz_service import (
            fetch_musicbrainz_release_metadata,
        )

        meta = fetch_musicbrainz_release_metadata(canonical_id) or {}
    except Exception as exc:
        logger.debug("Release unify: MB metadata fetch failed", error=str(exc))
        return values
    mapping = {
        "barcode": meta.get("barcode"),
        "catalognumber": meta.get("catalognumber"),
        "releasecountry": meta.get("releasecountry"),
        "media": meta.get("media"),
        "recordlabel": meta.get("recordlabel"),
        "releasetype": meta.get("album_type") or meta.get("releasetype"),
        "releasestatus": meta.get("albumstatus") or meta.get("releasestatus"),
        "releasedate": meta.get("releasedate"),
        "originaldate": meta.get("originaldate"),
        "originalyear": meta.get("originalyear"),
        "disctotal": meta.get("disctotal"),
        "tracktotal": meta.get("tracktotal") or len(meta.get("tracks") or []) or None,
        "release_title": meta.get("specific_release_title") or meta.get("release_title"),
        "release_year": meta.get("release_year"),
    }
    return {
        field: str(value).strip()
        for field, value in mapping.items()
        if value not in (None, "")
    }


def _write_files(rows: list[dict[str, Any]], tags: dict[str, Any]) -> int:
    written = 0
    try:
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            update_file_tags,
        )
    except Exception as exc:
        logger.debug("Release unify: tag writer unavailable", error=str(exc))
        return 0
    for row in rows:
        path = resolve_music_file_path(_norm(row.get("file_path")))
        if not path:
            continue
        try:
            if update_file_tags(path, tags, force=True):
                written += 1
        except Exception as exc:
            logger.debug(
                "Release unify: file tag write failed", path=path, error=str(exc),
            )
    return written


def unify_album_release_identity(
    artist: str,
    album: str,
    *,
    release_group_mbid: str | None = None,
    preferred_release_mbid: str | None = None,
) -> dict[str, Any]:
    """Collapse mixed release ids in one artist+album onto ONE release.

    Returns ``{"rows_updated", "files_updated", "release_id", "skipped"}``.
    ``skipped`` is a reason string and the call is a no-op otherwise.
    """
    if not _norm(artist) or not _norm(album):
        return {"rows_updated": 0, "files_updated": 0, "skipped": "missing_album"}

    rows = _load_rows(artist, album)
    if len(rows) < 2:
        return {"rows_updated": 0, "files_updated": 0, "skipped": "too_few_tracks"}

    groups = {_key(row.get("musicbrainz_releasegroupid")) for row in rows}
    groups.discard("")
    if len(groups) > 1:
        # Two release families sharing a name: do NOT merge them.
        return {"rows_updated": 0, "files_updated": 0, "skipped": "multiple_groups"}
    group_mbid = next(iter(groups), "") or _norm(release_group_mbid)
    if not group_mbid:
        return {"rows_updated": 0, "files_updated": 0, "skipped": "no_group"}

    canonical_id = _canonical_release_id(
        rows, group_mbid, _norm(preferred_release_mbid),
    )
    if not canonical_id:
        return {"rows_updated": 0, "files_updated": 0, "skipped": "no_canonical"}

    distinct = {
        _key(rid) for rid in (_row_release_id(row) for row in rows)
        if rid and _key(rid) != _key(canonical_id)
    }
    if not distinct:
        return {
            "rows_updated": 0, "files_updated": 0,
            "release_id": canonical_id, "skipped": "already_consistent",
        }

    release_values = _release_values(rows, canonical_id)
    set_parts = [
        "musicbrainz_album_mbid = :release_id",
        "musicbrainz_albumid = :release_id",
        # The GROUP is the valid half of a malformed pair (release id == group
        # id), and an import may have cleared it while rejecting the duplicate.
        "musicbrainz_releasegroupid = :group_id",
    ]
    params: dict[str, Any] = {"release_id": canonical_id, "group_id": group_mbid}
    for index, (field, value) in enumerate(release_values.items()):
        key = f"f{index}"
        set_parts.append(f"{field} = :{key}")
        params[key] = value

    try:
        with db_session() as session:
            result = session.execute(
                text(
                    f"UPDATE tracks SET {', '.join(set_parts)} "
                    "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                    "AND LOWER(album) = LOWER(:album)"
                ),
                {**params, "artist": artist, "album": album},
            )
            rows_updated = int(result.rowcount or 0)
    except Exception as exc:
        logger.warning(
            "Release unify: DB update failed", artist=artist, album=album,
            error=str(exc),
        )
        return {"rows_updated": 0, "files_updated": 0, "skipped": "db_failed"}

    tags: dict[str, Any] = {
        "musicbrainz_albumid": canonical_id,
        "musicbrainz_releasegroupid": group_mbid,
    }
    for field, value in release_values.items():
        tags[field] = value
    files_updated = _write_files(rows, tags)

    # This is a deliberate local correction: a stale import must not undo it.
    try:
        from services.scanning.reconciliation import (
            mark_local_tags_written_for_track_ids,
        )

        mark_local_tags_written_for_track_ids(
            [str(row.get("id") or "") for row in rows if row.get("id")]
        )
    except Exception as exc:
        logger.debug("Release unify: stamp failed", error=str(exc))

    logger.info(
        "Album release identity unified",
        artist=artist, album=album,
        release_id=canonical_id,
        rows=rows_updated, files=files_updated,
    )
    return {
        "rows_updated": rows_updated,
        "files_updated": files_updated,
        "release_id": canonical_id,
        "skipped": "",
    }
