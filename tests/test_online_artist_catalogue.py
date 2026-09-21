"""The online artist-catalogue rank lookup.

``get_online_artist_track_rank`` answers "where does this song sit in this
artist's real-world discography?" — the question the LOCAL database cannot
answer for an artist the library only owns one or two songs by.

It is the data source for the compilation rating fix, so the contracts pinned
here matter:
- ``rank`` is 1-based (1 = the artist's most popular song worldwide)
- ``percentile`` is ``rank / total``
- the catalogue is sorted by listeners DESC, so position IS popularity
- a missing artist / empty input returns zeros meaning "unknown"
- an uncharted title on a real artist is reported BEYOND the list, so it
  scores at the bottom rather than being skipped
"""

from __future__ import annotations

import pytest

from services.popularity import popularity_sources as ps

#: The rating bands, matching ``_DEFAULT_COMPILATION_ONLINE_CATALOGUE``.
ONLINE_RULES = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "min_catalogue_size": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """The catalogue cache is process-wide; clear it around every test."""
    ps._ONLINE_CATALOGUE_CACHE.clear()
    yield
    ps._ONLINE_CATALOGUE_CACHE.clear()


class _FakeLastFm:
    """Stands in for LastFmClient, exposing only what the lookup uses."""

    def __init__(self, top_tracks):
        self._top_tracks = top_tracks
        self.calls = 0

    def get_artist_top_tracks(self, artist, limit=100):
        self.calls += 1
        return self._top_tracks


def _stub_cache(monkeypatch, mapping):
    """Patch ``get_artist_top_tracks_map`` with a normalized-title map."""
    import services.popularity.popularity_cache_service as pcs

    def _fake(_client, _artist):
        return {k: v for k, v in mapping.items()}

    monkeypatch.setattr(pcs, "get_artist_top_tracks_map", _fake)


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------


class TestTheCatalogueIsSortedByPopularity:
    def test_entries_are_sorted_by_listeners_descending(self, monkeypatch):
        """Position in the returned list IS the artist's popularity ranking."""
        _stub_cache(monkeypatch, {
            "minor song": {"lastfm_listeners": 100},
            "huge song": {"lastfm_listeners": 90000},
            "mid song": {"lastfm_listeners": 5000},
        })

        catalogue = ps.get_online_artist_catalogue("Insolence", _FakeLastFm([]))

        assert [title for title, _ in catalogue] == ["huge song", "mid song", "minor song"]
        assert [listeners for _, listeners in catalogue] == [90000, 5000, 100]

    def test_zero_listener_entries_are_dropped(self, monkeypatch):
        """An entry with no listeners carries no rank information."""
        _stub_cache(monkeypatch, {
            "real song": {"lastfm_listeners": 500},
            "empty song": {"lastfm_listeners": 0},
        })

        catalogue = ps.get_online_artist_catalogue("A", _FakeLastFm([]))

        assert [t for t, _ in catalogue] == ["real song"]

    def test_empty_artist_returns_empty(self, monkeypatch):
        assert ps.get_online_artist_catalogue("", _FakeLastFm([])) == []

    def test_a_failing_client_returns_empty_not_an_error(self, monkeypatch):
        import services.popularity.popularity_cache_service as pcs

        def _boom(*_a, **_k):
            raise RuntimeError("api down")

        monkeypatch.setattr(pcs, "get_artist_top_tracks_map", _boom)

        assert ps.get_online_artist_catalogue("A", _FakeLastFm([])) == []


class TestTheCatalogueIsCachedPerArtist:
    def test_a_second_lookup_does_not_refetch(self, monkeypatch):
        """A multi-disc compilation must pay for one request per credited artist."""
        calls = {"n": 0}
        import services.popularity.popularity_cache_service as pcs

        def _counting(_client, _artist):
            calls["n"] += 1
            return {"song": {"lastfm_listeners": 100}}

        monkeypatch.setattr(pcs, "get_artist_top_tracks_map", _counting)

        ps.get_online_artist_catalogue("A", _FakeLastFm([]))
        ps.get_online_artist_catalogue("A", _FakeLastFm([]))
        ps.get_online_artist_catalogue("a", _FakeLastFm([]))  # case-insensitive

        assert calls["n"] == 1, f"expected 1 fetch, got {calls['n']}"


# ---------------------------------------------------------------------------
# The rank lookup
# ---------------------------------------------------------------------------


class TestTheRankLookup:
    def test_the_top_song_is_rank_one(self, monkeypatch):
        _stub_cache(monkeypatch, {
            "natural high": {"lastfm_listeners": 12051},
            "poison": {"lastfm_listeners": 8900},
            "filler": {"lastfm_listeners": 100},
        })

        info = ps.get_online_artist_track_rank("Insolence", "Natural High", _FakeLastFm([]))

        assert info["rank"] == 1
        assert info["total"] == 3
        # Mid-rank percentile -- see the docstring in get_online_artist_track_rank.
        assert info["percentile"] == pytest.approx(0.5 / 3)
        assert info["listeners"] == 12051
        assert info["top_listeners"] == 12051
        assert info["source"] == "lastfm_artist_top_tracks"

    def test_a_lower_song_gets_its_true_position(self, monkeypatch):
        _stub_cache(monkeypatch, {
            "natural high": {"lastfm_listeners": 12051},
            "poison": {"lastfm_listeners": 8900},
            "filler": {"lastfm_listeners": 100},
        })

        info = ps.get_online_artist_track_rank("Insolence", "Poison", _FakeLastFm([]))

        assert info["rank"] == 2
        assert info["top_listeners"] == 12051, "the artist's peak is reported"

    def test_matching_is_normalized(self, monkeypatch):
        """Last.fm casing/punctuation differences must not defeat the match."""
        _stub_cache(monkeypatch, {"natural high": {"lastfm_listeners": 900}})

        info = ps.get_online_artist_track_rank("Insolence", "NATURAL HIGH", _FakeLastFm([]))

        assert info["rank"] == 1

    def test_a_missing_catalogue_is_unknown(self, monkeypatch):
        _stub_cache(monkeypatch, {})

        info = ps.get_online_artist_track_rank("Nobody", "Nothing", _FakeLastFm([]))

        assert info["rank"] == 0
        assert info["total"] == 0
        assert info["source"] == "none"

    def test_an_uncharted_title_is_reported_beyond_the_list(self, monkeypatch):
        """A real artist with a song that never charted -> last place."""
        _stub_cache(monkeypatch, {
            "hit": {"lastfm_listeners": 5000},
            "album track": {"lastfm_listeners": 900},
        })

        info = ps.get_online_artist_track_rank("A", "Obscure B-side", _FakeLastFm([]))

        assert info["rank"] == 3
        assert info["total"] == 2
        assert info["percentile"] == 1.0
        assert info["source"] == "lastfm_artist_top_tracks_beyond"

    def test_empty_title_is_unknown(self, monkeypatch):
        _stub_cache(monkeypatch, {"hit": {"lastfm_listeners": 5000}})

        assert ps.get_online_artist_track_rank("A", "", _FakeLastFm([]))["rank"] == 0

    def test_the_library_only_owning_one_song_still_ranks_it_correctly(self, monkeypatch):
        """THE REPORTED CASE.

        The library owns exactly one Insolence song. Online, that song is the
        band's #1 -- so it must rank 1, and it must do so from a catalogue of
        many songs the library does NOT own.
        """
        _stub_cache(monkeypatch, {
            "natural high": {"lastfm_listeners": 12051},
            **{f"album track {i}": {"lastfm_listeners": 100 - i} for i in range(20)},
        })

        info = ps.get_online_artist_track_rank("Insolence", "Natural High", _FakeLastFm([]))

        assert info["rank"] == 1
        assert info["total"] == 21, "the full online catalogue is ranked against"

        # The artist's #1 must land in the 5★ band even though a 2% slice of a
        # 21-song catalogue covers less than one song.
        from services.popularity.stages import finalise_stage as fs

        stars, _ = fs._online_catalogue_stars(
            credited_artist="Insolence",
            track_title="Natural High",
            rules=ONLINE_RULES,
        )
        assert stars == 5, "an artist's most popular song must reach 5★"
