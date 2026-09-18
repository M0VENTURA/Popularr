"""Regression tests: one album must end up with ONE release year on every track.

The enrichment stage writes ``tracks.year`` and ``tracks.release_year`` PER
TRACK, and MusicBrainz resolves each recording to whichever release lists it
first.  For a multi-edition album ("Last Of Us" vs "Last Of Us (2018
Version)") different tracks of the SAME folder therefore resolved to
different years.

That split the album on screen, because the UI groups albums on (name, year):

    routes/ui_routes.py   album_key = f"{album.lower()}::{track_year}"
                          _leading_year() falls back to ``release_year``
    dashboard SQL         GROUP BY ..., COALESCE(year, release_year)

Guards:
- ``scan_stage_runner._resolve_album_authoritative_year`` produces ONE
  (original, edition) pair for the whole album.
- ``track_stage.process_track`` pins BOTH ``year`` and ``release_year`` on
  every track of the folder to that pair.
"""

from __future__ import annotations

import types

import pytest


def _default_single_result():
    return {
        "is_single": False,
        "confidence": "low",
        "confidence_score": 0.0,
        "single_status": "none",
        "sources": [],
        "reasons": [],
        "decision": {},
    }


ARTIST = "Arion"
ALBUM = "Last Of Us"


class TestResolveAlbumAuthoritativeYear:
    """The album-level year pair is resolved once, for the whole folder."""

    def _resolve(self, tracks, mb_batch=None):
        from services.popularity.scan_stage_runner import (
            _resolve_album_authoritative_year,
        )

        return _resolve_album_authoritative_year(tracks, mb_batch)

    def test_conflicting_track_years_collapse_to_earliest(self):
        # The reported defect: the SAME folder carrying two different years.
        # ``year`` is the album's ORIGINAL release, so the earliest wins and
        # both tracks agree.
        tracks = [
            {"title": "Last Of Us", "year": "2018"},
            {"title": "Seven", "year": "2020"},
        ]
        original, edition = self._resolve(tracks)
        assert original == 2018
        assert edition is None

    def test_conflicting_edition_years_take_the_majority(self):
        # ``release_year`` is the EDITION's year and belongs to the release,
        # not to the individual recording — the value most tracks carry is
        # the release's.
        tracks = [
            {"title": "A", "year": "2018", "release_year": 2018},
            {"title": "B", "year": "2018", "release_year": 2018},
            {"title": "C", "year": "2018", "release_year": 2020},
        ]
        original, edition = self._resolve(tracks)
        assert original == 2018
        assert edition == 2018

    def test_edition_year_tie_breaks_to_earliest(self):
        # Deterministic: a 2-2 tie must not depend on dict/row ordering.
        tracks = [
            {"title": "A", "year": "2018", "release_year": 2020},
            {"title": "B", "year": "2018", "release_year": 2018},
        ]
        _, edition = self._resolve(tracks)
        assert edition == 2018

    def test_unknown_years_return_none(self):
        # Nothing known -> the caller must leave the columns alone rather
        # than inventing a year.
        original, edition = self._resolve([{"title": "A"}, {"title": "B"}])
        assert original is None
        assert edition is None

    def test_full_dates_are_reduced_to_their_year(self):
        tracks = [{"title": "A", "year": "2018-04-20", "release_year": "2018-04-20"}]
        original, edition = self._resolve(tracks)
        assert original == 2018
        assert edition == 2018

    def test_stored_values_win_over_musicbrainz_batch(self):
        # A year already in the database may be a deliberate edit; the batch
        # is only a fallback.
        tracks = [{"title": "A", "year": "2018", "release_year": 2018}]
        original, edition = self._resolve(
            tracks,
            {"a::a": {"year": 1999, "release_year": 1999}},
        )
        assert original == 2018
        assert edition == 2018

    def test_musicbrainz_batch_fills_a_missing_original_year(self):
        tracks = [{"title": "A", "release_year": 2018}]
        original, edition = self._resolve(
            tracks,
            {
                "a::a": {
                    "original_release_year": 2011,
                    "version_release_year": 2018,
                }
            },
        )
        assert original == 2011
        assert edition == 2018


class TestProcessTrackUnifiesBothYearColumns:
    """Every track of a folder persists the same year AND release_year."""

    def _run(self, monkeypatch, *, tracks, current, album_context=None):
        import services.popularity.stages.track_stage as ts

        captured: dict = {}

        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_shared_mb_client", lambda: None)
        monkeypatch.setattr(
            ts, "detect_single_for_track", lambda **kw: _default_single_result()
        )
        monkeypatch.setattr(
            ts,
            "get_shared_mb_service",
            lambda: types.SimpleNamespace(
                get_composers_for_recording=lambda mbid: [],
                get_suggested_mbid=lambda *a, **k: ("", 0.0),
                lookup_recording_metadata=lambda *a, **k: {},
            ),
        )
        monkeypatch.setattr(
            ts,
            "insert_or_update_track",
            lambda track_id, effective_track: captured.update(effective_track),
        )

        ts.process_track(
            track=current,
            track_context={
                "artist": ARTIST,
                "album": ALBUM,
                "lastfm_title": current.get("title"),
            },
            album_context=album_context
            or {"album": ALBUM, "artist": ARTIST, "is_live_album": False},
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options={},
            album_tracks=tracks,
        )
        return captured

    def test_original_year_conflict_is_unified_across_the_album(self, monkeypatch):
        # The reported defect. Track B resolved to 2020 while its folder-mate
        # resolved to 2018; both must persist 2018 so the album is not split.
        tracks = [
            {"id": "a", "title": "Last Of Us", "artist": ARTIST, "album": ALBUM,
             "year": "2018", "release_year": 2018},
            {"id": "b", "title": "Seven", "artist": ARTIST, "album": ALBUM,
             "year": "2020", "release_year": 2020},
        ]
        captured = self._run(monkeypatch, tracks=tracks, current=dict(tracks[1]))
        assert str(captured.get("year"))[:4] == "2018"
        assert int(str(captured.get("release_year"))[:4]) == 2018

    def test_edition_year_is_unified_by_majority(self, monkeypatch):
        tracks = [
            {"id": "a", "title": "A", "artist": ARTIST, "album": ALBUM,
             "year": "2018", "release_year": 2018},
            {"id": "b", "title": "B", "artist": ARTIST, "album": ALBUM,
             "year": "2018", "release_year": 2018},
            {"id": "c", "title": "C", "artist": ARTIST, "album": ALBUM,
             "year": "2018", "release_year": 2020},
        ]
        captured = self._run(monkeypatch, tracks=tracks, current=dict(tracks[2]))
        assert str(captured.get("year"))[:4] == "2018"
        assert int(str(captured.get("release_year"))[:4]) == 2018

    def test_authoritative_album_year_wins_over_track_scan(self, monkeypatch):
        # The runner resolves the pair once per album and passes it through
        # album_context; it is authoritative over the per-track scan.
        tracks = [
            {"id": "a", "title": "A", "artist": ARTIST, "album": ALBUM,
             "year": "2020", "release_year": 2020},
        ]
        captured = self._run(
            monkeypatch,
            tracks=tracks,
            current=dict(tracks[0]),
            album_context={
                "album": ALBUM,
                "artist": ARTIST,
                "is_live_album": False,
                "authoritative_year": 2018,
                "authoritative_release_year": 2018,
            },
        )
        assert str(captured.get("year"))[:4] == "2018"
        assert int(str(captured.get("release_year"))[:4]) == 2018

    def test_unknown_years_are_not_invented(self, monkeypatch):
        # No year anywhere: the columns must stay empty, NOT be filled with
        # the album name or a fabricated value.
        tracks = [{"id": "a", "title": "A", "artist": ARTIST, "album": ALBUM}]
        captured = self._run(monkeypatch, tracks=tracks, current=dict(tracks[0]))
        assert captured.get("year") in (None, "")
        assert captured.get("release_year") in (None, "")

    def test_popularity_only_does_not_touch_the_album_year(self, monkeypatch):
        # A popularity-only pass writes no album identity, so it must not
        # re-date tracks either.
        import services.popularity.stages.track_stage as ts

        captured: dict = {}
        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_shared_mb_client", lambda: None)
        monkeypatch.setattr(
            ts, "detect_single_for_track", lambda **kw: _default_single_result()
        )
        monkeypatch.setattr(
            ts,
            "insert_or_update_track",
            lambda track_id, effective_track: captured.update(effective_track),
        )

        track = {
            "id": "a", "title": "A", "artist": ARTIST, "album": ALBUM,
            "year": "2020", "release_year": 2020,
        }
        ts.process_track(
            track=track,
            track_context={"artist": ARTIST, "album": ALBUM},
            album_context={"album": ALBUM, "artist": ARTIST},
            album_result={},
            options={"popularity_only": True},
            album_tracks=[track],
        )
        assert str(captured.get("year"))[:4] == "2020"
