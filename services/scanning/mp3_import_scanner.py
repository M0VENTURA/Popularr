"""MP3 metadata import scanner.

Scans the library directory for new audio files and imports their metadata
into the database. Replaces the old monolithic ``scan_mp3_import.py``.

Key Responsibilities:
    - Walks the music library directory for new audio files.
    - Extracts metadata using ``helpers.metadata_reader``.
    - Persists new tracks to the database via ``db.context``.
    - Tracks scan progress via checkpoint files.

Supported Formats:
    MP3, FLAC, M4A, WAV, OGG, OPUS

Architecture:
    Uses ``db.context`` for all database operations (no raw psycopg2).
    Progress tracking via ``scan_state`` checkpoint files.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from db.engine import db_session
from helpers.metadata_reader import read_mp3_metadata
from helpers.logging_config import log_unified
from services.scanning.scan_state import (
    get_scan_progress_path,
    read_progress_file,
    write_progress_with_current_artist,
)

logger = structlog.get_logger(__name__)

# Audio formats that the scanner can read.
from helpers.config_helpers import get_supported_audio_formats
SUPPORTED_FORMATS = frozenset(get_supported_audio_formats() | {".opus"})


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def _progress_path() -> str:
    return get_scan_progress_path("mp3_import")


def _write_progress(
    is_running: bool,
    current_file: str = "",
    processed: int = 0,
    total: int = 0,
    imported: int = 0,
    matched: int = 0,
    skipped: int = 0,
    errors: int = 0,
    status: str = "scanning",
    **extra: Any,
) -> None:
    pct = int((processed / total * 100) if total > 0 else 0)
    write_progress_with_current_artist(
        _progress_path(),
        "mp3_import",
        is_running,
        extra={
            "status": status,
            "current_file": current_file,
            "processed": processed,
            "total": total,
            "imported": imported,
            "matched": matched,
            "skipped": skipped,
            "errors": errors,
            "percent": pct,
            **extra,
        },
    )


def _stop_requested() -> bool:
    """Return True when the user has requested a graceful stop."""
    state = read_progress_file(_progress_path())
    return bool(state.get("stop_requested", False))


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class MP3ImportScanner:
    """Scans audio files and imports/updates their metadata in the database."""

    def __init__(
        self,
        directory: str | None = None,
        dry_run: bool = False,
        verbose: bool = False,
        mode: str = "database",
    ):
        """
        Args:
            directory: Path to scan (directory mode only).
            dry_run: When True, do not persist any changes.
            verbose: When True, log detailed progress.
            mode: ``"database"`` (read paths from tracks table) or
                  ``"directory"`` (walk a filesystem path).
        """
        self.directory = directory
        self.dry_run = dry_run
        self.verbose = verbose
        self.mode = mode

        # Running totals
        self.total_files = 0
        self.processed = 0
        self.imported = 0
        self.matched = 0
        self.skipped = 0
        self.errors = 0
        self.start_time: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> dict[str, Any]:
        """Run the scan and return a results dict."""
        log_unified(f"[MP3_SCANNER] Starting MP3 import scan from: {self.music_root}")
        self.start_time = datetime.now()

        _write_progress(True, status="starting")

        try:
            if self.mode == "database":
                self._scan_from_database()
            else:
                self._scan_directory()

        except Exception as exc:
            logger.exception("MP3 import scan failed", error=str(exc))
            self.errors += 1
            _write_progress(False, status="error", error=str(exc))
            return self._results(success=False, error=str(exc))

        elapsed = (datetime.now() - self.start_time).total_seconds()
        _write_progress(False, status="complete")
        log_unified(
            f"MP3 import done: {self.processed} processed, {self.imported} imported, "
            f"{self.matched} matched, {self.skipped} skipped, {self.errors} errors in {elapsed:.1f}s"
        )
        return self._results(success=True, elapsed=elapsed)

    # ------------------------------------------------------------------
    # Mode: database — read file paths from tracks table
    # ------------------------------------------------------------------

    def _scan_from_database(self) -> None:
        """Iterate over tracks that have a file_path and update from file."""
        log_unified("Scanning from database tracks …")

        rows = self._load_database_tracks()
        self.total_files = len(rows)
        logger.info("Found tracks with file paths", count=self.total_files)

        if not rows:
            return

        for idx, (track_id, file_path, db_artist, db_title, db_album) in enumerate(rows, 1):
            if _stop_requested():
                log_unified("Graceful stop requested — aborting scan")
                break

            self._update_track_from_file(track_id, file_path, db_artist, db_title, db_album)

            if idx % 50 == 0:
                logger.info("Progress", processed=idx, total=self.total_files)
                _write_progress(
                    True,
                    processed=self.processed,
                    total=self.total_files,
                    imported=self.imported,
                    matched=self.matched,
                    skipped=self.skipped,
                    errors=self.errors,
                )

    def _load_database_tracks(self) -> list[tuple]:
        """Return (id, file_path, artist, title, album) for tracks with files."""
        suffix_patterns = " OR ".join(
            f"file_path LIKE '%%{ext}'" for ext in SUPPORTED_FORMATS
        )
        with db_session() as session:
            result = session.execute(text(f"""
                SELECT id, file_path, artist, title, album
                FROM tracks
                WHERE file_path IS NOT NULL
                  AND file_path != ''
                  AND ({suffix_patterns})
                ORDER BY artist, album
            """))
            return result.fetchall() or []

    # ------------------------------------------------------------------
    # Mode: directory — walk filesystem for audio files
    # ------------------------------------------------------------------

    def _scan_directory(self) -> None:
        """Walk *directory* and import every audio file found."""
        root = self.directory or "/music"
        if not os.path.isdir(root):
            raise NotADirectoryError(f"Directory not found: {root}")

        # Count files first for progress reporting.
        audio_files: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if Path(fn).suffix.lower() in SUPPORTED_FORMATS:
                    audio_files.append(os.path.join(dirpath, fn))

        self.total_files = len(audio_files)
        logger.info("Found audio files", count=self.total_files, root=root)

        if not audio_files:
            return

        for idx, file_path in enumerate(audio_files, 1):
            if _stop_requested():
                log_unified("Graceful stop requested — aborting scan")
                break

            self._scan_single_file(file_path)

            if idx % 10 == 0:
                logger.info("Progress", processed=idx, total=self.total_files)
                _write_progress(
                    True,
                    current_file=os.path.basename(file_path),
                    processed=self.processed,
                    total=self.total_files,
                    imported=self.imported,
                    matched=self.matched,
                    skipped=self.skipped,
                    errors=self.errors,
                )

    def _scan_single_file(self, file_path: str) -> None:
        """Read metadata from *file_path* and import/update the DB row."""
        try:
            metadata = read_mp3_metadata(file_path) or {}
            if not metadata.get("title"):
                self.skipped += 1
                if self.verbose:
                    logger.info("Skipped file: no title metadata", file=file_path)
                return

            self.processed += 1
            success, msg = self._import_track(file_path, metadata)
            if self.verbose:
                if success:
                    logger.info("Import result", message=msg, file=file_path)
                else:
                    logger.warning("Import result", message=msg, file=file_path)

        except Exception as exc:
            self.errors += 1
            logger.warning("Error processing file", file=file_path, error=str(exc))

    # ------------------------------------------------------------------
    # Import / update logic
    # ------------------------------------------------------------------

    def _import_track(self, file_path: str, metadata: dict[str, Any]) -> tuple[bool, str]:
        """Insert or update a track row from *metadata*.

        Returns (success, message).
        """
        title = (metadata.get("title") or "Unknown Title").strip()
        artist = (metadata.get("artist") or "Unknown Artist").strip()
        album = (metadata.get("album") or "Unknown Album").strip()

        try:
            existing_id = self._find_existing_track(title, artist, album, file_path)

            if existing_id:
                if not self.dry_run:
                    self._update_track_row(existing_id, file_path, metadata)
                self.matched += 1
                return True, f"Matched: {artist} — {title}"
            else:
                if not self.dry_run:
                    self._insert_track_row(file_path, metadata)
                self.imported += 1
                return True, f"Imported: {artist} — {title}"

        except Exception as exc:
            self.errors += 1
            return False, f"Error: {exc}"

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _find_existing_track(
        self, title: str, artist: str, album: str, file_path: str = ""
    ) -> str | None:
        """Return a track id when a matching row already exists.

        ⭐ THE FILE PATH IS CHECKED FIRST.

        Metadata-matching alone made a re-import non-idempotent: if the file's
        title/artist/album tags had changed since the first import, no row
        matched and a SECOND row was inserted for the SAME path. The album page
        then listed the song twice. A path match is the strongest identity there
        is — the file itself — so it wins before any metadata heuristic.
        """
        with db_session() as session:
            if file_path:
                result = session.execute(
                    text("""
                        SELECT id FROM tracks
                        WHERE file_path IS NOT NULL
                          AND LOWER(REPLACE(file_path, :backslash, '/'))
                              = LOWER(REPLACE(:file_path, :backslash, '/'))
                        LIMIT 1
                    """),
                    {"file_path": file_path, "backslash": "\\"},
                )
                row = result.fetchone()
                if row:
                    return str(row[0])

            result = session.execute(
                text("""
                    SELECT id FROM tracks
                    WHERE LOWER(title) = LOWER(:title)
                      AND LOWER(artist) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    LIMIT 1
                """),
                {"title": title, "artist": artist, "album": album},
            )
            row = result.fetchone()
            if row:
                return str(row[0])

            # Broader match on artist + title only
            result = session.execute(
                text("""
                    SELECT id FROM tracks
                    WHERE LOWER(title) = LOWER(:title)
                      AND LOWER(artist) = LOWER(:artist)
                    LIMIT 1
                """),
                {"title": title, "artist": artist},
            )
            row = result.fetchone()
            return str(row[0]) if row else None

    def _update_track_row(self, track_id: str, file_path: str, meta: dict[str, Any]) -> None:
        """Update an existing track with fresh metadata from the file."""
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE tracks
                       SET file_path        = COALESCE(:file_path, file_path),
                           album_artist     = COALESCE(:album_artist, album_artist),
                           track_number     = COALESCE(:track_number, track_number),
                           disc_number      = COALESCE(:disc_number, disc_number),
                           year             = COALESCE(:year, year),
                           genres           = COALESCE(:genres, genres),
                           comment          = COALESCE(:comment, comment),
                           bpm              = COALESCE(:bpm, bpm),
                           composer         = COALESCE(:composer, composer),
                           isrc             = COALESCE(:isrc, isrc),
                           last_scanned     = CURRENT_TIMESTAMP
                     WHERE id = :id
                    """),
                {
                    "file_path": file_path,
                    "album_artist": meta.get("album_artist"),
                    "track_number": meta.get("track_number"),
                    "disc_number": meta.get("disc_number"),
                    "year": meta.get("date"),
                    "genres": meta.get("genre", ""),
                    "comment": meta.get("comment", ""),
                    "bpm": meta.get("bpm"),
                    "composer": meta.get("composer"),
                    "isrc": meta.get("isrc"),
                    "id": track_id,
                },
            )

    def _insert_track_row(self, file_path: str, meta: dict[str, Any]) -> None:
        """Insert a new track from file metadata."""
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO tracks
                        (artist, title, album, album_artist, file_path,
                         track_number, disc_number, year, genres, bpm,
                         composer, isrc, comment, last_scanned)
                    VALUES (:artist, :title, :album, :album_artist, :file_path,
                            :track_number, :disc_number, :year, :genres, :bpm,
                            :composer, :isrc, :comment, CURRENT_TIMESTAMP)
                """),
                {
                    "artist": meta.get("artist", "Unknown Artist"),
                    "title": meta.get("title", "Unknown Title"),
                    "album": meta.get("album", "Unknown Album"),
                    "album_artist": meta.get("album_artist"),
                    "file_path": file_path,
                    "track_number": meta.get("track_number"),
                    "disc_number": meta.get("disc_number"),
                    "year": meta.get("date"),
                    "genres": meta.get("genre", ""),
                    "bpm": meta.get("bpm"),
                    "composer": meta.get("composer"),
                    "isrc": meta.get("isrc"),
                    "comment": meta.get("comment", ""),
                },
            )

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _results(self, success: bool, error: str = "", elapsed: float = 0) -> dict[str, Any]:
        return {
            "success": success,
            "error": error or None,
            "scan_type": "mp3_import",
            "mode": self.mode,
            "dry_run": self.dry_run,
            "total_files": self.total_files,
            "processed": self.processed,
            "imported": self.imported,
            "matched": self.matched,
            "skipped": self.skipped,
            "errors": self.errors,
            "elapsed_seconds": elapsed,
            "timestamp": datetime.now().isoformat(),
        }
