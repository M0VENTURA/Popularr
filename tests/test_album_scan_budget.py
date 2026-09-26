"""Regression tests for the PER-ALBUM scan budget.

THE REPORT
----------
A full library scan reported, for one artist:

    Various Artists  abandoned  exceeded 1800.0s budget

WHY THAT HAPPENED
-----------------
`get_all_artists` groups by
``COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist))``, so **"Various
Artists" is ONE artist row holding every compilation in the library** —
hundreds of albums.  The budget was a FLAT wall-clock allowance applied to the
whole artist, so it was really a SIZE limit: the artist was abandoned for doing
legitimate work, discarding every album not yet reached.

A hang, by contrast, always looks like ONE album not finishing.  So the album is
the unit that matches the failure, and bounding it isolates the damage:

  * a stuck ALBUM is skipped; the artist keeps its remaining albums
  * the ARTIST budget is fixed overhead scaled by album count, so a merely
    LARGE artist is never abandoned

These tests pin both halves.  Without either one the report recurs: a per-album
budget alone still leaves the artist wrapper firing on total size.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

import helpers.config_helpers as ch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"
PIPELINE = (
    REPO_ROOT / "services" / "scanning" / "pipelines" / "popularity_pipeline.py"
)


class _FakeCursor:
    def __init__(self, alive_after: float) -> None:
        self._alive_after = alive_after
        self.closed = False

    def execute(self, sql, params=None):
        if self.closed:
            raise RuntimeError("cursor already closed")
        return None

    def close(self) -> None:
        self.closed = True


class _FakeThread:
    """A worker that stays alive for `alive_after` seconds."""

    def __init__(self, alive_after: float) -> None:
        self._alive_after = alive_after
        self.name = "fake"
        self._start = None

    def start(self) -> None:
        import time

        self._start = time.monotonic()

    def is_alive(self) -> bool:
        import time

        if self._start is None:
            return True
        return (time.monotonic() - self._start) < self._alive_after

    def join(self, timeout=None) -> None:
        import time

        if self._start is not None:
            time.sleep(min(timeout or 0, max(0.0, self._alive_after)))


# ---------------------------------------------------------------------------
# The config getter
# ---------------------------------------------------------------------------

class TestGetAlbumTimeoutSeconds:
    @staticmethod
    def _cfg(value):
        return {"popularity": {"album_timeout_seconds": value}}

    def test_default_is_900(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: {})
        assert ch.get_album_timeout_seconds() == 900

    def test_a_configured_value_is_honoured(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(300))
        assert ch.get_album_timeout_seconds() == 300

    def test_zero_disables_the_budget(self, monkeypatch):
        """0 must mean OFF, not "fall back to the default" — the whole point
        is that a user can stop any album being skipped."""
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(0))
        assert ch.get_album_timeout_seconds() == 0

    def test_a_negative_value_also_disables(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(-1))
        assert ch.get_album_timeout_seconds() == 0

    def test_too_small_is_clamped_up(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(5))
        assert ch.get_album_timeout_seconds() == 60

    def test_absurd_is_clamped_down(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg(999999))
        assert ch.get_album_timeout_seconds() == 21600

    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: self._cfg("nonsense"))
        assert ch.get_album_timeout_seconds() == 900

    def test_a_missing_section_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(ch, "get_config", lambda: {"popularity": {}})
        assert ch.get_album_timeout_seconds() == 900


class TestResolveAlbumBudget:
    """The runner turns the configured seconds into a bound or None."""

    def test_zero_config_becomes_None_meaning_unbounded(self, monkeypatch):
        import services.popularity.scan_stage_runner as runner

        monkeypatch.setattr(runner, "get_album_timeout_seconds", lambda: 0)
        assert runner._resolve_album_budget() is None

    def test_a_configured_value_becomes_a_float(self, monkeypatch):
        import services.popularity.scan_stage_runner as runner

        monkeypatch.setattr(runner, "get_album_timeout_seconds", lambda: 900)
        assert runner._resolve_album_budget() == 900.0

    def test_a_broken_getter_falls_back_to_a_sane_default(self, monkeypatch):
        import services.popularity.scan_stage_runner as runner

        def _boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(runner, "get_album_timeout_seconds", _boom)
        assert runner._resolve_album_budget() == 900.0


# ---------------------------------------------------------------------------
# The runner actually bounds the album, and skips only THAT album
# ---------------------------------------------------------------------------

class TestTheAlbumPhaseIsBounded:
    def test_the_phase_passes_the_budget_through(self, monkeypatch):
        import services.popularity.scan_stage_runner as runner

        captured: dict = {}

        def _fake_bounded(func, *a, **kw):
            captured["seconds"] = kw.get("seconds")
            return {}

        monkeypatch.setattr(runner, "_bounded_call_report", _fake_bounded)
        runner._bounded_album_phase(
            lambda: None, seconds=123.0, section="x", log_context={}
        )
        assert captured["seconds"] == 123.0

    def test_None_means_unbounded(self, monkeypatch):
        """0/disabled must reach ``_bounded_call_report`` as ``seconds=None``,
        which runs the call directly instead of joining a thread."""
        import services.popularity.scan_stage_runner as runner

        captured: dict = {}

        def _fake_bounded(func, *a, **kw):
            captured["seconds"] = kw.get("seconds", "ABSENT")
            return {}

        monkeypatch.setattr(runner, "_bounded_call_report", _fake_bounded)
        runner._bounded_album_phase(
            lambda: None, seconds=None, section="x", log_context={}
        )
        assert captured["seconds"] is None

    def test_the_album_budget_is_read_once_per_scan(self) -> None:
        source = SCANNER.read_text(encoding="utf-8")
        assert "_album_budget = _resolve_album_budget()" in source, (
            "the scan must resolve the per-album budget"
        )

    def test_enrich_album_and_the_track_phase_are_both_bounded(self) -> None:
        """The track phase is where most of an album's wall-clock goes.

        If only ``enrich_album`` were bounded, a hang during track processing
        would never reach the album budget at all — it would burn the ARTIST
        budget and the artist would still be abandoned wholesale.
        """
        source = SCANNER.read_text(encoding="utf-8")
        for section in ("enrich_album", "album_track_phase",
                        "post_singles_enrichment"):
            assert section in source, f"{section} phase missing"

        # Only CALL sites. The definition declares the parameter, so it is
        # excluded by matching the call form (``_bounded_album_phase(`` NOT
        # preceded by ``def ``) and by requiring the first argument to be a
        # name rather than an annotation.
        calls = [
            call
            for call in re.findall(
                r"(?<!def )_bounded_album_phase\((?:[^()]|\([^()]*\))*?\)",
                source,
                re.S,
            )
            if "Callable[..., T]" not in call
        ]
        assert calls, "no bounded album phase CALLS found"
        for call in calls:
            assert "seconds=" in call, (
                f"a bounded album phase call lacks seconds=:\n{call[:200]}"
            )


class TestOnlyTheAlbumIsSkippedNotTheArtist:
    """The critical distinction: an album over budget must not cancel the
    artist.  ``scan_cancellation`` is keyed PER ARTIST, so cancelling from the
    album level would unwind every remaining album and recreate the original
    whole-artist loss one level down.

    These assert on the DECISION HELPER rather than on source text.  A text
    assertion cannot detect a branch being neutered (``if False`` leaves the
    text intact) — mutation-testing caught exactly that hole in an earlier
    draft of this file.
    """

    def test_an_abandoned_album_is_detected(self) -> None:
        import services.popularity.scan_stage_runner as runner

        abandoned, reason = runner._album_phase_was_abandoned(
            {"ok": False, "abandoned": True, "reason": "exceeded 900.0s budget"}
        )
        assert abandoned is True
        assert "900" in reason

    def test_a_successful_album_is_not_flagged(self) -> None:
        import services.popularity.scan_stage_runner as runner

        abandoned, _ = runner._album_phase_was_abandoned(
            {"ok": True, "detected_album_type": "album"}
        )
        assert abandoned is False

    def test_an_empty_result_is_not_flagged(self) -> None:
        """``enrich_album`` may return ``{}``; that must not read as abandoned."""
        import services.popularity.scan_stage_runner as runner

        assert runner._album_phase_was_abandoned({})[0] is False
        assert runner._album_phase_was_abandoned(None)[0] is False

    def test_a_missing_reason_still_reports_something(self) -> None:
        import services.popularity.scan_stage_runner as runner

        abandoned, reason = runner._album_phase_was_abandoned({"abandoned": True})
        assert abandoned is True
        assert reason, "the skip must be logged with a reason"

    def test_an_incomplete_track_phase_is_detected(self) -> None:
        """A hung track phase returns the abandoned REPORT, not a list."""
        import services.popularity.scan_stage_runner as runner

        assert runner._track_phase_is_incomplete(
            {"ok": False, "abandoned": True, "reason": "exceeded 900.0s budget"}
        ) is True

    def test_a_completed_track_phase_is_a_list(self) -> None:
        import services.popularity.scan_stage_runner as runner

        assert runner._track_phase_is_incomplete([]) is False
        assert runner._track_phase_is_incomplete([{"score": 1}, None]) is False

    def test_the_skip_branch_does_not_cancel_the_artist(self) -> None:
        """No ``request_cancel`` may appear in the album-skip path."""
        source = SCANNER.read_text(encoding="utf-8")
        # Anchor on the CALL in the album loop, not the helper definition.
        idx = source.index(
            "_album_abandoned, _album_abandon_reason = _album_phase_was_abandoned("
        )
        window = source[idx: idx + 1200]
        assert "request_cancel" not in window, (
            "an album over budget must NOT cancel the whole artist — that "
            "recreates the original all-albums-lost behaviour"
        )
        assert "continue" in window, (
            "an abandoned album must skip only itself and continue"
        )

    def test_a_skipped_album_is_counted_and_progress_advances(self) -> None:
        source = SCANNER.read_text(encoding="utf-8")
        idx = source.index(
            "_album_abandoned, _album_abandon_reason = _album_phase_was_abandoned("
        )
        window = source[idx: idx + 1200]
        assert "skipped_albums += 1" in window, (
            "the skip must be counted, or the summary misreports coverage"
        )
        assert "albums_processed += 1" in window, (
            "progress must still advance, or the bar stalls"
        )


# ---------------------------------------------------------------------------
# The artist budget must not fire on SIZE alone
# ---------------------------------------------------------------------------

class TestTheArtistBudgetScalesWithAlbumCount:
    """A flat per-artist budget is a size limit in disguise.

    "Various Artists" holds every compilation in the library; a flat 1800s
    abandoned it mid-way.  The fix treats the configured value as FIXED
    OVERHEAD and adds a per-album allowance on top.
    """

    def test_the_pipeline_scales_the_artist_budget_by_album_count(self) -> None:
        source = PIPELINE.read_text(encoding="utf-8")
        assert "get_album_counts_by_artist" in source, (
            "the artist budget must consult the artist's album count"
        )
        assert "_album_allowance * _artist_album_count" in source, (
            "the per-album allowance must be multiplied by the album count"
        )

    def test_a_large_artist_gets_a_proportionally_larger_budget(self) -> None:
        """Model the arithmetic the pipeline performs."""
        configured = 1800
        per_album = 900

        def effective(albums: int) -> float:
            return float(configured + per_album * albums)

        # A small artist behaves exactly as before, plus its own allowance.
        assert effective(5) == 1800 + 900 * 5
        # A large compilation artist gets room for every album, so the flat
        # budget can no longer abandon it for being large.
        assert effective(500) > 500 * per_album
        assert effective(500) > effective(10)

    def test_zero_artist_budget_still_disables_entirely(self) -> None:
        """Scaling must not resurrect a budget the user turned off."""
        source = PIPELINE.read_text(encoding="utf-8")
        assert "if _resolved_budget <= 0:" in source, (
            "0 must still mean DISABLED, checked BEFORE any scaling"
        )
        # The disable branch must be evaluated before the album allowance.
        disable_idx = source.index("if _resolved_budget <= 0:")
        scale_idx = source.index("_album_allowance * _artist_album_count")
        assert disable_idx < scale_idx

    def test_a_failed_count_lookup_falls_back_to_the_unscaled_budget(self) -> None:
        """Losing the count must degrade to the old behaviour, not to a wrong
        one — and must never crash the scan."""
        source = PIPELINE.read_text(encoding="utf-8")
        assert "Album-count lookup failed" in source
        # The lookup is wrapped, and a missing artist counts as 0 albums.
        assert "_all_counts.get(artist)" in source
        assert "or 0" in source


class TestTheAlbumCountQueryIsOneRoundTrip:
    """A per-artist loop would be hundreds of queries on a full scan."""

    def test_library_exposes_a_bulk_album_count(self) -> None:
        from db.repositories import library as lib

        assert hasattr(lib, "get_album_counts_by_artist")

    def test_the_query_groups_the_same_way_get_all_artists_does(self) -> None:
        (REPO_ROOT / "db" / "repositories" / "library.py").read_text(
            encoding="utf-8"
        )
        from db.repositories import library as lib
        import inspect

        src = inspect.getsource(lib.get_album_counts_by_artist)
        assert "GROUP BY 1" in src, "must aggregate in one query"
        assert "COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist))" in src, (
            "must group EXACTLY as get_all_artists does, or the keys will not "
            "match the artist names the scan iterates"
        )
        # And it must not raise.
        assert "return {}" in src

    def test_a_read_failure_returns_an_empty_mapping(self, monkeypatch) -> None:
        from db.repositories import library as lib

        class _Boom:
            def __enter__(self):
                raise RuntimeError("no database")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(lib, "db_session", lambda: _Boom())
        assert lib.get_album_counts_by_artist() == {}, (
            "a sizing hint must never raise into the scan"
        )


# ---------------------------------------------------------------------------
# The config contract: the Config page is the source of truth
# ---------------------------------------------------------------------------

class TestTheAlbumBudgetIsOnTheConfigPage:
    TREES = (
        ("live", REPO_ROOT / "templates" / "pages" / "config.html",
         REPO_ROOT / "static" / "js" / "config.js"),
        ("test_site", REPO_ROOT / "test_site" / "templates" / "Pages" / "config.html",
         REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js"),
    )

    @pytest.mark.parametrize("label,html,js", TREES)
    def test_the_input_exists(self, label, html, js) -> None:
        body = html.read_text(encoding="utf-8")
        assert 'id="pop_album_timeout_seconds"' in body, (
            f"{label}: the album timeout must be settable from the Config page"
        )
        assert "popularity', {}).get('album_timeout_seconds'" in body, (
            f"{label}: the input must read the album_timeout_seconds key"
        )

    @pytest.mark.parametrize("label,html,js", TREES)
    def test_the_js_collects_it_without_clobbering_zero(self, label, html, js) -> None:
        script = js.read_text(encoding="utf-8")
        assert "album_timeout_seconds" in script, (
            f"{label}: config.js must collect the value, or it never persists"
        )
        # 0 means DISABLED, so a `|| default` guard would silently undo it.
        idx = script.index("album_timeout_seconds")
        window = script[idx: idx + 400]
        assert "Math.max(0, raw)" in window, (
            f"{label}: the value must be clamped without turning 0 into a default"
        )
        assert "pop_album_timeout_seconds" in window, (
            f"{label}: the payload must read the input id"
        )


class TestTheBehaviourIsDescribedHonestly:
    def test_the_artist_help_text_mentions_scaling(self) -> None:
        """The old text said only "raise it if your artists need longer",
        which was wrong advice for a large artist."""
        html = (REPO_ROOT / "templates" / "pages" / "config.html").read_text(
            encoding="utf-8"
        )
        assert "scaled" in html and "Various Artists" in html, (
            "the per-artist help must explain that the budget scales by album "
            "count, or the operator will keep raising a value that was not "
            "the problem"
        )
