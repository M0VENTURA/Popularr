"""Re-evaluate ``tracks.genres`` from the genre evidence already in the DB.

The popularity/enrichment scan writes a track's final genres once. When the
selection engine, the vocabulary or a source column changes, the stored values
do not update until the album is scanned again — and a scan re-queries the
providers. Every input the selector needs is already in the ``tracks`` table, so
this recomputes the final genres without any provider calls.

It is the genre twin of ``services.popularity.star_resync_service``:
``dry_run=True`` reports what would change and writes nothing.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.enrichment.genre_aggregation_service import (
    _same_genre_value,
    aggregate_genres,
)

logger = structlog.get_logger(__name__)

#: Source -> stored column. ``manual`` outranks everything in the selector.
SOURCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("musicbrainz", "musicbrainz_genres"),
    ("listenbrainz", "listenbrainz_genres"),
    ("lastfm", "lastfm_tags"),
    ("discogs", "discogs_genres"),
    ("spotify", "spotify_genres"),
    ("navidrome", "navidrome_genres"),
    ("audiodb", "audiodb_genres"),
    ("essentia", "essentia_genres"),
    ("wikidata", "wikidata_genres"),
    ("manual", "manual_genres"),
)

_SELECT_COLUMNS = (
    "id", "title", "album", "artist", "album_artist", "genres", "file_path",
    # ⭐ A genre's ``scope.confirm_release_types`` is judged against the
    # release-group type, so the resync has to carry it or every confirmed genre
    # (Soundtrack, Film Score, Video Game Music) would be rejected on a re-run
    # even for a real soundtrack.
    "musicbrainz_albumtype",
    *(column for _source, column in SOURCE_COLUMNS),
)


def _row_release_types(row: Any) -> tuple[str, ...]:
    """The release-group types for one stored row.

    Delegates to ``genre_scope.release_types`` so the scan and the resync split
    the value the same way — the column can hold "Album + Soundtrack" and both
    paths must agree that it is two types.
    """
    from services.enrichment.genre_scope import release_types

    raw = row.get("musicbrainz_albumtype") if hasattr(row, "get") else None
    return release_types(raw)


def _has_value(value: Any) -> bool:
    """True when a stored genre column carries at least one usable value."""
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return str(value).strip() not in ("", "[]", "{}", "null", "None")


def _source_map(row: Any) -> dict[str, Any]:
    """The selector's ``source@track`` evidence map for one stored row."""
    out: dict[str, Any] = {}
    for source, column in SOURCE_COLUMNS:
        value = row.get(column)
        if _has_value(value):
            out[f"{source}@track"] = value
    return out


def _track_limit() -> int:
    try:
        from helpers.config_helpers import get_genre_track_limit

        return max(1, int(get_genre_track_limit()))
    except Exception:
        return 3


def _load_rows(artist: str | None, album: str | None) -> list[dict[str, Any]]:
    where = ["COALESCE(album, '') <> ''"]
    params: dict[str, Any] = {}
    if artist:
        where.append("LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)")
        params["artist"] = artist
    if album:
        where.append("LOWER(album) = LOWER(:album)")
        params["album"] = album

    sql = (
        "SELECT " + ", ".join(_SELECT_COLUMNS) + " FROM tracks "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, disc_number, track_number"
    )
    with db_session() as session:
        rows = session.execute(text(sql), params).fetchall() or []

    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        out.append(dict(mapping) if mapping is not None else dict(row))
    return out


#: Rows per transaction for the genre column write. Large enough that 70k rows
#: are a handful of statements, small enough that any one statement (and the
#: lock it holds) is bounded, and that progress is published during the save.
_PERSIST_CHUNK = 1000


def _db_is_recovering() -> bool:
    """True while the engine's crash-recovery breaker is open."""
    try:
        from db.engine import is_db_outage_active

        return bool(is_db_outage_active())
    except Exception:
        return False


def _wait_for_db_recovery(timeout_seconds: float = 120.0) -> bool:
    """Block until PostgreSQL is accepting work again (no connection attempt)."""
    try:
        from db.engine import wait_for_db_available

        return bool(wait_for_db_available(timeout_seconds=timeout_seconds))
    except Exception:
        return False


def _persist(
    changes: list[tuple[str, str]],
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    if not changes:
        return
    stmt = text("UPDATE tracks SET genres = :genres WHERE CAST(id AS TEXT) = :id")
    batch = [{"genres": after, "id": track_id} for track_id, after in changes]
    total = len(batch)

    # Announce the save BEFORE the first (possibly blocking) statement, so the
    # UI can say what it is waiting on instead of showing the last walk frame.
    if on_progress is not None:
        try:
            on_progress(0, total)
        except Exception as exc:
            logger.debug("Genre resync persist progress failed", error=str(exc))

    def _run_batch(chunk: list[dict[str, Any]]) -> None:
        with db_session() as session:
            session.execute(stmt, chunk)

    def _run_batch_through_restart(chunk: list[dict[str, Any]]) -> None:
        """Execute one chunk, riding through a PostgreSQL crash/restart.

        ⚠️ A backend crash kills the connection MID-TRANSACTION ("terminating
        connection because of crash of another server process"). The breaker in
        ``db.engine`` knows how to wait for recovery; this loop uses it instead
        of treating the restart as a permanent failure (or grinding through the
        remaining chunks while the server is down).
        """
        for attempt in range(3):
            try:
                _run_batch(chunk)
                return
            except Exception as exc:
                if attempt < 2 and _db_is_recovering():
                    logger.warning(
                        "Database restarting during genre save — waiting",
                        attempt=attempt + 1,
                        error=str(exc)[:200],
                    )
                    if _wait_for_db_recovery(120.0):
                        continue
                raise

    for start in range(0, total, _PERSIST_CHUNK):
        chunk = batch[start:start + _PERSIST_CHUNK]
        try:
            _run_batch_through_restart(chunk)
        except Exception as batch_exc:
            if _db_is_recovering():
                # Still recovering after the wait: abort rather than firing
                # thousands of per-row attempts at a database that is down.
                raise RuntimeError(
                    "the database is restarting — retry the resync when it is back"
                ) from batch_exc
            logger.debug(
                "Genre resync batch update failed, falling back to per-row",
                error=str(batch_exc),
            )
            for param in chunk:
                try:
                    _run_batch_through_restart([param])
                except Exception as exc:
                    logger.warning(
                        "Genre resync row update failed",
                        track_id=param["id"],
                        error=str(exc),
                    )

        if on_progress is not None:
            try:
                on_progress(min(start + len(chunk), total), total)
            except Exception as exc:
                logger.debug("Genre resync persist progress failed", error=str(exc))


def _explain(
    source_map: dict[str, Any], max_genres: int, proposed: list[str]
) -> list[dict[str, Any]]:
    """The removal records behind the ALREADY-COMPUTED *proposed* result.

    The selector is run once per track by ``aggregate_genres``. Re-running it
    here purely to read ``removed`` would double the work AND risk the
    explanation disagreeing with the reported list if the vocabulary changed
    between the two calls — so this returns nothing rather than guessing when
    the engine cannot supply reasons. The authoritative records are produced by
    the engine itself; this is a best-effort convenience for the preview.
    """
    try:
        from services.enrichment.genre_selection import (
            select_album_or_track_genres,
        )

        if not _selection_engine_v2_enabled():
            return []
        result = select_album_or_track_genres(source_map, max_genres=max_genres)
        # Guard against a vocabulary edit landing between the two calls: only
        # report reasons when the re-run agrees on the ANSWER.
        if list(result.get("genres") or []) != list(proposed or []):
            return []
        return list(result.get("removed") or [])
    except Exception as exc:
        logger.debug("Genre removal explanation unavailable", error=str(exc))
        return []


def _selection_engine_v2_enabled() -> bool:
    try:
        from services.enrichment.genre_aggregation_service import (
            _selection_engine_v2_enabled as _enabled,
        )

        return bool(_enabled())
    except Exception:
        return False


def _resync_tag_options() -> tuple[bool, bool]:
    """``(write_file_tags, sync_navidrome)`` for the genre re-evaluation.

    ⭐⭐ THE DATABASE IS NOT WHY THE REPORTED BUG EXISTED.

    > "Final Genres show this, but they haven't been pushed to the tracks in
    > Navidrome — Post-Hardcore, Alternative Metal, Heavy Metal"

    ``tracks.genres`` was correct and the episode was real: this job only ever
    wrote the COLUMN. Nothing touched the audio files, so the ``TCON``/genre
    frame kept whatever the last scan wrote, and Navidrome — which reads the
    FILES — kept showing the old genres forever. The Config page promised
    "recomputes each track's final genres"; to the user that means what
    Navidrome displays.

    Default ON, matching the Star Rating Resync card: a re-evaluation the user
    has to re-run through a full scan to see any effect is a half-done job. Both
    can be switched off for a database-only pass.
    """
    try:
        from helpers.config_helpers import get_config

        cfg = get_config() or {}
        block = ((cfg.get("genres") or {}).get("resync") or {})
    except Exception:
        block = {}

    def _flag(key: str) -> bool:
        value = block.get(key, True)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    return _flag("write_file_tags"), _flag("sync_navidrome")


def _push_to_files(
    changed_rows: list[dict[str, Any]],
    *,
    write_file_tags: bool,
    sync_navidrome: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Write the new genres onto the audio files, then nudge Navidrome.

    Returns ``{"files_written", "files_failed", "navidrome"}`` so the caller can
    report what actually reached the library rather than only what the DB says.

    ``on_progress`` is called with ``(files_done, files_total)`` for the FIRST
    file, every 100 files after that, and at least every 2 seconds. This loop is
    the slow half of an apply — one read-modify-write per changed track — so it
    is the half that most needs a moving progress bar.
    """
    out: dict[str, Any] = {"files_written": 0, "files_failed": 0, "navidrome": None}

    if write_file_tags and changed_rows:
        try:
            from services.metadata.tag_file_service import (
                _resolve_music_file_path,
                update_file_tags,
            )
        except Exception as exc:
            logger.warning("Genre resync: tag writer unavailable", error=str(exc))
        else:
            total = len(changed_rows)
            last_report = 0.0
            for index, row in enumerate(changed_rows, start=1):
                # ⚠️ REPORT THE FIRST FILE TOO. Waiting for file 100 left the
                # bar on the previous phase for however long 100 read-modify-
                # writes take (minutes on a slow/network mount), which read as
                # "stuck". After that, a time budget keeps it moving even when
                # the per-file cost is high.
                now = time.monotonic()
                if on_progress is not None and (
                    index == 1 or index % 100 == 0 or now - last_report >= 2.0
                ):
                    last_report = now
                    try:
                        on_progress(index, total)
                    except Exception as exc:
                        logger.debug(
                            "Genre resync write progress callback failed",
                            error=str(exc),
                        )
                track_id = str(row.get("track_id") or "")
                if not track_id:
                    continue
                try:
                    # The path was selected with the row, so the common case no
                    # longer costs one DB query PER TRACK. Only a row built by a
                    # caller that omitted it falls back to a lookup.
                    path = str(row.get("file_path") or "").strip() or _resolve_track_file_path(track_id)
                    if not path:
                        out["files_failed"] += 1
                        continue
                    resolved = _resolve_music_file_path(path) or path
                    if update_file_tags(resolved, {"genres": row.get("after") or ""}):
                        out["files_written"] += 1
                    else:
                        out["files_failed"] += 1
                except Exception as exc:
                    out["files_failed"] += 1
                    logger.debug(
                        "Genre resync: tag write failed",
                        track_id=track_id, error=str(exc),
                    )

    if sync_navidrome:
        # A rescan is what makes Navidrome re-read the files it now holds. It is
        # fire-and-forget by design (the rescan is coalesced and must not block
        # this job), so the result is "requested", not "complete".
        try:
            from services.scanning.navidrome_rescan_service import request_rescan

            out["navidrome"] = bool(request_rescan())
        except Exception as exc:
            logger.debug("Genre resync: Navidrome rescan request failed", error=str(exc))

    return out


def _resolve_track_file_path(track_id: str) -> str | None:
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            ).fetchone()
    except Exception as exc:
        logger.debug("Genre resync: file path lookup failed", track_id=track_id, error=str(exc))
        return None
    value = row[0] if row else None
    return str(value).strip() if value and str(value).strip() else None


def resync_genres(
    *,
    artist: str | None = None,
    album: str | None = None,
    limit_tracks: int | None = None,
    dry_run: bool = False,
    write_file_tags: bool | None = None,
    sync_navidrome: bool | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Recompute final genres for stored tracks from their stored evidence.

    Args:
        artist: Restrict to one album artist (``album_artist`` when present).
        album: Restrict to one album title (combine with ``artist``).
        limit_tracks: Cap the number of tracks examined (smoke tests).
        dry_run: Compute and report only; write nothing at all.
        write_file_tags: Override the configured file-tag behaviour. Without
            this the column changed but Navidrome kept showing the old genres,
            because Navidrome reads the FILES.
        sync_navidrome: Override the configured rescan request.
        on_progress: Called with ``(done, total, files_written, stage)`` as the
            work advances; ``stage`` is ``aggregate`` (the walk), ``persist``
            (the genre-column save) or ``files`` (tag writes). The full-library
            pass is the longest step of a genre apply — aggregation over ~70k
            tracks, then a tag write per changed file — and without this the
            apply's progress bar sat still for the entire job.

    Returns:
        ``{"ok", "dry_run", "tracks", "with_evidence", "changed",
        "skipped_no_evidence", "changed_tracks", "files_written",
        "files_failed", "navidrome", "seconds"}``.
    """
    started = time.monotonic()
    max_genres = _track_limit()

    _cfg_tags, _cfg_navidrome = _resync_tag_options()
    if write_file_tags is None:
        write_file_tags = _cfg_tags
    if sync_navidrome is None:
        sync_navidrome = _cfg_navidrome
    if dry_run:
        # A preview must not touch a single file, whatever the config says.
        write_file_tags = False
        sync_navidrome = False

    rows = _load_rows(artist, album)
    if limit_tracks:
        rows = rows[: int(limit_tracks)]

    changes: list[tuple[str, str]] = []
    changed_rows: list[dict[str, Any]] = []
    with_evidence = 0

    total_rows = len(rows)

    def _report(done: int, files_written: int = 0, stage: str = "aggregate") -> None:
        """Publish progress. Never fatal — a callback must not abort the job.

        ``stage`` names WHICH half of the phase the counter belongs to
        (``aggregate`` / ``persist`` / ``files``). Without it the caller cannot
        tell "the walk finished" from "the file writes have not started", which
        is exactly what left the bar reading "67000/67039" while the job was
        saving rows and rewriting tags.
        """
        if on_progress is None:
            return
        try:
            on_progress(done, total_rows, files_written, stage)
        except Exception as exc:
            logger.debug("Genre resync progress callback failed", error=str(exc))

    for index, row in enumerate(rows, start=1):
        # Report every 500 tracks: one frame per track would be 70k writes to
        # the progress file for no visible benefit.
        if index % 500 == 0:
            _report(index)
        source_map = _source_map(row)
        if not source_map:
            continue
        with_evidence += 1

        proposed = aggregate_genres(
            source_map,
            max_genres=max_genres,
            context_title=str(row.get("title") or ""),
            context_album=str(row.get("album") or ""),
            # ⚠️ Required for ``scope.confirm_release_types``: without it a
            # soundtrack's Soundtrack marker could never be confirmed, and the
            # vocabulary's require_confirmation rule would drop it on every
            # re-evaluation even though the scan had accepted it.
            release_types=_row_release_types(row),
        )
        if not proposed:
            continue
        proposed_str = ", ".join(proposed)
        existing = row.get("genres")
        if _same_genre_value(existing, proposed_str):
            continue

        track_id = str(row.get("id") or "").strip()
        if not track_id:
            continue
        changes.append((track_id, proposed_str))
        changed_rows.append(
            {
                "track_id": track_id,
                "title": str(row.get("title") or ""),
                "artist": str(row.get("artist") or ""),
                "album": str(row.get("album") or ""),
                # Carried so the file-write phase does not need a DB lookup per
                # changed track (tens of thousands of round trips on a full
                # library, on the slowest phase of the job).
                "file_path": str(row.get("file_path") or ""),
                "before": str(existing or ""),
                "after": proposed_str,
                # WHY each candidate lost its slot — the preview's explanation.
                # ⚠️ ONLY THE PREVIEW READS THIS. Computing it re-runs the whole
                # selector per changed track; doing that on an APPLY doubled the
                # aggregation cost for data nothing consumed.
                "removed": _explain(source_map, max_genres, proposed) if dry_run else [],
            }
        )

    # The walk is DONE. Publish it explicitly: the every-500 cadence can end on
    # e.g. 67000/67039, and without this frame the last visible state stays
    # there while the save and the file writes run.
    _report(total_rows)

    pushed: dict[str, Any] = {
        "files_written": 0, "files_failed": 0, "navidrome": None,
    }
    if not dry_run:
        # Chunked, and every chunk publishes progress: a single 70k-row UPDATE
        # (and the lock it holds) was the other long, silent stretch.
        _persist(
            changes,
            on_progress=(
                lambda done, total: _report(done, stage="persist")
            ) if on_progress else None,
        )
        # ⭐ THE COLUMN ALONE IS NOT THE JOB. Navidrome reads the FILES, so a
        # pass that only updated the database left the library showing the old
        # genres forever ("Final Genres show this, but they haven't been pushed
        # to the tracks in Navidrome").
        # Name the file-write phase before its first read-modify-write, for the
        # same reason: file #1 can take a while on a network mount.
        if on_progress is not None and write_file_tags:
            _report(total_rows, files_written=0, stage="files")
        pushed = _push_to_files(
            changed_rows,
            write_file_tags=bool(write_file_tags),
            sync_navidrome=bool(sync_navidrome),
            on_progress=(
                lambda done, total: _report(
                    total_rows, files_written=done, stage="files"
                )
            ) if on_progress else None,
        )

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "Genre resync finished",
        tracks=len(rows), with_evidence=with_evidence,
        changed=len(changes), dry_run=dry_run,
        files_written=pushed.get("files_written"),
        files_failed=pushed.get("files_failed"),
        navidrome=pushed.get("navidrome"),
        seconds=elapsed,
    )
    return {
        "ok": True,
        "dry_run": dry_run,
        "tracks": len(rows),
        "with_evidence": with_evidence,
        "changed": len(changes),
        "skipped_no_evidence": len(rows) - with_evidence,
        "changed_tracks": changed_rows,
        # What actually reached the library, not just the database.
        "files_written": int(pushed.get("files_written") or 0),
        "files_failed": int(pushed.get("files_failed") or 0),
        "navidrome_rescan": pushed.get("navidrome"),
        "seconds": elapsed,
    }


def preview_genres(
    *,
    artist: str | None = None,
    album: str | None = None,
    limit_tracks: int | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: a dry run that writes nothing."""
    return resync_genres(
        artist=artist, album=album, limit_tracks=limit_tracks, dry_run=True
    )
