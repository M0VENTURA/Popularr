"""Scan-pipeline DB-first speedups (2026-10-11 audit G1/G2/G4/G5/G6).

Full-audit findings, each pinned by the behaviour it changes:

* **G1** — ``get_suggested_mbid`` RECORDED nothing for a miss, and the read
  path refused empty entries, so every scan process re-spent a full second
  of the global 1 req/s MusicBrainz throttle searching for every
  unresolvable track (duplicate-title tracks the album batch skips, releases
  MusicBrainz lacks). Misses are now recorded and served for a shorter
  negative TTL.

* **G2** — the artist Last.fm map covered only the top **200** tracks; every
  title outside it fell to the deep-cut path (search aggregation +
  track.getInfo + variant lookups — 3-6 Last.fm calls EACH, per TTL).
  Widened to 500 for the same single ``artist.getTopTracks`` call.

* **G4** — ``_album_release_year`` ran one DB SELECT per TRACK in the
  release-date check; now memoised per (artist, album) per process.

* **G5** — a completed "not a single" verdict is stable data (a track's
  release history does not change) yet expired on the same ladder as a
  positive, re-running the full singles arm battery per deep cut per day.
  Definitive negatives now live 3x the base TTL.

* **G6** — the ``appears_on_various_artists`` arm in
  ``detect_single_for_track`` was guarded by ``hasattr`` and the method was
  NEVER ported from ``old_system`` — the flag could never be set and the
  plumbing was dead. Removed.
"""

from __future__ import annotations

import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _strip_python(src: str) -> str:
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


# ---------------------------------------------------------------------------
# G1 — negative MBID resolutions are recorded and served
# ---------------------------------------------------------------------------


class _FakeMbHttp:
    def __init__(self, recordings=None):
        self.recordings = recordings or []
        self.search_calls = 0

    def search_recordings(self, query, limit=25, inc=""):
        self.search_calls += 1
        return list(self.recordings)


def _service(http):
    from services.enrichment.musicbrainz_service import MusicBrainzService

    svc = MusicBrainzService(enabled=True, http_client=http)
    svc._mbid_cache.clear()
    # Keep the tests off the disk flush path.
    svc._maybe_flush_cache = lambda *a, **k: None
    return svc


class TestNegativeMbidResolutionsAreServed:
    def test_a_miss_is_recorded_and_the_next_process_would_skip_the_search(self):
        http = _FakeMbHttp(recordings=[])  # nothing matches
        svc = _service(http)

        first = svc.get_suggested_mbid("Ghost Song", "Some Artist")
        assert first == ("", 0.0)
        assert http.search_calls == 1

        # Same process, same question: served from the recorded miss.
        second = svc.get_suggested_mbid("Ghost Song", "Some Artist")
        assert second == ("", 0.0)
        assert http.search_calls == 1, (
            "the recorded miss must short-circuit the repeat search — the "
            "search costs a full second of the 1 req/s MusicBrainz throttle"
        )

    def test_an_expired_miss_searches_again(self):
        http = _FakeMbHttp(recordings=[])
        svc = _service(http)
        svc.get_suggested_mbid("Ghost Song", "Some Artist")

        from services.enrichment import musicbrainz_service as mbs

        key = svc._cache_key("Ghost Song", "Some Artist")
        assert svc._mbid_cache[key][0] == "", "the miss must be the recorded entry"
        # Age the miss past the negative TTL.
        svc._mbid_cache[key] = [
            "", 0.0, time.time() - (mbs._MBID_NEGATIVE_TTL_SECONDS + 3600)
        ]
        svc.get_suggested_mbid("Ghost Song", "Some Artist")
        assert http.search_calls == 2, "an expired miss must be re-searched"

    def test_a_positive_is_still_recorded_and_served(self, monkeypatch):
        http = _FakeMbHttp(recordings=[
            {"id": "rec-1", "title": "Ghost Song", "releases": []},
        ])
        svc = _service(http)
        mbid, score = svc.get_suggested_mbid("Ghost Song", "Some Artist")
        assert mbid == "rec-1"
        assert http.search_calls == 1

        again = svc.get_suggested_mbid("Ghost Song", "Some Artist")
        assert again[0] == "rec-1"
        assert http.search_calls == 1

    def test_a_recorded_miss_never_clobbers_a_live_positive(self):
        http = _FakeMbHttp(recordings=[
            {"id": "rec-1", "title": "Ghost Song", "releases": []},
        ])
        svc = _service(http)
        svc.get_suggested_mbid("Ghost Song", "Some Artist")
        # Within the positive TTL the read path returns before any search, so
        # no miss can be recorded over it.
        svc.get_suggested_mbid("Ghost Song", "Some Artist")
        from services.enrichment import musicbrainz_service as mbs

        key = svc._cache_key("Ghost Song", "Some Artist")
        assert svc._mbid_cache[key][0] == "rec-1"
        assert mbs._MBID_NEGATIVE_TTL_SECONDS < mbs._MBID_CACHE_TTL_SECONDS, (
            "the negative TTL must be the SHORTER window"
        )


# ---------------------------------------------------------------------------
# G2 — the artist top-tracks map covers deep cuts from one call
# ---------------------------------------------------------------------------


class _FakeLastFm:
    def __init__(self, tracks):
        self.tracks = tracks
        self.top_calls = 0
        self.last_limit = None

    def get_artist_top_tracks(self, artist, limit=100):
        self.top_calls += 1
        self.last_limit = limit
        return self.tracks[:limit]

    def get_track_info(self, artist, title, track_mbid=None):
        raise AssertionError("deep-cut path must not run for an in-map title")

    def search_track(self, artist, title, limit=10):
        raise AssertionError("deep-cut path must not run for an in-map title")


@pytest.fixture
def lf_cache_reset():
    import services.popularity.popularity_cache_service as pcs

    with pcs._CACHE_LOCK:
        pcs._lf_top_tracks_cache.clear()
        pcs._lf_top_tracks_titles.clear()
        pcs._lf_top_tracks_tags.clear()
    yield
    with pcs._CACHE_LOCK:
        pcs._lf_top_tracks_cache.clear()
        pcs._lf_top_tracks_titles.clear()
        pcs._lf_top_tracks_tags.clear()


class TestTopTracksMapCoversDeepCuts:
    def test_the_widened_limit_is_requested(self, lf_cache_reset):
        import services.popularity.popularity_cache_service as pcs

        fake = _FakeLastFm([
            {"name": f"Track {i}", "listeners": 1000 - i, "playcount": 5000}
            for i in range(600)
        ])
        out = pcs._lf_top_tracks_map(fake, "Some Artist")
        assert fake.top_calls == 1, "one artist.getTopTracks call per artist"
        assert fake.last_limit == pcs._LF_TOP_TRACKS_LIMIT
        assert pcs._LF_TOP_TRACKS_LIMIT >= 500, (
            "the old 200 limit is exactly what forced per-track deep-cut calls"
        )
        assert len(out) >= 300, "titles beyond the old top-200 must be in the map"

    def test_a_rank_300_title_costs_zero_extra_calls(self, lf_cache_reset):
        from services.popularity.popularity_sources import (
            get_aggregated_lastfm_popularity,
        )

        tracks = [
            {"name": f"Track {i}", "listeners": 1000 - i, "playcount": 5000}
            for i in range(600)
        ]
        # Put the target at rank ~300.
        tracks[299] = {"name": "Deep Cut", "listeners": 321, "playcount": 999}
        fake = _FakeLastFm(tracks)

        agg = get_aggregated_lastfm_popularity(
            "Some Artist", "Deep Cut", lastfm_client=fake
        )
        assert int(agg.get("listeners") or 0) == 321
        assert fake.top_calls == 1, (
            "an in-map title must answer from the one catalog call — no "
            "search, no track.getInfo"
        )


# ---------------------------------------------------------------------------
# G4 — album release year is queried once per album, not per track
# ---------------------------------------------------------------------------


class TestAlbumYearIsMemoised:
    @pytest.fixture
    def year_probe(self, monkeypatch):
        import db.engine
        from services.enrichment import single_detection_service as sds

        calls = []

        @contextmanager
        def _session(*a, **k):
            calls.append(1)

            class _Row:
                _mapping = {"y": 1999}

            class _Result:
                def first(self):
                    return _Row()

            class _S:
                def execute(self, *a, **k):
                    return _Result()

            yield _S()

        monkeypatch.setattr(db.engine, "db_session", _session)
        with sds._CACHE_LOCK:
            sds._album_year_memo.clear()
        yield calls
        with sds._CACHE_LOCK:
            sds._album_year_memo.clear()

    def test_twelve_tracks_of_one_album_run_one_query(self, year_probe):
        from services.enrichment.single_detection_service import _album_release_year

        for _ in range(12):
            assert _album_release_year("Band", "Great Album") == 1999
        assert len(year_probe) == 1, (
            "the release-date check runs per TRACK — the SELECT must not"
        )

    def test_a_different_album_queries_again(self, year_probe):
        from services.enrichment.single_detection_service import _album_release_year

        _album_release_year("Band", "Great Album")
        _album_release_year("Band", "Other Album")
        assert len(year_probe) == 2

    def test_the_negative_answer_is_memoised(self, monkeypatch):
        import db.engine
        from services.enrichment import single_detection_service as sds

        calls = []

        @contextmanager
        def _session(*a, **k):
            calls.append(1)

            class _Result:
                def first(self):
                    return None

            class _S:
                def execute(self, *a, **k):
                    return _Result()

            yield _S()

        monkeypatch.setattr(db.engine, "db_session", _session)
        with sds._CACHE_LOCK:
            sds._album_year_memo.clear()
        try:
            from services.enrichment.single_detection_service import _album_release_year

            for _ in range(5):
                assert _album_release_year("Band", "Yearless Album") is None
            assert len(calls) == 1, "an album with no year must not re-query per track"
        finally:
            with sds._CACHE_LOCK:
                sds._album_year_memo.clear()


# ---------------------------------------------------------------------------
# G5 — definitive non-single verdicts outlive the base ladder
# ---------------------------------------------------------------------------


class TestDefinitiveNegativesLiveLonger:
    def _stamp(self, days_ago: float) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=days_ago * 24)

    def test_an_old_negative_is_still_fresh_beyond_the_base_ladder(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        # 1990 track: base TTL 168h (7d). Stamped 10 days ago → base says
        # stale; 3x (21d) says fresh.
        track = {
            "year": 1990,
            "is_single": False,
            "single_detection_last_updated": self._stamp(10),
        }
        assert singles_detection_is_fresh(track) is True

    def test_a_positive_keeps_the_base_ladder(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        track = {
            "year": 1990,
            "is_single": True,
            "single_detection_last_updated": self._stamp(10),
        }
        assert singles_detection_is_fresh(track) is False, (
            "positive verdicts must keep the original cadence — this is a "
            "control"
        )

    def test_a_new_negative_gets_the_multiplier_too(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        # <1yr track: base 24h. Stamped 2 days ago → base stale, 3x (72h) fresh.
        track = {
            "year": datetime.now().year,
            "is_single": False,
            "single_detection_last_updated": self._stamp(2),
        }
        assert singles_detection_is_fresh(track) is True

    def test_beyond_the_multiplied_window_it_expires(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        track = {
            "year": 1990,
            "is_single": False,
            "single_detection_last_updated": self._stamp(30),
        }
        assert singles_detection_is_fresh(track) is False


# ---------------------------------------------------------------------------
# G6 — the dead VA arm is gone
# ---------------------------------------------------------------------------


class TestDeadVaArmRemoved:
    @classmethod
    def _src(cls) -> str:
        path = (
            REPO_ROOT / "services" / "enrichment" / "single_detection_service.py"
        )
        return _strip_python(path.read_text(encoding="utf-8"))

    def test_the_never_ported_method_is_no_longer_probed(self):
        src = self._src()
        assert "appears_on_various_artists" not in src, (
            "the guarded method was never ported from old_system — the arm "
            "could never run and only advertised a capability that does not exist"
        )

    def test_the_flag_plumbing_is_gone(self):
        src = self._src()
        assert "mb_compilation_confirmed" not in src
        assert '"musicbrainz_compilation"' not in src
