"""Lookup MBID must measure against an edition that still has room to be missing.

REPORTED

> When doing a Lookup MBID on the album page, it's not correctly pulling in the
> missing tracks for albums that are sometimes double cd and also longer single
> cd.

Two answers pin it down: *the missing ones aren't shown* and *it picked the
wrong edition*.

TWO CAUSES, BOTH SELF-DEFEATING
-------------------------------

**1. The edition was chosen by proximity to what is already in the database.**

``get_musicbrainz_best_release`` scored every release in the group with::

    Value -= abs(local_track_count - release_track_count) * 100.0

``* 100`` outweighs every other signal combined (official +50, date +2.1,
title +30 ≈ 82), so **one track of difference flipped the choice** — and the
target was the count of rows we ALREADY HAVE. The edition closest to what we
own is the edition with nothing left to find:

* a **double-CD** whose disc 1 was imported first (12 rows) resolved to the
  12-track single-CD pressing → the whole second disc was invisible;
* a **longer single-CD** whose rows were still 13 resolved to the 13-track
  pressing → the extra tracks never appeared.

**2. A position counted as proof on its own.**

``get_missing_tracks`` excluded an MB track when *any* local row sat at the
same ``(disc, track)`` — regardless of title or length. A local rip whose own
numbering differs from MusicBrainz's (a double-CD imported disc by disc, a rip
numbered across the whole disc) therefore let an UNRELATED track claim a
genuinely-missing one as present.

The fix reuses ``_track_number_pairing_allowed`` — the ONE track-number rule
both queue matchers already use: a position is a tie-breaker, never proof.
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
from services.enrichment import musicbrainz_service as mbs  # noqa: E402


# ===========================================================================
# 1. Which EDITION the album is measured against
# ===========================================================================


def _rel(count: int, *, status: str = "Official", date: str = "2005-06-01",
         title: str = "Go Live") -> dict:
    return {"track_count": count, "status": status, "date": date, "title": title}


class TestTheMostCompleteEditionIsChosen:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(mbs, "_best_release_cache_get", lambda key: None)
        monkeypatch.setattr(mbs, "_best_release_cache_set", lambda key, val: None)

    def _best(self, monkeypatch, local: int, releases: list[dict]):
        monkeypatch.setattr(mbs, "_get_local_track_count", lambda a, b: local)
        monkeypatch.setattr(
            mbs, "_browse_group_releases", lambda rg, ctx: list(releases)
        )
        result = mbs.get_musicbrainz_best_release("Stray Kids", "Go Live", "rg-1")
        return result["best_release"]["track_count"], result

    def test_a_double_cd_whose_disc_one_is_imported_picks_the_double(
        self, monkeypatch
    ):
        """THE REPORT — 12 local rows, editions of 12 and 24."""
        count, _ = self._best(
            monkeypatch, 12, [_rel(12), _rel(24, date="2005-06-02")]
        )
        assert count == 24, (
            "the single-CD pressing was chosen because we only OWN 12 tracks, "
            "so the second disc could never be reported as missing"
        )

    def test_a_long_single_cd_beats_the_short_pressing(self, monkeypatch):
        count, _ = self._best(monkeypatch, 13, [_rel(13), _rel(16, date="2005-06-02")])
        assert count == 16, "the short pressing hid the extra tracks"

    def test_an_unknown_count_ranks_below_any_known_one(self, monkeypatch):
        count, _ = self._best(monkeypatch, 12, [_rel(0), _rel(12)])
        assert count == 12

    def test_equal_editions_still_break_ties_the_old_way(self, monkeypatch):
        """CONTROL — status/date/title still decide between equals."""
        count, result = self._best(
            monkeypatch,
            12,
            [_rel(12, status="Promotion", date="1990-01-01"),
             _rel(12, status="Official", date="2005-06-01")],
        )
        assert count == 12
        assert result["best_release"]["status"] == "Official"
        assert result["best_release"]["date"] == "2005-06-01"

    def test_a_single_edition_is_still_the_one(self, monkeypatch):
        """CONTROL — the ordinary case is unchanged."""
        count, result = self._best(monkeypatch, 12, [_rel(12)])
        assert count == 12
        assert result["confidence"] == 1.0

    def test_a_larger_edition_reports_low_confidence(self, monkeypatch):
        """The honest signal — and what makes the album page offer the picker."""
        _, result = self._best(
            monkeypatch, 12, [_rel(12), _rel(24, date="2005-06-02")]
        )
        assert result["confidence"] < 1.0

    def test_the_local_count_still_drives_confidence(self, monkeypatch):
        """CONTROL — ``local_track_count`` is reported, not used to choose."""
        _, result = self._best(monkeypatch, 12, [_rel(24), _rel(12, date="2005-06-02")])
        assert result["local_track_count"] == 12


# ===========================================================================
# 2. Which tracks the missing list reports
# ===========================================================================


@pytest.fixture(autouse=True)
def _wipe_tracks():
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
    yield
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))


@pytest.fixture
def harness(monkeypatch):
    """``get_missing_tracks`` with the DB-writing and queue-coverage halves off."""
    persisted: list[dict] = []

    monkeypatch.setattr(ams, "_queued_coverage", lambda a, b: ({}, {}))
    monkeypatch.setattr(
        ams, "_persist_missing_tracks",
        lambda a, b, missing: persisted.extend(missing),
    )
    monkeypatch.setattr(ams, "_rejected_missing_titles", lambda a, b: set())
    return persisted


def _seed(tid: str, title: str, track_number, *, disc: str = "1",
          duration=None, album: str = "Go Live", artist: str = "Stray Kids"):
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, title, track_number, disc_number, album, "
                "artist, album_artist, duration) "
                "VALUES (:id, :title, :tn, :dn, :album, :artist, :artist, :dur)"
            ),
            {"id": tid, "title": title, "tn": track_number, "dn": disc,
             "album": album, "artist": artist, "dur": duration},
        )
        session.commit()


def _mb(title: str, number, *, disc: int = 1, duration=None) -> dict:
    return {"title": title, "track_number": number, "disc_number": disc,
            "recording_mbid": f"rec-{number}", "duration": duration}


def _missing(monkeypatch, harness, mb_tracks, **kwargs):
    monkeypatch.setattr(
        ams, "fetch_musicbrainz_release_metadata",
        lambda rid, **k: {"tracks": mb_tracks},
    )
    result = ams.get_missing_tracks("Stray Kids", "Go Live", release_mbid="rel-1")
    return [m["title"] for m in result["missing_tracks"]], result


class TestAPositionNoLongerProvesPresence:
    def test_a_different_song_at_the_same_number_is_reported_missing(
        self, monkeypatch, harness
    ):
        """THE REPORT — the local numbering differs from MusicBrainz's."""
        _seed("t3", "Completely Different Song", "3", duration=200)
        missing, result = _missing(
            monkeypatch, harness,
            [_mb("Missing Song", 3, duration=240000)],
        )

        assert "Missing Song" in missing, (
            "the local track 3 claimed a different recording as present, so a "
            "genuinely missing track was never offered"
        )
        assert result["mb_total"] == 1

    def test_a_renamed_track_at_the_same_number_stays_present(
        self, monkeypatch, harness
    ):
        """CONTROL — a wording difference is not a missing track."""
        _seed("t3", "Rock and Roll Dreams Come Through", "3", duration=200)
        missing, _ = _missing(
            monkeypatch, harness,
            [_mb("Rock & Roll Dreams Come Through", 3, duration=200000)],
        )
        assert missing == []

    def test_an_exact_title_at_the_same_number_stays_present(
        self, monkeypatch, harness
    ):
        _seed("t3", "Levanter", "3", duration=200)
        missing, _ = _missing(monkeypatch, harness, [_mb("Levanter", 3, duration=200000)])
        assert missing == []

    def test_a_matching_title_with_no_position_is_still_present(
        self, monkeypatch, harness
    ):
        """CONTROL — the title path is untouched and needs no number."""
        _seed("t9", "Levanter", "9", duration=200)
        missing, _ = _missing(monkeypatch, harness, [_mb("Levanter", None, duration=200000)])
        assert missing == []

    def test_an_untitled_neighbour_still_gets_the_benefit_of_the_doubt(
        self, monkeypatch, harness
    ):
        """CONTROL — an untagged file must not be lost to a strict length rule.

        The shared pairing rule is deliberately permissive when a length is
        unknown: nothing contradicts the number, so the number stands.
        """
        _seed("t3", "Totally Different", "3", duration=None)
        missing, _ = _missing(
            monkeypatch, harness, [_mb("Another Song", 3, duration=None)]
        )
        assert missing == [], "an unknown length must stay permissive"

    def test_the_exclusion_count_still_adds_up(self, monkeypatch, harness):
        """The documented invariant: missing + excluded == mb_total."""
        _seed("t1", "One", "1", duration=100)
        _seed("t2", "Two", "2", duration=100)
        _, result = _missing(
            monkeypatch, harness,
            [_mb("One", 1, duration=100000), _mb("Not Here", 9, duration=100000)],
        )
        assert result["missing_count"] == 1
        assert result["mb_total"] == 2
        assert "Not Here" in [m["title"] for m in result["missing_tracks"]]
