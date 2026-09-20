"""Canonical on-disk tag names for MusicBrainz / album extended metadata.

WHY THIS EXISTS
---------------
The same field was being written under several spellings, so a track could end
up carrying two or three frames for one value and neither "looked wrong":

    MUSICBRAINZ ALBUMSTATUS      (the legacy space form, inherited from
    MUSICBRAINZ ALBUMTYPE         old_system's MB_TXXX_FIELDS + the MP3
                                  generic TXXX fallback)
    MUSICBRAINZ_ALBUMID          (the underscore form, used by the FLAC/Vorbis
    MUSICBRAINZ_ARTISTID          writer and by the album-extended map)
    MUSICBRAINZ_ALBUMTYPE        (the same field again, different separator)
    ORIGYEAR  +  ORIGINALYEAR    (two names AND a duplicate frame:
                                  ``delall("TXXX:ORIGNALYEAR")`` is
                                  case-sensitive, so writing
                                  "originalyear" over "ORIGINALYEAR"
                                  left BOTH frames in place)

They accumulated because deletion was exact-desc only, while writers disagreed
about case and separators.  This module is the ONE place that decides the name,
and it also clears every variant of a field before writing the canonical form,
so existing files are cleaned up on their next tag write.

HOW THE CANONICAL NAMES WERE CHOSEN
-----------------------------------
Every name below is verified against Navidrome's own ``mappings.yaml`` (the
file that decides which tags Navidrome reads):

    musicbrainz_albumid:
      aliases: [ txxx:musicbrainz album id, musicbrainz_albumid, ... ]
    musicbrainz_albumartistid / musicbrainz_artistid / musicbrainz_releasegroupid
    musicbrainz_recordingid: [..., musicbrainz_trackid, ...]
    musicbrainz_trackid:     [..., musicbrainz_releasetrackid, ...]
    releasetype:   [ txxx:musicbrainz album type, releasetype, musicbrainz_albumtype ]
    releasestatus: [ txxx:musicbrainz album status, releasestatus, musicbrainz_albumstatus ]
    releasecountry:[ txxx:musicbrainz album release country, releasecountry ]
    originaldate:  [ ..., originalyear, ..., origyear ]

Navidrome matches aliases case-INSENSITIVELY but does NOT strip separators, so
"musicbrainz_albumid" and "musicbrainz album id" are two different aliases and
both work.  We standardise on the **underscore** form for IDs because it is what
the FLAC/Vorbis writer and MusicBrainz Picard's file tags use; the FLAC side is
case-insensitive by Vorbis spec, so ONE name then serves both containers.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# THE canonical map: internal field name -> the ONE on-disk name
# ---------------------------------------------------------------------------
CANONICAL_TAG_NAMES: dict[str, str] = {
    # ── MusicBrainz entity IDs ────────────────────────────────────────────
    "musicbrainz_albumid": "MUSICBRAINZ_ALBUMID",
    "musicbrainz_album_mbid": "MUSICBRAINZ_ALBUMID",
    "musicbrainz_releaseid": "MUSICBRAINZ_ALBUMID",
    "musicbrainz_artistid": "MUSICBRAINZ_ARTISTID",
    "musicbrainz_artist_id": "MUSICBRAINZ_ARTISTID",
    "musicbrainz_albumartistid": "MUSICBRAINZ_ALBUMARTISTID",
    "musicbrainz_albumartist_id": "MUSICBRAINZ_ALBUMARTISTID",
    "musicbrainz_trackid": "MUSICBRAINZ_TRACKID",
    "mbid": "MUSICBRAINZ_TRACKID",
    "beets_mbid": "MUSICBRAINZ_TRACKID",
    "musicbrainz_releasetrackid": "MUSICBRAINZ_RELEASETRACKID",
    "musicbrainz_release_track_id": "MUSICBRAINZ_RELEASETRACKID",
    "musicbrainz_releasegroupid": "MUSICBRAINZ_RELEASEGROUPID",
    "musicbrainz_workid": "MUSICBRAINZ_WORKID",
    # ── Album classification ─────────────────────────────────────────────
    "musicbrainz_albumtype": "RELEASETYPE",
    "musicbrainz_releasetype": "RELEASETYPE",
    "releasetype": "RELEASETYPE",
    "musicbrainz_albumstatus": "RELEASESTATUS",
    "musicbrainz_releasestatus": "RELEASESTATUS",
    "releasestatus": "RELEASESTATUS",
    "musicbrainz_releasecountry": "RELEASECOUNTRY",
    "musicbrainz_albumcountry": "RELEASECOUNTRY",
    "releasecountry": "RELEASECOUNTRY",
    # ── Dates ────────────────────────────────────────────────────────────
    "originalyear": "ORIGINALYEAR",
    "origyear": "ORIGINALYEAR",
    "originaldate": "ORIGINALDATE",
    # ── Release detail ───────────────────────────────────────────────────
    "tracktotal": "TRACKTOTAL",
    "totaltracks": "TRACKTOTAL",
    "disctotal": "DISCTOTAL",
    "totaldiscs": "DISCTOTAL",
    "media": "MEDIA",
    "label": "LABEL",
    "catalognumber": "CATALOGNUMBER",
    "barcode": "BARCODE",
    "asin": "ASIN",
    "script": "SCRIPT",
    "discsubtitle": "DISCSUBTITLE",
    "copyright": "COPYRIGHT",
    "language": "LANGUAGE",
    "explicitstatus": "EXPLICITSTATUS",
    # ── Cover markers (both readers accept either spelling) ──────────────
    "is_cover": "IS_COVER",
    "original_cover_artist": "ORIGINAL_COVER_ARTIST",
}

#: Deliberately NOT in the registry — these keep the name their own writer
#: already used, because renaming them would change behaviour rather than fix a
#: duplicate:
#:   * ``musicbrainz_genres`` — the FLAC writer maps it onto Navidrome's
#:     ``GENRE`` field on purpose; the MP3 writer uses "MUSICBRAINZ GENRES".
#:   * ``original_title`` — the FLAC writer uses "ORIGINAL TITLE".
#:   * ``catalog`` / ``recordlabel`` — written under their own names.
UNMAPPED_TAG_FIELDS: tuple[str, ...] = (
    "musicbrainz_genres", "original_title", "catalog", "recordlabel",
)

#: Fields whose canonical name is a MusicBrainz extended tag.  Used by the
#: guard test and by anything that wants "the MB extended set".
MUSICBRAINZ_TAG_FIELDS: frozenset[str] = frozenset({
    "musicbrainz_albumid",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_trackid",
    "musicbrainz_releasetrackid",
    "musicbrainz_releasegroupid",
    "musicbrainz_workid",
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_releasecountry",
})

_SEPARATOR_RE = re.compile(r"[\s_\-:.=]+")


def normalise_tag_name(name: Any) -> str:
    """Case- and separator-insensitive key for a tag name.

    ``"MUSICBRAINZ ALBUM ID"`` and ``"musicbrainz_albumid"`` both become
    ``"musicbrainzalbumid"`` — the same rule the metadata readers already use,
    which is why old files still read correctly after the rename.  ``=`` is
    included because some taggers write ``MUSICBRAINZ_ALBUMID=value``.
    """
    return _SEPARATOR_RE.sub("", str(name or "").strip().lower())


#: Every spelling this app — or a sibling tagger — has used for each canonical
#: name, declared ONCE.
#:
#: Two kinds of entry live here and they must not be conflated:
#:   * separator/case variants ("MUSICBRAINZ ALBUM ID" vs "MUSICBRAINZ_ALBUMID"),
#:     which the normaliser finds on its own; and
#:   * genuine SYNONYMS — different words for one field
#:     ("ORIGYEAR" vs "ORIGINALYEAR", "TOTALTRACKS" vs "TRACKTOTAL"),
#:     which no normaliser can equate and therefore have to be listed.
#:
#: ``clear_keys_for`` turns both into the normalised key set a write must
#: remove, so a synonym is never silently left behind as a "second tag".
KNOWN_TAG_SPELLINGS: dict[str, tuple[str, ...]] = {
    "MUSICBRAINZ_ALBUMID": ("MUSICBRAINZ ALBUM ID", "MUSICBRAINZ_ALBUM_ID"),
    "MUSICBRAINZ_ARTISTID": ("MUSICBRAINZ ARTIST ID", "MUSICBRAINZ_ARTIST_ID"),
    "MUSICBRAINZ_ALBUMARTISTID": ("MUSICBRAINZ ALBUM ARTIST ID", "MUSICBRAINZ_ALBUM_ARTIST_ID"),
    "MUSICBRAINZ_TRACKID": ("MUSICBRAINZ TRACK ID", "MUSICBRAINZ_TRACK_ID"),
    "MUSICBRAINZ_RELEASETRACKID": (
        "MUSICBRAINZ RELEASE TRACK ID", "MUSICBRAINZ_RELEASE_TRACK_ID",
    ),
    "MUSICBRAINZ_RELEASEGROUPID": (
        "MUSICBRAINZ RELEASE GROUP ID", "MUSICBRAINZ_RELEASE_GROUP_ID",
    ),
    "MUSICBRAINZ_WORKID": ("MUSICBRAINZ WORK ID", "MUSICBRAINZ_WORK_ID"),
    "RELEASETYPE": (
        "MUSICBRAINZ_ALBUMTYPE", "MUSICBRAINZ ALBUM TYPE", "MUSICBRAINZ_ALBUM_TYPE",
    ),
    "RELEASESTATUS": (
        "MUSICBRAINZ_ALBUMSTATUS", "MUSICBRAINZ ALBUM STATUS", "MUSICBRAINZ_ALBUM_STATUS",
    ),
    "RELEASECOUNTRY": (
        "MUSICBRAINZ ALBUM RELEASE COUNTRY", "MUSICBRAINZ_RELEASECOUNTRY",
    ),
    "ORIGINALYEAR": ("ORIGYEAR", "ORIGINAL_YEAR", "ORIGINAL RELEASE YEAR"),
    "ORIGINALDATE": ("ORIGINAL_DATE", "ORIGINAL RELEASE DATE"),
    "IS_COVER": ("IS COVER",),
    "ORIGINAL_COVER_ARTIST": ("ORIGINAL COVER ARTIST",),
    "MUSICBRAINZ_GENRES": ("MUSICBRAINZ GENRES",),
    "TRACKTOTAL": ("TOTALTRACKS", "TOTAL TRACKS"),
    "DISCTOTAL": ("TOTALDISCS", "TOTAL DISCS"),
    "CATALOGNUMBER": ("CATALOG", "CATALOG NUMBER", "CATALOG_NO"),
    "BARCODE": ("BAR CODE",),
    "DISCSUBTITLE": ("DISC SUBTITLE", "SETSUBTITLE"),
    "MEDIA": ("MEDIATYPE",),
    "LABEL": ("RECORDLABEL", "ORGANIZATION", "PUBLISHER"),
    "ALBUMVERSION": ("VERSION",),
}


def _declared_spellings(canonical: str) -> tuple[str, ...]:
    return KNOWN_TAG_SPELLINGS.get(canonical, ())


#: Documentation view: canonical name -> separator/case variants ONLY (the
#: entries a normaliser finds by itself).  Derived so it cannot be mis-filed.
LEGACY_TAG_VARIANTS: dict[str, tuple[str, ...]] = {
    name: tuple(
        s for s in _declared_spellings(name)
        if normalise_tag_name(s) == normalise_tag_name(name)
    )
    for name in KNOWN_TAG_SPELLINGS
}

#: Documentation view: canonical name -> SYNONYMS (different words).
LEGACY_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    name: tuple(
        s for s in _declared_spellings(name)
        if normalise_tag_name(s) != normalise_tag_name(name)
    )
    for name in KNOWN_TAG_SPELLINGS
}


def clear_keys_for(field_or_canonical: Any) -> frozenset[str]:
    """Every normalised tag key that must be removed before writing this field.

    Covers both categories: the canonical name, its separator/case variants
    (equal after normalisation) and its declared synonyms (listed because they
    are not).
    """
    canonical = canonical_tag_name(field_or_canonical) or str(field_or_canonical or "")
    keys = {normalise_tag_name(canonical)}
    keys.update(normalise_tag_name(s) for s in _declared_spellings(canonical))
    return frozenset(k for k in keys if k)


def canonical_tag_name(field: Any) -> str | None:
    """The ONE on-disk name for an internal field, or ``None`` if unmapped."""
    return CANONICAL_TAG_NAMES.get(str(field or "").strip().lower())


def is_variant_of(name: Any, canonical: Any) -> bool:
    """True when *name* is any DECLARED spelling of *canonical*.

    Uses the declared spelling set rather than bare normalisation, because a
    synonym like ``ORIGYEAR`` cannot be derived from ``ORIGINALYEAR``.
    """
    canonical_name = canonical_tag_name(canonical) or str(canonical or "")
    return normalise_tag_name(name) in clear_keys_for(canonical_name)


def all_names_for(field_or_canonical: Any) -> tuple[str, ...]:
    """The canonical name plus every declared spelling of the same field."""
    canonical = canonical_tag_name(field_or_canonical) or str(field_or_canonical or "")
    return (canonical, *_declared_spellings(canonical))


# ---------------------------------------------------------------------------
# Variant clearing — the part that actually removes the duplicates
# ---------------------------------------------------------------------------

def clear_id3_variants(tag_obj: Any, field_or_canonical: Any) -> str:
    """Delete EVERY TXXX frame that is a declared spelling of this field.

    Returns the canonical desc to write.  Matching is by ``normalise_tag_name``
    against the declared key set rather than by exact desc, because
    ``ID3.delall("TXXX:ORIGINALYEAR")`` will NOT remove a frame stored as
    ``TXXX:orignalyear`` — which is exactly how duplicate frames survived an
    overwrite.
    """
    canonical = canonical_tag_name(field_or_canonical) or str(field_or_canonical or "")
    keys = clear_keys_for(canonical)
    if not keys:
        return canonical
    try:
        frame_keys = [str(k) for k in list(tag_obj.keys())]
    except Exception:
        return canonical
    for key in frame_keys:
        if not key.startswith("TXXX:"):
            continue
        if normalise_tag_name(key[5:]) in keys:
            try:
                tag_obj.delall(key)
            except Exception:
                pass
    return canonical


def clear_vorbis_variants(audio: Any, field_or_canonical: Any) -> str:
    """Delete every VorbisComment key that is a declared spelling of this field.

    Vorbis keys are case-insensitive by spec, but mutagen stores the case it was
    given and ``"foo" in audio`` is a case-insensitive lookup while
    ``audio["foo"] = ...`` ADDS a new key when the stored case differs — so the
    same field could end up stored twice.  Deleting every variant first makes
    the write idempotent.
    """
    canonical = canonical_tag_name(field_or_canonical) or str(field_or_canonical or "")
    keys = clear_keys_for(canonical)
    if not keys:
        return canonical
    try:
        for key in [str(k) for k in list(audio.keys())]:
            if normalise_tag_name(key) in keys:
                try:
                    del audio[key]
                except Exception:
                    pass
    except Exception:
        pass
    return canonical


def describe_variants() -> dict[str, tuple[str, ...]]:
    """Canonical name -> every spelling, for docs/tests/UI."""
    return {name: all_names_for(name) for name in CANONICAL_TAG_NAMES.values()}
