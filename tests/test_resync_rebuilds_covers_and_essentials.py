"""Does "Apply & Rebuild Playlists" rebuild covers and Essential Collections?

Answer (pinned here): YES — the apply rebuilds three families:

  1. the genre top-tracks lists           (``_create_genre_top_track_playlists``)
  2. the covers family                    (``_create_covers_playlist``, gated on
                                           ``playlists.covers_playlist_enabled``)
  3. the Essential Collections            (``_refresh_all_essential_collections``,
                                           gated on
                                           ``playlists.essential_playlists_enabled``)

Reported separately: *"the covers collection still has 3 star songs in it after
running the rebuild"*.

The covers floor is real and configurable (``covers_min_stars``, default 4) and
``_create_covers_playlist`` applies it twice — in the fetch and again as a
post-filter. So the 3★ tracks must have survived because the playlists were never
actually written. The mechanism is ``services/navidrome/import_gate``:
``sync_playlist_by_name`` DEFERS every playlist write while a Navidrome import
runs and returns ``skipped`` — and ``_playlist_sync_succeeded`` counts ``skipped``
as success, so the job reported a full rebuild having written nothing.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RESYNC = REPO_ROOT / "services" / "enrichment" / "genre_playlist_resync_service.py"
FINALISE = REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
SYNC_SVC = REPO_ROOT / "services" / "playlists" / "playlist_navidrome_service.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheApplyRebuildsAllThreeFamilies:
    def _apply_body(self) -> str:
        source = _src(RESYNC)
        start = source.index("def apply_genre_playlist_resync(")
        return source[start:]

    def test_genre_top_tracks(self):
        assert "_create_genre_top_track_playlists(cleanup_orphans=True)" in self._apply_body()

    def test_covers(self):
        body = self._apply_body()
        assert "_create_covers_playlist(" in body
        assert 'covers_playlist_enabled' in body, "covers has its own enable gate"

    def test_essential_collections(self):
        body = self._apply_body()
        assert "_refresh_all_essential_collections(" in body
        assert 'essential_playlists_enabled' in body

    def test_the_covers_floor_is_four_by_default(self):
        from services.popularity.stages.finalise_stage import _covers_min_stars

        assert _covers_min_stars({}) == 4
        assert _covers_min_stars({"covers_min_stars": 4}) == 4
        # 0 is a deliberate "no floor", not a default.
        assert _covers_min_stars({"covers_min_stars": 0}) == 0

    def test_the_covers_builder_applies_the_floor_even_when_rows_are_supplied(self):
        """The scan path passes one 3★ superset pool to every curated builder."""
        body = _src(FINALISE)
        start = body.index("def _create_covers_playlist(")
        block = body[start:start + 2000]
        assert "_min_stars = _covers_min_stars(cfg)" in block
        assert "row for row in rows" in block
        assert "if int(row.get(\"stars\") or 0) >= _min_stars" in block, (
            "passing `rows` in used to skip the covers floor entirely — the "
            "audit found 148 cover memberships below four stars"
        )


class TestTheImportGateSilentlyVoidedTheRebuild:
    def test_a_deferred_sync_counts_as_success(self):
        """This is the trap: 'skipped' is reported as a successful sync."""
        from services.popularity.stages.finalise_stage import _playlist_sync_succeeded

        assert _playlist_sync_succeeded({"skipped": 1, "updated": 0}) is True

    def test_the_sync_defers_while_an_import_runs(self):
        body = _src(SYNC_SVC)
        start = body.index("def sync_playlist_by_name(")
        block = body[start:start + 3200]
        assert "is_import_running()" in block
        assert 'result["skipped"] = True' in block

    def test_the_resync_waits_for_the_import_before_rebuilding(self):
        body = _src(RESYNC)
        wait_call = body.index("_wait_for_navidrome_import()")
        covers_call = body.index("_create_covers_playlist(")
        assert wait_call < covers_call, (
            "the wait must come BEFORE the playlist rebuild, or every sync in it "
            "is deferred and the playlists keep their old contents"
        )


class TestTheWaitHelper:
    def _svc(self):
        from services.enrichment import genre_playlist_resync_service

        return genre_playlist_resync_service

    def _patch(self, monkeypatch, svc, *, verdicts, quiet=True):
        """Feed a sequence of gate verdicts and a fake clock."""
        seq = list(verdicts)

        class _FakeTime:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += max(0.01, float(seconds))

        fake = _FakeTime()
        monkeypatch.setattr(svc, "time", fake)
        monkeypatch.setattr(svc, "_write_progress", lambda **kw: None)

        calls = {"n": 0}

        def _gate():
            calls["n"] += 1
            return seq[min(calls["n"] - 1, len(seq) - 1)]

        import services.navidrome.import_gate as gate

        monkeypatch.setattr(gate, "is_import_running_uncached", _gate)
        return fake, calls

    def test_no_import_returns_immediately(self, monkeypatch):
        svc = self._svc()
        _fake, calls = self._patch(monkeypatch, svc, verdicts=[False])
        assert svc._wait_for_navidrome_import(timeout_seconds=60) is True
        assert calls["n"] == 1

    def test_a_running_import_is_waited_out(self, monkeypatch):
        svc = self._svc()
        _fake, calls = self._patch(
            monkeypatch, svc, verdicts=[True, True, False]
        )
        assert svc._wait_for_navidrome_import(timeout_seconds=60) is True
        assert calls["n"] == 3

    def test_it_gives_up_at_the_timeout(self, monkeypatch):
        svc = self._svc()
        _fake, _calls = self._patch(monkeypatch, svc, verdicts=[True])
        assert svc._wait_for_navidrome_import(timeout_seconds=0) is False

    def test_a_broken_gate_fails_open(self, monkeypatch):
        svc = self._svc()

        import services.navidrome.import_gate as gate

        def _boom():
            raise RuntimeError("gate unavailable")

        monkeypatch.setattr(gate, "is_import_running_uncached", _boom)
        monkeypatch.setattr(svc, "_write_progress", lambda **kw: None)
        assert svc._wait_for_navidrome_import(timeout_seconds=60) is True


class TestADeferralIsReportedNotHidden:
    def test_the_result_carries_the_deferral(self):
        body = _src(RESYNC)
        assert 'result["playlists_deferred"] = True' in body
        assert 'result["warning"]' in body

    def test_the_progress_line_says_deferred(self):
        body = _src(RESYNC)
        assert "PLAYLISTS DEFERRED (import running)" in body

    def test_the_ui_renders_the_warning(self):
        js = _src(REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js")
        assert "_resyncDeferredNote" in js
        assert "playlists_deferred" in js
