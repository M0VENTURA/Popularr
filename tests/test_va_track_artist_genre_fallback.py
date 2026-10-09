"""A VA track with no genres of its own falls back to its PERFORMER's genres.

Request
-------
> When scanning a various artists collection, if the track has no genre data
> online, can it fall back to using the genres for the track artist? If on the
> local db it can be grabbed from that artist or looked up on Musicbrainz,
> discogs and last.fm

Before this, a VA track whose own source columns were all empty was LEFT ALONE
— correct as far as it went (its Navidrome value survived rather than being
blanked), but on a compilation that value is often the disc tagger's guess.
The performer is known, and the performer's genres are the missing evidence.

Order of evidence
-----------------
1. the **local database** — the artist's other rows (``tracks.genres``) and
   ``artists.lastfm_artist_tags``; both free, and already curated by an earlier
   scan;
2. only if that is empty — **MusicBrainz**, **Discogs**, **Last.fm** artist
   lookups.

``genres.min_weight`` is deliberately NOT applied to the artist-level result:
that rule exists to stop a lone weak TRACK tag defining a track, and applying
it here would mean a Last.fm-only artist gets no fallback at all — failing for
exactly the compilations the request was made for. The weight is the ORDER.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from services.enrichment import genre_aggregation_service as gas


@pytest.fixture(autouse=True)
def _clear_fallback_cache():
    """The lookup memo is process-wide, so tests must not inherit each other."""
    gas._ARTIST_GENRE_FALLBACK_CACHE.clear()
    yield
    gas._ARTIST_GENRE_FALLBACK_CACHE.clear()


def _no_network(monkeypatch, *, library=None, musicbrainz=None, discogs=None, lastfm=None):
    """Patch the four artist sources; return the call log."""
    calls: list[str] = []

    def _make(name, value):
        def _fetch(artist, **_kwargs):
            calls.append(name)
            return list(value or [])
        return _fetch

    monkeypatch.setattr(gas, "_artist_genres_from_library", _make("library", library))
    monkeypatch.setattr(gas, "_artist_genres_from_musicbrainz", _make("musicbrainz", musicbrainz))
    monkeypatch.setattr(gas, "_artist_genres_from_discogs", _make("discogs", discogs))
    monkeypatch.setattr(gas, "_artist_genres_from_lastfm", _make("lastfm", lastfm))
    return calls


# ===========================================================================
# 1. Ordering and filtering (pure)
# ===========================================================================
class TestHowArtistGenresAreRanked:
    def test_the_local_library_wins(self):
        assert gas._merge_artist_genre_sources(
            {"library": ["Melodic Death Metal"], "musicbrainz": ["Metal"]}
        ) == ["Melodic Death Metal", "Metal"]

    def test_musicbrainz_outranks_discogs_and_lastfm(self):
        ranked = gas._merge_artist_genre_sources(
            {"lastfm": ["gothic metal"], "discogs": ["doom metal"], "musicbrainz": ["death metal"]}
        )
        assert ranked == ["death metal", "doom metal"], (
            "the source priority must follow the configured weights "
            "(musicbrainz 0.40, discogs 0.25, lastfm 0.10)"
        )

    def test_the_result_is_capped(self):
        ranked = gas._merge_artist_genre_sources(
            {"musicbrainz": ["death metal", "doom metal", "black metal"]}, max_genres=2
        )
        assert ranked == ["death metal", "doom metal"]

    def test_junk_and_admin_tags_are_dropped(self):
        ranked = gas._merge_artist_genre_sources(
            {"lastfm": ["seen live", "2011", "doom metal"]}
        )
        assert ranked == ["doom metal"], (
            "a year tag or 'seen live' must never become a track's genre"
        )

    def test_a_spelling_variant_is_only_kept_once(self):
        ranked = gas._merge_artist_genre_sources(
            {"musicbrainz": ["Hip Hop"], "discogs": ["hip-hop"]}
        )
        assert ranked == ["Hip Hop"], "the same genre twice, in two spellings"

    def test_nothing_at_all_ranks_to_nothing(self):
        assert gas._merge_artist_genre_sources({"musicbrainz": [], "lastfm": []}) == []


# ===========================================================================
# 2. Which sources are consulted, and when
# ===========================================================================
class TestTheLookupOrder:
    def test_the_local_database_short_circuits_the_network(self, monkeypatch):
        """The whole point of 'if on the local db it can be grabbed'."""
        calls = _no_network(
            monkeypatch,
            library=["alternative metal"],
            musicbrainz=["should not be asked"],
        )

        resolved = gas.fallback_genres_for_artist("10 Years")

        assert resolved == ["alternative metal"]
        assert "musicbrainz" not in calls, (
            "MusicBrainz was queried even though the artist is already in the "
            "library — a 1 req/s call for an answer we hold"
        )
        assert calls == ["library"]

    def test_the_online_sources_run_only_when_the_library_is_empty(self, monkeypatch):
        calls = _no_network(
            monkeypatch,
            library=[],
            musicbrainz=["black metal"],
            discogs=["doom metal"],
            lastfm=["rock"],
        )

        resolved = gas.fallback_genres_for_artist("Netherwilds")

        assert resolved == ["black metal", "doom metal"]
        assert calls == ["library", "musicbrainz", "discogs", "lastfm"], (
            "all three requested sources must be consulted (lastfm's genre is "
            "outranked, but the lookup still happens)"
        )

    def test_a_lastfm_only_artist_still_gets_a_fallback(self, monkeypatch):
        """Documented difference from the track vote's weight threshold."""
        _no_network(monkeypatch, library=[], musicbrainz=[], discogs=[], lastfm=["post-rock"])

        assert gas.fallback_genres_for_artist("Some Artist") == ["post-rock"], (
            "Last.fm weighs 0.10, below genres.min_weight — that rule exists "
            "for a lone TRACK tag, and applying it here would leave exactly "
            "the artists this request targets without any genre at all"
        )

    def test_a_placeholder_performer_is_not_looked_up(self, monkeypatch):
        calls = _no_network(monkeypatch, musicbrainz=["metal"])

        assert gas.fallback_genres_for_artist("Various Artists") == []
        assert calls == [], "'Various Artists' is not an artist to look up"

    def test_an_empty_artist_is_not_looked_up(self, monkeypatch):
        calls = _no_network(monkeypatch, musicbrainz=["metal"])
        assert gas.fallback_genres_for_artist("   ") == []
        assert calls == []

    def test_a_placeholder_name_is_not_an_artist(self, monkeypatch):
        """CONTROL for the two above — a REAL artist IS looked up."""
        calls = _no_network(monkeypatch, musicbrainz=["metal"])
        assert gas.fallback_genres_for_artist("Netherwilds") == ["metal"]
        assert calls == ["library", "musicbrainz", "discogs", "lastfm"]


# ===========================================================================
# 3. The memo
# ===========================================================================
class TestTheLookupIsMemoised:
    def test_the_same_artist_is_looked_up_once(self, monkeypatch):
        calls = _no_network(monkeypatch, musicbrainz=["metal"])

        first = gas.fallback_genres_for_artist("Halestorm")
        second = gas.fallback_genres_for_artist("halestorm")

        assert first == second == ["metal"]
        assert calls.count("musicbrainz") == 1, (
            "a compilation's tracks share performers, so the same artist must "
            "not be re-resolved per track"
        )

    def test_a_miss_is_not_cached(self, monkeypatch):
        """An artist whose data appears later must be able to answer."""
        calls = _no_network(monkeypatch, musicbrainz=[])

        assert gas.fallback_genres_for_artist("Nobody") == []
        assert gas.fallback_genres_for_artist("Nobody") == []
        assert calls.count("musicbrainz") == 2, (
            "caching the MISS would freeze a not-yet-documented artist for the "
            "whole process lifetime"
        )


# ===========================================================================
# 3b. The Last.fm tags are kept, so the fallback converges
# ===========================================================================
class TestTheLastfmTagsAreCached:
    def test_the_tags_are_written_only_into_an_empty_column(self, monkeypatch):
        """The next scan then answers from the local tier, for free."""
        sql: list[str] = []

        class _Result:
            rowcount = 1

        class _Session:
            def execute(self, statement, params=None):
                sql.append(str(statement))
                return _Result()

        @contextmanager
        def _fake_db_session():
            yield _Session()

        monkeypatch.setattr(gas, "db_session", _fake_db_session)

        gas._cache_lastfm_artist_tags("Halestorm", ["hard rock", "metal"])

        assert len(sql) == 1
        assert "UPDATE artists SET lastfm_artist_tags" in sql[0]
        assert "TRIM(lastfm_artist_tags) = ''" in sql[0], (
            "a populated column is this app's cache-hit signal — a second "
            "writer must not clobber it"
        )

    def test_nothing_is_written_without_tags(self, monkeypatch):
        sql: list[str] = []

        class _Session:
            def execute(self, statement, params=None):
                sql.append(str(statement))
                raise AssertionError("no tags means no write")

        @contextmanager
        def _fake_db_session():
            yield _Session()

        monkeypatch.setattr(gas, "db_session", _fake_db_session)

        gas._cache_lastfm_artist_tags("Halestorm", [])
        gas._cache_lastfm_artist_tags("", ["metal"])
        assert sql == []

    def test_a_cache_failure_never_breaks_the_fallback(self, monkeypatch):
        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(gas, "db_session", _boom)
        gas._cache_lastfm_artist_tags("Halestorm", ["metal"])  # must not raise


# ===========================================================================
# 4. The sync actually uses it
# ===========================================================================
_WRITES: list[dict] = []


def _capture_writes(monkeypatch):
    """Patch the DB write and return the recorded statements."""
    _WRITES.clear()

    class _Result:
        rowcount = 1

    class _Session:
        def execute(self, statement, params=None):
            _WRITES.append(dict(params or {}))
            return _Result()

        def commit(self):
            pass

        def rollback(self):
            pass

    @contextmanager
    def _fake_db_session():
        yield _Session()

    monkeypatch.setattr(gas, "db_session", _fake_db_session)
    return _WRITES


def _track(**overrides):
    row = {
        "id": "t1",
        "title": "Blight!",
        "artist": "Netherwilds",
        "genres": "metal",
    }
    row.update(overrides)
    return row


class TestTheSyncFallsBack:
    def test_a_track_with_its_own_sources_still_uses_them(self, monkeypatch):
        """CONTROL — the fallback must not replace the normal path."""
        writes = _capture_writes(monkeypatch)
        called = _no_network(monkeypatch, musicbrainz=["should not be asked"])

        gas.sync_various_artists_track_genres(
            [_track(musicbrainz_genres="thrash metal")], album="Peasant Rising"
        )

        assert len(writes) == 1 and writes[0]["track_id"] == "t1", writes
        assert "thrash" in writes[0]["genres"].lower(), writes
        assert called == [], "the fallback ran for a track that had its own source"

    def test_a_track_with_nothing_gets_its_artist_genres(self, monkeypatch):
        writes = _capture_writes(monkeypatch)
        _no_network(monkeypatch, library=["melodic death metal"])

        gas.sync_various_artists_track_genres([_track()], album="Peasant Rising")

        assert writes == [{"genres": "melodic death metal", "track_id": "t1"}], (
            "a VA track with no sources must take its own artist's genres"
        )

    def test_the_write_is_still_per_track(self, monkeypatch):
        writes = _capture_writes(monkeypatch)
        _no_network(monkeypatch, library=["melodic death metal"])

        gas.sync_various_artists_track_genres(
            [_track(id="t1"), _track(id="t2"), _track(id="t3")],
            album="Peasant Rising",
        )

        assert [w["track_id"] for w in writes] == ["t1", "t2", "t3"], (
            "the album-wide UPDATE is exactly what a compilation must not do"
        )

    def test_an_empty_fallback_leaves_the_row_alone(self, monkeypatch):
        writes = _capture_writes(monkeypatch)
        _no_network(monkeypatch, library=[], musicbrainz=[], discogs=[], lastfm=[])

        gas.sync_various_artists_track_genres([_track()], album="X")

        assert writes == [], (
            "with no evidence at all the Navidrome value must survive — never "
            "blank a track"
        )

    def test_a_track_with_no_artist_is_left_alone(self, monkeypatch):
        writes = _capture_writes(monkeypatch)
        called = _no_network(monkeypatch, library=["metal"])

        gas.sync_various_artists_track_genres([_track(artist="")], album="X")

        assert writes == []
        assert called == [], "no performer means no lookup"

    def test_the_fallback_result_is_not_rewritten_every_scan(self, monkeypatch):
        """A row that already holds the artist's genres must not be written."""
        writes = _capture_writes(monkeypatch)
        _no_network(monkeypatch, library=["melodic death metal"])

        gas.sync_various_artists_track_genres(
            [_track(genres="Melodic Death Metal")], album="X"
        )

        assert writes == [], "an unchanged value must not churn the file tags"
