"""An album-sized release must not bind to a same-named EP release group.

REPORTED
--------
"The Unauthorized Biography of Reinhold Messner" by Ben Folds Five has 11
tracks but was marked EP. MusicBrainz really carries an Album and an EP under
the same title; the matcher picked the EP because its name score tied/beat the
album and the stored EP + release-group pair was then trusted forever.
"""
from __future__ import annotations

import pytest

import services.popularity.stages.album_stage as album_stage
from services.enrichment.musicbrainz_service import (
    _is_album_sized_track_count,
    _track_count_penalty,
)

ALBUM = "The Unauthorized Biography of Reinhold Messner"
ARTIST = "Ben Folds Five"


def _tracks(count: int, *, stored_type: str = "", stored_rg: str = ""):
    return [
        {
            "id": f"t{i}",
            "title": f"Song {i}",
            "musicbrainz_albumtype": stored_type,
            "musicbrainz_releasegroupid": stored_rg,
        }
        for i in range(1, count + 1)
    ]


def _candidate(rg: str, primary: str, score: float):
    return {
        "id": rg,
        "primary_type": primary,
        "match_score": score,
        "secondary_types": [],
        "first_release_date": "1999",
    }


class _FakeService:
    def __init__(self, matches):
        self.matches = matches

    def search_releasegroup_matches(self, *args, **kwargs):
        return list(self.matches)


def _patch_lookup(monkeypatch, matches):
    monkeypatch.setattr(album_stage, "get_shared_mb_service", lambda: _FakeService(matches))
    monkeypatch.setattr(
        album_stage,
        "_call_with_heartbeat",
        lambda section, func, *args, **kwargs: func(*args, **kwargs),
    )


class TestTheTrackCountPreference:
    def test_album_sized_is_above_six(self):
        assert _is_album_sized_track_count(7) is True
        assert _is_album_sized_track_count(6) is False
        assert _is_album_sized_track_count(None) is False

    def test_an_exact_count_is_free(self):
        assert _track_count_penalty([11], 11) == 0

    def test_a_higher_count_is_barely_penalised(self):
        assert _track_count_penalty([13], 11) == 0.04

    def test_a_lower_count_is_penalised_hard(self):
        assert _track_count_penalty([5], 11) == pytest.approx(0.6)

    def test_a_higher_release_beats_a_lower_one(self):
        """An expanded edition is the same album; a 5-track EP is not."""
        assert _track_count_penalty([5, 13], 11) < _track_count_penalty([5], 11)


class TestTheReleaseGroupSelection:
    def test_an_album_beats_a_better_scoring_ep(self, monkeypatch):
        _patch_lookup(monkeypatch, [
            _candidate("rg-ep", "EP", 0.99),
            _candidate("rg-album", "Album", 0.90),
        ])

        mb_type, rg = album_stage._lookup_musicbrainz_album_type(
            ARTIST, ALBUM, ARTIST, _tracks(11)
        )

        assert rg == "rg-album", (
            "an 11-track album must not bind to the same-named EP release group"
        )
        assert mb_type == "album"

    def test_a_stored_ep_is_re_resolved_for_an_album_sized_release(self, monkeypatch):
        _patch_lookup(monkeypatch, [
            _candidate("rg-ep", "EP", 0.99),
            _candidate("rg-album", "Album", 0.90),
        ])

        mb_type, rg = album_stage._lookup_musicbrainz_album_type(
            ARTIST,
            ALBUM,
            ARTIST,
            _tracks(11, stored_type="ep", stored_rg="rg-ep"),
        )

        assert (mb_type, rg) == ("album", "rg-album")

    def test_a_stored_ep_is_still_trusted_for_ep_sized_release(self, monkeypatch):
        """CONTROL — a 5-track local release may genuinely be the EP."""
        called = {"searched": False}

        def _must_not_search(*args, **kwargs):
            called["searched"] = True
            return []

        _patch_lookup(monkeypatch, [])
        monkeypatch.setattr(
            album_stage, "get_shared_mb_service", lambda: _FakeService([])
        )
        monkeypatch.setattr(
            album_stage,
            "_call_with_heartbeat",
            lambda section, func, *args, **kwargs: _must_not_search(),
        )

        mb_type, rg = album_stage._lookup_musicbrainz_album_type(
            ARTIST,
            ALBUM,
            ARTIST,
            _tracks(5, stored_type="ep", stored_rg="rg-ep"),
        )

        assert (mb_type, rg) == ("ep", "rg-ep")
        assert called["searched"] is False


class _Result:
    rowcount = 1

    def first(self):
        return None


class _RecordingSession:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPersistedEpIsHealed:
    def _run(self, monkeypatch, **kwargs):
        calls: list = []
        monkeypatch.setattr(
            album_stage, "db_session", lambda *a, **k: _RecordingSession(calls)
        )
        tracks = _tracks(11, stored_type="ep", stored_rg="rg-ep")
        album_stage._persist_album_type_to_tracks(
            ARTIST, ALBUM, tracks, "album", "rg-album", **kwargs
        )
        return calls, tracks

    def test_the_ep_type_and_release_group_are_replaced(self, monkeypatch):
        calls, tracks = self._run(
            monkeypatch, replace_secondary=True, replace_release_group=True
        )

        type_updates = [
            (sql, p) for sql, p in calls
            if "UPDATE tracks" in sql and "musicbrainz_albumtype" in sql
        ]
        rg_updates = [
            (sql, p) for sql, p in calls
            if "UPDATE tracks" in sql and "musicbrainz_releasegroupid" in sql
        ]
        assert type_updates and rg_updates

        assert type_updates[0][1]["replace_secondary"] is True
        assert "IN ('ep', 'single')" in type_updates[0][0]
        assert rg_updates[0][1]["replace_release_group"] is True

        assert tracks[0]["musicbrainz_albumtype"] == "album"
        assert tracks[0]["musicbrainz_releasegroupid"] == "rg-album"

    def test_without_the_override_nothing_is_rewritten(self, monkeypatch):
        calls, tracks = self._run(monkeypatch)

        type_updates = [
            (sql, p) for sql, p in calls
            if "UPDATE tracks" in sql and "musicbrainz_albumtype" in sql
        ]
        assert type_updates == [], "fill-only persistence must not replace an EP"
        rg_updates = [
            (sql, p) for sql, p in calls
            if "UPDATE tracks" in sql and "musicbrainz_releasegroupid" in sql
        ]
        if rg_updates:
            assert rg_updates[0][1]["replace_release_group"] is False
        assert tracks[0]["musicbrainz_albumtype"] == "ep"
        assert tracks[0]["musicbrainz_releasegroupid"] == "rg-ep"
