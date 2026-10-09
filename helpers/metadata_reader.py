"""
Metadata Reader

✅ Reads MP3 + FLAC metadata using mutagen
✅ No normalization or matching logic
✅ Config-driven behaviour
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from helpers.config_helpers import get_metadata_config


# =============================================================================
# ✅ MUTAGEN IMPORTS (safe — fallback to None when not installed)
# =============================================================================

try:
    from mutagen.id3 import ID3  # type: ignore[attr-defined]
    from mutagen.id3._frames import TCON  # type: ignore[import-untyped]
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC

    _MUTAGEN_AVAILABLE = True
except ImportError:
    ID3: Any = None  # type: ignore[no-redef]
    TCON: Any = None  # type: ignore[no-redef]
    EasyID3: Any = None  # type: ignore[no-redef]
    MP3: Any = None  # type: ignore[no-redef]
    FLAC: Any = None  # type: ignore[no-redef]
    _MUTAGEN_AVAILABLE = False


# =============================================================================
# ✅ INTERNAL HELPERS
# =============================================================================

def _parse_number_tag(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(str(value).split("/")[0])
    except Exception:
        return None


# =============================================================================
# ✅ FLAC READER
# =============================================================================

def _read_flac_metadata(file_path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    if not _MUTAGEN_AVAILABLE or FLAC is None:
        return metadata

    try:
        audio = FLAC(file_path)

        def _get(key: str) -> str:
            vals = audio.get(key.upper()) or audio.get(key.lower()) or []
            return str(vals[0]).strip() if vals else ""

        metadata["title"] = _get("TITLE")
        metadata["artist"] = _get("ARTIST")
        metadata["album"] = _get("ALBUM")
        metadata["album_artist"] = _get("ALBUMARTIST")
        metadata["genre"] = _get("GENRE")
        # DATE feeds the import identity backfill (_resolve_missing_identity)
        # and the discovered-file report — previously the reader never exposed
        # a year at all, so neither could fall back to the source file's tags.
        date = _get("DATE")
        if date:
            metadata["year"] = date

        track = _get("TRACKNUMBER")
        if track:
            metadata["track_number"] = _parse_number_tag(track)

        if hasattr(audio.info, "length"):
            metadata["duration_ms"] = int(audio.info.length * 1000)

    except Exception:
        pass

    return metadata


# =============================================================================
# ✅ MP3 READER (ID3)
# =============================================================================

def _read_mp3_id3_metadata(file_path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    if not _MUTAGEN_AVAILABLE or ID3 is None:
        return metadata

    try:
        audio = ID3(file_path)

        if "TIT2" in audio:
            metadata["title"] = str(audio["TIT2"].text[0])

        if "TPE1" in audio:
            metadata["artist"] = str(audio["TPE1"].text[0])

        if "TALB" in audio:
            metadata["album"] = str(audio["TALB"].text[0])

        if "TPE2" in audio:
            metadata["album_artist"] = str(audio["TPE2"].text[0])

        if "TRCK" in audio:
            metadata["track_number"] = _parse_number_tag(audio["TRCK"].text[0])

        if "TCON" in audio:
            metadata["genre"] = str(audio["TCON"].text[0])

        # v2.4 writes TDRC, v2.3 (which this app saves) uses TYER — accept both
        # so the import identity backfill can read the source file's date.
        for _date_frame in ("TDRC", "TYER"):
            if _date_frame in audio:
                metadata["year"] = str(audio[_date_frame].text[0])
                break

    except Exception:
        pass

    return metadata


# =============================================================================
# ✅ MP3 FALLBACK (EasyID3)
# =============================================================================

def _read_mp3_fallback_metadata(file_path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    if not _MUTAGEN_AVAILABLE or EasyID3 is None:
        return metadata

    try:
        audio = EasyID3(file_path)

        for key in ("title", "artist", "album"):
            if key in audio:
                metadata[key] = audio[key][0]

    except Exception:
        pass

    return metadata


# =============================================================================
# ✅ MP3 DURATION
# =============================================================================

def _read_mp3_duration(file_path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    if not _MUTAGEN_AVAILABLE or MP3 is None:
        return metadata

    try:
        audio = MP3(file_path)

        if hasattr(audio.info, "length"):
            metadata["duration_ms"] = int(audio.info.length * 1000)

    except Exception:
        pass

    return metadata


# =============================================================================
# ✅ MAIN READER
# =============================================================================

def read_mp3_metadata(file_path: str) -> Dict[str, Any]:
    """
    Read metadata from MP3 or FLAC file.
    Returns consistent dict of fields.
    """

    metadata: Dict[str, Any] = {}

    cfg = get_metadata_config()

    if not cfg.get("enabled", True):
        return metadata

    if not file_path or not os.path.exists(file_path):
        return metadata

    if not _MUTAGEN_AVAILABLE:
        return metadata

    ext = Path(file_path).suffix.lower()

    # ------------------------------------------------------------------
    # ✅ FLAC
    # ------------------------------------------------------------------

    if ext == ".flac" and cfg.get("enable_flac", True):
        metadata.update(_read_flac_metadata(file_path))

    # ------------------------------------------------------------------
    # ✅ MP3
    # ------------------------------------------------------------------

    elif ext == ".mp3":
        metadata.update(_read_mp3_id3_metadata(file_path))

        # fallback if missing key fields
        if not metadata.get("title") or not metadata.get("artist"):
            metadata.update(_read_mp3_fallback_metadata(file_path))

        metadata.update(_read_mp3_duration(file_path))

    # ------------------------------------------------------------------
    # ✅ FILE INFO (always)
    # ------------------------------------------------------------------

    try:
        stat = os.stat(file_path)
        metadata["file_size"] = stat.st_size
        metadata["file_path"] = file_path
    except Exception:
        pass

    return metadata


# =============================================================================
# ✅ GENRE TAG READING / WRITING
# =============================================================================


def read_genres_from_mp3(file_path: str) -> str:
    """Read all genre tags from an MP3 file, handling multiple TCON frames.

    Returns:
        Genre string with multiple genres separated by double backslash
        (e.g. ``"Rock\\Pop\\Electronic"``), or empty string if none found.
    """
    if not file_path or not os.path.exists(file_path) or not _MUTAGEN_AVAILABLE:
        return ""

    try:
        audio = ID3(file_path)
        genres: list[str] = []
        for key in audio.keys():
            if key.startswith("TCON"):
                frame = audio[key]
                if hasattr(frame, "text") and frame.text:
                    for val in frame.text:
                        parts = [g.strip() for g in str(val).split("\\") if g.strip()]
                        genres.extend(p for p in parts if p not in genres)
        return "\\".join(genres)
    except Exception:
        return ""


def write_genre_to_mp3(file_path: str, genres: str | list[str]) -> bool:
    """Write genre tag to an MP3 file.

    Args:
        file_path: Path to the MP3 file.
        genres: Genre string (double-backslash separated) or list of genres.

    Returns:
        True on success, False otherwise.
    """
    if not file_path or not os.path.exists(file_path) or not _MUTAGEN_AVAILABLE:
        return False

    if isinstance(genres, list):
        genre_str = "\\".join(str(g).strip() for g in genres if g)
    else:
        genre_str = str(genres).strip()

    if not genre_str:
        return False

    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("TCON")
        audio.tags.add(TCON(encoding=3, text=[genre_str]))
        audio.save()
        return True
    except Exception:
        return False


def write_genre_to_flac(file_path: str, genres: str | list[str]) -> bool:
    """Write genre tag to a FLAC file.

    Args:
        file_path: Path to the FLAC file.
        genres: Genre string (comma-separated) or list of genres.

    Returns:
        True on success, False otherwise.
    """
    if not file_path or not os.path.exists(file_path) or not _MUTAGEN_AVAILABLE:
        return False

    if isinstance(genres, list):
        genre_list = [g.strip() for g in genres if g and g.strip()]
    else:
        genre_list = [g.strip() for g in str(genres).split(",") if g.strip()]

    if not genre_list:
        return False

    try:
        audio = FLAC(file_path)
        audio["genre"] = genre_list
        audio.save()
        return True
    except Exception:
        return False


def write_genre_to_audio_file(file_path: str, genres: str | list[str]) -> bool:
    """Write genre tag to an audio file (MP3 or FLAC).

    Args:
        file_path: Path to the audio file.
        genres: Genre string or list of genres.

    Returns:
        True on success, False otherwise.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".mp3":
        return write_genre_to_mp3(file_path, genres)
    if ext in (".flac", ".fla"):
        return write_genre_to_flac(file_path, genres)
    return False


# =============================================================================
# ✅ MULTI-ARTIST PARSING (migrated from old_system/compilation_manager.py)
# =============================================================================


def parse_artists_field(artists_raw: str) -> list[str]:
    """Parse the raw ARTISTS field from MP3 tags (usually JSON array).

    Handles JSON arrays, semicolon/comma/pipe-delimited strings, and
    plain single-artist values.  Used to extract featured/collaboration
    artists from compilation tracks.

    Args:
        artists_raw: Raw artists field value (JSON array or delimited string).

    Returns:
        List of individual artist names.
    """
    if not artists_raw:
        return []

    try:
        if artists_raw.startswith("["):
            data = json.loads(artists_raw)
            if isinstance(data, list):
                return [str(a).strip() for a in data if a]

        for sep in ["; ", ";", " | ", "|", ", ", ","]:
            if sep in artists_raw:
                return [a.strip() for a in artists_raw.split(sep) if a.strip()]

        if artists_raw.strip():
            return [artists_raw.strip()]
    except Exception:
        pass

    return []


# =============================================================================
# RELEASE-TAG READBACK — what Navidrome keeps but never echoes
# =============================================================================

#: Internal fields whose value lives in a tag the app's own tag WRITER can
#: produce.  Resolved through ``services.metadata.tag_names`` so the reader and
#: the writer can never disagree about a spelling: ``clear_keys_for(field)`` is
#: exactly the key set a write REMOVES, so reading through it picks up whatever
#: was written — the canonical name, its separator/case variants and every
#: declared synonym (``ORIGYEAR``/``ORIGINALYEAR``, ``TOTALTRACKS``/``TRACKTOTAL``).
#:
#: ⚠️ WHY THIS EXISTS. Navidrome reads these tags into ``model.MediaFile.Tags``
#: and never sends them back: the Subsonic ``Child`` / ``AlbumID3`` structs are
#: a fixed field set, and ``osChildFromMediaFile`` only exposes title, ids,
#: ISRC, replaygain, genres/moods, participants, works and movements. So an
#: import of a file that was downloaded (and tagged) elsewhere arrives with
#: barcode, catalog number, media, script, release country/status, language,
#: record label, release type, track/disc totals … all EMPTY — the reported
#: "the album didn't pick up the tags on import from Navidrome". The file is
#: local and already carries them; this reads them back.
_RELEASE_TAG_FIELDS: tuple[str, ...] = (
    # Album classification / release identity
    "releasetype", "releasestatus", "releasecountry",
    "musicbrainz_albumtype", "musicbrainz_albumstatus",
    # Release detail — on disk, never on the wire
    "media", "recordlabel", "catalognumber", "barcode", "asin", "script",
    "discsubtitle", "albumversion", "copyright", "language",
    "tracktotal", "disctotal", "compilation", "grouping", "explicitstatus",
    # Dates
    "originalyear", "originaldate", "releasedate",
    # Credits / identifiers
    "composer", "isrc",
    "musicbrainz_releasegroupid", "musicbrainz_albumartistid",
    "musicbrainz_artistid", "musicbrainz_releasetrackid", "musicbrainz_workid",
    "musicbrainz_albumid", "musicbrainz_trackid",
    # Enrichment the download import writes to the file
    "musicbrainz_genres", "is_cover", "original_cover_artist",
    # Sort / people lists
    "albumartistsort", "artistsort", "artists",
)

#: Tags whose on-disk name is not derivable from the field name at all.
#: ``LABEL`` is the Picard/MusicBrainz spelling of ``recordlabel`` (which is an
#: ``UNMAPPED_TAG_FIELD`` and therefore has no canonical name of its own).
_EXTRA_TAG_KEY_ALIASES: dict[str, str] = {
    "label": "recordlabel",
    "recordlabel": "recordlabel",
    "organization": "recordlabel",
    "publisher": "recordlabel",
    "release_type": "releasetype",
    "musicbrainz_albumtype": "releasetype",
}


def _release_tag_lookup() -> dict[str, str]:
    """Normalised tag key → internal field name."""
    from services.metadata.tag_names import clear_keys_for, normalise_tag_name

    lookup: dict[str, str] = {}
    for field in _RELEASE_TAG_FIELDS:
        for key in clear_keys_for(field):
            lookup.setdefault(key, field)
    for key, field in _EXTRA_TAG_KEY_ALIASES.items():
        lookup.setdefault(normalise_tag_name(key), field)
    return lookup


def read_release_tag_values(file_path: str) -> dict[str, str]:
    """Read the release-level tags an MP3/FLAC file carries, by field name.

    Returns ``{internal_field: value}`` for non-empty values only. Best-effort
    and cheap: an unreadable or missing file yields ``{}`` so a caller can treat
    "no file" exactly like "Navidrome had nothing to say".
    """
    from services.metadata.tag_names import normalise_tag_name

    if not file_path:
        return {}
    try:
        if not os.path.isfile(file_path):
            return {}
    except (OSError, ValueError):
        return {}

    lookup = _release_tag_lookup()
    raw: dict[str, str] = {}

    def _put(key: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            raw.setdefault(key, text)

    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import ID3

            tags = ID3(file_path)
            # Every TXXX frame: the release detail tags are all custom frames
            # and the desc spelling varies by tagger (BARCODE / bar code / …).
            for frame in tags.getall("TXXX"):
                desc = getattr(frame, "desc", "")
                text = getattr(frame, "text", None)
                if desc and text:
                    _put(normalise_tag_name(desc), str(text[0]))
            # Standard frames the custom-frame table does not cover.
            for frame_id, field in (
                ("TSRC", "isrc"), ("TCOM", "composer"), ("TIT3", "discsubtitle"),
                ("TPUB", "recordlabel"), ("TLAN", "language"),
                ("TSST", "discsubtitle"), ("TIT1", "grouping"),
            ):
                frames = tags.getall(frame_id)
                for frame in frames:
                    text = getattr(frame, "text", None)
                    if text:
                        _put(normalise_tag_name(field), str(text[0]))
                        break
            # TRCK / TPOS carry "1/3" — the totals are the second half.
            for frame_id, total_field in (("TRCK", "tracktotal"), ("TPOS", "disctotal")):
                frames = tags.getall(frame_id)
                for frame in frames:
                    text = getattr(frame, "text", None)
                    if text and "/" in str(text[0]):
                        _put(normalise_tag_name(total_field), str(text[0]).split("/", 1)[1])
                    break
        elif suffix == ".flac":
            from mutagen.flac import FLAC

            audio = FLAC(file_path)
            for key, values in (audio.tags or {}).items():
                if not values:
                    continue
                _put(normalise_tag_name(key), values[0])
    except Exception:
        # A tag read must never fail an import — the caller falls back to what
        # Navidrome did supply.
        return {}

    out: dict[str, str] = {}
    for key, value in raw.items():
        field = lookup.get(key)
        if not field:
            continue
        cleaned = value.strip()
        if cleaned:
            out.setdefault(field, cleaned)
    return out
