"""
Audio file tag read/write service.

Physical MP3/FLAC/MP4 tag writing lives here.
DB tag reads are delegated to repository functions only.
"""

from __future__ import annotations

import filecmp
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
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
    # ⚠️ ``MP3Type`` BELONGS HERE. `cast()` is a runtime CALL, so its first
    # argument is evaluated, and `from __future__ import annotations` does not
    # defer it. With the name imported only under TYPE_CHECKING, every call to
    # ``write_id3_tags`` raised NameError on its first line, the bare
    # ``except Exception`` swallowed it, and the intended MP3 path NEVER RAN —
    # every write silently took the fallback instead. Nothing failed loudly
    # because the fallback also writes tags, so this stayed invisible.
    MP3Type = Any

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
# SHARED HELPERS
# =============================================================================

#: Every genre separator the app uses: commas, semicolons, slashes AND
#: backslashes (Navidrome / ID3v2.3 join genres with ``\\`` — e.g.
#: ``metal\\nu metal\\rock``). One pattern for every writer so ID3, Vorbis and
#: MP4 can never split the same value differently.
_GENRE_SPLIT_RE = re.compile(r"[,;/\\]+")


def _split_genres(value: Any) -> list[str]:
    """A genre value as a clean list — one entry per genre.

    A list/tuple/set is taken item by item (each item is already one genre); a
    string is split on every app separator, so ``"Rock, Metal"`` becomes two
    genres rather than one literal ``"Rock, Metal"`` genre that Navidrome
    renders as a single broken genre folder.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = _GENRE_SPLIT_RE.split(str(value))
    return [p.strip() for p in parts if p.strip()]


def _as_image_bytes(value: Any, file_path: str = "") -> bytes | None:
    """Cover art as EXACTLY ``bytes`` — or None when it cannot be.

    mutagen's binary frames raise ``TypeError("data has to be bytes")`` for a
    ``bytearray``, a ``memoryview`` or a ``str`` — and a blob read back from
    PostgreSQL arrives as a **memoryview** (``album_art.image_data`` is BYTEA).
    A ``str`` is REFUSED rather than encoded: it means a path or a base64
    payload, and encoding it would silently embed garbage into the file.
    """
    if isinstance(value, bytes):
        return value or None
    if isinstance(value, (bytearray, memoryview)):
        data = bytes(value)
        return data or None
    logger.error(
        "Cover art for %s is %s, not bytes — not embedded",
        file_path or "<file>", type(value).__name__,
    )
    return None


# ── Per-file write lock ──────────────────────────────────────────────────────
# Every write copies the file, edits the copy and swaps it in. Two writers on
# the SAME file at once (a scan's tag sync and an album-page save, say) each
# copy the ORIGINAL, so the second swap silently discards the first one's
# changes. Serialising per resolved path makes the second writer start from the
# first one's result. Locks are per file, so different files still write in
# parallel (the album save's 4-way pool is unaffected).
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(file_path: str) -> threading.Lock:
    key = os.path.realpath(file_path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


# =============================================================================
# PUBLIC
# =============================================================================

# MP3 frame ids per tag key (used by the fill-missing-only pre-check). Fields
# mapped to "TXXX" — and any field not listed — are checked by their TXXX
# description instead (see ``_txxx_desc_for_field``).
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

#: Fields the fill-missing-only pre-check NEVER treats as "already populated".
#: The star rating is computed by the app on every scan; under fill-missing-only
#: a file that ever got a rating would keep it forever. It used to depend on the
#: format — MP3's POPM frame has no ``text`` so it always rewrote, while FLAC's
#: RATING comment was detected and frozen. Now both always write.
_FILL_MISSING_ALWAYS_WRITE = frozenset({"rating"})

#: Writer-internal keys that are not tags in their own right.
_NON_TAG_KEYS = frozenset({"cover_art_mime"})


def _txxx_desc_for_field(field: str) -> str | None:
    """The TXXX description ``write_id3_tags`` uses for *field* (or None)."""
    if field == "writer":
        return "WRITER"
    canonical = canonical_tag_name(field)
    if canonical:
        return canonical
    if field in _EXTENDED_ALBUM_TAG_FIELDS:
        return str(field).replace("_", " ").upper()
    return None


def _vorbis_key_for_field(field: str) -> str:
    """The Vorbis comment name ``write_flac_tags`` uses for *field*."""
    return canonical_tag_name(field) or _VORBIS_FIELD_MAP.get(field, field)


def _existing_non_empty_fields(file_path: str, tags: Dict[str, Any]) -> set[str]:
    """Tag keys whose on-disk value is already populated (fill-missing-only).

    Checked with the SAME names each writer would write, for every field in
    *tags*. The old check had two gaps that silently disabled the policy:

    * FLAC looked fields up by their INTERNAL names (``track_number``,
      ``year``, ``album_artist``, ``genres``) while the writer stores the
      Vorbis names (``tracknumber``, ``date``, ``albumartist``, ``genre``) —
      so those always read "missing" and were overwritten.
    * MP3 only knew the fields in ``_MP3_FRAME_FOR_FIELD``; ``mbid``,
      ``writer`` and every album-extended TXXX field were always overwritten.
    * MP4/M4A had no check at all.
    """
    present: set[str] = set()
    if not MUTAGEN_AVAILABLE:
        return present

    fields = [
        f for f in tags
        if f not in _FILL_MISSING_ALWAYS_WRITE and f not in _NON_TAG_KEYS
    ]
    if not fields:
        return present

    suffix = Path(file_path).suffix.lower()
    try:
        if suffix == ".mp3":
            try:
                tag_obj = ID3(file_path)
            except Exception:
                return present  # no ID3 tag at all → everything is missing
            # Compared with normalise_tag_name so a file still carrying a
            # LEGACY spelling counts as populated — fill_missing_only is the
            # user asking us not to touch existing values, and a variant of
            # the field IS that value.
            txxx_names = {
                normalise_tag_name(getattr(f, "desc", ""))
                for f in tag_obj.getall("TXXX")
                if getattr(f, "text", None)
            }
            for field in fields:
                frame_id = _MP3_FRAME_FOR_FIELD.get(field)
                if frame_id and frame_id != "TXXX":
                    frames = tag_obj.getall(frame_id)
                    if frame_id == "APIC":
                        has_value = any(getattr(f, "data", None) for f in frames)
                    else:
                        has_value = any(getattr(f, "text", None) for f in frames)
                    if has_value:
                        present.add(field)
                    continue
                desc = _txxx_desc_for_field(field)
                if desc and normalise_tag_name(desc) in txxx_names:
                    present.add(field)

        elif suffix == ".flac":
            audio = FLAC(file_path)
            vtags = audio.tags
            for field in fields:
                if field == "cover_art_data":
                    if any(getattr(p, "type", None) == 3 for p in audio.pictures):
                        present.add(field)
                    continue
                if vtags is None:
                    continue
                # Vorbis comment lookups are case-insensitive.
                values = vtags.get(_vorbis_key_for_field(field))
                if values and any(str(v).strip() for v in values):
                    present.add(field)

        elif suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]
            audio = MP4(file_path)
            if audio.tags is None:
                return present
            for field in fields:
                atom = _mp4_atom_for_field(field)
                if atom and audio.tags.get(atom):
                    present.add(field)
    except Exception as exc:
        logger.debug("Fill-missing pre-check could not read %s: %s", file_path, exc)
    return present


def _write_tags_to_file_impl(file_path: str, tags: Dict[str, Any]) -> bool:
    # ── Resolve the path ───────────────────────────────────────────────────
    # A stored path may be RELATIVE to the music root (Navidrome stores it
    # that way, and ``rename-file`` can too). Resolving here — once, for every
    # caller — means a relative path is written instead of failing with
    # "File not found" at whichever call site forgot to resolve it.
    if file_path and not os.path.isfile(file_path):
        resolved = _resolve_music_file_path(file_path)
        if resolved:
            file_path = resolved
    if not file_path or not os.path.isfile(file_path):
        logger.error("File not found: %s", file_path)
        return False

    # ── Tagging policy gate ────────────────────────────────────────────────
    # ``tagging.write_tags_to_file`` master toggle lets Popularr run as a
    # read-only database scanner/UI (external tagger, read-only mounts,
    # network drives).  ``ratings_only`` restricts writes to POPM/RATING
    # frames; ``fill_missing_only`` never overwrites a populated frame.
    try:
        from helpers.config_helpers import get_tagging_config
        cfg = get_tagging_config() or {}
    except Exception:
        cfg = {}

    if not cfg.get("write_tags_to_file", True):
        logger.debug("[TAG] File tag writes disabled (tagging.write_tags_to_file=false) — DB only")
        return False

    tags = dict(tags or {})

    if cfg.get("ratings_only"):
        # ONLY the rating. The old check let the WHOLE payload through as long
        # as it also carried a rating, so "ratings only" still wrote titles,
        # genres and art whenever they travelled with a rating.
        if "rating" not in tags:
            logger.debug("[TAG] ratings_only mode — skipping non-rating write to %s", file_path)
            return False
        tags = {"rating": tags["rating"]}

    # fill_missing_only: never overwrite a frame that already carries a value.
    # Empty incoming values still pass through (they are explicit "clear this
    # frame" requests — the writers delete the frame on empty).
    if cfg.get("fill_missing_only"):
        already = _existing_non_empty_fields(file_path, tags)
        tags = {
            k: v for k, v in tags.items()
            if k not in already or not str(v or "").strip()
        }
        if "cover_art_data" not in tags:
            tags.pop("cover_art_mime", None)
        if not tags:
            return False

    # Timestamp policy (Navidrome compatibility):
    #
    # Navidrome's scanner detects file changes by path + mtime, so a file
    # whose bytes changed MUST keep the fresh mtime of the swap — restoring
    # the old one made the swapped file look "unchanged" and its mapped-tags
    # index stayed stale.  A TRUE no-op (identical bytes) must leave the file
    # untouched entirely.  ``_write_tags_atomic`` now does both: it compares
    # the edited copy with the original BEFORE the swap and, when nothing
    # changed, discards the copy and never touches the original — so its
    # timestamps are preserved without the old approach of reading BOTH
    # versions of the whole file into memory (two full copies of a 100 MB
    # FLAC per write, four writes at a time on an album save).
    suffix = Path(file_path).suffix.lower()

    # MP4/M4A carries the same metadata in atoms rather than ID3 frames or
    # Vorbis comments; without this branch every m4a write logged "Unsupported
    # file format" and did nothing.
    if suffix not in (".mp3", ".flac", ".m4a", ".mp4"):
        logger.warning("Unsupported file format: %s", suffix)
        return False

    with _path_lock(file_path):
        return _write_tags_atomic(file_path, tags)


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

    When the edited copy is byte-identical to the original the copy is
    discarded and the original is never touched (a no-op write keeps its
    timestamps and does not make Navidrome re-read the file).

    Returns True on success (including a no-op).  On failure the original
    file is left untouched.
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
        elif suffix in (".m4a", ".mp4"):
            ok = write_mp4_tags(tmp_path, tags)
        elif suffix == ".flac":
            ok = write_flac_tags(tmp_path, tags)
        else:
            logger.error("Unsupported file format: %s", suffix)
            return False

        if not ok:
            return False

        # True no-op: nothing to swap in. ``filecmp`` streams both files in
        # chunks and stops at the first difference (tags sit at the start of
        # the file, so a real change is found almost immediately).
        try:
            if filecmp.cmp(tmp_path, file_path, shallow=False):
                return True
        except OSError:
            pass

        # Preserve the original's permissions — and, when we are allowed to,
        # its OWNER — on the replacement. A container running as root would
        # otherwise hand every edited file to root, and the media server or
        # the user could no longer modify it.
        try:
            st = os.stat(file_path)
            os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
            if hasattr(os, "chown"):
                try:
                    os.chown(tmp_path, st.st_uid, st.st_gid)
                except OSError:
                    pass
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
    MP4 has no standard rating atom, so an M4A rating is a no-op.
    """
    stars = max(1, min(5, int(stars or 0)))
    return write_tags_to_file(file_path, {"rating": stars})

# =============================================================================
# HELPERS
# =============================================================================

def _set_text_frame(tag_obj: ID3Type, frame_id: str, frame_cls, value: Any) -> None:
    tag_obj.delall(frame_id)
    if value is None or value == "":
        return
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
        except Exception as exc:
            # Say WHY the primary path was abandoned. A silent switch here hid
            # a NameError on the line above for as long as it existed (see the
            # MP3Type note in the type layer) — the fallback masked it by
            # working, so nothing anywhere reported that this branch was dead.
            logger.debug(
                "MP3 primary tag path unusable, falling back to ID3(file_path): %s", exc
            )
            try:
                tag_obj = cast(ID3Type, ID3(file_path))
            except Exception:
                tag_obj = cast(ID3Type, ID3())
            save = lambda: tag_obj.save(file_path, v2_version=3)

        for field, value in tags.items():
            if field in _NON_TAG_KEYS:
                continue
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
                genres = _split_genres(value)
                if genres:
                    tag_obj.add(TCON(encoding=3, text=genres))
            elif field == "comment":
                tag_obj.delall("COMM")
                if value:
                    tag_obj.add(COMM(encoding=3, lang="eng", desc="", text=[str(value)]))
            elif field == "rating":
                if value:
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
                # existing WRITER variants before writing so an edited writer
                # never leaves a stale duplicate frame.  LYRICIST variants are
                # cleared too, but only when this same payload is not also
                # writing a lyricist — otherwise the order of the keys decided
                # whether the lyricist survived.
                _clear_txxx_variants(tag_obj, "writer")
                if "lyricist" not in tags:
                    _clear_txxx_variants(tag_obj, "lyricist")
                if value:
                    tag_obj.add(TXXX(encoding=3, desc="WRITER", text=[str(value)]))
            elif field == "cover_art_data":
                if value:
                    data = _as_image_bytes(value, file_path)
                    if data is not None:
                        tag_obj.delall("APIC")
                        tag_obj.add(
                            APIC(
                                encoding=3,
                                mime=tags.get("cover_art_mime") or "image/jpeg",
                                type=3,
                                desc="Cover",
                                data=data,
                            )
                        )
            else:
                # ── Album-extended fields ───────────────────────────────────
                # Standard MusicBrainz / album fields that have no native ID3
                # frame (release type/status/country, originalyear, totals,
                # media, label, barcode, cover markers, ...) must be written as
                # TXXX frames — Navidrome reads these back and uses them to
                # group/merge albums.
                #
                # The NAME comes from the canonical registry, and every
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
    #
    # ⚠️ ``musicbrainz_genres`` used to map to ``GENRE`` here. Whenever the
    # registry had no name for it, a payload carrying both ``genres`` and
    # ``musicbrainz_genres`` wrote GENRE twice and the MusicBrainz list
    # (including the "Cover" marker) REPLACED the track's real genres. It
    # keeps its own comment, like the MP3 writer's TXXX frame.
    "musicbrainz_genres": "musicbrainz_genres",
}


def _set_flac_front_cover(audio: FLACType, data: bytes, mime: str) -> None:
    """Make *data* the file's ONE front cover, keeping every other picture.

    ``add_picture`` APPENDS, so embedding art on every album save stacked
    another copy of the cover into the file each time — the file grew and
    players picked whichever front cover they found first.
    """
    others = [p for p in audio.pictures if getattr(p, "type", None) != 3]
    audio.clear_pictures()
    for pic in others:
        audio.add_picture(pic)
    pic = FLACPicture()
    pic.data = data
    pic.mime = mime or "image/jpeg"
    pic.type = 3
    pic.desc = "Cover"
    audio.add_picture(pic)


def write_flac_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    if not MUTAGEN_AVAILABLE:
        logger.error("Mutagen not available for FLAC writing")
        return False

    try:
        audio = cast(FLACType, FLAC(file_path))
        if audio.tags is None:
            audio.add_tags()

        for field, value in tags.items():
            if field in _NON_TAG_KEYS:
                continue

            # Cover art is a PICTURE block, never a comment. It used to fall
            # through to the generic branch, which stored ``str(bytes)`` — the
            # whole image as a Python bytes repr — in a COVER_ART_DATA comment.
            if field == "cover_art_data":
                if value:
                    data = _as_image_bytes(value, file_path)
                    if data is not None:
                        _set_flac_front_cover(
                            audio, data, tags.get("cover_art_mime") or "image/jpeg",
                        )
                continue

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

            if field.lower() == "genre":
                # Split into multiple Vorbis GENRE values so Navidrome sees
                # each genre, never one literal separator-joined string.
                genres = _split_genres(value)
                if genres:
                    audio[field] = genres
                elif field in audio:
                    del audio[field]
            elif isinstance(value, (list, tuple, set)):
                # A list must become MULTIPLE Vorbis values — writing
                # str(list) would embed the literal "['Rock', 'Metal']".
                audio[field] = [str(v).strip() for v in value if str(v).strip()]
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

def write_tags_to_file(file_path: str, tags: Dict[str, Any]) -> bool:
    """Write tags, then durably mark the local correction.

    ⭐ The mark is what protects the correction from a later, older Navidrome
    import. It is written only after the file write SUCCEEDS: a failed write
    must not claim local authority. Rating/cover-only writes are excluded:
    those columns are already popularity-owned and must not force a scan.
    """
    ok = _write_tags_to_file_impl(file_path, tags)
    _non_metadata = {"rating", "playcount", "skipcount", "cover_art_data", "cover_art_mime"}
    _is_metadata_write = bool(tags) and any(key not in _non_metadata for key in tags)
    if ok and _is_metadata_write:
        try:
            from services.scanning.reconciliation import (
                mark_local_tags_written_for_path,
            )

            mark_local_tags_written_for_path(file_path)
        except Exception as exc:
            logger.debug("Local tag write marker failed: %s", exc)
        try:
            from services.scanning.navidrome_rescan_service import (
                mark_rescan_required,
            )

            mark_rescan_required("file tags written")
        except Exception:
            pass
    return ok


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
    # ⭐ ``recording_mbid`` is the CANONICAL recording-id column every consumer
    # reads. Without this mapping an edit/save that corrected it was DB-only:
    # the file kept the old MUSICBRAINZ_TRACKID, Navidrome echoed it back, and
    # the import preserved the DB correction on every scan — the reported
    # repeating "local correction preserved … mbid" warning. Listed LAST so it
    # wins over the legacy ``mbid``/``musicbrainz_trackid`` aliases.
    "recording_mbid": "mbid",
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

#: Album/RELEASE-level MusicBrainz fields that must reach the file tags when a
#: caller supplies them.  ``update_file_metadata`` forwards only an explicit
#: key list, so anything not named there is silently discarded — which is how
#: an "album-page parity" payload lost its release-country / album-type /
#: album-artist-MBID on the way to the writer.
#:
#: Deliberately EXCLUDES ``year`` / ``release_year``: those have their own
#: DATE-vs-ORIGINALYEAR handling in ``build_tag_updates`` and are already
#: applied above.
_ALBUM_MB_TAG_COLUMNS: tuple[str, ...] = (
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid",
    "releasecountry",
    "recordlabel",
    "catalognumber",
    "barcode",
    "media",
    "originalyear",
    "originaldate",
    # ``releasedate`` was missing, so the import wrote the release date to the
    # DATABASE but not the FILE; the album page's save (which goes through
    # ``build_tag_updates``) did write it, so the two disagreed.
    "releasedate",
    # Release/medium totals the album page writes (TRACKTOTAL/DISCTOTAL
    # TXXX or Vorbis) — a queued import must carry them too.
    "tracktotal",
    "disctotal",
)


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
        k: v for k, v in (tags or {}).items()
        if v is not None and str(v).strip() != ""
    }
    if not tags_to_write:
        logger.warning("No editable tags to write for track %s", track_id)
        return False

    return write_tags_to_file(file_path, tags_to_write)


def update_file_metadata(file_path: str, metadata: Dict[str, Any]) -> bool:
    """Write the caller's metadata onto *file_path*.

    Contract: a key whose value is ``None`` means "not supplied" and the frame
    is LEFT ALONE; ``""`` still means "explicitly clear this frame" (the
    single-disc ``disc_number`` clear depends on it).  Every base field used to
    be unconditionally included, so a queue row that lacked e.g. ``year``
    delivered ``year=None`` to the writers — which treat ``None`` as a delete —
    and the import actively WIPED the downloaded file's own date/track-number
    frames (part of the reported "imported without year/track number" bug).
    """
    if not file_path:
        return False

    tag_updates = {
        key: value
        for key, value in {
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
        }.items()
        if value is not None
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

    # Track-level MusicBrainz enrichment carried on the queue row's metadata
    # JSONB (``add_release_tracks_to_queue_detailed`` persists it): ISRC →
    # TSRC and the track-artist MBID → MUSICBRAINZ_ARTISTID.  The album page
    # writes both, so a queued import without them never matched the release
    # it was downloaded for.
    if metadata.get("isrc"):
        tag_updates["isrc"] = metadata.get("isrc")
    if metadata.get("musicbrainz_artistid"):
        tag_updates["musicbrainz_artistid"] = metadata.get("musicbrainz_artistid")

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

    # ---- Album-level MusicBrainz fields -------------------------------------
    # These are the RELEASE-level identity fields an album-page lookup writes
    # onto every track of the album (type / status / country / album-artist
    # MBID / release-group MBID).  ``_COLUMN_TO_TAG_FIELD`` already owns the
    # column→tag mapping, so it is reused rather than duplicated — any future
    # rename stays in one place.
    for _column in _ALBUM_MB_TAG_COLUMNS:
        _album_value = metadata.get(_column)
        if _album_value is not None and str(_album_value).strip() != "":
            tag_updates[_COLUMN_TO_TAG_FIELD.get(_column, _column)] = _album_value

    return write_tags_to_file(file_path, tag_updates)

# =============================================================================
# ALBUM ART
# =============================================================================

def embed_album_art(file_path: str, image_data: bytes, mime_type: str = "image/jpeg") -> bool:
    """Embed album art image data into an audio file's tags.

    Goes through ``write_tags_to_file`` like every other write, so art now:

    * honours the tagging config — it used to write even with
      ``tagging.write_tags_to_file`` switched OFF (read-only mode);
    * is written ATOMICALLY and under the per-file lock (it rewrote the file
      in place, racing any concurrent tag write on the same file);
    * REPLACES the FLAC front cover instead of appending another copy on every
      embed;
    * saves MP3 as ID3v2.3 like every other write (it used to save v2.4, so
      the version of a file's tag depended on which write touched it last);
    * supports M4A.

    Args:
        file_path: Path to the audio file (absolute or relative to the music root).
        image_data: Raw image bytes (``bytearray``/``memoryview`` accepted).
        mime_type: Image MIME type (``image/jpeg`` or ``image/png``).

    Returns:
        True on success, False otherwise.
    """
    if not file_path or not image_data:
        return False
    data = _as_image_bytes(image_data, file_path)
    if data is None:
        return False
    return write_tags_to_file(
        file_path,
        {"cover_art_data": data, "cover_art_mime": mime_type or "image/jpeg"},
    )

# =============================================================================
# MP4 / M4A
# =============================================================================

#: Fields that map onto a NATIVE MP4 atom (mutagen stores text atoms as a
#: list of strings, so the value built here is already in mutagen's shape).
_MP4_TEXT_ATOMS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_artist": "aART",
    "year": "\xa9day",
    "genre": "\xa9gen",
    "genres": "\xa9gen",
    "composer": "\xa9wrt",
    "comment": "\xa9cmt",
    "lyrics": "\xa9lyr",
}

#: Fields MP4 has no native atom for, stored as iTunes freeform atoms
#: (``----:com.apple.iTunes:<name>``) — the same scheme Picard and MusicBrainz
#: use, so the values round-trip through other taggers.
#:
#: ``mbid`` is listed because ``build_tag_updates`` maps
#: ``musicbrainz_trackid`` → ``mbid``; without it every M4A written from a DB
#: row lost its recording id.
_MP4_FREEFORM_ATOMS = {
    "mbid": "MusicBrainz Track Id",
    "musicbrainz_trackid": "MusicBrainz Track Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_releasetrackid": "MusicBrainz Release Track Id",
    "musicbrainz_workid": "MusicBrainz Work Id",
    "originalyear": "Original Year",
    "originaldate": "Original Date",
    "original_title": "Original Title",
    "original_cover_artist": "Original Album Artist",
    "isrc": "ISRC",
    "iswc": "ISWC",
    "writer": "Writer",
    "lyricist": "Lyricist",
    "is_cover": "Cover",
}

#: Pair-valued atoms: ``(track, total)`` / ``(disc, total)``.  We never know
#: the total, and 0 is MP4's "unknown".
_MP4_PAIR_ATOMS = {
    "track_number": "trkn",
    "disc_number": "disk",
}


def _mp4_atom_for_field(field: str) -> str | None:
    """The MP4 atom key *field* is stored under (or None)."""
    if field in _MP4_TEXT_ATOMS:
        return _MP4_TEXT_ATOMS[field]
    if field in _MP4_PAIR_ATOMS:
        return _MP4_PAIR_ATOMS[field]
    if field in _MP4_FREEFORM_ATOMS:
        return f"----:com.apple.iTunes:{_MP4_FREEFORM_ATOMS[field]}"
    if field == "cover_art_data":
        return "covr"
    return None


def _mp4_tag_items(tags: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the writer's logical field names into MP4 atoms mutagen can store.

    Kept separate from the write so the mapping is testable without a real
    audio file. Returns atom → value, where a value of ``None`` means "clear
    this atom" (the same contract the ID3/Vorbis writers follow: ``""`` clears,
    an absent key is left alone).

    ``rating`` is deliberately absent: MP4 has no standard rating atom, so a
    star rating written here would be invisible to every player. Cover art is
    handled by ``write_mp4_tags`` (it needs an ``MP4Cover``).
    """
    from helpers.track_ordering import leading_int

    items: Dict[str, Any] = {}
    for field, atom in _MP4_TEXT_ATOMS.items():
        if field not in tags or tags[field] is None:
            continue
        if field in ("genre", "genres"):
            # Several genres = several values in the one atom. ``str(list)``
            # used to store the literal "['Rock', 'Metal']" as ONE genre.
            genres = _split_genres(tags[field])
            items[atom] = genres or None
            continue
        text = str(tags[field]).strip()
        items[atom] = [text] if text else None
    for field, atom in _MP4_PAIR_ATOMS.items():
        if field not in tags or tags[field] is None:
            continue
        number = leading_int(tags[field])
        items[atom] = [(number, 0)] if number is not None else None
    for field, atom in _MP4_FREEFORM_ATOMS.items():
        if field not in tags or tags[field] is None:
            continue
        text = str(tags[field]).strip()
        key = f"----:com.apple.iTunes:{atom}"
        items[key] = text.encode("utf-8") if text else None
    return items


def write_mp4_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    """Write the caller's metadata onto an MP4/M4A file.

    Same contract as :func:`write_id3_tags` / :func:`write_flac_tags`: a
    ``None`` value clears the atom, a missing key leaves it alone.
    """
    if not MUTAGEN_AVAILABLE:
        logger.error("Mutagen not available for MP4 writing")
        return False
    try:
        from mutagen.mp4 import MP4, MP4Cover  # type: ignore[import-untyped]

        audio = MP4(file_path)
        if audio.tags is None:
            audio.add_tags()
        for atom, value in _mp4_tag_items(tags).items():
            if value is None:
                audio.tags.pop(atom, None)
                continue
            audio.tags[atom] = value

        if tags.get("cover_art_data"):
            data = _as_image_bytes(tags["cover_art_data"], file_path)
            if data is not None:
                mime = str(tags.get("cover_art_mime") or "image/jpeg").lower()
                fmt = MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG
                audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]

        audio.save()
        return True
    except Exception as exc:
        logger.error("Failed to write MP4 tags to %s: %s", file_path, exc, exc_info=True)
        return False

# =============================================================================
# FLAC → MP3 / M4A CONVERSION
# =============================================================================

def convert_flac_to_m4a(flac_path: str, bitrate: str = "256k") -> str | None:
    """Convert a FLAC file to M4A (AAC) using ffmpeg.

    Mirrors :func:`convert_flac_to_mp3` so both share the caller contract:
    returns the new path or ``None`` on failure, and deletes the original FLAC
    after a successful conversion.
    """
    return _convert_flac(flac_path, target="m4a", bitrate=bitrate)


def convert_flac_to_mp3(flac_path: str, bitrate: str = "320k") -> str | None:
    """Convert a FLAC file to MP3 using ffmpeg.

    Args:
        flac_path: Path to the FLAC file.
        bitrate: Target bitrate (default ``"320k"``).

    Returns:
        Path to the converted MP3 file, or ``None`` on failure.
        The original FLAC file is deleted after a successful conversion.
    """
    return _convert_flac(flac_path, target="mp3", bitrate=bitrate)


def _convert_flac(flac_path: str, *, target: str, bitrate: str) -> str | None:
    """Convert a FLAC file to *target* (``"mp3"`` or ``"m4a"``) using ffmpeg.

    Shared by :func:`convert_flac_to_mp3` and :func:`convert_flac_to_m4a` so
    the existence/ffmpeg checks, the timeout, the failure reporting and the
    "delete the original afterwards" behaviour stay in one place — the only
    difference between the two is the codec arguments.
    """
    import subprocess

    if target not in ("mp3", "m4a"):
        logger.error("Unsupported conversion target: %s", target)
        return None
    label = f"FLAC→{target.upper()}"

    if not os.path.exists(flac_path):
        logger.error("FLAC file not found: %s", flac_path)
        return None

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.error("ffmpeg not available — cannot convert %s", label)
        return None

    out_path = os.path.splitext(flac_path)[0] + f".{target}"
    # MP3 keeps the VBR-quality setting it always used; AAC has no -q:a
    # equivalent, so it takes an explicit bitrate instead.
    codec_args = (
        ["-c:a", "aac", "-b:a", bitrate]
        if target == "m4a"
        else ["-b:a", bitrate, "-q:a", "0"]
    )
    cmd = [
        "ffmpeg", "-i", flac_path,
        *codec_args,
        "-v", "error", "-y", out_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        if result.returncode != 0:
            logger.error("%s conversion failed: %s", label, result.stderr)
            return None

        if not os.path.exists(out_path):
            logger.error("Conversion succeeded but output file not found")
            return None

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        logger.info("Converted %s: %s (%.1f MB)", label, out_path, size_mb)

        try:
            os.remove(flac_path)
            logger.debug("Deleted original FLAC: %s", flac_path)
        except Exception as exc:
            logger.warning("Could not delete original FLAC: %s", exc)

        return out_path

    except subprocess.TimeoutExpired:
        logger.error("%s conversion timed out after 300s: %s", label, flac_path)
        return None
    except Exception as exc:
        logger.error("%s conversion error: %s", label, exc, exc_info=True)
        return None
