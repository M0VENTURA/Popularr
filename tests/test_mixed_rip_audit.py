"""The mixed-rip audit finds releases whose own files disagree.

A folder can be one album to Navidrome (default album PID: the MusicBrainz id
wins) and several otherwise, because the files disagree about ``releasedate`` —
the tag a customised Navidrome PID rule includes. Listing those releases is how
the folders that *will* split get found, instead of opening Get Info on each.

The audit is read-only and must not over-report: a release whose files agree on
every tag is not a mixed rip.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session, get_engine
from db.models import Track

MBID = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
CLEAN = "11111111-2222-3333-4444-555555555555"

_ROWS = [
    # The reported mixed rip: one MBID, two release dates, two titles.
    dict(id="t15", title="Here to Stay (T Ray's mix)", artist="Korn",
         album_artist="Korn", album="Here to Stay (T Ray's mix)",
         musicbrainz_albumid=MBID, releasedate="2002-06-19",
         originaldate="2002-06-10", media="Enhanced CD",
         popularity=50, final_score=50),
    dict(id="t01", title="Here to Stay", artist="Korn", album_artist="Korn",
         album="Here to Stay", musicbrainz_albumid=MBID, releasedate="",
         originaldate="2002-06-10", media="Digital Media",
         popularity=40, final_score=40),
    # A clean release: everything agrees.
    dict(id="c1", title="Clean A", artist="X", album_artist="X", album="Clean",
         musicbrainz_albumid=CLEAN, releasedate="2001-01-01", originaldate="",
         media="CD", popularity=10, final_score=10),
    dict(id="c2", title="Clean B", artist="X", album_artist="X", album="Clean",
         musicbrainz_albumid=CLEAN, releasedate="2001-01-01", originaldate="",
         media="CD", popularity=11, final_score=11),
]


@pytest.fixture
def library():
    from tests.conftest import _recreate_test_schema  # noqa: F401 - autouse covers it

    Track.__table__.create(get_engine(), checkfirst=True)
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tracks"))
        for row in _ROWS:
            conn.execute(
                text(f"INSERT INTO tracks ({', '.join(row)}) "
                     f"VALUES ({', '.join(':' + c for c in row)})"),
                row,
            )
    yield


class TestTheReportedCase:
    def test_it_finds_the_mixed_release(self, library) -> None:
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        found = find_mixed_rip_albums()
        ids = [r["musicbrainz_albumid"] for r in found]
        assert MBID in ids

    def test_it_reports_the_conflicting_tags(self, library) -> None:
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        entry = next(r for r in find_mixed_rip_albums() if r["musicbrainz_albumid"] == MBID)
        assert "releasedate" in entry["conflicts"], (
            "the date is the tag most likely to split the album in Navidrome"
        )
        assert "" in entry["conflicts"]["releasedate"], (
            "a missing date must be shown as a distinct value, not skipped"
        )
        assert "Here to Stay" in entry["albums"]
        assert "Here to Stay (T Ray's mix)" in entry["albums"]


class TestItDoesNotOverReport:
    def test_a_consistent_release_is_not_listed(self, library) -> None:
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        ids = [r["musicbrainz_albumid"] for r in find_mixed_rip_albums()]
        assert CLEAN not in ids, "agreeing files are not a mixed rip"

    def test_the_track_count_is_the_release_total(self, library) -> None:
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        entry = next(r for r in find_mixed_rip_albums() if r["musicbrainz_albumid"] == MBID)
        assert entry["tracks"] == 2

    def test_unscored_tracks_are_ignored(self, library) -> None:
        """The audit describes the music library, not an unscored import."""
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO tracks (id, title, album, album_artist, "
                "musicbrainz_albumid, releasedate) "
                "VALUES ('z1', 'Z', 'Clean', 'X', :m, '1999-01-01')"), {"m": CLEAN})
        entry_ids = [r["musicbrainz_albumid"] for r in find_mixed_rip_albums()]
        # Adding an UNSORED row with a new date must not make CLEAN a mixed rip.
        assert CLEAN not in entry_ids

    def test_the_result_is_ordered_most_conflicted_first(self, library) -> None:
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        counts = [len(r["conflicts"]) for r in find_mixed_rip_albums()]
        assert counts == sorted(counts, reverse=True)


class TestItIsReadOnly:
    def test_it_writes_nothing(self, library) -> None:
        from services.popularity.mixed_rip_audit import find_mixed_rip_albums

        with get_engine().begin() as conn:
            before = conn.execute(text("SELECT COUNT(*) FROM tracks")).scalar()
        find_mixed_rip_albums()
        with get_engine().begin() as conn:
            after = conn.execute(text("SELECT COUNT(*) FROM tracks")).scalar()
        assert before == after
