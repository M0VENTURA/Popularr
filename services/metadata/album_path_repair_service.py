"""Repair an album's rows from the FOLDERS their files actually sit in.

Why this exists
---------------
Reported: "many albums somehow got merged into the Various Artists albums such as
Spotify - Essential Alternative", where the album page showed rows whose files
lived in completely different folders:

    /music/Various Artists/2026 - Spotify - Essential Alternative/02. Gorillaz - Clint Eastwood.mp3
    /music/Jimmy Eat World/2002 - Good To Go (EP)/01. Jimmy Eat World - The Middle (Acoustic).mp3

The cause was ``merge_album_names`` rewriting the ALBUM TAG of every file in every
source album without checking where the file lived. A file never moves, so a
foreign rip kept its path while its tag claimed the compilation — and because the
tag that identified its true album was overwritten, the DB alone cannot recover
it.

The PATH is the surviving evidence. This module reconstructs ``album`` (and
optionally ``album_artist`` / ``year``) from the download naming format, and
reports what it WOULD change before changing anything.

Reading a folder name is a heuristic, so nothing here is destructive:
``dry_run`` is the DEFAULT and the caller must ask for the write explicitly.
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

#: Leading year in a folder name: "2002 - Good To Go (EP)".
_FOLDER_YEAR_RE = re.compile(r"^\s*((?:19|20)\d{2})\s*[-–—]\s*(.+?)\s*$")

#: Folder names that are NOT an album (the library root layout).
_NOT_AN_ALBUM_FOLDER = frozenset({
    "", "music", "downloads", "playlists", "unknown artist", "unknown", ".", "..",
})

#: Folder names that are a library ROOT rather than an artist. Kept separate from
#: the list above because "Various Artists" IS a legitimate artist FOLDER — it is
#: only invalid as an album name.
_NOT_AN_ARTIST_FOLDER = frozenset({
    "", "music", "downloads", "playlists", ".", "..",
})


def _album_from_folder(file_path: str) -> tuple[str, str]:
    """``(album, year)`` inferred from a file's PARENT folder name.

    The download naming format is ``{album_artist}/{year} - {album}/…``, so the
    parent folder is normally ``"2002 - Good To Go (EP)"``. Returns ``("", "")``
    when the folder does not look like an album, so a caller can never invent one.
    """
    parent = ""
    try:
        parent = os.path.basename(os.path.dirname(str(file_path or "")))
    except Exception:
        return "", ""
    text_parent = str(parent or "").strip()
    if not text_parent or text_parent.casefold() in _NOT_AN_ALBUM_FOLDER:
        return "", ""
    match = _FOLDER_YEAR_RE.match(text_parent)
    if match:
        return match.group(2).strip(), match.group(1)
    return text_parent, ""


def _artist_from_path(file_path: str) -> str:
    """The ARTIST folder: the grandparent of the file.

    One level up from the album folder in ``{album_artist}/{year} - {album}/file``.
    Empty when there is no such level, or when that level is a library ROOT
    (``/music``) — proposing "music" as an album artist would be worse than
    leaving the value alone.
    """
    try:
        album_dir = os.path.dirname(str(file_path or ""))
        candidate = os.path.basename(os.path.dirname(album_dir)).strip()
    except Exception:
        return ""
    if not candidate or candidate.casefold() in _NOT_AN_ARTIST_FOLDER:
        return ""
    return candidate


def scan_album_path_mismatches(artist: str, album: str) -> dict[str, Any]:
    """Rows of *artist*/*album* whose FILE PATH disagrees with the stored album.

    Read-only. Returns the proposed changes so the caller can warn BEFORE any
    write, which is the whole point: a repair that silently rewrote rows from a
    folder name would be the same class of mistake as the merge that caused this.
    """
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, title, artist, album, album_artist, year, file_path
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                      AND COALESCE(file_path, '') <> ''
                    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
                """),
                {"artist": artist, "album": album},
            ).mappings().all()
            candidates = [dict(r) for r in rows]
    except Exception as exc:
        logger.error("Album path scan failed", artist=artist, album=album, error=str(exc))
        return {"success": False, "error": str(exc), "changes": [], "scanned": 0}

    changes: list[dict[str, Any]] = []
    for row in candidates:
        file_path = str(row.get("file_path") or "")
        folder_album, folder_year = _album_from_folder(file_path)
        if not folder_album:
            continue
        if folder_album.casefold() == str(row.get("album") or "").casefold():
            continue

        proposed: dict[str, Any] = {"album": folder_album}
        if folder_year and str(row.get("year") or "").strip() != folder_year:
            proposed["year"] = folder_year
        folder_artist = _artist_from_path(file_path)
        current_album_artist = str(
            row.get("album_artist") or row.get("artist") or ""
        ).strip()
        if folder_artist and folder_artist.casefold() != current_album_artist.casefold():
            proposed["album_artist"] = folder_artist

        changes.append({
            "track_id": str(row.get("id") or ""),
            "title": str(row.get("title") or ""),
            "file_path": file_path,
            "current": {
                "album": str(row.get("album") or ""),
                "album_artist": current_album_artist,
                "year": str(row.get("year") or ""),
            },
            "proposed": proposed,
        })

    return {
        "success": True,
        "artist": artist,
        "album": album,
        "scanned": len(candidates),
        "changes": changes,
        "changed_count": len(changes),
    }


def repair_album_from_paths(
    artist: str,
    album: str,
    *,
    dry_run: bool = True,
    write_file_tags: bool = False,
) -> dict[str, Any]:
    """Re-derive ``album`` / ``year`` / ``album_artist`` from each file's folder.

    ``dry_run`` defaults to TRUE. Nothing is written unless the caller
    explicitly asks, because the inference is a heuristic on a folder name.

    ``write_file_tags`` defaults to FALSE as well: the DB is what the album page
    and the playlist builders read, and rewriting a file tag is the irreversible
    half (it is what destroyed the evidence in the first place). A file tag write
    is also 3–4 full passes over the audio file.
    """
    scan = scan_album_path_mismatches(artist, album)
    if not scan.get("success"):
        return scan
    changes = scan.get("changes") or []

    if dry_run or not changes:
        return {
            **scan,
            "dry_run": bool(dry_run),
            "repaired": 0,
            "files_written": 0,
        }

    repaired = 0
    stamped_paths: list[str] = []
    files_written = 0
    file_errors = 0

    for change in changes:
        proposed = change.get("proposed") or {}
        if not proposed:
            continue
        try:
            sets = ", ".join(f"{k} = :{k}" for k in proposed)
            params = dict(proposed)
            params["id"] = change["track_id"]
            with db_session() as session:
                session.execute(
                    text(f"UPDATE tracks SET {sets} WHERE CAST(id AS TEXT) = :id"),
                    params,
                )
            repaired += 1
            stamped_paths.append(change["file_path"])
        except Exception as exc:
            logger.debug(
                "Album path repair row failed",
                track_id=change.get("track_id"), error=str(exc),
            )

    # The DB now disagrees with the FILES, which would re-split the album on
    # Navidrome's next scan — the exact bug this repair is undoing. Stamp the
    # rows as a local correction so a later, older import cannot undo the repair.
    if stamped_paths:
        try:
            from services.scanning.reconciliation import (
                mark_local_tags_written_for_paths,
            )

            mark_local_tags_written_for_paths(stamped_paths)
        except Exception as exc:
            logger.debug("Album path repair stamp failed", error=str(exc))

    if write_file_tags:
        try:
            from services.metadata.tag_file_service import update_file_tags

            for change in changes:
                proposed = change.get("proposed") or {}
                if not proposed:
                    continue
                try:
                    tags = {
                        k: proposed[k]
                        for k in ("album", "album_artist", "year")
                        if k in proposed
                    }
                    if update_file_tags(change["file_path"], tags):
                        files_written += 1
                    else:
                        file_errors += 1
                except Exception:
                    file_errors += 1
        except Exception as exc:
            logger.debug("Album path repair tag writer unavailable", error=str(exc))

    logger.info(
        "Album repaired from file paths",
        artist=artist, album=album,
        rows=repaired, files=files_written, file_errors=file_errors,
    )
    return {
        **scan,
        "dry_run": False,
        "repaired": repaired,
        "files_written": files_written,
        "file_errors": file_errors,
    }
