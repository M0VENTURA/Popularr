"""Era-calibration prominence benchmark tests.

The previous ``_build_album_model`` computed M_peak / R_eff from re-anchored
ALBUM-RELATIVE medians (``apply_album_relative_popularity``), which collapse
every album's median to ~50 by construction.  R_eff therefore came out
~0.75-1.0 for EVERY album — a 900k-listener album and a 50k-listener album
both classified ``era=peak`` with the same 5★ allowance, so a forced re-scan
reproduced the same Eat-the-Elephant-style skew forever.

The fix uses a RAW-LISTENER prominence benchmark (log-scaled blend of
``lastfm_listeners`` + ``listenbrainz_listens``) for M_peak / R_eff when
listener data is available — cross-album magnitude survives, so a weak album
drops to ``era=minor`` and its 5★ allowance is capped.
"""

from __future__ import annotations

from contextlib import contextmanager


def _fake_rows(rows):
    class _Result:
        def fetchall(self):
            return rows

    return _Result()


def _session_factory(rows):
    @contextmanager
    def _cm():
        yield _FakeSession(rows)

    return _cm


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _fake_rows(self._rows)


def _album_row(album, title, score, year=2000, lf=0, lb=0):
    return {
        "title": title,
        "album": album,
        "final_score": score,
        "year": year,
        "lastfm_listeners": lf,
        "listenbrainz_listens": lb,
    }


def _album_result(album, scores, lf_list=None, lb_list=None, year=2000):
    lf_list = lf_list or [0] * len(scores)
    lb_list = lb_list or [0] * len(scores)
    return [
        {
            "album": album,
            "title": f"{album} Track {i}",
            "popularity_score": s,
            "year": year,
            "lastfm_listeners": lf_list[i],
            "listenbrainz_listens": lb_list[i],
        }
        for i, s in enumerate(scores)
    ]


def _model_for(album_results, db_rows, monkeypatch, artist="A Perfect Circle"):
    from services.popularity.stages import finalise_stage as fs

    # ``finalise_stage`` binds ``db_session`` at import time, so patching
    # ``db.engine.db_session`` does not reach it. Patch the module the model
    # actually calls.
    monkeypatch.setattr(fs, "db_session", _session_factory(db_rows))
    return fs._build_album_model(
        artist,
        album_results,
        artist_scores=[50.0, 55.0, 60.0, 65.0, 70.0],
    )


class TestProminenceEraBenchmark:
    def test_low_prominence_album_drops_to_minor_era(self, monkeypatch):
        """Eat the Elephant (median ~80k LF) vs Mer de Noms (median ~800k LF):
        the weak album must NOT classify as era=peak.  The raw-listener
        benchmark separates them while the flattened album-relative medians
        could not."""
        # Mer de Noms: 600k-900k listeners per track (the catalogue peak).
        mdn_rows = [
            _album_row("Mer de Noms", f"MdN Track {i}", 60 + i, year=2000,
                       lf=lf, lb=max(2000, lf // 10))
            for i, lf in enumerate([600000, 700000, 800000, 900000])
        ]
        # Eat the Elephant: 45k-136k listeners (an order of magnitude lower).
        ete_rows = [
            _album_row("Eat the Elephant", f"EtE Track {i}", 50 + i, year=2018,
                       lf=lf, lb=max(300, lf // 10))
            for i, lf in enumerate([45000, 60000, 80000, 136000])
        ]
        db_rows = mdn_rows + ete_rows

        mdn_model = _model_for(
            _album_result("Mer de Noms", [60, 61, 62, 63],
                          lf_list=[600000, 700000, 800000, 900000],
                          lb_list=[60000, 70000, 80000, 90000], year=2000),
            db_rows, monkeypatch,
        )
        ete_model = _model_for(
            _album_result("Eat the Elephant", [50, 51, 52, 53],
                          lf_list=[45000, 60000, 80000, 136000],
                          lb_list=[4500, 6000, 8000, 13600], year=2018),
            db_rows, monkeypatch,
        )

        # Both use the prominence benchmark (listener data present).
        assert mdn_model.get("benchmark_source") == "prominence"
        assert ete_model.get("benchmark_source") == "prominence"
        # Mer de Noms is the catalogue peak; Eat the Elephant sits far below.
        assert mdn_model.get("era") == "peak"
        assert ete_model.get("era") != "peak"
        # R_eff for the weak album is meaningfully damped.
        assert float(ete_model.get("reff") or 1.0) < 0.75
        # M_peak reflects the RAW listener magnitude, not the flattened 50-67.
        assert float(mdn_model.get("m_peak") or 0) > float(ete_model.get("album_median") or 0)

    def test_prominence_benchmark_requires_two_albums_with_listener_data(self, monkeypatch):
        """A single album with listener data cannot anchor a catalogue peak —
        it falls back to the score-based path (legacy behaviour)."""
        db_rows = [
            _album_row("Only Album", f"Only Track {i}", 50 + i, year=2000,
                       lf=800000, lb=80000)
            for i in range(4)
        ]
        model = _model_for(
            _album_result("Only Album", [50, 51, 52, 53],
                          lf_list=[800000, 800000, 800000, 800000],
                          lb_list=[80000, 80000, 80000, 80000], year=2000),
            db_rows, monkeypatch,
        )
        assert model.get("has_benchmark")
        # A single-album catalogue has no peak to compare against — the
        # album is its own peak (era=peak, legacy single-→-5★ behaviour).
        assert model.get("era") == "peak"
        assert model.get("benchmark_source") == "scores"

    def test_missing_listener_data_falls_back_to_score_medians(self, monkeypatch):
        """Legacy rows / test fixtures without listener counts use the
        re-anchored score medians (the previous behaviour)."""
        db_rows = [
            _album_row("Album A", f"A Track {i}", 50 + i, year=2000)
            for i in range(4)
        ] + [
            _album_row("Album B", f"B Track {i}", 58 + i, year=2003)
            for i in range(4)
        ]
        model = _model_for(
            _album_result("Album A", [50, 51, 52, 53], year=2000),
            db_rows, monkeypatch,
        )
        assert model.get("has_benchmark")
        assert model.get("benchmark_source") == "scores"

    def test_catalog_wide_mpeak_identical_across_albums_with_prominence(self, monkeypatch):
        """The two-pass invariant holds on the prominence benchmark too: every
        album of the artist resolves the SAME catalog-wide M_peak regardless
        of scan order."""
        albums = [
            ("Mer de Noms", 2000, [600000, 700000, 800000, 900000], [60, 61, 62, 63]),
            ("Thirteenth Step", 2003, [500000, 600000, 700000, 718000], [58, 59, 60, 61]),
            ("Eat the Elephant", 2018, [45000, 60000, 80000, 136000], [50, 51, 52, 53]),
        ]
        db_rows = []
        for name, year, lfs, scores in albums:
            for i, (lf, sc) in enumerate(zip(lfs, scores)):
                db_rows.append(_album_row(name, f"{name} Track {i}", sc, year=year,
                                          lf=lf, lb=max(300, lf // 10)))

        m_peaks = set()
        eras = set()
        for name, year, lfs, scores in albums:
            m = _model_for(
                _album_result(name, scores,
                              lf_list=lfs,
                              lb_list=[max(300, lf // 10) for lf in lfs],
                              year=year),
                db_rows, monkeypatch,
            )
            m_peaks.add(round(float(m.get("m_peak") or 0), 6))
            eras.add(m.get("era"))

        # Identical catalog-wide M_peak across every album.
        assert len(m_peaks) == 1
        # The eras DIFFER (Mer de Noms = peak, Eat the Elephant = damped) —
        # which is the whole point: prominence separates them.
        assert len(eras) >= 2


class TestAlbumProminenceScore:
    def test_log_scaled_blend_preserves_magnitude(self):
        from services.popularity.popularity_math import album_prominence_score

        big = album_prominence_score(800000, 80000)
        small = album_prominence_score(80000, 8000)
        assert big > small
        # Log scale: 10x listeners = ~+16 points, not +10x.
        assert big - small > 5.0
        assert big - small < 30.0

    def test_zero_counts_return_zero(self):
        from services.popularity.popularity_math import album_prominence_score

        assert album_prominence_score(0, 0) == 0.0
        assert album_prominence_score(0, 500) > 0.0

    def test_median_helper(self):
        from services.popularity.popularity_math import (
            album_prominence_median,
            album_prominence_score,
        )

        rows = [
            {"lastfm_listeners": 800000, "listenbrainz_listens": 80000},
            {"lastfm_listeners": 700000, "listenbrainz_listens": 70000},
            {"lastfm_listeners": 0, "listenbrainz_listens": 0},
        ]
        med = album_prominence_median(rows)
        assert med > 0.0
        # Zero-count rows are dropped; median of the two valid rows.
        assert 0.0 < med < album_prominence_score(800000, 80000)


class TestFourStarArtistZGates:
    """4★ requires catalogue prominence (artist_z hard minimum), not just
    "best track on this specific record"."""

    def _track(self, score):
        return {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": score, "final_score": score,
            "lastfm_listeners": 5000, "listenbrainz_listens": 4000,
            "lb_percentile": 0.95, "lastfm_score": 8.0, "listenbrainz_score": 9.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }

    def test_album_top_but_catalogue_weak_is_demoted(self):
        from services.popularity.stages import finalise_stage as fs

        # The track tops its OWN album (album_z ~ 0.625 → 4★ band) but sits at
        # the BOTTOM of the artist catalogue (artist_z ≈ -1.2) — the artist-z
        # hard minimum must demote it to 3★.  Album scores are tight around
        # 50; the artist catalogue sits far higher [55..100].
        track = self._track(55.0)
        album_scores = [40.0, 45.0, 50.0, 52.0, 55.0]
        artist_scores = [55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 90.0, 95.0, 100.0]
        stars = fs._assign_stars(track, album_scores, artist_scores)
        assert stars == 3

    def test_album_and_catalogue_standout_stays_four(self):
        from services.popularity.stages import finalise_stage as fs

        # The track tops its album AND sits at the top of the catalogue →
        # artist_z clears the gate → 4★.
        track = self._track(100.0)
        album_scores = [88.0, 89.0, 90.0, 91.0, 100.0]
        artist_scores = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        stars = fs._assign_stars(track, album_scores, artist_scores)
        assert stars == 4

    def test_no_artist_context_keeps_pure_album_band(self):
        from services.popularity.stages import finalise_stage as fs

        # A tiny artist catalogue (fewer than 5 valid scores) cannot anchor an
        # artist-z gate — the pure album-relative band stands (4★).
        track = self._track(55.0)
        album_scores = [40.0, 45.0, 50.0, 52.0, 55.0]
        stars = fs._assign_stars(track, album_scores, [50.0, 52.0, 55.0])
        assert stars == 4
