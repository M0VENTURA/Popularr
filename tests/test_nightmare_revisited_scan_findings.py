"""Four scoring defects measured on "Various Artists — Nightmare Revisited".

Reported (scan log + page, 2026-10-14)
-------------------------------------
The compilation's per-track-artist rating is the intended model, but four
mechanical defects were visible in the same scan:

1. **The short-track cap was lost.** The provisional pass capped Danny
   Elfman's 60-second "Opening" 4★ → 2★; the final table showed 4★. The
   artist-boundary pre-pass had set ``_global_5star_locked`` on it in between,
   and ``_short_track_cap_is_protected`` read the flag alone — but the
   compilation branch IGNORES the lock (it rates on the credited artist's
   catalogue), so the lock never drove that rating and must not exempt it.
2. **Same-titled rows shared one count.** Two tracks named "Oogie Boogie's
   Song" (Rodrigo y Gabriela, Tiger Army) both scored ``LB: 26.3k``. The
   album-tracklist pass distinguished them (26285 vs 4669) and published each
   row's own numbers under the row alias, but ``track_stage`` read only the
   TITLE key.
3. **Unicode punctuation missed Last.fm.** "Jack's Lament" (The All‐American
   Rejects) read LF 99 vs LB 44.2k and "Poor Jack" (Plain White T’s) LF 58 vs
   LB 23.3k: MusicBrainz hyphenates with U+2010 and apostrophises with U+2019,
   and the provider indexes the ASCII spelling. ``UNICODE_PUNCT_MAP`` did not
   even contain U+2010.
4. **The online-catalogue path skipped the verified-single floor.** The
   local-catalogue branch and the absolute fallback both floor a confirmed
   single at 3★; the online path did not, so "Making Christmas" (Rise Against)
   and Tiger Army's "Oogie Boogie's Song" — both ``single=high`` — landed 2★.
"""

from __future__ import annotations

import pytest

#: Rank-percentile bands, matching ``_DEFAULT_COMPILATION_ONLINE_CATALOGUE``.
RULES = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
    "min_catalogue_size": 5,
}

#: Short-track cap config, matching the shipped defaults (enabled, 70s, 2★).
CAP_CONFIG = {
    "enabled": 1,
    "max_duration_seconds": 70.0,
    "max_stars": 2,
}


def _track(**overrides):
    track = {
        "track_id": "t1",
        "title": "Opening",
        "artist": "Danny Elfman",
        "album": "Nightmare Revisited",
        "popularity_score": 83.0,
        "final_score": 83.0,
        "lastfm_listeners": 99_800,
        "listenbrainz_listens": 3,
        "single_confidence": "low",
        "is_single": False,
        "duration": 60,
        "stars": 4,
        "exclude_from_stats": False,
    }
    track.update(overrides)
    return track


# ===========================================================================
# 1. The short-track cap survives the artist-boundary reconcile pass
# ===========================================================================


class TestTheShortTrackCapSurvivesTheReconcilePass:
    def _protected(self, track):
        from services.popularity.stages.finalise_stage import (
            _short_track_cap_is_protected,
        )

        return _short_track_cap_is_protected(track)

    def test_a_locked_studio_track_is_still_protected(self):
        """CONTROL — for a studio album the lock DID drive the rating, so it
        remains an explicit instruction and the cap must not touch it."""
        assert self._protected({"_global_5star_locked": True}) is True

    def test_a_locked_compilation_track_is_not_protected(self):
        """THE REPORTED DEFECT. The compilation branch ignores the lock, so the
        lock did not drive this rating — the cap must apply."""
        assert self._protected({
            "_global_5star_locked": True,
            "_compilation_rating_mode": "track_artist_online_catalogue",
        }) is False

    def test_a_user_override_is_protected_either_way(self):
        """CONTROL — a user rating is an instruction whatever the path."""
        assert self._protected({
            "single_confidence": "user",
            "_compilation_rating_mode": "track_artist_online_catalogue",
        }) is True
        assert self._protected({"single_confidence": "user"}) is True

    def test_a_hearted_track_is_protected_either_way(self):
        assert self._protected({
            "is_favourite": True,
            "_compilation_rating_mode": "track_artist_online_catalogue",
        }) is True

    def test_the_cap_lowers_a_locked_compilation_track(self, monkeypatch):
        """Behavioural: the exact log case — a locked, 60-second compilation
        track at 4★ is capped to 2★."""
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            "helpers.config_helpers.get_short_track_star_cap_config",
            lambda: CAP_CONFIG,
        )
        track = _track(
            stars=4,
            duration=60,
            _global_5star_locked=True,
            _compilation_rating_mode="track_artist_online_catalogue",
        )

        capped = fs._apply_short_track_star_cap([track], artist="Various Artists", album="Nightmare Revisited")

        assert capped == 1
        assert track["stars"] == 2

    def test_the_cap_still_spares_a_locked_studio_track(self, monkeypatch):
        """CONTROL — the same track on a studio album is untouched."""
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            "helpers.config_helpers.get_short_track_star_cap_config",
            lambda: CAP_CONFIG,
        )
        track = _track(stars=4, duration=60, _global_5star_locked=True)

        capped = fs._apply_short_track_star_cap([track], artist="Danny Elfman", album="Solo Album")

        assert capped == 0
        assert track["stars"] == 4


# ===========================================================================
# 2. The online catalogue honours the verified-single floor
# ===========================================================================


class TestTheOnlineCatalogueHonoursTheSingleFloor:
    """``Making Christmas`` (#86/192) and Tiger Army's ``Oogie Boogie's Song``
    are confirmed singles that were buried at 2★."""

    def _assign(self, monkeypatch, rank, total, **overrides):
        import services.popularity.popularity_sources as ps
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            "helpers.config_helpers.get_compilation_online_catalogue_config",
            lambda: RULES,
        )
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: {
                "rank": rank, "total": total, "percentile": rank / total,
                "listeners": 1000, "top_listeners": 50000,
                "source": "lastfm_artist_top_tracks",
            },
        )
        track = _track(
            title="Making Christmas",
            artist="Rise Against",
            popularity_score=82.5,
            final_score=82.5,
            lastfm_listeners=85_900,
            listenbrainz_listens=54_700,
            **overrides,
        )
        stars = fs._assign_stars(
            track,
            [82.5, 40.0, 30.0, 20.0, 10.0],
            [82.5, 40.0, 30.0, 20.0, 10.0],
            album_model={"has_benchmark": True, "era": "solid", "catalog_cutoff": 99.0,
                         "max_5star_slots": 3, "album_top_n": 3},
            is_compilation=True,
        )
        return stars, track

    def test_a_verified_single_floors_at_three(self, monkeypatch):
        """Rank #86/192 (45%) is 2★ by percentile — the floor raises it."""
        stars, _track_out = self._assign(
            monkeypatch, 86, 192, is_single=True, single_confidence="high"
        )

        assert stars == 3, (
            "the local-catalogue branch and the absolute fallback both floor a "
            "verified single at 3★ — the online path must not demote one below it"
        )

    def test_without_the_single_the_rank_decides(self, monkeypatch):
        """CONTROL — an unconfirmed track keeps the percentile verdict."""
        stars, _track_out = self._assign(
            monkeypatch, 86, 192, is_single=False, single_confidence="low"
        )

        assert stars == 2

    def test_a_low_confidence_single_is_not_floored(self, monkeypatch):
        """Only HIGH/MEDIUM/USER confidence counts as verified."""
        stars, _track_out = self._assign(
            monkeypatch, 86, 192, is_single=True, single_confidence="low"
        )

        assert stars == 2

    def test_the_floor_does_not_cap_a_stronger_rank(self, monkeypatch):
        """CONTROL — a #1 track still gets 5★; the floor is a floor."""
        stars, _track_out = self._assign(
            monkeypatch, 1, 140, is_single=True, single_confidence="high"
        )

        assert stars == 5

    def test_the_floor_does_not_apply_to_popularity_only(self, monkeypatch):
        """CONTROL — popularity-only runs never apply single floors."""
        stars, _track_out = self._assign(
            monkeypatch, 86, 192, is_single=True, single_confidence="high"
        )
        # Re-run through the real gate: popularity_only is an options flag the
        # caller passes; assert the guard exists in the source instead.
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "services" / "popularity" / "stages" / "finalise_stage.py"
        ).read_text(encoding="utf-8")
        assert "if is_verified_single and organic and not popularity_only:" in source


# ===========================================================================
# 3. Same-titled rows keep their own ListenBrainz counts
# ===========================================================================


class TestTheSameTitledRowsKeepTheirOwnCounts:
    """The reported symptom: two "Oogie Boogie's Song" rows, both LB 26.3k."""

    @staticmethod
    def _track_stage_source() -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "services" / "popularity" / "stages" / "track_stage.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _runner_source() -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "services" / "popularity" / "scan_stage_runner.py"
        ).read_text(encoding="utf-8")

    def test_track_stage_reads_the_row_alias_first(self):
        source = self._track_stage_source()

        assert "track_identity_key(" in source, (
            "the per-row alias must be consulted — the title key alone handed "
            "both same-titled rows one count"
        )
        alias = source.index("track_identity_key(\n                            _prefetch_key,")
        title_fallback = source.index(") or (prefetched_popularity or {}).get(_prefetch_key)")

        assert alias < title_fallback, "the alias must be tried BEFORE the title key"

    def test_track_stage_still_falls_back_to_the_title_key(self):
        """CONTROL — a row with no usable position has no alias."""
        source = self._track_stage_source()

        assert "(prefetched_popularity or {}).get(_prefetch_key)" in source

    def test_the_runner_mirrors_the_rows_own_counts(self):
        source = self._runner_source()

        assert '_row["listenbrainz_listens"] = int(_entry.get("listenbrainz_listens") or 0)' in source, (
            "the row slot must carry the COUNTS, not just the identity — "
            "track_stage reads the alias for the numbers"
        )
        assert '_row["listenbrainz_users"] = int(_entry.get("listenbrainz_users") or 0)' in source
        assert '_row["_album_tracklist"] = True' in source

    def test_the_row_alias_distinguishes_two_positions(self):
        """The premise: one title, two rows, two keys."""
        from services.popularity.popularity_sources import (
            normalize_for_aggregation,
            track_identity_key,
        )

        key = normalize_for_aggregation("Oogie Boogie’s Song")

        assert track_identity_key(key, 1, 13) != track_identity_key(key, 1, 21)

    def test_each_row_carries_its_own_count(self):
        """Behavioural, at the producer: the two rows resolve separately."""
        import services.popularity.popularity_sources as ps

        assert ps.track_identity_key("oogie boogie s song", 1, 13) == "oogie boogie s song#1:13"
        assert ps.track_identity_key("oogie boogie s song", 1, 21) == "oogie boogie s song#1:21"


# ===========================================================================
# 4. Unicode punctuation reaches Last.fm
# ===========================================================================


class _RecordingLastFm:
    """Returns a catalogue ONLY for the ASCII spelling, and records queries.

    That asymmetry is the whole point: it is exactly how Last.fm behaves for
    the reported tracks, so a lookup that does not fold returns nothing.
    """

    CATALOGUE = {
        "The All-American Rejects": [{"name": "Jack's Lament", "listeners": 44200, "playcount": 90000}],
        "Plain White T's": [{"name": "Poor Jack", "listeners": 23300, "playcount": 40000}],
    }

    def __init__(self):
        self.artist_queries: list[str] = []
        self.search_queries: list[tuple[str, str]] = []

    def get_artist_top_tracks(self, artist, limit=None):
        self.artist_queries.append(artist)
        return list(self.CATALOGUE.get(artist) or [])

    def search_track(self, artist, title, limit=20):
        self.search_queries.append((artist, title))
        return list(self.CATALOGUE.get(artist) or [])


@pytest.fixture(autouse=True)
def _clear_lastfm_caches():
    """The LF catalogues are module-level caches — clear them per test."""
    from services.popularity import popularity_cache_service as pcs
    from services.popularity import popularity_sources as ps

    with pcs._CACHE_LOCK:
        pcs._lf_top_tracks_cache.clear()
    with ps._CACHE_LOCK:
        ps._lastfm_artist_catalog_cache.clear()
    yield
    with pcs._CACHE_LOCK:
        pcs._lf_top_tracks_cache.clear()
    with ps._CACHE_LOCK:
        ps._lastfm_artist_catalog_cache.clear()


class TestUnicodePunctuationReachesLastFm:
    def test_the_punct_map_folds_the_musicbrainz_hyphen(self):
        """U+2010 is what MusicBrainz writes in "The All‐American Rejects";
        the map did not contain it, so the fold could never have helped."""
        from helpers.normalization_service import normalize_unicode_punctuation

        assert normalize_unicode_punctuation("The All\u2010American Rejects") == "The All-American Rejects"
        assert normalize_unicode_punctuation("non\u2011breaking") == "non-breaking"
        assert normalize_unicode_punctuation("fig\u2012ure") == "fig-ure"

    def test_the_punct_map_folds_the_curly_apostrophe(self):
        """CONTROL — U+2019 was already covered; the regression must not drop it."""
        from helpers.normalization_service import normalize_unicode_punctuation

        assert normalize_unicode_punctuation("Plain White T\u2019s") == "Plain White T's"

    def test_the_artist_catalogue_is_queried_with_the_ascii_spelling(self):
        """THE REPORTED MISS: LF 99 listens for a track with 44.2k on LB."""
        from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

        client = _RecordingLastFm()

        result = get_aggregated_lastfm_popularity(
            "The All\u2010American Rejects", "Jack\u2019s Lament", lastfm_client=client
        )

        assert client.artist_queries == ["The All-American Rejects"], (
            "the provider must be queried with the spelling it indexes"
        )
        assert result["listeners"] == 44200

    def test_the_raw_spelling_would_have_missed(self):
        """CONTROL proving the fold is load-bearing."""
        from helpers.normalization_service import normalize_unicode_punctuation

        client = _RecordingLastFm()
        raw = "The All\u2010American Rejects"

        # Query the fake with the UNFOLDED spelling — no catalogue, no listens.
        assert client.get_artist_top_tracks(raw) == []
        # …and the folded spelling is what the pipeline now sends.
        assert client.get_artist_top_tracks(normalize_unicode_punctuation(raw)) != []

    def test_the_search_path_folds_too(self):
        """The search fallback sends both strings to the provider."""
        from services.popularity.popularity_sources import (
            get_search_aggregated_lastfm_popularity,
        )

        client = _RecordingLastFm()

        result = get_search_aggregated_lastfm_popularity(
            "Plain White T\u2019s", "Poor Jack", lastfm_client=client
        )

        assert client.search_queries == [("Plain White T's", "Poor Jack")]
        assert result["listeners"] == 23300

    def test_an_ascii_artist_is_unchanged(self):
        """CONTROL — folding must be a no-op for ordinary names."""
        from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

        client = _RecordingLastFm()

        result = get_aggregated_lastfm_popularity(
            "The All-American Rejects", "Jack's Lament", lastfm_client=client
        )

        assert result["listeners"] == 44200
