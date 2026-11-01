"""Regression tests for two live-track adjustments.

1. **LB MBID resolution passes ``raw_title``** (which keeps the "(Live)"
   marker) so an alternate version like "Time Is Running Out (Live)" resolves
   to its OWN recording instead of matching the studio original — previously
   ``title`` (cleaned via ``lastfm_title``, brackets stripped) was passed and
   the live cut read ~0 ListenBrainz listens forever.

2. **"(Live)"/"(Acoustic)" title-suffixed tracks on studio albums are honoured
   as live** — the live weight penalty applies to their popularity score and
   the 4★ cap applies to their star rating, so a bonus live cut can never
   outrank the album's real singles.
"""

from __future__ import annotations


class TestLiveAlternateTrackTitleHelper:
    def _check(self):
        from services.catalog.album_classification_service import is_live_or_alternate_track_title
        return is_live_or_alternate_track_title

    def test_trailing_parenthetical_live_markers(self):
        for title in [
            "Time Is Running Out (Live)",
            "Time Is Running Out (Live In Tokyo 1994)",
            "Hurt (Acoustic)",
            "Hurt (Acoustic Version)",
            "Layla (Unplugged)",
            "Rhapsody (Orchestral)",
        ]:
            assert self._check()(title) is True, title

    def test_dash_separator_live_markers(self):
        for title in ["Song - Live", "Song - Acoustic", "Song – Unplugged"]:
            assert self._check()(title) is True, title

    def test_bare_live_word_is_not_a_version_marker(self):
        # A song literally titled "Live ..." must NOT be mis-flagged — the
        # same format-tag discipline as ``is_live_album_enhanced``.
        for title in [
            "Live Fast, Die Young",
            "Live In Colour",
            "Live and Let Die",
            "(how to live) as ghosts",
            "Song",
            "Song (Radio Edit)",
        ]:
            assert self._check()(title) is False, title


class TestLiveTitleCapsAtFourStars:
    """A "(Live)"/"(Acoustic)" title on a studio album caps at 4★."""

    def _assign(self, title, **overrides):
        from services.popularity.stages.finalise_stage import _assign_stars

        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": title,
            "popularity_score": 95.0, "final_score": 95.0,
            "lastfm_listeners": 5000, "listenbrainz_listens": 4000,
            "lb_percentile": 0.95, "lastfm_score": 8.0, "listenbrainz_score": 9.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return _assign_stars(
            track, [10.0, 20.0, 30.0, 40.0, 95.0], [10.0, 20.0, 30.0, 40.0, 95.0]
        )

    def test_high_confidence_single_live_title_capped_at_four(self):
        # A bonus "(Live)" cut must not outrank real singles even when it
        # carries a high-confidence single flag — the 4★ cap applies.
        assert self._assign("Song (Live)", is_single=True, single_confidence="high") <= 4

    def test_popularity_marked_acoustic_title_capped_at_four(self):
        assert self._assign("Song (Acoustic)", popularity_marked=True) <= 4

    def test_dash_live_title_capped_at_four(self):
        assert self._assign("Song - Live", is_single=True, single_confidence="high") <= 4

    def test_normal_high_confidence_single_still_five(self):
        assert self._assign("Song", is_single=True, single_confidence="high") == 5

    def test_bare_live_word_title_not_capped(self):
        # "Live Fast, Die Young" is a song title, not a live version → 5★.
        assert self._assign("Live Fast, Die Young", is_single=True, single_confidence="high") == 5

    def test_popularity_marked_plain_title_reaches_five(self):
        assert self._assign("Song", popularity_marked=True) == 5


class TestTrackStageLiveTitleWiring:
    """A "(Live)" title feeds ``is_live_track`` / the result ``is_live``.

    The scan's popularity pass must score a "(Live)" bonus cut with the live
    weight penalty (``is_live_track=True``) and surface ``is_live`` on the
    result so the 4★ cap applies downstream.
    """

    def _run(self, monkeypatch, track_title, lastfm_title):
        import services.popularity.stages.track_stage as ts

        seen = {}

        class FakeMB:
            def get_suggested_mbid(self, title, artist, limit=5):
                seen["suggested_title"] = title
                return "rec-live", 0.9

            def lookup_recording_metadata(self, title, artist):
                return {}

        class FakeLB:
            def get_recording_popularity(self, mbid):
                return {"total_listen_count": 1234, "total_user_count": 99}

        # ``track_stage`` calls the shared MB service/client (no per-track
        # ``MusicBrainzService`` construction anymore) — patch those module
        # bindings so no real API call happens.
        monkeypatch.setattr(ts, "get_shared_mb_service", lambda *a, **k: FakeMB())
        monkeypatch.setattr(ts, "get_shared_mb_client", lambda *a, **k: FakeMB())
        monkeypatch.setattr(ts, "ListenBrainzClient", FakeLB)
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(
            ts, "detect_single_for_track",
            lambda **kw: {"is_single": False, "confidence": "low",
                          "confidence_score": 0.0, "single_status": "none",
                          "sources": [], "reasons": []},
        )

        persisted: dict = {}

        def _persist(track_id, row):
            persisted.update(row)

        monkeypatch.setattr(ts, "insert_or_update_track", _persist)
        seen["persisted"] = persisted

        # Record the live flag passed into the scoring call.
        original_score = ts.calculate_combined_popularity_score

        def _scoring_probe(**kwargs):
            seen["is_live_track"] = bool(kwargs.get("is_live_track"))
            return original_score(**kwargs)

        monkeypatch.setattr(ts, "calculate_combined_popularity_score", _scoring_probe)

        track = {
            "id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": track_title,
            "final_score": 0.0,
            "lastfm_listeners": 0,
            "listenbrainz_listens": 0,
            "single_detection_last_updated": None,
        }
        album_context = {
            "album": "Absolution", "artist": "Muse",
            "tracks": [track], "is_live_album": False,
        }
        result = ts.process_track(
            track=track,
            track_context={
                "artist": "Muse", "album": "Absolution",
                "lastfm_title": lastfm_title,
            },
            album_context=album_context,
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options={},
        )
        return result, seen

    def test_live_title_scores_with_live_penalty_and_is_live(self, monkeypatch):
        result, seen = self._run(
            monkeypatch,
            track_title="Time Is Running Out (Live)",
            lastfm_title="Time Is Running Out",
        )
        # The MBID lookup must use the RAW title (keeping "(Live)"), not the
        # cleaned lastfm_title.
        assert seen["suggested_title"] == "Time Is Running Out (Live)"
        # The live cut is scored as a live track (weight penalty) and surfaced
        # as is_live so the star rating caps at 4★.
        assert seen["is_live_track"] is True
        assert result["is_live"] is True
        # ⭐ The verdict must reach the ROW, not only the returned dict: the
        # album stage was the only writer of `is_live`, so a live bonus cut on a
        # studio album stayed 0 and was scored/tagged as studio.
        assert seen["persisted"].get("is_live") == 1
        # The live recording's own counts flowed in.
        assert result["listenbrainz_listens"] == 1234

    def test_dash_live_title_is_scored_and_persisted_live(self, monkeypatch):
        """`About A Girl - Live` is the dash form the old bracket regex missed."""
        result, seen = self._run(
            monkeypatch,
            track_title="About A Girl - Live",
            lastfm_title="About A Girl",
        )
        assert seen["is_live_track"] is True
        assert result["is_live"] is True
        assert seen["persisted"].get("is_live") == 1

    def test_plain_title_scores_without_live_flag(self, monkeypatch):
        result, seen = self._run(
            monkeypatch,
            track_title="Time Is Running Out",
            lastfm_title="Time Is Running Out",
        )
        assert seen["suggested_title"] == "Time Is Running Out"
        assert seen["is_live_track"] is False
        assert result["is_live"] is False
        assert not seen["persisted"].get("is_live")
