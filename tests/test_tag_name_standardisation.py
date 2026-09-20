"""One canonical on-disk name per MusicBrainz / album-extended tag.

The reported symptom was a single track carrying SEVERAL spellings of one value:

    MUSICBRAINZ ALBUMSTATUS      (legacy space form)
    MUSICBRAINZ ALBUMTYPE
    MUSICBRAINZ_ALBUMARTISTID    (underscore form, from the FLAC/extended map)
    MUSICBRAINZ_ALBUMID
    MUSICBRAINZ_ALBUMTYPE        (the same field again)
    MUSICBRAINZ_ARTISTID
    MUSICBRAINZ_RELEASEGROUPID
    MUSICBRAINZ_RELEASETRACKID
    MUSICBRAINZ_TRACKID
    ORIGYEAR + ORIGINALYEAR      (two names AND a duplicate frame)

Two mechanisms produced that, and both are pinned here:

1. **Hand-written descs in several tables** (`_MB_TXXX_DESC`, the TXXX literals in
   ``write_id3_tags``, the generic fallback's ``field.replace("_", " ").upper()``
   and ``_VORBIS_FIELD_MAP``) disagreed about case and separators.  They now all
   read from ``services.metadata.tag_names``.
2. **Deletion was exact-desc only.**  ``ID3.delall("TXXX:ORIGNALYEAR")`` does NOT
   remove a frame stored as ``TXXX:orignalyear``, so writing the "same" tag again
   left both frames in place.  Every write now clears every case/separator
   variant of the field first.

Every canonical name is verified against Navidrome's own ``mappings.yaml``
aliases — see the module docstring in ``services/metadata/tag_names.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.metadata.tag_names import (
    CANONICAL_TAG_NAMES,
    KNOWN_TAG_SPELLINGS,
    LEGACY_TAG_ALIASES,
    LEGACY_TAG_VARIANTS,
    all_names_for,
    canonical_tag_name,
    clear_id3_variants,
    clear_keys_for,
    clear_vorbis_variants,
    is_variant_of,
    normalise_tag_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TAG_FILE_SERVICE = REPO_ROOT / "services" / "metadata" / "tag_file_service.py"
TAG_NAMES = REPO_ROOT / "services" / "metadata" / "tag_names.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. The registry itself
# ---------------------------------------------------------------------------

def test_every_declared_spelling_is_cleared_by_a_write() -> None:
    """No declared spelling may survive the write of its canonical name.

    This is the invariant that actually removes duplicates. It covers BOTH kinds
    of declared spelling: separator/case variants (which normalisation finds)
    and synonyms like ORIGYEAR/TOTALTRACKS (which it cannot, so they are
    listed).
    """
    for canonical in KNOWN_TAG_SPELLINGS:
        keys = clear_keys_for(canonical)
        assert normalise_tag_name(canonical) in keys
        for spelling in KNOWN_TAG_SPELLINGS[canonical]:
            assert normalise_tag_name(spelling) in keys, (
                f"{spelling!r} would NOT be cleared by a write of {canonical!r} — "
                "it would stay behind as a second tag for the same field."
            )


def test_synonyms_are_listed_not_inferred() -> None:
    """A different word can never be found by normalisation — prove it."""
    assert normalise_tag_name("ORIGYEAR") != normalise_tag_name("ORIGINALYEAR")
    assert "ORIGYEAR" in LEGACY_TAG_ALIASES["ORIGINALYEAR"]
    assert "TOTALTRACKS" in LEGACY_TAG_ALIASES["TRACKTOTAL"]
    # ...while a separator variant needs no listing beyond the declaration.
    assert "MUSICBRAINZ ALBUM ID" in LEGACY_TAG_VARIANTS["MUSICBRAINZ_ALBUMID"]


def test_normalisation_ignores_case_and_separators() -> None:
    for a, b in (
        ("MUSICBRAINZ ALBUM ID", "musicbrainz_albumid"),
        ("ORIGYEAR", "orig_year"),
        ("MUSICBRAINZ-TRACK-ID", "MusicBrainz Track Id"),
        ("RELEASETYPE", "releasetype"),
    ):
        assert normalise_tag_name(a) == normalise_tag_name(b)


def test_the_reported_fields_are_all_mapped() -> None:
    """Every tag from the report resolves to one canonical name."""
    reported = {
        "musicbrainz_albumid": "MUSICBRAINZ_ALBUMID",
        "musicbrainz_artistid": "MUSICBRAINZ_ARTISTID",
        "musicbrainz_albumartistid": "MUSICBRAINZ_ALBUMARTISTID",
        "musicbrainz_trackid": "MUSICBRAINZ_TRACKID",
        "musicbrainz_releasetrackid": "MUSICBRAINZ_RELEASETRACKID",
        "musicbrainz_releasegroupid": "MUSICBRAINZ_RELEASEGROUPID",
        "musicbrainz_albumtype": "RELEASETYPE",
        "musicbrainz_albumstatus": "RELEASESTATUS",
        "originalyear": "ORIGINALYEAR",
    }
    for field, expected in reported.items():
        assert canonical_tag_name(field) == expected, f"{field} → {canonical_tag_name(field)}"


def test_origyear_and_originalyear_are_one_field() -> None:
    assert canonical_tag_name("origyear") == "ORIGINALYEAR"
    assert all_names_for("originalyear")[0] == "ORIGINALYEAR"
    assert "ORIGYEAR" in all_names_for("originalyear")


def test_aliases_share_one_canonical_name() -> None:
    """Field aliases must not invent a second on-disk name."""
    assert canonical_tag_name("musicbrainz_album_mbid") == canonical_tag_name("musicbrainz_albumid")
    assert canonical_tag_name("mbid") == canonical_tag_name("musicbrainz_trackid")
    assert canonical_tag_name("musicbrainz_releasetype") == canonical_tag_name("musicbrainz_albumtype")


def test_unmapped_fields_stay_unmapped() -> None:
    """Renaming these would change behaviour, not fix a duplicate."""
    for field in ("musicbrainz_genres", "original_title", "catalog", "recordlabel", "title", ""):
        assert canonical_tag_name(field) is None


# ---------------------------------------------------------------------------
# 2. Variant clearing — the part that removes the duplicates
# ---------------------------------------------------------------------------

class _FakeTXXX:
    def __init__(self, desc: str, text: str = "x") -> None:
        self.desc = desc
        self.text = [text]


class _FakeID3(dict):
    """Minimal ID3 double: exact-key ``delall``, like mutagen's."""

    def keys(self):  # type: ignore[override]
        return list(super().keys())

    def getall(self, frame_id: str):
        return self.get(frame_id, [])

    def delall(self, frame_id: str) -> None:
        # EXACT match only — this is the mutagen behaviour that let a
        # case-variant frame survive an overwrite.
        self.pop(frame_id, None)

    def add(self, frame) -> None:
        self.setdefault(f"TXXX:{frame.desc}", []).append(frame)


def test_clear_id3_variants_removes_every_spelling() -> None:
    tag_obj = _FakeID3()
    tag_obj["TXXX:MUSICBRAINZ ALBUM ID"] = [_FakeTXXX("MUSICBRAINZ ALBUM ID", "old")]
    tag_obj["TXXX:musicbrainz_albumid"] = [_FakeTXXX("musicbrainz_albumid", "older")]
    tag_obj["TXXX:MUSICBRAINZ_ALBUMID"] = [_FakeTXXX("MUSICBRAINZ_ALBUMID", "newest")]
    tag_obj["TXXX:UNRELATED"] = [_FakeTXXX("UNRELATED", "keep me")]

    canonical = clear_id3_variants(tag_obj, "musicbrainz_albumid")

    assert canonical == "MUSICBRAINZ_ALBUMID"
    assert not [k for k in tag_obj.keys() if "ALBUMID" in k.upper().replace(" ", "_")], (
        "a spelling of the field survived — this is the duplicate-frame bug"
    )
    assert "TXXX:UNRELATED" in tag_obj, "an unrelated frame must never be touched"


def test_clear_id3_variants_handles_the_origyear_case() -> None:
    """ORIGINALYEAR + origyear on one track must collapse to one frame."""
    tag_obj = _FakeID3()
    tag_obj["TXXX:ORIGINALYEAR"] = [_FakeTXXX("ORIGINALYEAR", "1991")]
    # The duplicate: same field, different case → exact delall leaves it behind.
    tag_obj["TXXX:originalyear"] = [_FakeTXXX("originalyear", "1991")]
    tag_obj["TXXX:ORIGYEAR"] = [_FakeTXXX("ORIGYEAR", "1991")]

    clear_id3_variants(tag_obj, "originalyear")

    assert tag_obj == {}, f"duplicate ORIGINALYEAR frames survived: {list(tag_obj)}"


class _FakeFLAC(dict):
    """Minimal VorbisComment double (mutagen stores the case it was given)."""

    def __delitem__(self, key):  # pragma: no cover - exercised via clear
        super().__delitem__(key)


def test_clear_vorbis_variants_removes_case_variants() -> None:
    audio = _FakeFLAC({
        "MUSICBRAINZ_ALBUMID": ["rel-1"],
        "musicbrainz_albumid": ["rel-2"],
        "ORIGYEAR": ["1991"],
        "genre": ["Metal"],
    })
    clear_vorbis_variants(audio, "musicbrainz_albumid")
    assert list(audio) == ["ORIGYEAR", "genre"]

    clear_vorbis_variants(audio, "originalyear")
    assert list(audio) == ["genre"], "ORIGYEAR must be cleared by the ORIGINALYEAR write"


# ---------------------------------------------------------------------------
# 3. The writers must USE the registry (no hand-written descs left)
# ---------------------------------------------------------------------------

_LEGACY_SPACE_FORM_DESCS = (
    '"MUSICBRAINZ ALBUM ID"',
    '"MUSICBRAINZ TRACK ID"',
    '"MUSICBRAINZ ARTIST ID"',
    '"MUSICBRAINZ ALBUM ARTIST ID"',
    '"MUSICBRAINZ RELEASE GROUP ID"',
    '"MUSICBRAINZ RELEASE TRACK ID"',
    '"MUSICBRAINZ WORK ID"',
    '"MUSICBRAINZ ALBUMTYPE"',
    '"MUSICBRAINZ ALBUMSTATUS"',
    '"MUSICBRAINZ GENRES"',
    '"IS COVER"',
    '"ORIGINAL COVER ARTIST"',
    '"ORIG YEAR"',
)


@pytest.mark.parametrize("literal", _LEGACY_SPACE_FORM_DESCS)
def test_mp3_writer_has_no_hand_written_legacy_desc(literal: str) -> None:
    """A hand-written desc is how one field acquired two spellings."""
    body = _read(TAG_FILE_SERVICE)

    # The FILE may still mention a legacy spelling inside a comment explaining
    # the history; only live string literals matter, so strip comments first.
    code = re.sub(r"#.*", "", body)
    assert literal not in code, (
        f"{TAG_FILE_SERVICE.name} still writes the legacy desc {literal}. "
        "Use canonical_tag_name()/clear_id3_variants() from "
        "services.metadata.tag_names instead — hard-coded names are what "
        "re-split one field into two tags."
    )


def test_writer_tables_are_derived_from_the_registry() -> None:
    body = _read(TAG_FILE_SERVICE)
    assert "from services.metadata.tag_names import (" in body, (
        "tag_file_service must import the canonical registry"
    )
    assert "canonical_tag_name" in body
    assert "clear_id3_variants" in body
    assert "clear_vorbis_variants" in body


def test_mb_txxx_desc_map_matches_the_registry() -> None:
    """The fill-missing pre-check must look for the SAME names we write."""
    import services.metadata.tag_file_service as tfs

    for field, desc in tfs._MB_TXXX_DESC.items():
        assert desc == canonical_tag_name(field), (
            f"_MB_TXXX_DESC[{field!r}] is {desc!r}, but the canonical name is "
            f"{canonical_tag_name(field)!r} — the pre-check would miss the frame "
            "we just wrote."
        )


def test_every_canonical_name_is_upper_case_without_spaces() -> None:
    """The one format: UPPER_SNAKE. Spaces would recreate the old ambiguity."""
    for name in CANONICAL_TAG_NAMES.values():
        assert name == name.upper(), f"{name!r} is not upper-case"
        assert " " not in name, f"{name!r} contains a space — use an underscore"
        assert "-" not in name, f"{name!r} contains a hyphen"


def test_registry_is_the_only_place_that_names_musicbrainz_tags() -> None:
    """No other live module may hard-code a MusicBrainz tag NAME.

    Readers are allowed to hold ALIAS lists (they must understand old files);
    writers are not, because that is how the spellings diverged.  ``tag_names``
    itself holds the canonical + legacy tables.
    """
    writers = (
        "services/metadata/tag_file_service.py",
        "services/metadata/album_tag_sync_service.py",
    )
    pattern = re.compile(r'["\'](?:MUSICBRAINZ[ _][A-Z_ ]+|ORIGYEAR)["\']')
    offenders: list[str] = []
    for rel in writers:
        body = re.sub(r"#.*", "", _read(REPO_ROOT / rel))
        for match in pattern.findall(body):
            offenders.append(f"{rel}: {match}")
    assert not offenders, (
        "hard-coded MusicBrainz tag names found in a writer:\n  "
        + "\n  ".join(offenders)
        + "\nUse services.metadata.tag_names so one field keeps one name."
    )
