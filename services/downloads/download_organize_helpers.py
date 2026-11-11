"""Download organisation helpers.

Low-level file operations for organising downloaded tracks into
the music library structure. Handles:
- Moving files from downloads to library.
- Applying naming conventions (``downloads.file_name_format``).
- FLAC -> MP3 conversion (``downloads.conversion.*``).
- Cleaning up source directories.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import structlog

from helpers.config_helpers import conversion_target, get_config, is_flac_conversion

logger = structlog.get_logger(__name__)

# ffmpeg conversion guard rail (mirrors old_system's transfer timeout).
_TRANSFER_TIMEOUT_SECONDS = 300


# =============================================================================
# HELPERS
# =============================================================================

def _first_non_empty(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str):
            if v.strip():
                return v.strip()
            continue
        return v
    return None


def _extract_year_for_path(year: Any) -> str:
    """Pull a 4-digit year out of a (possibly full-date) value."""
    if not year:
        return "Unknown"
    m = re.search(r"(19|20)\d{2}", str(year))
    return m.group(0) if m else "Unknown"


def _disc_number_int(value: Any) -> int:
    """Parse a disc number, tolerating ``1/2`` / ``"Disc 2"`` style values."""
    try:
        num = int(str(value).split("/")[0].strip())
    except Exception:
        return 1
    return num if num > 0 else 1


def _any_multi_disc(disc_values: Any) -> bool:
    """True when the given disc numbers describe a multi-disc album.

    A missing disc number is treated as disc 1, so ``[None, 2]`` is multi-disc.
    An empty sequence is NOT multi-disc — callers that need to distinguish
    "unknown" from "single disc" must check for rows themselves.
    """
    numbers = {_disc_number_int(value) for value in disc_values or []}
    return len(numbers) > 1 or any(num > 1 for num in numbers)


def _format_track_number_for_rename(
    track_number: Any,
    disc_number: Any = None,
    multi_disc: bool | None = None,
) -> str:
    """Format a track number for use in path format strings.

    ``multi_disc=True`` prefixes the disc for EVERY disc, so a two-disc album
    yields ``101``, ``102``, ``201``, ``202``. This matters because all discs
    share one album folder: without the disc-1 prefix both discs start at
    ``01`` and the numbers repeat.

    ``multi_disc=False`` always uses a plain 2-digit number. ``None`` keeps the
    legacy behaviour (prefix only disc 2+) for callers that have no album-wide
    disc information.
    """
    try:
        disc_num = _disc_number_int(disc_number)
        track_num = int(str(track_number).split("/")[0]) if track_number else 0
        if track_num < 0:
            track_num = 0
        if multi_disc is None:
            multi_disc = disc_num > 1
        if multi_disc:
            return f"{disc_num}{track_num:02d}"
        return f"{track_num:02d}"
    except Exception:
        return "00"


def _detect_multi_disc(album: Any, album_artist: Any) -> bool | None:
    """Look up whether an album has more than one disc in the library/queue.

    Returns ``None`` when neither table has rows for the album (unknown), so
    the caller can fall back to the legacy per-track rule instead of wrongly
    declaring a fresh multi-disc import single-disc.
    """
    album_text = str(album or "").strip()
    if not album_text:
        return None
    artist_text = str(album_artist or "").strip()
    try:
        from sqlalchemy import text

        from db.engine import db_session

        artist_clause = "COALESCE(NULLIF(album_artist, ''), artist) = :artist"
        params = {"album": album_text, "artist": artist_text}
        with db_session() as session:
            found_rows = False
            for table in ("tracks", "download_queue"):
                rows = session.execute(
                    text(
                        f"SELECT disc_number FROM {table} "
                        f"WHERE album = :album AND ({artist_clause})"
                    ),
                    params,
                ).fetchall()
                if rows:
                    found_rows = True
                    if _any_multi_disc([row[0] for row in rows]):
                        return True
        return False if found_rows else None
    except Exception:
        return None


def _build_target_path(
    root: str,
    album_artist: Any,
    year: Any,
    album: Any,
    artist: Any,
    title: Any,
    track_number: Any,
    source_file: str,
    disc_number: Any = None,
    multi_disc: bool | None = None,
) -> str:
    """Build the destination path using ``downloads.file_name_format`` from config."""
    ext = os.path.splitext(source_file)[1]
    file_name_format = _read_track_file_name_format()

    track_artist = artist or "Unknown Artist"
    album_artist_val = _normalize_album_artist_for_path(album_artist) or track_artist

    if multi_disc is None:
        multi_disc = _detect_multi_disc(album, album_artist_val)

    format_vars = {
        "album_artist": _sanitize_path_component(album_artist_val),
        "year": _extract_year_for_path(year),
        "album": _sanitize_path_component(album or "Unknown Album"),
        "track_number": _format_track_number_for_rename(
            track_number, disc_number, multi_disc=multi_disc
        ),
        "artist": _sanitize_path_component(track_artist),
        "title": _sanitize_path_component(title or "Unknown Title"),
    }

    try:
        relative_path = file_name_format.format(**format_vars)
    except Exception:
        relative_path = (
            f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
            f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}"
        )

    relative_path = relative_path.strip().replace("\\", "/").lstrip("/")
    parts = []
    for part in relative_path.split("/"):
        clean = _sanitize_path_component(part)
        if clean and clean not in (".", ".."):
            parts.append(clean)
    relative_path = "/".join(parts) or "Unknown Artist"

    return os.path.join(root, f"{relative_path}{ext}")


def _read_track_file_name_format() -> str:
    """Read configurable file naming format."""
    try:
        cfg = get_config() or {}
        downloads_cfg = cfg.get("downloads") or {}
        fmt = downloads_cfg.get("file_name_format")

        if isinstance(fmt, str) and fmt.strip():
            return fmt.strip()
    except Exception:
        pass

    return "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"


def _sanitize_path_component(value: str) -> str:
    if not value:
        return ""
    invalid = '<>:"|?*\\'
    for char in invalid:
        value = value.replace(char, "_")
    return value.strip().strip(".")


def _normalize_album_artist_for_path(value: str) -> str:
    normalized = str(value or "").strip()

    key = (
        normalized.lower()
        .replace("-", " ")
        .replace("_", " ")
        .replace(".", " ")
    )
    key = " ".join(key.split())

    if key in ("various", "various artist", "various artists", "va", "v/a") or key.startswith("various"):
        return "Various Artists"

    return normalized


def _read_download_conversion_settings() -> dict[str, Any]:
    """Read ``downloads.conversion`` settings from config with safe defaults."""
    settings: dict[str, Any] = {
        "enabled": False,
        "mode": "flac_to_mp3",
        "mp3_bitrate_kbps": 320,
        "original_handling": "move_to_original",
        "original_subfolder": "Original",
    }
    try:
        cfg = get_config() or {}
        conversion_cfg = (cfg.get("downloads") or {}).get("conversion") or {}
        if isinstance(conversion_cfg, dict):
            settings["enabled"] = bool(conversion_cfg.get("enabled", settings["enabled"]))
            mode = str(conversion_cfg.get("mode", settings["mode"]) or settings["mode"]).strip().lower()
            if is_flac_conversion(mode) or mode == "none":
                settings["mode"] = mode
            try:
                bitrate = int(conversion_cfg.get("mp3_bitrate_kbps", settings["mp3_bitrate_kbps"]))
                settings["mp3_bitrate_kbps"] = max(96, min(320, bitrate))
            except Exception:
                pass
            handling = str(
                conversion_cfg.get("original_handling", settings["original_handling"])
                or settings["original_handling"]
            ).strip().lower()
            if handling in ("move_to_original", "delete"):
                settings["original_handling"] = handling
            subfolder = str(
                conversion_cfg.get("original_subfolder", settings["original_subfolder"])
                or settings["original_subfolder"]
            ).strip()
            settings["original_subfolder"] = _sanitize_path_component(subfolder) or "Original"
    except Exception as exc:
        logger.debug("Could not read download conversion settings", error=str(exc))
    return settings


def _is_under_original_subfolder(path_value: Any, downloads_root: Any, original_subfolder: Any) -> bool:
    if not path_value or not downloads_root or not original_subfolder:
        return False
    try:
        abs_path = os.path.abspath(path_value)
        original_root = os.path.abspath(os.path.join(downloads_root, original_subfolder))
        return os.path.commonpath([abs_path, original_root]) == original_root
    except Exception:
        return False


def _build_original_archive_path(source_path: str, downloads_root: str, original_subfolder: str) -> str:
    original_root = os.path.join(downloads_root, original_subfolder)
    abs_source = os.path.abspath(source_path)
    abs_downloads = os.path.abspath(downloads_root)

    try:
        if os.path.commonpath([abs_source, abs_downloads]) == abs_downloads:
            rel = os.path.relpath(abs_source, abs_downloads)
            candidate = os.path.join(original_root, rel)
        else:
            candidate = os.path.join(original_root, os.path.basename(abs_source))
    except Exception:
        candidate = os.path.join(original_root, os.path.basename(abs_source))

    base, ext = os.path.splitext(candidate)
    counter = 1
    unique_candidate = candidate
    while os.path.exists(unique_candidate):
        unique_candidate = f"{base}_{counter}{ext}"
        counter += 1
    return unique_candidate


def _resolve_downloads_root() -> str:
    try:
        from services.infrastructure.filesystem_service import resolve_downloads_dir
        return resolve_downloads_dir()
    except Exception:
        return os.environ.get("DOWNLOADS_DIR", "/downloads")


def _convert_flac_and_handle_original(source_path: str, dest_path: str, settings: dict[str, Any]) -> bool:
    source_path = str(source_path).replace("\\", "/")
    bitrate_kbps = int(settings.get("mp3_bitrate_kbps", 320) or 320)
    target = conversion_target(settings.get("mode")) or "mp3"
    label = f"FLAC to {target.upper()}"
    codec_args = (
        ["-codec:a", "aac", "-b:a", f"{bitrate_kbps}k"]
        if target == "m4a"
        else ["-codec:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-id3v2_version", "3"]
    )
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-vn", *codec_args,
        "-map_metadata", "0", dest_path,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_TRANSFER_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"{label} conversion timed out", timeout_seconds=_TRANSFER_TIMEOUT_SECONDS, source=source_path)
        return False
    except FileNotFoundError:
        logger.warning(f"ffmpeg is required for {label} conversion but is not available in PATH")
        return False
    except Exception as exc:
        logger.warning(f"{label} conversion launch failed", error=str(exc))
        return False

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-5:]
        logger.warning(f"{label} conversion failed", stderr=" | ".join(stderr_tail))
        return False

    downloads_root = _resolve_downloads_root()
    handling = settings.get("original_handling", "move_to_original")
    subfolder = settings.get("original_subfolder", "Original")
    try:
        if handling == "delete":
            os.remove(source_path)
        elif not _is_under_original_subfolder(source_path, downloads_root, subfolder):
            archive_path = _build_original_archive_path(source_path, downloads_root, subfolder)
            os.makedirs(os.path.dirname(archive_path), exist_ok=True)
            shutil.move(source_path, archive_path)
            logger.info("Archived original FLAC", archive_path=archive_path)
    except Exception as archive_err:
        logger.warning("Conversion succeeded but original handling failed", source=source_path, error=str(archive_err))

    logger.info(f"Converted {label} successfully", source=source_path, dest=dest_path)
    return True


def is_match(path: str, item: dict[str, Any]) -> bool:
    filename = os.path.basename(path).lower()
    artist = (item.get("artist") or "").lower()
    title = (item.get("title") or "").lower()
    return artist in filename and title in filename


def _read_file_title(file_path: str) -> str:
    try:
        from helpers.metadata_reader import read_mp3_metadata
        meta = read_mp3_metadata(file_path) or {}
        return str(meta.get("title") or "").strip()
    except Exception:
        return ""


def _titles_match(a: str, b: str) -> bool:
    norm = lambda v: re.sub(r"\s+", " ", str(v or "").strip().casefold())
    return bool(norm(a) and norm(a) == norm(b))


def move_track_to_library(
    track: dict[str, Any],
    release_metadata: dict[str, Any],
    music_root: str,
    *,
    replace_path: str | None = None,
) -> Dict[str, Any]:
    file_path = track.get("file_path")

    if not file_path:
        return {"success": False, "error": "Missing file_path"}

    file_path = str(file_path).replace("\\", "/")

    conversion_settings = _read_download_conversion_settings()
    source_ext = os.path.splitext(file_path)[1].lower()
    target_format = conversion_target(conversion_settings.get("mode"))
    converting = bool(
        conversion_settings.get("enabled")
        and target_format
        and source_ext == ".flac"
    )

    multi_disc = track.get("multi_disc")
    if multi_disc is None:
        multi_disc = release_metadata.get("multi_disc")

    target_path = _build_target_path(
        music_root,
        release_metadata.get("album_artist"),
        release_metadata.get("year"),
        release_metadata.get("album"),
        track.get("artist"),
        track.get("title"),
        track.get("track_number"),
        file_path,
        disc_number=track.get("disc_number"),
        multi_disc=multi_disc,
    )

    if converting and target_format:
        target_path = f"{os.path.splitext(target_path)[0]}.{target_format}"

    # A redownload-for-a-better-version can resolve to the OLD file's exact
    # path. The duplicate check below would then "skip" the new file and leave
    # the wrong version in place. Move the old file aside first and restore it
    # if the new move/conversion fails.
    replace_abs = os.path.realpath(replace_path) if replace_path else ""
    target_abs = os.path.realpath(target_path) if target_path else ""
    replacement_target = bool(replace_abs and target_abs and replace_abs == target_abs)
    backup_path: str | None = None

    def _restore_backup() -> None:
        if not backup_path:
            return
        try:
            if os.path.exists(target_path):
                os.remove(target_path)
            os.replace(backup_path, target_path)
        except Exception as restore_exc:
            logger.warning(
                "Could not restore replaced version after a failed import",
                target=target_path, backup=backup_path, error=str(restore_exc),
            )

    if replacement_target and os.path.exists(target_path):
        backup_path = f"{target_path}.replace-{os.getpid()}"
        try:
            os.replace(target_path, backup_path)
        except Exception as exc:
            return {"success": False, "error": f"could not back up old version: {exc}"}

    if os.path.exists(target_path):
        if _titles_match(_read_file_title(target_path), track.get("title")):
            logger.info(
                "Target already exists and matches track — skipping duplicate import",
                target_path=target_path,
                artist=track.get("artist"),
                title=track.get("title"),
            )
            return {"success": True, "target_path": target_path, "duplicate": True}
            
        suffix = Path(target_path).suffix
        target_path = f"{target_path[:-len(suffix)]}_{os.getpid()}{suffix}" if target_path.endswith(suffix) else f"{target_path}_{os.getpid()}"

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        if converting:
            if not _convert_flac_and_handle_original(file_path, target_path, conversion_settings):
                _restore_backup()
                return {"success": False, "error": f"{target_format.upper()} conversion failed"}
            if backup_path:
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
            return {"success": True, "target_path": target_path, "converted": True}
        shutil.move(file_path, target_path)
    except Exception as exc:
        _restore_backup()
        return {"success": False, "error": str(exc)}

    if backup_path:
        try:
            os.remove(backup_path)
        except Exception:
            pass
    return {"success": True, "target_path": target_path}


_AUDIO_EXTS = {".flac", ".wav", ".alac", ".aiff", ".ape", ".wv", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}


def _filename_title_candidates(filename: str) -> list[str]:
    base = os.path.splitext(os.path.basename(filename))[0]
    base = re.sub(r"^\d{1,4}\s*[-._]\s*", "", base)
    base = re.sub(r"_\d{2,}$", "", base)
    parts = [p.strip() for p in re.split(r"\s*[-–—]\s*", base) if p.strip()]
    candidates = []
    if len(parts) >= 2:
        candidates.append(re.sub(r"\s+", " ", parts[-1].casefold()))
    candidates.append(re.sub(r"\s+", " ", base.casefold()))
    return candidates


#: Two files are only considered the same track when their durations agree
#: within this many seconds. An UNKNOWN duration means "not a duplicate".
_DEDUPE_DURATION_TOLERANCE_SECONDS = 2.0
#: The loser is RENAMED to this suffix, never removed. It is no longer an audio
#: extension, so neither the scanner nor a player will pick it up again.
_DEDUPE_QUARANTINE_SUFFIX = ".duplicate"


def _dedupe_tag_value(tags: Any, *keys: str) -> str:
    """First non-empty tag value, for both MP3 frames and Vorbis comments."""
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text_value = str(value).strip()
        if text_value:
            return text_value
    return ""


def _read_dedupe_metadata(path: str) -> tuple[str, str, float, int, int]:
    """``(artist, title, duration_seconds, size_bytes, quality_score)``."""
    lossless = os.path.splitext(path)[1].lower() in {
        ".flac", ".wav", ".alac", ".aiff", ".ape", ".wv"
    }
    score = 100000 if lossless else 0
    artist = title = ""
    duration = 0.0
    try:
        size = int(os.path.getsize(path))
    except OSError:
        size = 0
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path)
        if audio is not None:
            info = getattr(audio, "info", None)
            if info is not None:
                score += int(getattr(info, "bitrate", 0) or 0) // 1000
                score += int(getattr(info, "sample_rate", 0) or 0) // 100
                duration = float(getattr(info, "length", 0) or 0)
            tags = getattr(audio, "tags", None)
            if tags:
                artist = _dedupe_tag_value(tags, "TPE1", "artist", "ARTIST")
                title = _dedupe_tag_value(tags, "TIT2", "title", "TITLE")
    except Exception:
        pass
    return artist, title, duration, size, score


def _dedupe_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _quarantine_duplicate(path: str) -> str | None:
    """Rename the duplicate out of the audio namespace. NEVER deletes it."""
    target = path + _DEDUPE_QUARANTINE_SUFFIX
    counter = 1
    while os.path.exists(target):
        target = f"{path}{_DEDUPE_QUARANTINE_SUFFIX}.{counter}"
        counter += 1
    try:
        os.replace(path, target)
        return target
    except Exception as exc:
        logger.debug("Failed to quarantine duplicate", path=path, error=str(exc))
        return None


def dedupe_library_folder(folder_path: str, keep_path: str | None = None) -> dict[str, Any]:
    """Quarantine duplicate audio files in ONE library folder.

    ⚠️ THIS MUST NEVER DELETE. The reported pair

        removed: 05. Stray Kids - BEWARE.mp3
        kept:    02. Stray Kids - Double Knot -Japanese ver.-.mp3

    are DIFFERENT tracks: a bad/shared tag made the old `artist::title` key
    group them, and the loser was removed irrecoverably. A candidate now needs
    independent agreement on ALL of:

    * non-empty artist / title, and a KNOWN duration;
    * the tag title **and** the title parsed from the FILENAME;
    * duration within ``_DEDUPE_DURATION_TOLERANCE_SECONDS``.

    The loser is renamed to ``<file>.duplicate`` (quarantined) so it can be
    reviewed and deleted by hand. Nothing is ever removed here.
    """
    if not folder_path or not os.path.isdir(folder_path):
        return {"removed": 0, "quarantined": 0, "groups": 0}

    try:
        from services.downloads.download_scan_service import _duplicate_cleanup_config

        if not _duplicate_cleanup_config("delete_duplicate_files", True):
            return {
                "removed": 0,
                "quarantined": 0,
                "groups": 0,
                "skipped": True,
                "reason": "duplicate cleanup disabled",
            }
    except Exception:
        pass

    keep_abs = os.path.abspath(keep_path) if keep_path else None

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    try:
        entries = list(os.scandir(folder_path))
    except OSError as exc:
        logger.debug("Library folder dedup scan failed", folder=folder_path, error=str(exc))
        return {"removed": 0, "quarantined": 0, "groups": 0}

    for entry in entries:
        if not entry.is_file():
            continue
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in _AUDIO_EXTS:
            continue

        artist, title, duration, size, quality = _read_dedupe_metadata(entry.path)
        filename_title = (_filename_title_candidates(entry.name) or [""])[0]
        if not title:
            title = filename_title
        if not title or duration <= 0:
            # Unprovable: no tag/filename title or no duration. Leave it alone.
            logger.debug(
                "Duplicate check skipped — no usable title/duration",
                path=entry.path,
                title=title,
                duration=duration,
            )
            continue

        # The FILENAME title is an independent witness. A mis-tagged BEWARE file
        # cannot pair with a file whose NAME says "Double Knot".
        group_key = (
            _dedupe_key(artist),
            _dedupe_key(title),
            _dedupe_key(filename_title),
        )
        grouped.setdefault(group_key, []).append({
            "path": entry.path,
            "artist": artist,
            "title": title,
            "filename_title": filename_title,
            "duration": duration,
            "size": size,
            "quality": quality,
        })

    quarantined = 0
    group_count = 0
    for group_key, files in grouped.items():
        if len(files) < 2:
            continue
        files.sort(key=lambda item: item["duration"])
        clusters: list[list[dict[str, Any]]] = []
        for item in files:
            if (
                clusters
                and abs(
                    item["duration"] - clusters[-1][-1]["duration"]
                ) <= _DEDUPE_DURATION_TOLERANCE_SECONDS
            ):
                clusters[-1].append(item)
            else:
                clusters.append([item])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            group_count += 1
            cluster.sort(key=lambda item: (-item["quality"], item["path"]))
            keep_item = cluster[0]
            if keep_abs:
                for item in cluster:
                    if os.path.abspath(item["path"]) == keep_abs:
                        keep_item = item
                        break

            for item in cluster:
                if os.path.abspath(item["path"]) == os.path.abspath(keep_item["path"]):
                    continue
                if keep_abs and os.path.abspath(item["path"]) == keep_abs:
                    continue
                quarantine_path = _quarantine_duplicate(item["path"])
                if quarantine_path is None:
                    continue
                quarantined += 1
                logger.info(
                    "Quarantined duplicate track from library folder",
                    folder=folder_path,
                    quarantined_path=item["path"],
                    quarantine_path=quarantine_path,
                    kept=keep_item["path"],
                    group_key=list(group_key),
                    kept_artist=keep_item["artist"],
                    kept_title=keep_item["title"],
                    kept_filename_title=keep_item["filename_title"],
                    kept_duration=round(keep_item["duration"], 3),
                    kept_size=keep_item["size"],
                    removed_artist=item["artist"],
                    removed_title=item["title"],
                    removed_filename_title=item["filename_title"],
                    removed_duration=round(item["duration"], 3),
                    removed_size=item["size"],
                    criterion=(
                        "same artist + tag title + filename title + "
                        f"duration within {_DEDUPE_DURATION_TOLERANCE_SECONDS:g}s"
                    ),
                    used="tags + filename + duration (no audio fingerprint/hash)",
                )

    if quarantined:
        logger.info(
            "Library folder dedup complete",
            folder=folder_path,
            quarantined=quarantined,
            groups=group_count,
        )
    return {
        "removed": quarantined,
        "quarantined": quarantined,
        "groups": group_count,
    }
