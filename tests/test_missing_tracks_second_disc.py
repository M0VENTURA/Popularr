"""Missing tracks on a SECOND disc must actually populate.

REPORTED

> "Missing tracks on the second disk aren't populating"

Screenshot: a two-disc Various Artists compilation. Disc 2 rendered tracks
1-6 and 8-20, so disc 2 / track 7 (Trivium — "Like Light to the Flies") was
genuinely absent, yet the album page listed no missing tracks at all.

TWO CAUSES, both in ``services.metadata.album_missing_service``

**1. A recording owned on another album was treated as present on this one.**

``get_missing_tracks`` built a cross-album "already owned" set from
track-artist + title and then SKIPPED any MusicBrainz track found in it. The
Trivium track exists on Trivium's own album, so the disc-2 gap was excluded and
``missing_count`` came back 0. Owning a song elsewhere is not owning this
album's copy: ``missing + excluded == mb_total`` still held, so the row
vanished silently.

**2. A "n of m" disc tag crashed the whole computation.**

``disc_number`` is TEXT and stores values like ``"2/2"``. Every disc read was a
bare ``int(...)``, which raised ``ValueError`` on those rows. The exception
escaped ``get_missing_tracks``, so the album's missing list was never produced
at all — not for disc 2, not for anything. The album page already groups discs
with the tolerant ``leading_int`` (``"2/2"`` is disc 2), so the page showed a
"Disc 2" section that the missing check could not read.

WHAT IS PINNED HERE

* a gap on disc 2 of a multi-disc album is reported;
* the same recording owned on ANOTHER album is still missing HERE, annotated
  with ``owned_on_album`` rather than hidden;
* a track present on THIS album is still NOT missing (the position/title gates
  are untouched — the fix must not resurrect re-downloads);
* compound disc tags (``"2/2"``, ``"1/2"``, ``"02"``) are read as their leading
  number instead of raising.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import services.metadata.album_missing_service as ams  # noqa: E402
from db.engine import db_session  # noqa: E402
from db.models import Track  # noqa: E402

_ARTIST = "Various Artists"
_ALBUM = "2004 - MTV2 Handpicked Ball, Volume 2"

#: Disc 2 as it exists in the library: a hole at track 7.
_DISC2_PRESENT = [
    (1, "Medusa and Hemlock", "Cradle of Filth"),
    (2, "The Great Dividers", "Uneath"),
    (3, "God Forbid", "Anthero"),
    (4, "The Agony Scene", "We Bury Our Dead at Dawn"),
    (5, "Children of Bodom", "Needled 24/7"),
    (6, "Love Lost in a Hail of Gun Fire", "Bleeding Through"),
    (8, "Fuel for Hatred", "Satyricon"),
    (9, "Rain to the Sound of Panic", "Himsa"),
]
_GAP = (7, "Like Light to the Flies", "Trivium")


@pytest.fixture(autouse=True)
def _wipe_tracks():
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
    yield
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))


@pytest.fixture
def harness(monkeypatch):
    """``get_missing_tracks`` with persistence/queue halves stubbed."""
    persisted: list[dict] = []
    monkeypatch.setattr(ams, "_queued_coverage", lambda a, b, **k: ({}, {}, {}, {}))
    monkeypatch.setattr(
        ams, "_persist_missing_tracks",
        lambda a, b, missing: persisted.extend(missing),
    )
    monkeypatch.setattr(ams, "_rejected_missing_titles", lambda a, b: set())
    return persisted


def _seed(tid: str, title: str, track_number, *, disc: str, album: str,
          artist: str, album_artist: str = _ARTIST) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, title, track_number, disc_number, album, "
                "artist, album_artist, duration) "
                "VALUES (:id, :title, :tn, :dn, :album, :artist, :aa, 200)"
            ),
            {"id": tid, "title": title, "tn": track_number, "dn": disc,
             "album": album, "artist": artist, "aa": album_artist},
        )
        session.commit()


def _mb(title: str, number, *, disc: int, artist: str, duration: int = 200000) -> dict:
    return {
        "title": title, "track_number": number, "disc_number": disc,
        "recording_mbid": f"rec-{disc}-{number}", "duration": duration,
        "artist": artist,
    }


def _release(monkeypatch, tracks: list[dict]) -> None:
    monkeypatch.setattr(
        ams, "fetch_musicbrainz_release_metadata",
        lambda rid, **k: {"tracks": tracks, "release_year": "2004"},
    )


def _run() -> dict:
    return ams.get_missing_tracks(_ARTIST, _ALBUM, release_mbid="rel-1")


def _seed_disc2(*, disc: str = "2") -> None:
    for tn, title, artist in _DISC2_PRESENT:
        _seed(f"d2-{tn}", title, str(tn), disc=disc, album=_ALBUM, artist=artist)


class TestASecondDiscGapIsReported:
    def test_a_gap_on_disc_two_is_missing(self, monkeypatch, harness):
        """THE REPORT — disc 2 renders 1-6 and 8-20, so 7 must be reported."""
        _seed_disc2()
        _release(monkeypatch, [_mb(t, n, disc=2, artist=a)
                               for n, t, a in _DISC2_PRESENT]
                 + [_mb(_GAP[1], _GAP[0], disc=2, artist=_GAP[2], duration=181000)])

        result = _run()

        titles = [m["title"] for m in result["missing_tracks"]]
        assert _GAP[1] in titles, (
            "the disc-2 gap was swallowed; the second disc's missing tracks "
            "never populate"
        )
        assert result["missing_count"] == 1
        assert result["missing_tracks"][0]["disc_number"] == 2

    def test_the_arithmetic_is_still_self_checking(self, monkeypatch, harness):
        """missing + excluded == mb_total, or a gate is still silent."""
        _seed_disc2()
        tracks = [_mb(t, n, disc=2, artist=a) for n, t, a in _DISC2_PRESENT]
        tracks.append(_mb(_GAP[1], _GAP[0], disc=2, artist=_GAP[2]))
        _release(monkeypatch, tracks)

        result = _run()

        assert result["missing_count"] + sum(result["excluded"].values()) == result["mb_total"]


class TestARecordingOwnedElsewhereIsStillMissingHere:
    def test_cross_album_ownership_does_not_hide_the_gap(self, monkeypatch, harness):
        """THE REPORT — the same recording is owned on Trivium's own album."""
        _seed_disc2()
        _seed("triv-own", _GAP[1], "3", disc="1", album="Ascendancy",
              artist=_GAP[2], album_artist=_GAP[2])
        _release(monkeypatch, [_mb(t, n, disc=2, artist=a)
                               for n, t, a in _DISC2_PRESENT]
                 + [_mb(_GAP[1], _GAP[0], disc=2, artist=_GAP[2])])

        result = _run()

        titles = [m["title"] for m in result["missing_tracks"]]
        assert _GAP[1] in titles, (
            "owning the recording on another album made this album look "
            "complete, which is exactly what hid the disc-2 gap"
        )

    def test_the_owning_album_is_annotated(self, monkeypatch, harness):
        """The row stays, but it says where the copy already lives."""
        _seed_disc2()
        _seed("triv-own", _GAP[1], "3", disc="1", album="Ascendancy",
              artist=_GAP[2], album_artist=_GAP[2])
        _release(monkeypatch, [_mb(t, n, disc=2, artist=a)
                               for n, t, a in _DISC2_PRESENT]
                 + [_mb(_GAP[1], _GAP[0], disc=2, artist=_GAP[2])])

        result = _run()

        row = next(m for m in result["missing_tracks"] if m["title"] == _GAP[1])
        assert row.get("owned_on_album") == "Ascendancy"

    def test_a_track_present_on_this_album_is_still_not_missing(
        self, monkeypatch, harness
    ):
        """CONTROL — the fix must not resurrect re-downloads."""
        _seed_disc2()
        _release(monkeypatch, [_mb(t, n, disc=2, artist=a)
                               for n, t, a in _DISC2_PRESENT])

        result = _run()

        assert result["missing_tracks"] == []
        assert result["excluded"]["in_library"] == len(_DISC2_PRESENT)

    def test_a_disc_one_track_on_disc_one_is_unaffected(self, monkeypatch, harness):
        """CONTROL — the ordinary single-disc case is untouched."""
        _seed("d1-1", "Opener", "1", disc="1", album=_ALBUM, artist="Some Band")
        _release(monkeypatch, [_mb("Opener", 1, disc=1, artist="Some Band")])

        result = _run()

        assert result["missing_tracks"] == []


class TestCompoundDiscTagsAreReadable:
    @pytest.mark.parametrize("tag,expected", [
        ("2/2", 2), ("1/2", 1), ("02", 2), ("2", 2),
        ("", 1), (None, 1), ("0", 1), (" 3 ", 3),
    ])
    def test_disc_of_reads_the_leading_number(self, tag, expected):
        assert ams._disc_of(tag) == expected

    def test_a_compound_disc_tag_no_longer_raises(self, monkeypatch, harness):
        """THE CRASH — ``int("2/2")`` aborted the whole computation."""
        _seed_disc2(disc="2/2")
        _release(monkeypatch, [_mb(t, n, disc=2, artist=a)
                               for n, t, a in _DISC2_PRESENT]
                 + [_mb(_GAP[1], _GAP[0], disc=2, artist=_GAP[2])])

        result = _run()

        assert result["missing_count"] == 1, (
            "a '2/2' disc tag must be read as disc 2, not raise ValueError"
        )
        assert result["missing_tracks"][0]["title"] == _GAP[1]
