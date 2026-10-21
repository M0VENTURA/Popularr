"""Reconcile the album identity of imported downloads with the release they were queued against.

The reported problem
--------------------
> When metadata is added to a track that is downloaded from the queue, it
> doesn't always match the metadata of the release that was added to the queue.
> Sometimes the album year or the release mbid is different.

Two separate faults produce that, and this module addresses the second:

1. **The import itself could pick the wrong release.**  ``_resolve_album_level_metadata``
   inherited album-scoped fields from a library sibling matched on artist+album
   NAME with no release constraint, and preferred a sibling that carried a
   release group — so a same-named album from a DIFFERENT release (an original
   vs a remaster) donated its release group, barcode, catalogue number and year
   to the incoming track, and because inheritance ran before MusicBrainz the
   release actually queued was then only consulted for the leftovers.  Fixed at
   the source by scoping inheritance to the same release / release group; rows
   that name no release at all keep the name-only behaviour, which is the case
   that fallback exists for.

2. **Rows imported BEFORE that fix keep the wrong identity**, and nothing
   revisits them — the track looks consistent with itself, so no later scan has
   any reason to touch it.  That is what this heal is for.

What "the right identity" means
-------------------------------
The queue row is the user's explicit statement of which release they wanted, so
it is the reference.  ``release_mbid`` / ``release_id`` is resolved through
MusicBrainz (MBID-first, per the project's matching strategy) and the result is
used to fill anything the row itself does not carry.  A row that names NO
release has no authoritative identity to heal toward and is skipped — guessing
from the name is exactly the fault being repaired.

How a correction is applied
---------------------------
The unit of correction is THE RELEASE, and the rows corrected are the tracks the
queue's own rows produced — never every track that happens to share the album
name.

That distinction is not pedantry. The library can legitimately hold two
editions of one album under the same artist+album name, and the sibling-
inheritance fault being repaired is precisely a case of one of them donating its
identity to the other. A heal that swept the whole album group would then do the
damage in reverse: it would drag a genuinely different release onto whichever
release the queue happened to name, and the two editions could never be told
apart again. A track is corrected because a queue row that names its release
produced it, and for no other reason.

Grouping by release also means ONE MusicBrainz fetch per release rather than one
per track — which matters, because that client is rate-limited.

``tracks.year`` is the release GROUP's first release year and
``tracks.release_year`` is this EDITION's; the file's DATE tag takes the edition
year and ORIGINALYEAR takes the original, so the two are mapped explicitly
rather than copied across. Only the fields that actually drifted are written,
because the tag writers treat an empty value as "delete this frame".
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Queue statuses whose downloads have landed in the library. A row still in
#: flight has nothing to compare against yet — the import itself is what will
#: write its identity.
HEALABLE_STATUSES: frozenset[str] = frozenset(
    {"completed", "imported", "in_collection"}
)

#: Tracks columns this heal owns. Deliberately the album-scoped identity only:
#: per-track enrichment (writer, genres, ISRC …) is a different concern and
#: correcting it from here would fight the scan.
#:
#: ``year`` / ``release_year`` are included but are NOT interchangeable — see
#: ``_reference_identity``.
HEAL_COLUMNS: tuple[str, ...] = (
    "musicbrainz_album_mbid",
    "musicbrainz_releasegroupid",
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartistid",
    "releasecountry",
    "recordlabel",
    "catalognumber",
    "barcode",
    "media",
    "disctotal",
    "releasedate",
    "originaldate",
    "originalyear",
    "year",
    "release_year",
    "release_title",
)

#: Columns the DATABASE can hold but that carry no useful identity when they are
#: the only thing present. Used to decide whether a reference is authoritative.
_IDENTITY_COLUMNS: tuple[str, ...] = (
    "musicbrainz_album_mbid",
    "musicbrainz_releasegroupid",
)


def _norm(value: Any) -> str:
    """Compare values on their stripped string form, case-insensitively.

    MBIDs arrive with mixed case from different sources and years arrive as
    ``1994`` / ``"1994"`` / ``"1994-08-22"``; a raw ``!=`` would report drift on
    every row and the heal would rewrite files it was not asked to touch.
    """
    return str(value if value is not None else "").strip().casefold()


def _release_mbid(item: dict[str, Any]) -> str:
    return str(item.get("release_mbid") or item.get("release_id") or "").strip()


def _reference_identity(item: dict[str, Any]) -> dict[str, Any]:
    """The identity the library row should have, from the row and MusicBrainz.

    MBID-first: the release the row names is fetched from MusicBrainz and wins
    over the row's own stored copy — a row can itself have been written from an
    incomplete or stale payload. The row's values are the fallback for anything
    MusicBrainz cannot answer (or when it is unreachable), because the row is
    still better evidence than the wrong release already on the track.

    ⚠️ ``year`` and ``release_year`` are mapped deliberately, not copied: on a
    QUEUE row ``year`` is the edition year (that is what
    ``add_release_tracks_to_queue_detailed`` stores and what the import writes
    into the DATE tag), while on a TRACKS row ``year`` is the release group's
    ORIGINAL year and ``release_year`` is the edition. Copying the queue's
    ``year`` straight into ``tracks.year`` would relabel every original-year
    field with the edition year.
    """
    reference: dict[str, Any] = {}

    stored = item.get("metadata")
    if isinstance(stored, str):
        try:
            import json

            stored = json.loads(stored or "{}")
        except Exception:
            stored = {}
    album_payload = stored.get("album_metadata") if isinstance(stored, dict) else None

    # ── the row's own columns, then its stored album payload ────────────────
    #
    # ⚠️ ``year`` is EXCLUDED from this copy, and that exclusion is the whole
    # point of the warning above. On a download_queue row ``year`` holds the
    # EDITION year, so copying it here would put it in ``reference`` under the
    # name ``tracks.year`` uses for the ORIGINAL year — and with MusicBrainz
    # unreachable there is nothing later to overwrite it, so the heal would
    # relabel the original-year field with the edition year on every row it
    # touched. ``year`` is therefore set ONLY from MusicBrainz, and when that
    # cannot answer, the field is left alone rather than guessed at.
    for column in HEAL_COLUMNS:
        if column == "year":
            continue
        value = item.get(column)
        if value not in (None, ""):
            reference[column] = value
    if isinstance(album_payload, dict):
        for column, value in album_payload.items():
            if column == "year":
                continue
            if column in HEAL_COLUMNS and reference.get(column) in (None, ""):
                if value not in (None, ""):
                    reference[column] = value

    # The row's ``year`` (and ``release_year``) is the EDITION year, which is
    # what ``tracks.release_year`` and the file's DATE tag both mean.
    edition_year = str(item.get("year") or item.get("release_year") or "").strip()
    if edition_year:
        reference["release_year"] = edition_year

    release_mbid = _release_mbid(item)
    if release_mbid:
        reference["musicbrainz_album_mbid"] = release_mbid

    # ── MusicBrainz, keyed by the release the row names ─────────────────────
    if release_mbid:
        try:
            from services.enrichment.musicbrainz_service import (
                fetch_musicbrainz_release_metadata,
            )

            release = fetch_musicbrainz_release_metadata(release_mbid)
        except Exception as exc:
            logger.debug(
                "[HEAL] MusicBrainz release fetch failed — row values only",
                release_mbid=release_mbid,
                error=str(exc),
            )
            release = None

        if isinstance(release, dict):
            for column, value in _album_fields_from_release(release).items():
                if value not in (None, ""):
                    reference[column] = value
            reference["musicbrainz_album_mbid"] = release_mbid
            if not reference.get("release_year") and release.get("release_year"):
                reference["release_year"] = release["release_year"]

    return reference


def _album_fields_from_release(release: dict[str, Any]) -> dict[str, Any]:
    """Map a flattened MusicBrainz release to tracks-column names.

    The same mapping the import uses, plus the two year columns it deliberately
    leaves to the import path (``release_year`` = this edition, ``year`` = the
    release group's original year).

    This is the ONLY source of ``year``: the release group's first-release year
    is not something a queue row records.
    """
    from services.downloads.download_completion_service import (
        _album_level_mb_fields,
    )

    out = dict(_album_level_mb_fields(release))
    edition_year = release.get("release_year") or ""
    original_year = release.get("original_year") or release.get("originalyear") or ""
    if edition_year:
        out["release_year"] = edition_year
    if original_year:
        out["year"] = original_year
    return out


def _find_imported_track(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Locate the library row this queue row became.

    The landed path is the strongest link (the mover records it), then the
    recording MBID. A name match is NOT used: an album can legitimately hold
    two same-titled tracks and picking the wrong one would corrupt an innocent
    row.
    """
    from sqlalchemy import text

    from db.engine import db_session

    landed = str(item.get("music_file_path") or "").strip()
    recording_mbid = str(item.get("recording_mbid") or "").strip()

    try:
        with db_session() as session:
            if landed:
                row = session.execute(
                    text("SELECT id FROM tracks WHERE file_path = :p LIMIT 1"),
                    {"p": landed},
                ).fetchone()
                if row is not None:
                    return {"id": str(row[0]), "file_path": landed}
            if recording_mbid:
                row = session.execute(
                    text(
                        """
                        SELECT id, file_path FROM tracks
                        WHERE recording_mbid = :m
                        ORDER BY CASE WHEN COALESCE(file_path, '') = '' THEN 1 ELSE 0 END, id
                        LIMIT 1
                        """
                    ),
                    {"m": recording_mbid},
                ).fetchone()
                if row is not None:
                    mapping = getattr(row, "_mapping", None) or {}
                    return {
                        "id": str(mapping.get("id") or row[0]),
                        "file_path": str(mapping.get("file_path") or ""),
                    }
    except Exception as exc:
        logger.debug(
            "[HEAL] Could not locate the imported track",
            queue_id=item.get("id"),
            error=str(exc),
        )
    return None


def _load_track_rows(track_ids: list[str]) -> list[dict[str, Any]]:
    """The library rows for these ids, with the columns the heal owns."""
    if not track_ids:
        return []

    from sqlalchemy import text

    from db.engine import db_session

    columns = ", ".join(HEAL_COLUMNS)
    placeholders = ", ".join(f":tid_{i}" for i in range(len(track_ids)))
    params = {f"tid_{i}": tid for i, tid in enumerate(track_ids)}

    try:
        with db_session() as session:
            rows = session.execute(
                text(
                    f"SELECT id, file_path, {columns} FROM tracks "
                    f"WHERE id IN ({placeholders})"
                ),
                params,
            ).fetchall() or []
    except Exception as exc:
        logger.warning("[HEAL] Track load failed", error=str(exc))
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        out.append(dict(mapping) if mapping else dict(row))
    return out


def _drift_for_row(row: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """The subset of *reference* that differs from this row's current values.

    Only fields the reference actually knows about are considered, and only
    real differences are returned — an empty dict means the row already agrees
    and the heal is a no-op for it.
    """
    drift: dict[str, Any] = {}
    for column in HEAL_COLUMNS:
        wanted = reference.get(column)
        if wanted in (None, ""):
            continue
        if _norm(row.get(column)) != _norm(wanted):
            drift[column] = wanted
    return drift


def _apply_db_corrections(track_ids: list[str], corrections: dict[str, Any]) -> int:
    """Write *corrections* to the given tracks rows. Returns rows touched."""
    if not track_ids or not corrections:
        return 0

    from sqlalchemy import text

    from db.engine import db_session

    assignments = ", ".join(f"{col} = :{col}" for col in corrections)
    params: dict[str, Any] = dict(corrections)
    placeholders = ", ".join(f":tid_{i}" for i in range(len(track_ids)))
    params.update({f"tid_{i}": tid for i, tid in enumerate(track_ids)})

    try:
        with db_session() as session:
            result = session.execute(
                text(f"UPDATE tracks SET {assignments} WHERE id IN ({placeholders})"),
                params,
            )
            return result.rowcount or 0
    except Exception as exc:
        logger.warning(
            "[HEAL] DB correction failed",
            columns=sorted(corrections),
            error=str(exc),
        )
        return 0


def _apply_file_corrections(file_paths: list[str], corrections: dict[str, Any]) -> tuple[int, int]:
    """Write *corrections* to the physical files. Returns (written, failed).

    ⚠️ Only the corrected keys are passed. The writers treat an empty value as
    "delete this frame", so sending a full payload with the uncorrected fields
    blank would strip tags the heal was not asked to touch.
    """
    if not file_paths or not corrections:
        return 0, 0

    try:
        from services.metadata.tag_file_service import (
            build_tag_updates,
            resolve_music_file_path,
            write_tags_to_file,
        )
    except Exception as exc:
        logger.warning("[HEAL] Tag writer unavailable — DB and files may disagree", error=str(exc))
        return 0, len(file_paths)

    # Reuse the ONE column→tag mapping so the heal writes exactly what a manual
    # album edit writes (including the DATE-vs-ORIGINALYEAR handling).
    tag_updates = build_tag_updates(dict(corrections))
    if not tag_updates:
        return 0, 0

    written = 0
    failed = 0
    for path in file_paths:
        if not path:
            continue
        try:
            resolved = resolve_music_file_path(str(path))
            if resolved and write_tags_to_file(resolved, dict(tag_updates)):
                written += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            logger.debug("[HEAL] File tag write failed", file_path=path, error=str(exc))

    if failed:
        logger.warning(
            "[HEAL] Some files could not be retagged — DB and file tags disagree "
            "for those tracks",
            written=written,
            failed=failed,
        )
    return written, failed


def _candidate_rows(limit: int | None, queue_ids: list[int] | None) -> list[dict[str, Any]]:
    """Queue rows that have landed in the library and name a release."""
    from sqlalchemy import text

    from db.engine import db_session

    statuses = ", ".join(f"'{s}'" for s in sorted(HEALABLE_STATUSES))
    clauses = [
        f"status IN ({statuses})",
        "COALESCE(music_file_path, '') <> ''",
        # No release identity means no authoritative reference — the whole
        # point is to align to the release the user chose.
        "(COALESCE(release_mbid, '') <> '' OR COALESCE(release_id, '') <> '')",
    ]
    params: dict[str, Any] = {}
    if queue_ids:
        placeholders = ", ".join(f":qid_{i}" for i in range(len(queue_ids)))
        clauses.append(f"id IN ({placeholders})")
        params.update({f"qid_{i}": qid for i, qid in enumerate(queue_ids)})

    sql = (
        "SELECT id, artist, album, album_artist, title, release_mbid, release_id, "
        "musicbrainz_releasegroupid, year, release_year, recording_mbid, "
        "music_file_path, metadata "
        "FROM download_queue "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY id"
    )
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    try:
        with db_session() as session:
            rows = session.execute(text(sql), params).fetchall() or []
    except Exception as exc:
        logger.error("[HEAL] Could not load queue rows", error=str(exc))
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        out.append(dict(mapping) if mapping else dict(row))
    return out


def heal_queue_metadata(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    queue_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Align imported downloads with the release their queue row names.

    Args:
        dry_run: report what WOULD change and write nothing. Always safe to run.
        limit: cap the number of queue rows examined (the MusicBrainz fetch is
            rate-limited, so a full library heal should be paced).
        queue_ids: restrict to specific rows, for a targeted repair from the UI.

    Returns a summary: ``examined``, ``releases``, ``drifted``, ``healed``,
    ``skipped``, ``tracks_updated``, ``files_written``, ``files_failed``,
    ``dry_run``, and ``albums`` — one entry per corrected release naming the
    fields it changed.
    """
    candidates = _candidate_rows(limit, queue_ids)
    summary: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "examined": len(candidates),
        "releases": 0,
        "drifted": 0,
        "healed": 0,
        "skipped": 0,
        "tracks_updated": 0,
        "files_written": 0,
        "files_failed": 0,
        "albums": [],
    }

    # ── group by the release the rows name ─────────────────────────────────
    # One correction per release: every row that names it is a track of the
    # same download, so they share a reference identity (one MusicBrainz
    # fetch, not one per track) and one correction set.
    by_release: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        mbid = _release_mbid(item)
        if mbid:
            by_release.setdefault(mbid.casefold(), []).append(item)
    summary["releases"] = len(by_release)

    for items in by_release.values():
        reference = _reference_identity(items[0])
        if not any(str(reference.get(c) or "").strip() for c in _IDENTITY_COLUMNS):
            # Nothing authoritative to heal toward.
            summary["skipped"] += len(items)
            continue

        # ⚠️ ONLY the tracks these rows produced. Not every track that shares
        # the album name — see the module docstring for why that distinction
        # is the difference between repairing the library and corrupting it.
        landed: list[tuple[dict[str, Any], str]] = []
        for item in items:
            track = _find_imported_track(item)
            if track and track.get("id"):
                landed.append((item, str(track["id"])))
            else:
                summary["skipped"] += 1

        if not landed:
            continue

        rows = _load_track_rows([tid for _, tid in landed])
        if not rows:
            summary["skipped"] += len(landed)
            continue

        # A row already correct contributes nothing; a correction one track
        # needs still applies to its siblings, because album identity is
        # album-scoped and these tracks are all from the same release.
        corrections: dict[str, Any] = {}
        for row in rows:
            for column, value in _drift_for_row(row, reference).items():
                corrections.setdefault(column, value)

        if not corrections:
            continue

        summary["drifted"] += 1

        entry: dict[str, Any] = {
            "artist": items[0].get("album_artist") or items[0].get("artist"),
            "album": items[0].get("album"),
            "release_mbid": reference.get("musicbrainz_album_mbid") or _release_mbid(items[0]),
            "queue_rows": len(items),
            "tracks": len(rows),
            "fields": sorted(corrections),
        }

        if dry_run:
            entry["applied"] = False
            summary["albums"].append(entry)
            continue

        tracks_updated = _apply_db_corrections(
            [str(r.get("id")) for r in rows if r.get("id") is not None], corrections
        )
        written, failed = _apply_file_corrections(
            [str(r.get("file_path") or "") for r in rows], corrections
        )

        summary["tracks_updated"] += tracks_updated
        summary["files_written"] += written
        summary["files_failed"] += failed
        summary["healed"] += 1
        entry.update(
            {
                "applied": True,
                "tracks_updated": tracks_updated,
                "files_written": written,
                "files_failed": failed,
            }
        )
        summary["albums"].append(entry)

        try:
            from helpers.logging_config import log_unified

            log_unified(
                f"[HEAL] {entry['artist']} - {entry['album']}: aligned "
                f"{len(rows)} track(s) to release {entry['release_mbid']} "
                f"({', '.join(sorted(corrections))})"
            )
        except Exception:
            pass

    return summary


def heal_on_startup() -> dict[str, Any] | None:
    """Run the heal at queue-processor start when it is enabled in config.

    Returns the summary, or None when the heal is disabled.
    """
    try:
        from helpers.config_helpers import get_metadata_heal_config

        cfg = get_metadata_heal_config()
    except Exception as exc:
        logger.debug("[HEAL] Config unavailable", error=str(exc))
        return None

    if not cfg.get("enabled"):
        return None

    limit = cfg.get("limit") or None
    try:
        summary = heal_queue_metadata(limit=limit if isinstance(limit, int) else None)
    except Exception as exc:
        logger.warning("[HEAL] Startup heal failed", error=str(exc))
        return None

    if summary.get("healed"):
        logger.info(
            "[HEAL] Queue metadata healed on startup",
            albums=summary["healed"],
            tracks=summary["tracks_updated"],
        )
    return summary
