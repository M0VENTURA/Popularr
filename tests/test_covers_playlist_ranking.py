"""The reported Great Covers problems, as behaviour tests.

REPORTED
--------
* "the playlist's order has essentially no relationship to listener counts or
  final scores (correlation about 0 for both), so it looks shuffled"
* "only 50 of the 139 tracks are among the 139 most popular metal covers"
* "115 of the tracks are rated 5 stars, so tracks were picked mainly for being
  the top-rated song on their album"
* "Wrong version: Faith (edited) is used … prefer the unedited album version"
* false covers from an album-level "Cover" tag, and a remix

THE RULE (matching the genre Top Tracks builder)
------------------------------------------------
stars >= 4, rank by REACH, apply the artist cap while walking down the ranking.
A lower cap matters here because covers are dominated by a few acts — Metallica
alone has 26 of the 139 most popular metal covers.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs


def _row(track_id, title, artist, *, lastfm=0, lb=0, stars=5,
         is_compilation=0, original_cover_artist="", album=""):
    return {
        "id": track_id, "title": title, "artist": artist,
        "album_artist": artist, "album": album or f"{title} Album",
        "file_path": f"/music/{artist}/{title}.mp3", "duration": 200,
        "stars": stars, "final_score": 90.0, "popularity_score": 90.0,
        "is_live": 0, "is_compilation": is_compilation, "is_cover": 1,
        "lastfm_listeners": lastfm, "listenbrainz_listens": lb,
        "genres": "Heavy Metal",
        "original_cover_artist": original_cover_artist,
    }


@pytest.fixture
def build(monkeypatch):
    """Run the REAL covers builder with a stubbed Navidrome."""

    def _run(rows, *, max_per_artist=3, max_tracks=150):
        import helpers.config_helpers as config_helpers

        captured: dict[str, list[str]] = {}
        monkeypatch.setattr(fs, "_fetch_curated_playlist_rows",
                            lambda min_stars=4: rows)
        monkeypatch.setattr(fs, "_genre_playlists_enabled", lambda: True)
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [])

        def _sync(name, song_ids, rows=None, **_kw):
            captured[name] = list(song_ids)
            return {"updated": 1, "created": 0, "unchanged": 0, "failed": 0}

        monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", _sync)
        monkeypatch.setattr(config_helpers, "get_config", lambda: {
            "playlists": {
                "covers_playlist_enabled": True,
                "covers_playlist_name": "Great Covers",
                "covers_max_tracks": max_tracks,
                "covers_min_stars": 4,
                "genre_playlists_max_per_artist": max_per_artist,
                "playlist_interleave_artists": False,
            }
        })
        fs._create_covers_playlist(
            (config_helpers.get_config() or {}).get("playlists") or {}
        )
        return captured.get("Great Covers", [])

    return _run


class TestItRanksByReachNotByStars:
    def test_a_huge_four_star_cover_beats_a_small_five_star_one(self, build):
        """⚠️ THE REPORTED CASE. "Personal Jesus" has 489k listeners but is 4★.

        The old key sorted STARS first, so every 5★ cover outranked it — 115 of
        the 139 slots went to 5★ tracks purely because they were the top-rated
        song on their own album. Reach is the only cross-artist number.
        """
        ids = build([
            _row("big", "Personal Jesus (Depeche Mode Cover)", "Marilyn Manson",
                 lastfm=489_095, stars=4),
            _row("small", "Terrible Lie (Nine Inch Nails Cover)", "Static-X",
                 lastfm=119, stars=5),
        ])
        assert ids[0] == "big", (
            f"a 489k-listener cover must lead a 119-listener one: {ids}"
        )

    def test_it_is_reach_descending(self, build):
        ids = build([
            _row("a", "A", "X", lastfm=100),
            _row("b", "B", "Y", lastfm=900_000),
            _row("c", "C", "Z", lastfm=50_000),
        ])
        assert ids == ["b", "c", "a"], ids

    def test_listenbrainz_is_only_a_fallback(self, build):
        ids = build([
            _row("lf", "A", "X", lastfm=10, lb=9_000_000),
            _row("lb", "B", "Y", lastfm=0, lb=5_000_000),
            _row("pad", "C", "Z", lastfm=5),
        ])
        assert ids[:2] == ["lb", "lf"], ids


class TestTheArtistCapWalksDownTheRanking:
    def test_a_dominant_acts_slots_go_to_its_biggest_covers(self, build):
        """Metallica has 26 of the 139 most popular covers; a cap of 3 must keep
        its TOP THREE, not an arbitrary three."""
        ids = build(
            [
                _row("m1", "M1", "Metallica", lastfm=900_000),
                _row("m2", "M2", "Metallica", lastfm=800_000),
                _row("m3", "M3", "Metallica", lastfm=700_000),
                _row("m4", "M4", "Metallica", lastfm=600_000),
                _row("o1", "O1", "Other", lastfm=500_000),
            ],
            max_per_artist=3,
        )
        assert set(ids) == {"m1", "m2", "m3", "o1"}, ids
        assert "m4" not in ids

    def test_the_cap_runs_before_the_length_slice(self, build):
        """⚠️ ORDER MATTERS. Capping AFTER the slice would return fewer than
        ``max_tracks`` whenever the cap removed anything."""
        rows = [_row(f"m{i}", f"M{i}", "Metallica", lastfm=900_000 - i) for i in range(6)]
        rows += [_row(f"o{i}", f"O{i}", f"Other{i}", lastfm=100_000 - i) for i in range(6)]
        ids = build(rows, max_per_artist=2, max_tracks=8)
        assert len(ids) == 8, f"the list came up short: {ids}"
        assert sum(1 for i in ids if i.startswith("m")) == 2


class TestVersionPreference:
    def test_the_unedited_album_version_wins(self, build):
        """⚠️ REPORTED: "Faith (edited) is used. The original Faith has the same
        listener count, so prefer the unedited album version."

        Identical reach, so only an explicit preference can decide it.
        """
        ids = build([
            _row("edited", "Faith (edited) (George Michael Cover)", "Limp Bizkit",
                 lastfm=621_154, stars=5, album="Three Dollar Bill, Yall$"),
            _row("original", "Faith (George Michael Cover)", "Limp Bizkit",
                 lastfm=621_289, stars=4, album="Greatest Hitz"),
        ])
        assert ids == ["original"], (
            f"the unedited version must be preferred: {ids}"
        )

    def test_the_album_copy_beats_the_compilation_copy(self, build):
        ids = build([
            _row("comp", "Song", "X", lastfm=5000, is_compilation=1),
            _row("album", "Song", "X", lastfm=5000, is_compilation=0),
        ])
        assert ids == ["album"], ids

    def test_a_scanned_row_beats_an_unscanned_duplicate(self, build):
        """A duplicate row that was never scanned reports 0 listeners."""
        ids = build([
            _row("unscanned", "Song", "X", lastfm=0),
            _row("scanned", "Song", "X", lastfm=12_345),
        ])
        assert ids == ["scanned"], ids


class TestFalseCoversAreDropped:
    def test_a_remix_of_the_artists_own_song_is_not_a_cover(self, build):
        """⚠️ "Take a Look Around (Timbaland remix)" — a remix of YOUR OWN song
        is not a cover of someone else's."""
        ids = build([
            _row("remix", "Take a Look Around (Timbaland remix)", "Limp Bizkit",
                 lastfm=999_999),
            _row("real", "Smooth Criminal (Michael Jackson Cover)", "Alien Ant Farm",
                 lastfm=90_000, original_cover_artist="Michael Jackson"),
        ])
        assert ids == ["real"], ids

    def test_a_row_with_per_track_evidence_survives(self, build):
        """CONTROL — the guard must not empty a legitimate playlist."""
        ids = build([
            _row("a", "Song (The Who Cover)", "X", lastfm=9000),
            _row("b", "Other (Dio Cover)", "Y", lastfm=8000),
        ])
        assert set(ids) == {"a", "b"}, ids

    def test_the_guard_stands_down_when_no_row_has_evidence(self, build):
        """⚠️ FAIL OPEN. An older library may have no per-track provenance at
        all; dropping everything there would empty the playlist entirely."""
        ids = build([
            _row("a", "Song", "X", lastfm=9000),
            _row("b", "Other", "Y", lastfm=8000),
        ])
        assert set(ids) == {"a", "b"}, ids


class TestTheStarFloor:
    def test_it_defaults_to_four(self):
        assert fs._covers_min_stars({}) == 4

    def test_zero_disables_it(self):
        """⚠️ ``or 4`` would have turned 0 into 4 — the fallback-cap bug again."""
        assert fs._covers_min_stars({"covers_min_stars": 0}) == 0

    def test_a_bad_value_falls_back(self):
        assert fs._covers_min_stars({"covers_min_stars": "nonsense"}) == 4


class TestTheRotationKeepsTheRanking:
    def test_the_weekly_rotation_never_reorders_across_popularity_bands(self):
        """⚠️ THE OTHER HALF OF "it looks shuffled".

        The covers path called ``_weekly_shuffle`` with NO ``tier_key``, so it
        fell through to a WHOLE-LIST shuffle — the pushed order was unrelated to
        the ranking whatever the sort key said. It now rotates within a reach
        band, like the genre Top Tracks builder.
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
        out = _weekly_shuffle(
            rows, playlist_key="Great Covers", scope="genre_top_tracks",
            enabled=True, tier_key=_genre_shuffle_band,
        )
        assert {r["id"] for r in out[:2]} == {"a", "b"}, out
        assert {r["id"] for r in out[2:]} == {"c", "d"}, out
