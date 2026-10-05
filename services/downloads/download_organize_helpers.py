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


def _format_track_number_for_rename(track_number: Any, disc_number: Any = None) -> str:
    """Format a track number for use in path format strings (2-digit, disc-aware)."""
    try:
        disc_num = int(str(disc_number).split("/")[0]) if disc_number else 1
        track_num = int(str(track_number).split("/")[0]) if track_number else 0
        if disc_num <= 0:
            disc_num = 1
        if disc_num > 1:
            return f"{disc_num}{track_num:02d}"
        return f"{track_num:02d}"
    except Exception:
        return "00"


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
) -> str:
    """Build the destination path using ``downloads.file_name_format`` from config."""
    ext = os.path.splitext(source_file)[1]
    file_name_format = _read_track_file_name_format()

    track_artist = artist or "Unknown Artist"
    album_artist_val = _normalize_album_artist_for_path(album_artist) or track_artist

    format_vars = {
        "album_artist": _sanitize_path_component(album_artist_val),
        "year": _extract_year_for_path(year),
        "album": _sanitize_path_component(album or "Unknown Album"),
        "track_number": _format_track_number_for_rename(track_number, disc_number),
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
            # WHITELIST of accepted modes: anything else was silently dropped
            # back to the default, so a new mode had to be added HERE as well
            # as everywhere it is read (see helpers.config_helpers).
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
        return resolve_downloads_dir(prefer_music_subfolder=False)
    except Exception:
        return os.environ.get("DOWNLOADS_DIR", "/downloads")


def _convert_flac_and_handle_original(source_path: str, dest_path: str, settings: dict[str, Any]) -> bool:
    """Convert FLAC into the configured format via ffmpeg, then handle the original per config.

    The target container comes from ``conversion_target(mode)`` — MP3
    (libmp3lame + ID3v2.3) or M4A (AAC), so both share the original-handling
    logic below instead of it being duplicated per format.
    """
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


def move_track_to_library(track: dict[str, Any], release_metadata: dict[str, Any], music_root: str) -> Dict[str, Any]:
    """Move a track into the configured library structure."""
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
    )

    if converting and target_format:
        target_path = f"{os.path.splitext(target_path)[0]}.{target_format}"

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
                return {"success": False, "error": f"{target_format.upper()} conversion failed"}
            return {"success": True, "target_path": target_path, "converted": True}
        shutil.move(file_path, target_path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    return {"success": True, "target_path": target_path}


# Audio extensions considered when sweeping a library folder for duplicates.
_AUDIO_EXTS = {".flac", ".wav", ".alac", ".aiff", ".ape", ".wv", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}


def _filename_title_candidates(filename: str) -> list[str]:
    """Extract likely track-title candidates from an audio filename.

    Handles common layouts: ``02 - Artist - Title.ext``, ``Artist - Title.ext``,
    ``02 Title.ext``, and ``Title.ext``.  Returns normalized (casefolded,
    whitespace-collapsed) candidates, most-specific first.
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    # Strip leading track numbers ("02 - ...", "02. ...", "0206 - ...").
    base = re.sub(r"^\d{1,4}\s*[-._]\s*", "", base)
    # Strip a trailing pid-suffix from the duplicate fallback ("Title_12345").
    base = re.sub(r"_\d{2,}$", "", base)
    # Split "Artist - Title" / "Artist - Album - Title" on the LAST separator.
    parts = [p.strip() for p in re.split(r"\s*[-–—]\s*", base) if p.strip()]
    candidates = []
    if len(parts) >= 2:
        candidates.append(re.sub(r"\s+", " ", parts[-1].casefold()))
    candidates.append(re.sub(r"\s+", " ", base.casefold()))
    return candidates


def dedupe_library_folder(folder_path: str, keep_path: str | None = None) -> dict[str, Any]:
    """Sweep a library folder and remove duplicate copies of the same track.

    Groups audio files by normalized title (last ``-``-separated segment and
    full basename, with leading track numbers and pid-suffixes stripped).
    For each group with more than one file, keeps the highest-quality file
    (lossless > bitrate > sample rate) and deletes the rest.

    This cleans up the accumulated duplicates from repeated downloads of the
    same track — e.g. ``02 - Lay Your Head to Rest.flac``,
    ``02 - Lay Your Head to Rest_12345.flac`` — leaving a single best copy.

    ``keep_path`` is never deleted (the file just moved in).
    """
    if not folder_path or not os.path.isdir(folder_path):
        return {"removed": 0, "groups": 0}

    keep_abs = os.path.abspath(keep_path) if keep_path else None

    def _quality(path: str) -> int:
        score = 100000 if os.path.splitext(path)[1].lower() in {".flac", ".wav", ".alac", ".aiff", ".ape", ".wv"} else 0
        try:
            from mutagen import File as MutagenFile
            audio = MutagenFile(path)
            if audio and hasattr(audio, "info"):
                score += int(getattr(audio.info, "bitrate", 0) or 0) // 1000
                score += int(getattr(audio.info, "sample_rate", 0) or 0) // 100
        except Exception:
            pass
        return score

    # group normalized-title -> list of (path, quality)
    groups: dict[str, list[tuple[str, int]]] = {}
    for entry in os.scandir(folder_path):
        if not entry.is_file():
            continue
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in _AUDIO_EXTS:
            continue
        candidates = _filename_title_candidates(entry.name)
        key = candidates[0] if candidates else entry.name.casefold()
        groups.setdefault(key, []).append((entry.path, _quality(entry.path)))

    removed = 0
    group_count = 0
    for key, files in groups.items():
        if len(files) < 2:
            continue
        group_count += 1
        # Keep the highest quality; on ties keep the first (alphabetical) path.
        files.sort(key=lambda fp: (-fp[1], fp[0]))
        keep = files[0][0]
        if keep_abs and os.path.abspath(keep) != keep_abs:
            # Prefer keeping the just-moved file even if quality ties.
            if os.path.abspath(keep_abs) in {os.path.abspath(f) for f, _ in files}:
                keep = keep_abs
        for path, _ in files:
            if os.path.abspath(path) == os.path.abspath(keep):
                continue
            if keep_abs and os.path.abspath(path) == keep_abs:
                continue
            try:
                os.remove(path)
                removed += 1
                logger.info(
                    "Removed duplicate track from library folder",
                    folder=folder_path, removed_path=path, kept=keep,
                )
            except Exception as exc:
                logger.debug("Failed to remove duplicate", path=path, error=str(exc))

    if removed:
        logger.info("Library folder dedup complete", folder=folder_path, removed=removed, groups=group_count)
    return {"removed": removed, "groups": group_count}
