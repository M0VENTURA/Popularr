"""Last.fm ``track.search`` results must keep the per-match playcount.

REPORTED (popularity report review, AFI CSV)

    > Listeners Without Plays: 53 tracks have a lastfm_playcount of exactly 0
    > but show thousands of lastfm_listeners.

Root cause, verified in code: ``search_track`` mapped every match to
``name``/``artist``/``listeners``/``url`` and DROPPED ``playcount`` — the raw
``track.search`` API returns both per match. Every track resolved through the
search aggregation path (tracks outside the artist's top-N catalogue: live
renditions, bootlegs, feat. variants, demos) then stored listeners > 0 with
playcount 0 for ever, and the report column is a lie. The two numbers are one
payload on Last.fm — a listener with zero plays is not a state Last.fm can be
in, so losing one should never be silent.

The same hole existed in the ``get_track_info`` search fallback
(``_track_info_from_search``): when the follow-up ``track.getInfo`` came back
empty, listeners survived from the search winner while the playcount was
written 0. It now falls back to the search winner's playcount too.
"""

from __future__ import annotations

import pytest

from services.enrichment.lastfm_service import LastFmService


class _FakeLastFm:
    """Minimal client: only what the aggregation path touches."""

    def __init__(self, matches: list[dict]):
        self.matches = matches

    def search_track(self, artist, title, limit=10):
        return list(self.matches)


class _FakeHttp:
    """Returns one canned JSON payload for every request."""

    def __init__(self, payload: dict):
        self.payload = payload

    def get_json(self, method: str, **params):
        return self.payload


# ---------------------------------------------------------------------------
# search_track: the mapping must keep the playcount
# ---------------------------------------------------------------------------
class TestSearchTrackKeepsThePlaycount:
    def test_the_mapping_carries_both_numbers(self):
        svc = LastFmService.__new__(LastFmService)
        svc.api_key = "test-key"
        svc.http = _FakeHttp(
            {
                "results": {
                    "trackmatches": {
                        "track": [
                            {
                                "name": "Miss Murder",
                                "artist": "AFI",
                                "listeners": "1099696",
                                "playcount": "9687043",
                                "url": "https://www.last.fm/music/AFI/_/Miss+Murder",
                            }
                        ]
                    }
                }
            }
        )

        out = svc.search_track("AFI", "Miss Murder", limit=10)

        assert out and out[0]["listeners"] == 1099696
        assert out[0]["playcount"] == 9687043, (
            "the playcount was dropped by the mapping — every search-resolved "
            "track then stored listeners>0 with playcount 0"
        )

    def test_a_match_without_playcount_still_maps_to_zero(self):
        svc = LastFmService.__new__(LastFmService)
        svc.api_key = "test-key"
        svc.http = _FakeHttp(
            {
                "results": {
                    "trackmatches": {
                        "track": [{"name": "X", "artist": "AFI", "listeners": "5"}]
                    }
                }
            }
        )

        out = svc.search_track("AFI", "X", limit=10)

        assert out[0]["playcount"] == 0


# ---------------------------------------------------------------------------
# The aggregation path must sum what search_track now carries
# ---------------------------------------------------------------------------
class TestSearchAggregationSumsThePlaycount:
    def test_listeners_and_plays_move_together(self):
        from services.popularity.popularity_sources import (
            get_search_aggregated_lastfm_popularity,
        )

        fake = _FakeLastFm(
            [
                {
                    "name": "Totalimmortal (Live)",
                    "artist": "AFI",
                    "listeners": 4660,
                    "playcount": 51230,
                    "url": "https://www.last.fm/music/AFI/_/Totalimmortal+(Live)",
                }
            ]
        )

        agg = get_search_aggregated_lastfm_popularity(
            "AFI",
            "Totalimmortal (Live)",
            lastfm_client=fake,
            is_live_release=True,
        )

        assert agg.get("listeners") == 4660
        assert agg.get("track_play") == 51230, (
            "the summed playcount stayed 0 — the report column would still lie"
        )

    def test_multiple_variants_are_summed_together(self):
        from services.popularity.popularity_sources import (
            get_search_aggregated_lastfm_popularity,
        )

        fake = _FakeLastFm(
            [
                {
                    "name": "100 Words",
                    "artist": "AFI",
                    "listeners": 28585,
                    "playcount": 201000,
                    "url": "u1",
                },
                {
                    "name": "100 Words (from Sing the Sorrow sessions 2003)",
                    "artist": "AFI",
                    "listeners": 19947,
                    "playcount": 130000,
                    "url": "u2",
                },
            ]
        )

        agg = get_search_aggregated_lastfm_popularity(
            "AFI", "100 Words", lastfm_client=fake
        )

        assert agg.get("listeners") == 28585 + 19947
        assert agg.get("track_play") == 201000 + 130000


# ---------------------------------------------------------------------------
# get_track_info search fallback: playcount survives an empty getInfo
# ---------------------------------------------------------------------------
class TestTrackInfoSearchFallbackKeepsThePlaycount:
    def test_an_empty_getinfo_keeps_the_search_playcount(self):
        svc = LastFmService.__new__(LastFmService)
        svc.api_key = "test-key"
        svc.search_track = lambda artist, title, limit=20: [
            {
                "name": "Born in the USA",
                "artist": "AFI",
                "listeners": 6250,
                "playcount": 41000,
                "url": "u",
            }
        ]
        svc._get_track_info_once = lambda artist, title, track_mbid=None: {
            "track_play": 0,
            "listeners": 0,
            "toptags": {},
            "lookup_artist": artist,
            "returned_artist": "",
            "track_name": title,
            "url": "",
            "album": "",
        }

        info = svc._track_info_from_search("AFI", "Born in the USA")

        assert info is not None
        assert info["listeners"] == 6250
        assert info["track_play"] == 41000, (
            "getInfo came back empty and the fallback wrote playcount 0 — the "
            "same impossible listeners-with-no-plays pair as the search path"
        )

    def test_a_real_getinfo_playcount_still_wins(self):
        svc = LastFmService.__new__(LastFmService)
        svc.api_key = "test-key"
        svc.search_track = lambda artist, title, limit=20: [
            {
                "name": "Miss Murder",
                "artist": "AFI",
                "listeners": 1099696,
                "playcount": 9687043,
                "url": "u",
            }
        ]
        svc._get_track_info_once = lambda artist, title, track_mbid=None: {
            "track_play": 9999999,
            "listeners": 1200000,
            "toptags": {},
            "lookup_artist": artist,
            "returned_artist": "AFI",
            "track_name": "Miss Murder",
            "url": "",
            "album": "",
        }

        info = svc._track_info_from_search("AFI", "Miss Murder")

        assert info["track_play"] == 9999999, (
            "track.getInfo is the authority; the search fallback must not "
            "override a real answer"
        )
        assert info["listeners"] == 1200000