"""One album, one release id — on disk.

Reported as: "Something is still updating Navidrome files and splitting them
after they are correctly merged".

The user's tag dump showed exactly that. Two files of ONE album (Korn /
Untouchables), sharing one release-group id, carrying DIFFERENT release ids:

    …/01 - Korn - Here to Stay.mp3            musicbrainz_albumid: 3c8d8b38-…
    …/15. Korn - Here to Stay (T Ray's mix).mp3   musicbrainz_albumid: 2606ffe5-…

Navidrome's default album PID prefers``musicbrainz_albumid``, so that pair IS a
split album however correct the database is.

The album file-tag fill read ``track["musicbrainz_albumid"]`` PER ROW and that
field is written with force semantics (``_FILE_IDENTITY_TAGS``), so a single row
still holding a different release id stamped it back onto its own file — undoing
the merge on the next Navidrome scan. The id is an ALBUM-level fact.
"""

from __future__ import annotations

import pytest

from services.metadata.album_tag_sync_service import (
    _db_tag_candidates,
    _dominant_album_id,
)

MERGED = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
STRAY = "2606ffe5-3d2d-4549-8fa3-14e3ed6b1cfd"
RGID = "4041334a-fdca-3a73-878e-8513b464a88d"


def _row(i: int, mid: str) -> dict:
    return {
        "id": str(i),
        "title": f"Track {i}",
        "artist": "Korn",
        "album_artist": "Korn",
        "album": "Untouchables",
        "musicbrainz_albumid": mid,
        "musicbrainz_album_mbid": mid,
        "musicbrainz_releasegroupid": RGID,
        "track_number": str(i),
        "disc_number": "1",
    }


def _stamp(rows: list[dict]) -> None:
    """What the album pass now does before writing files."""
    chosen = _dominant_album_id(rows)
    if chosen:
        for r in rows:
            r["musicbrainz_albumid"] = chosen
            r["musicbrainz_album_mbid"] = chosen


def _written_ids(rows: list[dict]) -> set[str]:
    return {
        _db_tag_candidates(r, album_year="2002", perfect=True).get(
            "musicbrainz_albumid", "<absent>"
        )
        for r in rows
    }


class TestTheAlbumIdIsChosenOnce:
    """The representative id must be one of the ids actually present."""

    @pytest.mark.parametrize(
        "ids,expected",
        [
            ([MERGED] * 13 + [STRAY], MERGED),         # the reported shape
            ([MERGED] + [STRAY] * 13, STRAY),
            ([MERGED, MERGED, STRAY, STRAY], MERGED),   # even split
            ([MERGED] * 4, MERGED),                     # single id
            ([""] * 3, ""),                             # no ids at all
        ],
    )
    def test_majority_wins(self, ids, expected) -> None:
        rows = [_row(i, m) for i, m in enumerate(ids)]
        assert _dominant_album_id(rows) == expected

    def test_it_is_stable_across_row_order(self) -> None:
        rows = [_row(i, m) for i, m in enumerate([MERGED, STRAY, MERGED])]
        assert _dominant_album_id(rows) == _dominant_album_id(rows[::-1])


class TestEveryFileGetsTheSameId:
    """THE REGRESSION — two distinct ids on disk is a split album."""

    def test_the_reported_album_writes_one_id(self) -> None:
        rows = [_row(i, m) for i, m in enumerate([MERGED] * 13 + [STRAY])]

        # Without the convergence this is the split: 2 distinct values.
        assert _written_ids(rows) == {MERGED, STRAY}, (
            "fixture check: the raw rows must disagree for this test to mean anything"
        )

        _stamp(rows)
        assert _written_ids(rows) == {MERGED}

    @pytest.mark.parametrize("perfect", [True, False])
    def test_one_id_regardless_of_the_perfect_gate(self, perfect: bool) -> None:
        """The identity tags are only filled on a perfect match; when they ARE
        filled, every file must still receive the album's single id."""
        rows = [_row(i, m) for i, m in enumerate([MERGED] * 3 + [STRAY])]
        _stamp(rows)
        written = {
            _db_tag_candidates(r, album_year="2002", perfect=perfect).get(
                "musicbrainz_albumid", "<absent>"
            )
            for r in rows
        }
        assert len(written) == 1

    def test_a_single_id_album_is_untouched(self) -> None:
        rows = [_row(i, MERGED) for i in range(4)]
        before = [dict(r) for r in rows]
        _stamp(rows)
        assert rows == before
