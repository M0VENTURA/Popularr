"""Popularity scan performance: the caches must actually be reused.

A full audit of the scan engine found several places where a NON-FORCED rescan
re-did work it had already done. Each fix below is pinned here, because every one
of them fails silently — the scan still produces correct numbers, just slowly, so
nothing else in the suite notices if the optimisation regresses.

The four defects, in order of cost:

1. ``mbid_cache.json`` was WRITTEN and never READ. The cache exists with a
   30-day positive TTL and a 7-day negative TTL, but ``_mbid_cache`` started
   empty in every process, so after a restart every track re-paid a
   rate-limited (1 req/s) MusicBrainz recording search.

2. ``_build_album_listener_distributions`` is album-scoped but was recomputed
   per track — up to 3× each, since it is also reached from
   ``_reconstruct_raw_blend`` on the cached path and from the singles block.
   O(tracks²) title normalisation per album.

3. ``_album_edition_annotation`` walks every album title, per track.

4. ``_artist_dominant_genres`` is an artist-level LIMIT-500 query issued per
   genre-less track with no cache at all.

Plus two structural issues:

5. The genre caches evicted FIFO (``next(iter(...))``), so the hottest keys —
   the ones re-read every pass — were discarded first.

6. ``get_features_config`` rebuilt its merged dict on every ``get_feature``
   call: ~10 per album in the skip gate plus one per track via
   ``should_freeze_track``.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest


@pytest.fixture()
def mb(monkeypatch):
    return importlib.import_module("services.enrichment.musicbrainz_service")


@pytest.fixture()
def stage():
    return importlib.import_module("services.popularity.stages.track_stage")


# ---------------------------------------------------------------------------
# 1. The MBID cache survives a restart
# ---------------------------------------------------------------------------

class TestTheMbidCacheIsRestored:
    def test_the_restore_is_deferred_until_first_use(self, mb, tmp_path, monkeypatch):
        cache_file = tmp_path / "mbid_cache.json"
        cache_file.write_text(
            json.dumps({"entries": {"k::t": ["mbid-1", 0.9, time.time()]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mb, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(mb, "_CACHE_FILE_ENTRIES", None, raising=False)

        # No injected client => this is a real service and may use the file.
        service = mb.MusicBrainzService(enabled=False)
        assert service._cache_loaded is False, (
            "the on-disk cache was read in __init__ instead of on first use"
        )
        service._ensure_cache_loaded()
        assert "k::t" in service._mbid_cache
        assert service._cache_loaded is True

    def test_an_injected_client_never_reads_the_production_cache(
        self, mb, tmp_path, monkeypatch
    ):
        """⚠️ THE TEST-ISOLATION RULE, and it is not cosmetic.

        Integration tests build a service with a fake HTTP client to assert on
        exact call sequences. Restoring the real cache into one made lookups
        answer from unrelated data — 7 tests failed across two files before this
        guard existed.
        """
        cache_file = tmp_path / "mbid_cache.json"
        cache_file.write_text(
            json.dumps({"entries": {"k::t": ["mbid-1", 0.9, time.time()]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mb, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(mb, "_CACHE_FILE_ENTRIES", None, raising=False)

        class _NoHttp:
            def __getattr__(self, _name):
                raise AssertionError("no HTTP should happen in this test")

        service = mb.MusicBrainzService(http_client=_NoHttp(), enabled=True)
        service._ensure_cache_loaded()
        assert service._mbid_cache == {}, (
            "a service with an injected client inherited the production cache"
        )

    def test_a_written_cache_is_read_back(self, mb, tmp_path, monkeypatch):
        """THE REGRESSION: written since forever, never loaded."""
        cache_file = tmp_path / "mbid_cache.json"
        now = time.time()
        cache_file.write_text(
            json.dumps(
                {
                    "entries": {
                        "korn::here to stay": [
                            "a270e077-fb2f-47de-88a8-d518999ec1de",
                            0.97,
                            now,
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(mb, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(mb, "_CACHE_FILE_ENTRIES", None, raising=False)

        service = mb.MusicBrainzService.__new__(mb.MusicBrainzService)
        service._mem_lock = __import__("threading").Lock()
        service._mbid_cache = __import__("collections").OrderedDict()

        service._load_cache()
        assert "korn::here to stay" in service._mbid_cache, (
            "the persisted MBID cache was not restored, so every restart "
            "re-pays a rate-limited MusicBrainz recording search per track"
        )
        assert service._mbid_cache["korn::here to stay"][0] == (
            "a270e077-fb2f-47de-88a8-d518999ec1de"
        )

    def test_expired_entries_are_not_restored(self, mb, tmp_path, monkeypatch):
        """A stale entry must not look fresh across restarts."""
        cache_file = tmp_path / "mbid_cache.json"
        ancient = time.time() - (mb._MBID_CACHE_TTL_SECONDS + 3600)
        cache_file.write_text(
            json.dumps({"entries": {"old::song": ["mbid-x", 0.9, ancient]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mb, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(mb, "_CACHE_FILE_ENTRIES", None, raising=False)

        service = mb.MusicBrainzService.__new__(mb.MusicBrainzService)
        service._mem_lock = __import__("threading").Lock()
        service._mbid_cache = __import__("collections").OrderedDict()
        service._load_cache()

        assert service._mbid_cache == {}, "an expired entry was restored as fresh"

    def test_a_corrupt_file_is_ignored(self, mb, tmp_path, monkeypatch):
        """The cache is an optimisation, never a source of truth."""
        cache_file = tmp_path / "mbid_cache.json"
        cache_file.write_text('{"entries": {"trunc', encoding="utf-8")
        monkeypatch.setattr(mb, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(mb, "_CACHE_FILE_ENTRIES", None, raising=False)

        service = mb.MusicBrainzService.__new__(mb.MusicBrainzService)
        service._mem_lock = __import__("threading").Lock()
        service._mbid_cache = __import__("collections").OrderedDict()
        service._load_cache()  # must not raise

        assert service._mbid_cache == {}

    def test_the_save_merges_rather_than_truncating(self, mb, tmp_path, monkeypatch):
        """Two service instances share the file; a flush must not erase the other's.

        The save used to write ONLY ``self._mbid_cache``, so whichever instance
        flushed last deleted every entry the other had resolved — the file never
        accumulated, and restoring it would have restored a fraction of the work.
        """
        cache_file = tmp_path / "mbid_cache.json"
        cache_file.write_text(
            json.dumps({"entries": {"other::song": ["mbid-other", 0.9, time.time()]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mb, "CACHE_FILE", str(cache_file))

        service = mb.MusicBrainzService.__new__(mb.MusicBrainzService)
        service._mem_lock = __import__("threading").Lock()
        service._mbid_cache = __import__("collections").OrderedDict(
            {"mine::song": ["mbid-mine", 0.8, time.time()]}
        )
        service._cache_dirty = True
        service._cache_last_save = 0.0

        service._maybe_flush_cache(force=True)

        saved = json.loads(cache_file.read_text(encoding="utf-8"))["entries"]
        assert "mine::song" in saved
        assert "other::song" in saved, (
            "a flush deleted the other instance's entries — the file can never "
            "accumulate across instances or restarts"
        )


# ---------------------------------------------------------------------------
# 2 & 3. Album-level facts computed once per album
# ---------------------------------------------------------------------------

class TestAlbumFactsAreComputedOnce:
    def test_listener_distributions_are_memoised(self, stage, monkeypatch):
        """12 tracks of one album must not compute 12 (or 36) times."""
        calls = {"n": 0}
        real = stage._build_album_listener_distributions

        def _spy(**kwargs):
            calls["n"] += 1
            return real(**kwargs)

        monkeypatch.setattr(stage, "_build_album_listener_distributions", _spy)

        tracks = [{"title": f"Song {i}", "duration": 200} for i in range(12)]
        pop = {
            f"song {i}": {"lastfm_listeners": 100 + i, "listenbrainz_listens": 50 + i}
            for i in range(12)
        }
        ctx = {"album": "Test Album", "tracks": tracks}

        for _ in range(12):
            stage._album_listener_distributions(
                album_context=ctx, album_tracks=tracks, prefetched_popularity=pop
            )

        assert calls["n"] == 1, f"computed {calls['n']} times for one album"

    def test_a_different_album_recomputes(self, stage):
        """CONTROL: the memo is per album_context, so albums cannot collide."""
        tracks = [{"title": f"S{i}", "duration": 200} for i in range(4)]
        pop = {f"s{i}": {"lastfm_listeners": 10 + i} for i in range(4)}

        ctx_a = {"album": "A", "tracks": tracks}
        ctx_b = {"album": "B", "tracks": tracks}
        stage._album_listener_distributions(
            album_context=ctx_a, album_tracks=tracks, prefetched_popularity=pop
        )
        stage._album_listener_distributions(
            album_context=ctx_b, album_tracks=tracks, prefetched_popularity=pop
        )
        # Each ctx got its own slot; neither leaked into the other.
        assert "_listener_dist_cache" in ctx_a
        assert "_listener_dist_cache" in ctx_b
        assert ctx_a["_listener_dist_cache"] is not ctx_b["_listener_dist_cache"]

    def test_edition_annotation_is_memoised(self, stage):
        """It walks every album title; that must happen once, not per track."""
        ctx = {"album": "Untouchables", "tracks": [{"title": f"T{i}"} for i in range(5)]}

        first = stage._album_edition_annotation(ctx, ctx["tracks"])
        assert "_edition_annotation" in ctx, (
            "the album edition annotation was not memoised onto the context, so "
            "every track re-walks every album title"
        )
        # A poisoned cache must be served, proving the second call does not
        # recompute.
        ctx["_edition_annotation"] = "sentinel"
        assert stage._album_edition_annotation(ctx, ctx["tracks"]) == "sentinel"
        del first


# ---------------------------------------------------------------------------
# 4 & 5. The genre caches: memoised per artist, and LRU not FIFO
# ---------------------------------------------------------------------------

class TestTheGenreCaches:
    def test_bounded_get_promotes_the_entry(self, stage):
        """⚠️ FIFO evicted the hottest keys first.

        The scan touches albums in order, so the entries inserted FIRST are the
        ones needed again EARLIEST next pass. A cache that evicts by insertion
        order is therefore guaranteed to discard exactly what it should keep.
        """
        old_max = stage._GENRE_CACHE_MAX
        stage._GENRE_CACHE_MAX = 3
        try:
            cache: dict = {}
            for key in ("a", "b", "c"):
                stage._bounded_cache_put(cache, key, key.upper())
            # Read the OLDEST entry — a FIFO cache would still evict it next.
            assert stage._bounded_cache_get(cache, "a") == "A"
            stage._bounded_cache_put(cache, "d", "D")

            assert "a" in cache, "the just-read (hot) entry was evicted"
            assert "b" not in cache, "the least-recently-used entry should go"
        finally:
            stage._GENRE_CACHE_MAX = old_max

    def test_get_distinguishes_absent_from_cached_none(self, stage):
        cache: dict = {}
        assert stage._bounded_cache_get(cache, "missing") is stage._MISSING
        stage._bounded_cache_put(cache, "none", None)
        assert stage._bounded_cache_get(cache, "none") is None

    def test_artist_dominant_genres_is_cached(self, stage, monkeypatch):
        """An artist-level query must not run once per genre-less track."""
        calls = {"n": 0}

        class _FakeResult:
            def fetchall(self):
                return []

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a, **k):
                calls["n"] += 1
                return _FakeResult()

        import db.engine as engine

        monkeypatch.setattr(engine, "db_session", lambda: _FakeSession())
        stage._ARTIST_DOMINANT_GENRES_CACHE.clear()

        for _ in range(5):
            stage._artist_dominant_genres("Skindred")

        assert calls["n"] == 1, (
            f"queried {calls['n']} times for one artist — the per-track query "
            "this cache exists to prevent is back"
        )


# ---------------------------------------------------------------------------
# NOT optimised, on purpose
# ---------------------------------------------------------------------------

class TestThingsMeasuredAndLeftAlone:
    def test_the_feature_map_is_not_cached(self):
        """⚠️ A DELIBERATE NON-OPTIMISATION, pinned so it is not "fixed" again.

        ``get_feature`` looks hot (~10 per album, once per track via
        ``should_freeze_track``), but the merged dict is ~60 keys and copying it
        benchmarks at 0.37 µs — 3.7 ms across a thousand albums. Caching it would
        add a second global to invalidate for no measurable gain.
        """
        from helpers import config_helpers as ch

        assert not hasattr(ch, "_FEATURES_CACHE"), (
            "the feature map was cached again; measure it first — it was ~0.37µs "
            "per call and bought nothing"
        )
