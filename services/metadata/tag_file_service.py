"""
Audio file tag read/write service.

Physical MP3/FLAC tag writing lives here.
DB tag reads are delegated to repository functions only.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Dict, Any, TYPE_CHECKING, cast


# =============================================================================
# TYPE LAYER (separate from runtime)
# =============================================================================

if TYPE_CHECKING:
    from mutagen.flac import FLAC as FLACType, Picture as FLACPictureType
    from mutagen.id3 import (
        ID3 as ID3Type,
        APIC, COMM, POPM, TALB, TBPM, TCOM, TCON,
        TDRC, TIT2, TMOO, TPE1, TPE2, TPOS, TRCK, TXXX
    )
    from mutagen.mp3 import MP3 as MP3Type
else:
    ID3Type = Any
    FLACType = Any
    FLACPictureType = Any


# =============================================================================
# IMPORTS
# =============================================================================

from db.repositories.tag_repository import get_track_tags
from services.metadata.tag_names import (
    canonical_tag_name,
    clear_id3_variants,
    clear_vorbis_variants,
    normalise_tag_name,
)

logger = logging.getLogger(__name__)


# =============================================================================
# SAFE RUNTIME IMPORTS
# =============================================================================

MUTAGEN_AVAILABLE = True

try:
    from mutagen.flac import FLAC, Picture as FLACPicture
    from mutagen.id3 import (
        ID3,
        APIC, COMM, POPM, TALB, TBPM, TCOM, TCON,
        TDRC, TIT2, TMOO, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX, USLT
    )
    from mutagen.mp3 import MP3
except Exception:
    MUTAGEN_AVAILABLE = False

    # ✅ Callable fallback (prevents "not callable" errors)
    class _MissingMutagen:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Mutagen is not available")

    FLAC = _MissingMutagen
    FLACPicture = _MissingMutagen
    ID3 = _MissingMutagen

    APIC = COMM = POPM = TALB = TBPM = TCOM = TCON = _MissingMutagen
    TDRC = TIT2 = TMOO = TPE1 = TPE2 = TPOS = TRCK = TSRC = TXXX = USLT = _MissingMutagen

    MP3 = _MissingMutagen


# =============================================================================
# PUBLIC
# =============================================================================

# MP3 frame ids per tag key (used by the fill-missing-only pre-check).
_MP3_FRAME_FOR_FIELD = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB",
    "album_artist": "TPE2", "albumartist": "TPE2", "composer": "TCOM",
    "track_number": "TRCK", "disc_number": "TPOS",
    "year": "TDRC", "date": "TDRC", "genre": "TCON", "genres": "TCON",
    "comment": "COMM", "cover_art_data": "APIC", "rating": "POPM",
    "isrc": "TSRC", "lyrics": "USLT",
    "musicbrainz_artistid": "TXXX", "musicbrainz_artist_id": "TXXX",
    "musicbrainz_albumartistid": "TXXX",
    "musicbrainz_trackid": "TXXX",
    "musicbrainz_albumid": "TXXX",
    "musicbrainz_releasegroupid": "TXXX",
    "musicbrainz_releasetrackid": "TXXX",
    "musicbrainz_workid": "TXXX",
}

# MusicBrainz entity IDs — the ONLY fields with a dedicated TXXX branch.
# Every name comes from the canonical registry; never hand-write a desc here.
_MB_ID_FIELDS = (
    "mbid",
    "musicbrainz_trackid",
    "musicbrainz_album_mbid",
    "musicbrainz_albumid",
    "musicbrainz_artistid",
    "musicbrainz_artist_id",
    "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid",
    "musicbrainz_releasetrackid",
    "musicbrainz_workid",
)
_MB_ID_CANONICAL = {name for name in (canonical_tag_name(f) for f in _MB_ID_FIELDS) if name}

# Album-extended fields the SCAN may write to FILES.  Kept as an explicit
# allow-list (an arbitrary DB column must never become a tag) but the NAMES now
# come from the canonical registry, so this list can no longer drift from what
# the readers and the FLAC writer use.
_EXTENDED_ALBUM_TAG_FIELDS = frozenset({
    "releasetype", "releasestatus", "releasecountry",
    "musicbrainz_albumtype", "musicbrainz_albumstatus", "musicbrainz_releasecountry",
    "originalyear", "originaldate",
    "tracktotal", "disctotal", "media", "label", "recordlabel",
    "catalognumber", "catalog", "barcode", "asin", "script", "discsubtitle",
    "copyright", "language", "explicitstatus",
    "is_cover", "original_cover_artist", "musicbrainz_genres",
    "iswc", "original_title", "lyricist",
})

# TXXX descriptions for the MusicBrainz ID frames.  DERIVED from the canonical
# registry — a hand-written desc here is how one field silently acquired two
# spellings (MUSICBRAINZ ALBUM ID *and* MUSICBRAINZ_ALBUMID on the same track).
_MB_TXXX_DESC = {
    field: name for field, name in ((f, canonical_tag_name(f)) for f in _MB_ID_FIELDS) if name
}


def _existing_non_empty_fields(file_path: str, tags: Dict[str, Any]) -> set[str]:
    """Tag keys whose on-disk frame already carries a value (fill-missing-only)."""
    present: set[str] = set()
    if not MUTAGEN_AVAILABLE:
        return present
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import ID3 as _ID3
            tag_obj = _ID3(file_path)
            for field, frame_id in _MP3_FRAME_FOR_FIELD.items():
                if frame_id == "TXXX":
                    # TXXX-based MBID frames: presence depends on the specific
                    # description, not on any TXXX frame existing at all.
                    # Compared with normalise_tag_name so a file still carrying a
                    # LEGACY spelling counts as populated — fill_missing_only is
                    # the user asking us not to touch existing values, and a
                    # variant of the field IS that value.
                    desc = _MB_TXXX_DESC.get(field)
                    if desc and any(
                        getattr(f, "text", None)
                        and normalise_tag_name(getattr(f, "desc", "")) == normalise_tag_name(desc)
                        for f in tag_obj.getall("TXXX")
                    ):
                        present.add(field)
                    continue
                if frame_id not in tag_obj:
                    continue
                if any(getattr(f, "text", None) for f in tag_obj.getall(frame_id)):
                    present.add(field)
        elif suffix == ".flac":
            from mutagen.flac import FLAC as _FLAC
            audio = _FLAC(file_path)
            for field in tags:
                values = audio.tags.get(field)
                if values and any(str(v).strip() for v in values):
                    present.add(field)
    except Exception:
        pass
    return present


def write_tags_to_file(file_path: str, tags: Dict[str, Any]) -> bool:
    if not file_path or not os.path.exists(file_path):
        logger.error("File not found: %s", file_path)
        return False

    # ── Tagging policy gate ────────────────────────────────────────────────
    # ``tagging.write_tags_to_file`` master toggle lets Popularr run as a
    # read-only database scanner/UI (external tagger, read-only mounts,
    # network drives).  ``ratings_only`` restricts writes to POPM/RATING
    # frames; ``fill_missing_only`` never overwrites a populated frame;
    # ``preserve_file_timestamps`` restores mtime/atime after the write.
    try:
        from helpers.config_helpers import get_tagging_config
        cfg = get_tagging_config()
    except Exception:
        cfg = {}
    if not cfg.get("write_tags_to_file", True):
        logger.debug("[TAG] File tag writes disabled (tagging.write_tags_to_file=false) — DB only")
        return False
    if cfg.get("ratings_only") and not any(k == "rating" for k in (tags or {})):
        logger.debug("[TAG] ratings_only mode — skipping non-rating write to %s", file_path)
        return False

    tags = dict(tags or {})

    # fill_missing_only: never overwrite a frame that already carries a value.
    # Empty incoming values still pass through (they are explicit "clear this
    # frame" requests — the writers delete the frame on empty).
    if cfg.get("fill_missing_only"):
        already = _existing_non_empty_fields(file_path, tags)
        tags = {
            k: v for k, v in tags.items()
            if k not in already or not str(v or "").strip()
        }
        if not tags:
            return False

    # Timestamp policy (Navidrome compatibility):
    #
    # The atomic writer replaces the original with a NEW inode
    # (temp + os.replace).  Navidrome's scanner detects file changes by
    # path + mtime — if we restore the OLD mtime after the swap (the prior
    # behaviour when preserve_file_timestamps=true), the swapped file looks
    # "unchanged" to Navidrome, its incremental scan never re-reads it, and
    # the "mapped tags" index stays stale even though raw tags updated
    # (the reported bug; the old in-place writer always bumped mtime, so
    # this never happened before the atomic-write change).
    #
    # To stay Navidrome-friendly we keep the swap's fresh mtime whenever the
    # file's bytes actually changed.  Timestamps are only restored when the
    # write was a TRUE no-op (identical bytes) — e.g. fill_missing_only left
    # every frame as-is, or the writer cleared a frame that was already
    # absent.  Comparing hashes is cheap here because metadata writes are
    # infrequent (user edits, not per-scan).
    stat_before = None
    if cfg.get("preserve_file_timestamps", True):
        try:
            stat_before = os.stat(file_path)
        except OSError:
            stat_before = None

        if stat_before is not None:
            try:
                with open(file_path, "rb") as fh:
                    bytes_before = fh.read()
            except OSError:
                stat_before = None

    suffix = Path(file_path).suffix.lower()

    if suffix in (".mp3", ".flac"):
        ok = _write_tags_atomic(file_path, tags)
    else:
        logger.warning("Unsupported file format: %s", suffix)
        ok = False

    if ok and stat_before is not None:
        try:
            with open(file_path, "rb") as fh:
                bytes_after = fh.read()
            changed = bytes_after != bytes_before
        except OSError:
            changed = True
        if not changed:
            # True no-op: restore the original timestamps so the file is
            # byte-for-byte and timestamp-for-timestamp untouched.
            try:
                os.utime(file_path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
            except OSError:
                pass
    return ok


def _write_tags_atomic(file_path: str, tags: Dict[str, Any]) -> bool:
    """Write tags to *file_path* ATOMICALLY: write to a temp sibling, then
    ``os.replace()`` over the original.

    Mutagen rewrites the file in place; if a browser/player is streaming the
    file (or the process is killed mid-write) the original can be left
    partially written/corrupt.  Writing a temp copy in the SAME directory
    and atomically renaming it means readers either see the old file or the
    new one — never a torn write.  ``os.replace`` is atomic on POSIX and
    Windows when source and target share a filesystem (same directory
    guarantees this).

    Returns True on success.  On failure the original file is left untouched.
    """
    directory = os.path.dirname(os.path.abspath(file_path)) or "."
    suffix = Path(file_path).suffix.lower()

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".populartag_", suffix=suffix, delete=False,
        ) as tmpf:
            tmp_path = tmpf.name

        # Copy the original bytes so the tag writer mutates a full copy.
        shutil.copy2(file_path, tmp_path)

        if suffix == ".mp3":
            ok = write_id3_tags(tmp_path, tags)
        else:
            ok = write_flac_tags(tmp_path, tags)

        if not ok:
            return False

        # Preserve the original's permissions on the replacement.
        try:
            st = os.stat(file_path)
            os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
        except OSError:
            pass

        # Atomic swap: readers see old or new, never partial.
        os.replace(tmp_path, file_path)
        tmp_path = ""
        return True
    except Exception as exc:
        logger.error("Failed to write tags atomically to %s: %s", file_path, exc, exc_info=True)
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def write_rating_to_file(file_path: str, stars: int) -> bool:
    """Write a 1-5 star rating into the file tags: POPM (MP3) / RATING (FLAC).

    Gated by the ``tagging`` config — the master toggle must be enabled;
    ``ratings_only`` mode still permits rating writes (that is its purpose).
    """
    try:
        from helpers.config_helpers import get_tagging_config
        if not get_tagging_config().get("write_tags_to_file", True):
            return False
    except Exception:
        pass
    stars = max(1, min(5, int(stars or 0)))
    return write_tags_to_file(file_path, {"rating": stars})


# =============================================================================
# HELPERS
# =============================================================================

def _set_text_frame(tag_obj: ID3Type, frame_id: str, frame_cls, value: Any) -> None:
    if value is None or value == "":
        tag_obj.delall(frame_id)
        return

    tag_obj.delall(frame_id)
    tag_obj.add(frame_cls(encoding=3, text=[str(value)]))


def _clear_txxx_variants(tag_obj: ID3Type, normalized_target: str) -> None:
    """Delete every TXXX frame whose desc normalises to *normalized_target*.

    Delegates to the canonical registry (``services.metadata.tag_names``): the
    comparison must be case- AND separator-insensitive, because
    ``ID3.delall("TXXX:ORIGNALYEAR")`` does not remove a frame stored as
    ``TXXX:orignalyear`` — which is exactly how duplicate frames survived an
    overwrite.
    """
    clear_id3_variants(tag_obj, normalized_target)


# =============================================================================
# MP3 / ID3
# =============================================================================

def write_id3_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    if not MUTAGEN_AVAILABLE:
        logger.error("Mutagen not available for ID3 writing")
        return False

    try:
        try:
            audio = cast(MP3Type, MP3(file_path, ID3=ID3))  # type: ignore[arg-type]

            if audio.tags is None:
                audio.add_tags()

            tag_obj = cast(ID3Type, audio.tags)
            save = lambda: audio.save(v2_version=3)

        except Exception:
            try:
                tag_obj = cast(ID3Type, ID3(file_path))
            except Exception:
                tag_obj = cast(ID3Type, ID3())

            save = lambda: tag_obj.save(file_path, v2_version=3)

        for field, value in tags.items():

            if field == "title":
                _set_text_frame(tag_obj, "TIT2", TIT2, value)

            elif field == "artist":
                _set_text_frame(tag_obj, "TPE1", TPE1, value)

            elif field == "album":
                _set_text_frame(tag_obj, "TALB", TALB, value)

            elif field in {"album_artist", "albumartist"}:
                _set_text_frame(tag_obj, "TPE2", TPE2, value)

            elif field == "composer":
                _set_text_frame(tag_obj, "TCOM", TCOM, value)

            elif field == "track_number":
                _set_text_frame(tag_obj, "TRCK", TRCK, value)

            elif field == "disc_number":
                _set_text_frame(tag_obj, "TPOS", TPOS, value)

            elif field in {"year", "date"}:
                _set_text_frame(tag_obj, "TDRC", TDRC, value)

            elif field in {"genre", "genres"}:
                tag_obj.delall("TCON")

                if value:
                    import re

                    if isinstance(value, list):
                        genres = [str(v).strip() for v in value if str(v).strip()]
                    else:
                        # Split on ALL genre separators the app uses: commas,
                        # semicolons, slashes AND backslashes (Navidrome /
                        # ID3v2.3 conventions join genres with ``\`` — e.g.
                        # ``metal\nu metal\rock`` — which must become THREE
                        # TCON values, never one literal ``metal\nu metal\rock``
                        # genre that Navidrome would render as a single broken
                        # genre folder).
                        genres = [
                            g.strip()
                            for g in re.split(r"[,;/\\]+", str(value))
                            if g.strip()
                        ]

                    if genres:
                        tag_obj.add(TCON(encoding=3, text=genres))

            elif field == "comment":
                tag_obj.delall("COMM")

                if value:
                    tag_obj.add(COMM(encoding=3, lang="eng", desc="", text=[str(value)]))

            elif field == "rating" and value:
                # POPM popularimeter: rating byte 0-255 (5★ = 255, 1★ = 51).
                tag_obj.delall("POPM")
                tag_obj.add(POPM(
                    email="",
                    rating=max(0, min(255, int(value) * 51)),
                    count=0,
                ))

            elif (canonical_tag_name(field) or "") in _MB_ID_CANONICAL:
                # ONE canonical desc per field, and every spelling of it removed
                # first — so a track written by the old space-form code and the
                # new underscore form ends up with a single frame.
                _canonical = clear_id3_variants(tag_obj, field)

                if value:
                    tag_obj.add(TXXX(encoding=3, desc=_canonical, text=[str(value)]))

            elif field == "isrc":
                tag_obj.delall("TSRC")

                if value:
                    tag_obj.add(TSRC(encoding=3, text=[str(value)]))

            elif field == "lyrics":
                tag_obj.delall("USLT")

                if value:
                    tag_obj.add(USLT(encoding=3, lang="eng", desc="", text=[str(value)]))

            elif field == "writer":
                # TXXX:WRITER — the tag config's writer aliases
                # (``tags.writer.aliases``) read this frame back.  Clears any
                # existing WRITER/LYRICIST variants before writing so an
                # edited writer never leaves a stale duplicate frame.
                _clear_txxx_variants(tag_obj, "writer")
                _clear_txxx_variants(tag_obj, "lyricist")

                if value:
                    tag_obj.add(TXXX(encoding=3, desc="WRITER", text=[str(value)]))

            elif field == "cover_art_data" and value:
                tag_obj.delall("APIC")

                tag_obj.add(
                    APIC(
                        encoding=3,
                        mime=tags.get("cover_art_mime", "image/jpeg"),
                        type=3,
                        desc="Cover",
                        data=bytes(value),
                    )
                )

            else:
                # ── Album-extended fields ───────────────────────────────────
                # Standard MusicBrainz / album fields that have no native ID3
                # frame (release type/status/country, originalyear, totals,
                # media, label, barcode, cover markers, ...) must be written as
                # TXXX frames — Navidrome reads these back and uses them to
                # group/merge albums.  They used to be SILENTLY DROPPED for MP3
                # files, so tracks that never received MB enrichment stayed
                # missing the album-level tags and Navidrome split the album.
                #
                # The NAME now comes from the canonical registry, and every
                # spelling of it is cleared first, so one field can never occupy
                # two frames.  Fields outside the allow-list are ignored: an
                # arbitrary DB column must not become a tag.
                if field in _EXTENDED_ALBUM_TAG_FIELDS:
                    _canonical = canonical_tag_name(field) or str(field).replace("_", " ").upper()
                    clear_id3_variants(tag_obj, _canonical)
                    if value is not None and str(value).strip() != "":
                        tag_obj.add(TXXX(encoding=3, desc=_canonical, text=[str(value)]))

        save()
        return True

    except Exception as exc:
        logger.error("Failed to write ID3 tags: %s", exc, exc_info=True)
        return False


# =============================================================================
# FLAC
# =============================================================================

# VorbisComment field-name mapping — FLAC uses its own standard names, NOT
# the internal field names (Navidrome/mutagen read ``date``/``tracknumber``/
# ``discnumber``/``albumartist``; writing ``year``/``track_number`` etc.
# produced non-standard comments that players ignore).  Genres are stored
# under ``GENRE`` (singular) in Vorbis comments; the MP3 writer's plural
# ``genres`` key must map to it too.
_VORBIS_FIELD_MAP: Dict[str, str] = {
    "track_number": "tracknumber",
    "disc_number": "discnumber",
    "album_artist": "albumartist",
    "year": "date",
    "genres": "genre",
    "genre": "genre",
    # MusicBrainz and album-extended names are NOT listed here — they come from
    # the canonical registry (``services.metadata.tag_names``), applied ahead of
    # this map in ``write_flac_tags``.  Only Vorbis-standard RENAMES belong in
    # this table; two tables for the same name is how MP3 and FLAC drifted.
    "musicbrainz_genres": "GENRE",
}


def write_flac_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    if not MUTAGEN_AVAILABLE:
        logger.error("Mutagen not available for FLAC writing")
        return False

    try:
        audio = cast(FLACType, FLAC(file_path))

        for field, value in tags.items():
            # ONE name per field: the canonical registry decides for every
            # MusicBrainz/album-extended tag, and all of its spellings are
            # removed first.  Vorbis lookups are case-insensitive on READ but
            # mutagen stores the case it was given, so assigning
            # ``audio["musicbrainz_albumid"]`` ADDS a second key when the file
            # already holds ``MUSICBRAINZ_ALBUMID`` — the FLAC twin of the
            # duplicate-TXXX problem.
            _canonical = canonical_tag_name(field)
            if _canonical:
                field = clear_vorbis_variants(audio, _canonical)
            else:
                field = _VORBIS_FIELD_MAP.get(field, field)
            if value is None or str(value).strip() == "":
                # Empty value = explicit "clear this field" request (mirrors
                # the MP3 writer, where empty deletes the frame) — e.g. the
                # transfer pipeline clears disc_number on single-disc albums.
                try:
                    if field in audio:
                        del audio[field]
                except Exception:
                    pass
                continue

            if isinstance(value, (list, tuple, set)):
                # A genres list must become MULTIPLE Vorbis values — writing
                # str(list) would embed the literal "['Rock', 'Metal']".
                audio[field] = [str(v).strip() for v in value if str(v).strip()]
            elif field == "genre":
                # A single genre string may carry the app's separators
                # (comma, semicolon, slash, backslash — e.g. ``metal\nu
                # metal\rock``).  Split it into multiple Vorbis GENRE values
                # so Navidrome sees three genres, never one literal
                # backslash-joined string.
                import re as _re
                audio[field] = [
                    g.strip()
                    for g in _re.split(r"[,;/\\]+", str(value))
                    if g.strip()
                ]
            else:
                audio[field] = [str(value)]

        audio.save()
        return True

    except Exception as exc:
        logger.error("Failed to write FLAC tags: %s", exc, exc_info=True)
        return False


# =============================================================================
# PUBLIC WRAPPERS
# =============================================================================

def update_file_tags(file_path: str, tag_updates: Dict[str, Any]) -> bool:
    if not file_path or not tag_updates:
        return False

    return write_tags_to_file(file_path, tag_updates)


def _get_track_file_path(track_id: str) -> str | None:
    """Return the track's stored ``file_path`` (or None)."""
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            row = session.execute(
                _text("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            value = row[0] if row else None
            return str(value).strip() if value and str(value).strip() else None
    except Exception as exc:
        logger.error("Failed to load file path for track %s: %s", track_id, exc)
        return None


def _resolve_music_file_path(path_value: str | None) -> str | None:
    """Resolve a stored path to an absolute existing file.

    Navidrome stores paths relative to its music folder (e.g.
    ``Artist/Album/01 - Track.mp3``); the DB may also hold a fully absolute
    path.  Try the stored value first, then each configured music root joined
    with the relative path.  Returns the first path that exists on disk, or
    None.

    The music-root candidates include the CONFIG-driven root
    (``resolve_music_dir`` — ``music.root`` / ``music_root`` /
    ``navidrome.music_folder`` from config.yaml) in ADDITION to the legacy
    env vars, so a deployment whose library lives outside ``/music`` still
    resolves and tag writes actually land on the real file.
    """
    if not path_value:
        return None
    raw = str(path_value).strip()
    if not raw:
        return None

    candidates = [raw]
    roots: list[str] = []
    # Config-driven root first (authoritative — the app reads the library
    # root from config, not just env).
    try:
        from services.infrastructure.filesystem_service import resolve_music_dir
        cfg_root = resolve_music_dir()
        if cfg_root:
            roots.append(cfg_root)
    except Exception:
        pass
    # Legacy env-var roots.
    for env_key in ("MUSIC_FOLDER", "MUSIC_ROOT", "MUSIC_DIR"):
        env_val = os.environ.get(env_key)
        if env_val:
            roots.append(str(env_val).strip())
    roots.append("/music")

    if not os.path.isabs(raw):
        for root in roots:
            if root:
                candidates.append(os.path.join(str(root).strip(), raw))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    return None


# Public alias — album/track edit routes resolve stored (often relative)
# Navidrome paths through the SAME helper so every file write targets the
# real file instead of failing silently on a relative path.
resolve_music_file_path = _resolve_music_file_path


# DB column name → tag writer field name.  Used to convert an edit payload
# (or the track row's columns) into the field names ``write_tags_to_file``
# understands.  Kept in ONE place so the track page, album page, tag sync
# and the metadata APIs all write identical frames.
_COLUMN_TO_TAG_FIELD: dict[str, str] = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "album_artist",
    "albumartist": "album_artist",
    "genres": "genres",
    "genre": "genres",
    "year": "year",
    "date": "year",
    "composer": "composer",
    "writer": "writer",
    "arranger": "arranger",
    "mixer": "mixer",
    "producer": "producer",
    "work": "work",
    "track_number": "track_number",
    "disc_number": "disc_number",
    "comment": "comment",
    "lyrics": "lyrics",
    "mbid": "mbid",
    "musicbrainz_trackid": "mbid",
    "beets_mbid": "mbid",
    "isrc": "isrc",
    "iswc": "iswc",
    "original_title": "original_title",
    "bpm": "bpm",
    "titlesort": "titlesort",
    "albumsort": "albumsort",
    "artistsort": "artistsort",
    "composersort": "composersort",
    "albumartistsort": "albumartistsort",
    "lyricistsort": "lyricistsort",
    "artistssort": "artistssort",
    "albumartistssort": "albumartistssort",
    "artists": "artists",
    "albumartists": "albumartists",
    "conductor": "conductor",
    "performer": "performer",
    "director": "director",
    "djmixer": "djmixer",
    "engineer": "engineer",
    "remixer": "remixer",
    "lyricist": "lyricist",
    "albumversion": "albumversion",
    "recordlabel": "recordlabel",
    "copyright": "copyright",
    "releasedate": "releasedate",
    "releasetype": "releasetype",
    "releasestatus": "releasestatus",
    "releasecountry": "releasecountry",
    "media": "media",
    "barcode": "barcode",
    "catalognumber": "catalognumber",
    "asin": "asin",
    "originalyear": "originalyear",
    "originaldate": "originaldate",
    "tracktotal": "tracktotal",
    "disctotal": "disctotal",
    "script": "script",
    "discsubtitle": "discsubtitle",
    "subtitle": "subtitle",
    "grouping": "grouping",
    "movement": "movement",
    "movementname": "movementname",
    "movementtotal": "movementtotal",
    "key": "key",
    "language": "language",
    "license": "license",
    "website": "website",
    "encodedby": "encodedby",
    "encodersettings": "encodersettings",
    "explicitstatus": "explicitstatus",
    "musicbrainz_albumid": "musicbrainz_albumid",
    "musicbrainz_album_mbid": "musicbrainz_albumid",
    "musicbrainz_releaseid": "musicbrainz_albumid",
    "musicbrainz_artistid": "musicbrainz_artistid",
    "musicbrainz_artist_id": "musicbrainz_artistid",
    "musicbrainz_albumartistid": "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid": "musicbrainz_releasegroupid",
    "musicbrainz_releasetrackid": "musicbrainz_releasetrackid",
    "musicbrainz_workid": "musicbrainz_workid",
    "musicbrainz_albumtype": "musicbrainz_albumtype",
    "musicbrainz_albumstatus": "musicbrainz_albumstatus",
    "musicbrainz_genres": "musicbrainz_genres",
    "is_cover": "is_cover",
    "original_cover_artist": "original_cover_artist",
    "replaygain_track_gain": "replaygain_track_gain",
    "replaygain_track_peak": "replaygain_track_peak",
    "replaygain_album_gain": "replaygain_album_gain",
    "replaygain_album_peak": "replaygain_album_peak",
    "r128_track_gain": "r128_track_gain",
    "r128_album_gain": "r128_album_gain",
}


def build_tag_updates(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a DB-column payload to tag-writer field names.

    ``payload`` uses the tracks-table column names (e.g. ``album_artist``,
    ``musicbrainz_albumid``); the writers expect their own field names
    (``album_artist``, ``musicbrainz_albumid``, …).  Only non-empty values
    are included (empty values would tell the writer to DELETE the frame,
    which is the right behaviour when a field is explicitly cleared).
    """
    tags: dict[str, Any] = {}
    for column_name, tag_name in _COLUMN_TO_TAG_FIELD.items():
        value = payload.get(column_name)
        if value is not None and str(value).strip() != "":
            tags[tag_name] = value

    # ---- years ---------------------------------------------------------
    # The DATE/YEAR tag describes the RELEASE THE FILE IS FROM, so a 2026
    # remaster of a 1995 album is dated 2026; the album's ORIGINAL year belongs
    # in the ORIGINALYEAR/ORIGINALDATE pair. ``year`` here is the album's
    # ORIGINAL year (the scan stores the release group's first release year),
    # so it must NOT reach the DATE tag when an edition year is known — which
    # is what it did, making every remaster claim to be the original release.
    _edition_year = str(payload.get("release_year") or "").strip()
    _original_year = str(payload.get("year") or "").strip()
    if _edition_year:
        tags["year"] = _edition_year
        if _original_year and _original_year != _edition_year:
            # Only a FALLBACK: an explicit ``originalyear``/``originaldate`` on
            # the payload (the MusicBrainz value) stays authoritative.
            tags.setdefault("originalyear", _original_year)
            tags.setdefault("originaldate", _original_year)
    return tags


def sync_track_tags_to_file(track_id: str) -> bool:
    tags = get_track_tags(track_id)

    file_path = _resolve_music_file_path(_get_track_file_path(track_id))

    if not file_path:
        logger.warning("No audio file resolved for track %s", track_id)
        return False

    # Only write non-empty values — passing None/"" for a frame would make the
    # writer DELETE that frame from the file (see ``_set_text_frame``).
    tags_to_write = {
        k: v for k, v in tags.items()
        if v is not None and str(v).strip() != ""
    }

    if not tags_to_write:
        logger.warning("No editable tags to write for track %s", track_id)
        return False

    return write_tags_to_file(file_path, tags_to_write)


def update_file_metadata(file_path: str, metadata: Dict[str, Any]) -> bool:
    if not file_path:
        return False

    tag_updates = {
        "title": metadata.get("title"),
        "artist": metadata.get("artist"),
        "album": metadata.get("album"),
        "album_artist": metadata.get("album_artist"),
        "track_number": metadata.get("track_number"),
        "disc_number": metadata.get("disc_number"),
        # DATE/YEAR = the EDITION's year when the caller knows one, else the
        # original (see ``build_tag_updates`` for why). ``originalyear`` /
        # ``originaldate`` are written below when supplied.
        "year": metadata.get("release_year") or metadata.get("year"),
    }

    if metadata.get("originalyear"):
        tag_updates["originalyear"] = metadata.get("originalyear")
    elif metadata.get("release_year") and metadata.get("year"):
        tag_updates["originalyear"] = metadata.get("year")

    if metadata.get("originaldate"):
        tag_updates["originaldate"] = metadata.get("originaldate")

    if metadata.get("recording_mbid"):
        tag_updates["musicbrainz_trackid"] = metadata.get("recording_mbid")

    if metadata.get("release_mbid"):
        tag_updates["musicbrainz_albumid"] = metadata.get("release_mbid")

    # MusicBrainz enrichment carried through the queue row's metadata JSONB.
    if metadata.get("writer"):
        tag_updates["writer"] = metadata.get("writer")
    if metadata.get("composer"):
        tag_updates["composer"] = metadata.get("composer")
    if metadata.get("lyricist"):
        tag_updates["lyricist"] = metadata.get("lyricist")
    if metadata.get("is_cover") in (1, True, "1", "true", "True"):
        tag_updates["is_cover"] = "1"
    if metadata.get("original_cover_artist"):
        tag_updates["original_cover_artist"] = metadata.get("original_cover_artist")
    if metadata.get("original_title"):
        tag_updates["original_title"] = metadata.get("original_title")
    if metadata.get("musicbrainz_genres"):
        tag_updates["musicbrainz_genres"] = metadata.get("musicbrainz_genres")
    if metadata.get("work_mbid"):
        tag_updates["musicbrainz_workid"] = metadata.get("work_mbid")
    if metadata.get("iswc"):
        tag_updates["iswc"] = metadata.get("iswc")

    return write_tags_to_file(file_path, tag_updates)


# =============================================================================
# ALBUM ART
# =============================================================================


def embed_album_art(file_path: str, image_data: bytes, mime_type: str = "image/jpeg") -> bool:
    """Embed album art image data into an audio file's tags.

    Args:
        file_path: Path to the audio file (MP3 or FLAC).
        image_data: Raw image bytes.
        mime_type: Image MIME type (``image/jpeg`` or ``image/png``).

    Returns:
        True on success, False otherwise.
    """
    if not file_path or not os.path.exists(file_path) or not image_data:
        return False

    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3  # type: ignore[attr-defined]
            from mutagen.id3._frames import APIC  # type: ignore[import-untyped]
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(
                encoding=3, mime=mime_type, type=3, desc="Cover", data=image_data,
            ))
            audio.save()
            return True

        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(file_path)
            pic = Picture()
            pic.data = image_data
            pic.mime = mime_type
            pic.type = 3
            audio.add_picture(pic)
            audio.save()
            return True

        logger.warning("Unsupported format for album art: %s", ext)
        return False
    except Exception as exc:
        logger.error("Failed to embed album art in %s: %s", file_path, exc, exc_info=True)
        return False


# =============================================================================
# FLAC → MP3 CONVERSION
# =============================================================================


def convert_flac_to_mp3(flac_path: str, bitrate: str = "320k") -> str | None:
    """Convert a FLAC file to MP3 using ffmpeg.

    Args:
        flac_path: Path to the FLAC file.
        bitrate: Target bitrate (default ``"320k"``).

    Returns:
        Path to the converted MP3 file, or ``None`` on failure.
        The original FLAC file is deleted after a successful conversion.
    """
    import subprocess

    if not os.path.exists(flac_path):
        logger.error("FLAC file not found: %s", flac_path)
        return None

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.error("ffmpeg not available — cannot convert FLAC to MP3")
        return None

    mp3_path = os.path.splitext(flac_path)[0] + ".mp3"
    cmd = [
        "ffmpeg", "-i", flac_path,
        "-b:a", bitrate, "-q:a", "0",
        "-v", "error", "-y", mp3_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        if result.returncode != 0:
            logger.error("FLAC→MP3 conversion failed: %s", result.stderr)
            return None
        if not os.path.exists(mp3_path):
            logger.error("Conversion succeeded but output file not found")
            return None

        size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
        logger.info("Converted FLAC→MP3: %s (%.1f MB)", mp3_path, size_mb)

        try:
            os.remove(flac_path)
            logger.debug("Deleted original FLAC: %s", flac_path)
        except Exception as exc:
            logger.warning("Could not delete original FLAC: %s", exc)

        return mp3_path
    except subprocess.TimeoutExpired:
        logger.error("FLAC→MP3 conversion timed out after 300s: %s", flac_path)
        return None
    except Exception as exc:
        logger.error("FLAC→MP3 conversion error: %s", exc, exc_info=True)
        return None