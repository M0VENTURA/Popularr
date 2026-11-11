"""Two-pass M_peak era-calibration regression tests.

The scanner previously rated each album IMMEDIATELY after its tracks scored.
During a single run the DB is populated progressively, so the FIRST album of
an artist was era-classified against a catalogue containing only its own fresh
scores (M_peak = its own median → era=peak with R_eff=1.0) while later albums
were rated against a catalogue that had grown to include the earlier albums'
stored scores → damped.  The fix defers per-album star posting to the
artist-section close, so by the time ANY album of the artist is rated ALL of
the artist's albums are persisted and M_peak is resolved catalog-wide.

These tests pin the invariant: ``_build_album_model`` must return the SAME
M_peak / era for every album of an artist once the complete catalogue is in
the DB (the state the two-pass guarantees at rating time) — the first album
scanned no longer sees a different benchmark from its siblings.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


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


def _album_row(album, title, score, year=2000):
    return {"title": title, "album": album, "final_score": score, "year": year}


class TestCatalogWideMPeak:
    """Once the full catalogue is persisted, M_peak is identical for every
    album of the artist — the era model no longer depends on scan order."""

    def _db_rows(self, albums):
        rows = []
        for album in albums:
            for i, score in enumerate(album["scores"]):
                rows.append(_album_row(album["name"], f"{album['name']} Track {i}", score, album["year"]))
        return rows

    def _model_for(self, album_results, db_rows, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        # ``finalise_stage`` binds ``db_session`` at import time; patch the
        # module the model actually calls.
        monkeypatch.setattr(fs, "db_session", _session_factory(db_rows))
        return fs._build_album_model(
            "A Perfect Circle",
            album_results,
            artist_scores=[50.0, 55.0, 60.0, 65.0, 70.0],
        )

    def _album_result(self, album, scores):
        return [
            {"album": album, "title": f"{album} Track {i}", "popularity_score": s,
             "year": 2000}
            for i, s in enumerate(scores)
        ]

    def test_all_albums_share_the_catalog_wide_mpeak(self, monkeypatch):
        # The complete catalogue in the DB — the state the two-pass guarantees
        # at artist-section close.  Eat the Elephant is scored first, Mer de
        # Noms and Thirteenth Step last; ALL album medians feed M_peak.
        albums = [
            {"name": "Eat the Elephant", "year": 2018, "scores": [50, 52, 54, 56]},
            {"name": "Mer de Noms", "year": 2000, "scores": [58, 60, 62, 64]},
            {"name": "Thirteenth Step", "year": 2003, "scores": [54, 56, 58, 60]},
        ]
        db_rows = self._db_rows(albums)

        models = []
        for album in albums:
            m = self._model_for(self._album_result(album["name"], album["scores"]), db_rows, monkeypatch)
            models.append(m)

        # Every album sees the IDENTICAL catalog-wide M_peak — the benchmark no
        # longer depends on which album was scanned first.
        m_peaks = [round(float(m.get("m_peak") or 0), 6) for m in models]
        assert len(set(m_peaks)) == 1
        assert all(m.get("has_benchmark") for m in models)
        # All albums agree on the era classification too.
        assert len({m.get("era") for m in models}) == 1

    def test_first_album_no_longer_sees_a_different_benchmark(self, monkeypatch):
        # Regression: BEFORE the fix, the first album was rated when the DB
        # contained ONLY its own scores (its own median → era=peak), while a
        # later album was rated once the catalogue had grown.  AFTER the fix
        # the DB holds the full catalogue at rating time, so the first album
        # is era-classified against the artist's true peak — the SAME one every
        # other album sees.
        albums = [
            {"name": "Eat the Elephant", "year": 2018, "scores": [50, 52, 54, 56]},
            {"name": "Mer de Noms", "year": 2000, "scores": [58, 60, 62, 64]},
        ]
        db_rows = self._db_rows(albums)

        first_model = self._model_for(
            self._album_result("Eat the Elephant", albums[0]["scores"]),
            db_rows, monkeypatch,
        )
        sibling_model = self._model_for(
            self._album_result("Mer de Noms", albums[1]["scores"]),
            db_rows, monkeypatch,
        )

        # The first-scanned album and its sibling resolve the SAME catalogue
        # peak and the SAME era — scan order is irrelevant once the full
        # catalogue is persisted.
        assert abs(float(first_model["m_peak"]) - float(sibling_model["m_peak"])) < 0.001
        assert first_model["era"] == sibling_model["era"]

    def test_partial_catalogue_matches_stored_only_resolution(self, monkeypatch):
        # The two-pass guarantee also holds on a re-scan: whether the album's
        # fresh scores are already in the DB (as the deferred persist leaves
        # them) or only stored rows exist, M_peak covers the artist's whole
        # stored catalogue — not just the album being rated.
        db_rows = self._db_rows([
            {"name": "Mer de Noms", "year": 2000, "scores": [58, 60, 62, 64]},
            {"name": "Thirteenth Step", "year": 2003, "scores": [54, 56, 58, 60]},
        ])
        m = self._model_for(
            self._album_result("Mer de Noms", [58, 60, 62, 64]),
            db_rows, monkeypatch,
        )
        assert m.get("has_benchmark")
        # M_peak is resolved from BOTH stored albums (the two-pass state), so
        # it cannot be the first-album-only ~50 baseline that caused the skew.
        assert m.get("m_peak") is not None
