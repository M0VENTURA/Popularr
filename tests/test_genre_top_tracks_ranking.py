"""Genre Top Tracks — the six rule adjustments, tested on the REAL builder.

The owner's spec for genre G:

1. Pool: tracks whose final genres include G; live/remix/acoustic/demo removed;
   duplicates merged keeping the original album version.
2. Include if 4★+ AND reach >= the GLOBAL 50th percentile of all tracks in G
   (not artist-relative).
3. Reach = Last.fm listeners, falling back to ListenBrainz listens.
4. Rank by reach, highest first, applying the artist cap WHILE WALKING DOWN so
   each artist's slots go to their most popular tracks.
5. Covers are held to the same threshold — no exemption.
6. Length capped, order preserved.

⚠️ TWO THINGS MAKE THESE TESTS HONEST.

* THE FLOOR IS ALWAYS IN PLAY. Rule 2 drops everything below the genre's median,
  so a fixture with three tracks keeps only two no matter what rule is under
  test. ``pad()`` therefore adds DROPPED tracks below the ones under test, which
  pushes the median down and leaves the survivors alone. A test that skips this
  is testing the floor, not the rule it names.
* THE RANKINGS MUST DISAGREE. The old order key led with
  ``artist_catalogue_percentile``, an ARTIST-relative question. Each test is
  built so the global and per-artist answers differ; one that passes under both
  proves nothing.
"""

from __future__ import annotations

import pytest

_REACHES = {"lastfm_listeners": 0, "listenbrainz_listens": 0}


def _row(track_id, title, artist, *, lastfm=0, lb=0, stars=5, genres="Rock",
         is_live=0, is_compilation=0):
    return {
        "id": track_id, "title": title, "artist": artist,
        "album_artist": artist, "album": f"{title} Album",
        "file_path": f"/music/{artist}/{title}.mp3", "duration": 200,
        "stars": stars, "final_score": 50.0, "popularity_score": 50.0,
        "is_live": is_live, "is_compilation": is_compilation,
        "lastfm_listeners": lastfm, "listenbrainz_listens": lb,
        "genres": genres, "year": "2000", "release_year": "2000",
    }


def pad(count, *, below):
    """Tracks that the 50th-percentile floor will DROP on purpose.

    Kept strictly below *below* so the genre median lands under the tracks a test
    cares about, leaving those untouched by rule 2.
    """
    return [
        _row(f"pad{i}", f"Pad {i}", f"Padding Act {i}", lastfm=below - (count - i))
        for i in range(count)
    ]


@pytest.fixture
def build(monkeypatch):
    """Run the REAL builder with a stubbed Navidrome, returning the written ids."""

    def _run(rows, *, max_per_artist=0, max_tracks=0, min_stars=4):
        import helpers.config_helpers as config_helpers
        from services.popularity.stages import finalise_stage as fs

        calls: dict[str, list[str]] = {}

        monkeypatch.setattr(
            fs, "_fetch_genre_playlist_rows", lambda min_stars, genres=None: rows
        )
        monkeypatch.setattr(fs, "_genre_playlists_enabled", lambda: True)
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
        monkeypatch.setattr(fs, "_genre_playlists_active", lambda: True)

        class _Client:
            def fetch_all_playlists(self):
                return []

        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [_Client()])

        def _sync(name, song_ids, rows=None, **_kw):
            calls[name] = list(song_ids)
            return {"updated": 1, "created": 0, "unchanged": 0, "failed": 0}

        monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", _sync)
        monkeypatch.setattr(config_helpers, "get_config", lambda: {
            "playlists": {
                "genre_playlists_create_threshold": 1,
                "genre_playlists_min_stars": min_stars,
                "genre_playlists_max_genres": 4,
                "genre_playlists_max_tracks": max_tracks,
                "genre_playlists_max_per_artist": max_per_artist,
                "playlist_interleave_artists": False,
            }
        })

        fs._create_genre_top_track_playlists()
        return calls.get("Rock - Top Tracks", [])

    return _run


class TestStep4RankingIsGlobal:
    def test_a_major_acts_biggest_song_beats_a_minor_acts_best(self, build):
        """⚠️ THE test that distinguishes global from artist-relative.

        Both are "the artist's #1" under the old percentile key, so that key
        could not tell them apart. Reach does, outright.
        """
        ids = build([
            _row("minor", "Small Song", "Nobody", lastfm=120),
            _row("major", "Huge Song", "Somebody", lastfm=9_000_000),
            *pad(2, below=120),
        ])
        assert ids[:2] == ["major", "minor"], (
            "a genre list must rank by cross-artist reach, not by each artist's "
            f"internal percentile: {ids}"
        )

    def test_it_is_reach_descending(self, build):
        ids = build([
            _row("low", "L", "X", lastfm=9000),
            _row("high", "H", "Y", lastfm=900_000),
            _row("mid", "M", "Z", lastfm=90_000),
            *pad(3, below=9000),
        ])
        assert ids == ["high", "mid", "low"], f"reach descending expected: {ids}"

    def test_lastfm_wins_and_listenbrainz_is_only_a_fallback(self, build):
        ids = build([
            # Last.fm present: it is used, and the huge LB value is ignored.
            _row("lastfm_wins", "A", "X", lastfm=10, lb=9_000_000),
            _row("lb_fallback", "B", "Y", lastfm=0, lb=5_000_000),
            # Reach 7 and 6 — positive, below the two tracks under test, and
            # strictly below the resulting median of 8.5.
            _row("pad0", "Pad 0", "P0", lastfm=7),
            _row("pad1", "Pad 1", "P1", lastfm=6),
        ])
        assert ids == ["lb_fallback", "lastfm_wins"], (
            "reach is Last.fm listeners, falling back to ListenBrainz only when "
            f"Last.fm is missing: {ids}"
        )


class TestStep4TheArtistCapIsAppliedWhileWalkingDown:
    def test_each_acts_slots_go_to_their_biggest_tracks(self, build):
        """One act with three tracks and a cap of 2 must keep its TOP TWO."""
        ids = build(
            [
                _row("a1", "A1", "Prolific", lastfm=9000),
                _row("a2", "A2", "Prolific", lastfm=8000),
                _row("a3", "A3", "Prolific", lastfm=7000),
                _row("b1", "B1", "Other", lastfm=6000),
                *pad(4, below=6000),
            ],
            max_per_artist=2,
        )
        assert set(ids) == {"a1", "a2", "b1"}, (
            "the cap must be applied to the RANKED list so the act keeps its "
            f"most popular tracks: {ids}"
        )
        assert "a3" not in ids

    def test_the_cap_does_not_reorder_the_survivors(self, build):
        ids = build(
            [
                _row("a1", "A1", "Prolific", lastfm=9000),
                _row("b1", "B1", "Other", lastfm=6000),
                _row("a2", "A2", "Prolific", lastfm=3000),
                *pad(3, below=3000),
            ],
            max_per_artist=2,
        )
        assert ids == ["a1", "b1", "a2"], f"ranking order must survive the cap: {ids}"


class TestStep2TheGlobalPercentileFloor:
    def test_a_track_below_the_genre_median_is_dropped(self, build):
        """Reaches 10, 100, 200, 800_000 → median 150, so only 10 is cut."""
        ids = build([
            _row("bottom", "A", "X", lastfm=10),
            _row("pad0", "Pad 0", "P0", lastfm=100),
            _row("pad1", "Pad 1", "P1", lastfm=200),
            _row("top1", "C", "Z", lastfm=800_000),
        ])
        assert "bottom" not in ids, (
            f"the global 50th-percentile floor must cut the bottom half: {ids}"
        )
        assert "top1" in ids

    def test_the_floor_is_global_not_per_artist(self, build):
        """⚠️ A weak act's BEST track is still dropped.

        Under a per-artist floor that track is its act's #1 and always survives.
        Under the global floor it is measured against the whole genre, which is
        what the spec asks for.
        """
        ids = build([
            _row("weak1", "W1", "Weak Act", lastfm=100),
            _row("weak2", "W2", "Weak Act", lastfm=200),
            _row("strong1", "S1", "Strong Act", lastfm=5_000_000),
            _row("strong2", "S2", "Strong Act", lastfm=6_000_000),
        ])
        assert not any(i.startswith("weak") for i in ids), (
            "a genre dominated by a strong act must drop the weak act entirely; "
            f"keeping it would mean the floor is artist-relative: {ids}"
        )

    def test_a_track_above_the_median_survives_whatever_its_artist(self, build):
        """The other half of the rule: a minor act is not excluded per se."""
        ids = build([
            _row("quiet1", "Q1", "Quiet", lastfm=10),
            _row("quiet2", "Q2", "Quiet", lastfm=20),
            _row("quiet3", "Q3", "Quiet", lastfm=30),
            _row("loud", "L", "Quiet", lastfm=90_000),
        ])
        assert "loud" in ids, (
            f"a minor act's big track belongs in the list: {ids}"
        )

    def test_no_reach_data_does_not_empty_the_playlist(self, build):
        """⚠️ A threshold of 0.0 would keep everything by accident; None skips."""
        ids = build([
            _row("a", "A", "X", lastfm=0, lb=0),
            _row("b", "B", "Y", lastfm=0, lb=0),
        ])
        assert set(ids) == {"a", "b"}, (
            "a genre with no listener data anywhere must not be gated into "
            f"silence: {ids}"
        )


class TestStep1VersionExclusion:
    def test_a_live_take_never_takes_a_slot(self, build):
        """Its reach is the HIGHEST of the two, so only eligibility can drop it."""
        ids = build([
            _row("studio", "Song", "X", lastfm=9000),
            _row("live", "Song (Live)", "X", lastfm=9_500_000, is_live=1),
        ])
        assert "live" not in ids, f"a live take must not pool at all: {ids}"

    def test_a_duplicate_keeps_the_non_compilation_copy(self, build):
        ids = build([
            _row("album", "Song", "X", lastfm=5000, is_compilation=0),
            _row("comp", "Song", "X", lastfm=5000, is_compilation=1),
        ])
        assert ids == ["album"], (
            f"the original album version must win the duplicate merge: {ids}"
        )


class TestStep5CoversGetNoExemption:
    def test_a_cover_below_the_floor_is_dropped(self, build):
        """⚠️ "drops The Thunder Rolls and Imagine" — held to the same threshold."""
        ids = build([
            _row("behind_blue_eyes", "Behind Blue Eyes", "Limp Bizkit", lastfm=800_000),
            _row("smooth_criminal", "Smooth Criminal", "Alien Ant Farm", lastfm=900_000),
            _row("thunder_rolls", "The Thunder Rolls", "Cover Act", lastfm=40),
            _row("imagine", "Imagine", "Cover Act", lastfm=60),
        ])
        assert set(ids) == {"behind_blue_eyes", "smooth_criminal"}, (
            f"a cover earns its place on reach like anything else: {ids}"
        )


class TestStep6Length:
    def test_the_cap_keeps_the_top_of_the_ranking(self, build):
        ids = build(
            [
                _row("a", "A", "X", lastfm=9000),
                _row("b", "B", "Y", lastfm=8000),
                _row("c", "C", "Z", lastfm=7000),
                *pad(3, below=7000),
            ],
            max_tracks=2,
        )
        assert ids == ["a", "b"], f"the cap must slice the top of the ranking: {ids}"


class TestTheRotationKeepsTheRanking:
    def test_a_weekly_rotation_never_moves_a_track_across_popularity_bands(self):
        """⚠️ Rule 4 says "rank by popularity"; a whole-list shuffle broke that.

        The rotation used to reorder the entire playlist after the slice, so the
        pushed order stopped being "most popular first" as soon as the week
        rolled over. It now bands on the reach VALUE, so only comparable tracks
        trade places.
        """
        from services.popularity.stages.finalise_stage import (
            _genre_shuffle_band, _weekly_shuffle,
        )

        rows = [
            _row("a", "A", "X", lastfm=9_400_000),
            _row("b", "B", "Y", lastfm=9_100_000),
            _row("c", "C", "Z", lastfm=900),
            _row("d", "D", "W", lastfm=800),
        ]
        rotated = _weekly_shuffle(
            rows, playlist_key="Rock - Top Tracks", scope="genre_top_tracks",
            enabled=True, tier_key=_genre_shuffle_band,
        )
        assert {r["id"] for r in rotated} == {"a", "b", "c", "d"}, "membership moved"
        # The two ~9e6 tracks may swap; the two ~9e2 tracks may swap; but a band
        # may never cross into the other.
        first_two = {r["id"] for r in rotated[:2]}
        last_two = {r["id"] for r in rotated[2:]}
        assert first_two == {"a", "b"}, f"a high-reach band moved down: {rotated}"
        assert last_two == {"c", "d"}, f"a low-reach band moved up: {rotated}"

    def test_the_band_is_the_value_rounded_to_one_significant_figure(self):
        from services.popularity.stages.finalise_stage import _genre_shuffle_band

        assert _genre_shuffle_band({"lastfm_listeners": 9_000_000}) == \
            _genre_shuffle_band({"lastfm_listeners": 9_400_000})
        assert _genre_shuffle_band({"lastfm_listeners": 9_000_000}) != \
            _genre_shuffle_band({"lastfm_listeners": 900_000})
        assert _genre_shuffle_band({"lastfm_listeners": 0}) == 0.0
