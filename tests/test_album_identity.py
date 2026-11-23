"""Album identity: a release's MusicBrainz ID outranks its title.

⭐ THE REPORTED BUG

Two files of one Korn release —

    "Here to Stay"                 releasedate 2002-06-19
    "Here to Stay (T Ray's mix)"  releasedate (missing)

— carry the SAME ``musicbrainz_albumid``. Navidrome's default album rule prefers
that id, so they are ONE album there. Popularr grouped albums by
``(album_artist, album)``, so they became TWO here: the re-release was a
one-track album, and a one-track album has no distribution to curve a
star rating across, so its album-relative rating meant nothing.

The rule pinned below mirrors Navidrome's precedence: use the MBID when it
exists, and fall back to the name tuple ONLY when it does not.
"""

from __future__ import annotations

import pytest

from services.popularity.album_identity import (
    NO_MBID,
    album_identity,
    album_mbid,
    group_tracks_by_album,
)

KORN = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
REMASTER = "11111111-2222-3333-4444-555555555555"


def _row(**kw):
    return kw


class TestTheReportedCase:
    def test_two_titles_with_one_mbid_are_one_album(self) -> None:
        a = _row(album="Here to Stay (T Ray's mix)", album_artist="Korn",
                 musicbrainz_albumid=KORN)
        b = _row(album="Here to Stay", album_artist="Korn", musicbrainz_albumid=KORN)
        assert album_identity(a) == album_identity(b)

    def test_they_group_together(self) -> None:
        rows = [
            _row(album="Here to Stay (T Ray's mix)", album_artist="Korn",
                 musicbrainz_albumid=KORN),
            _row(album="Here to Stay", album_artist="Korn", musicbrainz_albumid=KORN),
            _row(album="Here to Stay", album_artist="Korn", musicbrainz_albumid=REMASTER),
        ]
        groups = group_tracks_by_album(rows)
        assert len(groups) == 2, "one album per MBID; the name must not split it"

    def test_a_missing_releasedate_is_irrelevant(self) -> None:
        """The tag that actually split the folder in Navidrome must not matter here."""
        a = _row(album="A", album_artist="X", musicbrainz_albumid=KORN,
                 releasedate="2002-06-19")
        b = _row(album="A", album_artist="X", musicbrainz_albumid=KORN, releasedate="")
        assert album_identity(a) == album_identity(b)


class TestTheMbidWinsOnlyWhenPresent:
    def test_different_mbids_are_different_albums_even_with_one_title(self) -> None:
        """A remaster shares a title but is not the same release."""
        a = _row(album="Here to Stay", album_artist="Korn", musicbrainz_albumid=KORN)
        b = _row(album="Here to Stay", album_artist="Korn", musicbrainz_albumid=REMASTER)
        assert album_identity(a) != album_identity(b)

    def test_no_mbid_falls_back_to_name(self) -> None:
        a = _row(album="Here to Stay", album_artist="Korn")
        b = _row(album="here to stay", album_artist="KORN")
        assert album_identity(a) == album_identity(b), "case must not split a name key"

    def test_mbid_and_no_mbid_are_never_merged(self) -> None:
        """Cannot prove an untagged row belongs to a tagged release."""
        tagged = _row(album="Here to Stay", album_artist="Korn", musicbrainz_albumid=KORN)
        untagged = _row(album="Here to Stay", album_artist="Korn")
        assert album_identity(tagged) != album_identity(untagged)

    def test_the_rule_that_produced_a_key_is_recorded(self) -> None:
        """So a caller can never compare an MBID key with a name key by accident."""
        assert album_identity(_row(musicbrainz_albumid=KORN))[0] == "mbid"
        assert album_identity(_row(album="A", album_artist="B"))[0] == "name"


class TestBothSpellingsAndPlaceholders:
    @pytest.mark.parametrize("key", ["musicbrainz_albumid", "musicbrainz_album_mbid"])
    def test_either_column_works(self, key: str) -> None:
        assert album_identity({key: KORN, "album": "A", "album_artist": "B"}) == ("mbid", KORN)

    def test_the_id_is_lowercased(self) -> None:
        assert album_mbid({"musicbrainz_albumid": KORN.upper()}) == KORN

    @pytest.mark.parametrize("placeholder", ["[no id]", "none", "NULL", "0", "-", "  "])
    def test_tag_writer_placeholders_are_absent(self, placeholder: str) -> None:
        """Otherwise two untagged rips would "share" an id and be merged."""
        assert album_mbid({"musicbrainz_albumid": placeholder}) == NO_MBID

    def test_the_tag_column_wins_over_the_proposal_column(self) -> None:
        assert album_mbid(
            {"musicbrainz_albumid": "bbb", "musicbrainz_album_mbid": "aaa"}
        ) == "bbb"


def test_grouping_preserves_first_seen_order() -> None:
    rows = [
        _row(album="B", album_artist="X", musicbrainz_albumid="b" * 8),
        _row(album="A", album_artist="X", musicbrainz_albumid="a" * 8),
        _row(album="B2", album_artist="X", musicbrainz_albumid="b" * 8),
    ]
    groups = group_tracks_by_album(rows)
    assert list(groups) == [("mbid", "b" * 8), ("mbid", "a" * 8)]
    assert [r["album"] for r in groups[("mbid", "b" * 8)]] == ["B", "B2"]
