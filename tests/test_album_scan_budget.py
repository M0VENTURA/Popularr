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


# ---------------------------------------------------------------------------
# REGRESSION: the wrapper must be UNWRAPPED, or a completed album is discarded
# ---------------------------------------------------------------------------
#
# THE REPORT
# ----------
# A full scan logged every track's score AND its Single verdict, then:
#
#     [POPULARITY] Album 'Ricky Martin - 17: Greatest Hits' skipped (did not
#     complete) — continuing with the artist's remaining albums
#     Popularity Scan - All albums were skipped (recently scanned or up to date).
#     [FINALISE_STAGE] Finalising scan — 0 tracks processed
#
# WHY THAT HAPPENED
# -----------------
# ``_bounded_call_report`` has TWO return conventions:
#
#     seconds is None    -> the function's RAW result
#     seconds is a value -> a WRAPPER {"ok", "result", "abandoned", ...}
#
# ``_bounded_album_phase`` forwarded ``seconds`` straight through, so the moment
# a per-album budget was configured — it is by DEFAULT — every call site began
# receiving the wrapper instead of the phase's result:
#
#   * ``album_result = wrapper`` -> ``.get("detected_album_type")`` is None, so
#     the log read "Album enriched: ... (type=None)".
#   * ``_track_results_ordered = wrapper`` -> not a list, so the OLD
#     ``_track_phase_is_incomplete`` (``return not isinstance(result, list)``)
#     reported "did not complete" for a phase that had finished PERFECTLY.  The
#     album was skipped, its tracks were never appended to ``results``, and
#     SINGLE DETECTION NEVER RAN during a full scan.
#
# These tests CALL the helpers, because a source-text assertion cannot catch a
# neutered branch (``if False`` leaves the text intact).

class TestTheAlbumPhaseUnwrapsTheBudgetReport:
    """Behavioural, end-to-end over the real helpers and the real budget."""

    @staticmethod
    def _phase_result() -> dict:
        """What ``enrich_album`` actually returns (note: NOT a budget report)."""
        return {
            "detected_album_type": "album+compilation",
            "album": {"id": 1},
        }

    def test_a_configured_budget_still_returns_the_phases_own_result(self):
        """The whole bug in one assertion: budget ON must not change the shape."""
        import services.popularity.scan_stage_runner as runner

        result = runner._bounded_album_phase(
            self._phase_result,
            seconds=900.0,
            section="enrich_album",
            log_context={"artist": "A", "album": "B"},
        )

        assert isinstance(result, dict), (
            "a finished phase must return its own result, not a wrapper"
        )
        assert result.get("detected_album_type") == "album+compilation", (
            "with a budget configured the album type must survive, or the log "
            "reads '(type=None)' and the caller cannot use the enrichment"
        )
        assert "abandoned" not in result, (
            "a completed call must NOT look like an abandonment"
        )

    def test_a_disabled_budget_returns_the_same_thing(self):
        """``seconds=None`` (album_timeout_seconds=0) must agree with the above."""
        import services.popularity.scan_stage_runner as runner

        result = runner._bounded_album_phase(
            self._phase_result,
            seconds=None,
            section="enrich_album",
            log_context={},
        )
        assert result == self._phase_result(), (
            "0/disabled must behave exactly like a configured budget that did "
            "not fire — one code path, one shape"
        )

    def test_the_disabled_path_is_decided_by_seconds_not_by_the_payload_shape(self):
        """``seconds is None`` IS the discriminator; the shape is only a guard.

        With the budget DISABLED, ``_bounded_call_report`` returns whatever the
        phase produced — so a phase result that merely CONTAINS an ``abandoned``
        key (documentation, a nested report, a future phase's own bookkeeping)
        must be passed through untouched. Sniffing the payload instead of
        ``seconds`` would misread it as a budget abandonment and discard the
        album's real result — the same class of bug this whole change fixes.
        """
        import services.popularity.scan_stage_runner as runner

        def _phase_that_happens_to_report_abandoned() -> dict:
            return {
                "detected_album_type": "album",
                "abandoned": True,          # phase's OWN data, not a budget report
                "reason": "inner sub-task gave up",
            }

        result = runner._bounded_album_phase(
            _phase_that_happens_to_report_abandoned,
            seconds=None,
            section="enrich_album",
            log_context={},
        )

        assert result.get("detected_album_type") == "album", (
            "with the budget disabled the phase result must come back whole — "
            "discriminating on the payload shape would silently drop it"
        )
        assert result.get("reason") == "inner sub-task gave up", (
            "the phase's own reason must survive; it is not the budget's"
        )

    def test_a_completed_track_phase_is_not_incomplete_with_a_budget(self):
        """The regression proper: this is what killed single detection."""
        import services.popularity.scan_stage_runner as runner

        track_results = [{"track_id": 1, "score": 10.0}, {"track_id": 2, "score": 20.0}]

        def _track_phase(**_kw):
            return track_results

        result = runner._bounded_album_phase(
            _track_phase,
            track_jobs=[],
            max_workers=1,
            artist="A",
            album="B",
            seconds=900.0,
            section="album_track_phase",
            log_context={"artist": "A", "album": "B"},
        )

        assert isinstance(result, list), (
            "the track phase's list must come back as a list even when a "
            "budget is configured"
        )
        assert runner._track_phase_is_incomplete(result) is False, (
            "a track phase that FINISHED must never be reported incomplete — "
            "that is what skipped every album and discarded every Single "
            "verdict during a full scan"
        )

    def test_a_phase_that_exceeds_its_budget_is_still_reported(self):
        """The unwrap must not swallow a genuine abandonment."""
        import time

        import services.popularity.scan_stage_runner as runner

        def _hang():
            time.sleep(5)
            return {"detected_album_type": "album"}

        result = runner._bounded_album_phase(
            _hang,
            seconds=0.2,
            section="enrich_album",
            log_context={},
        )

        abandoned, reason = runner._album_phase_was_abandoned(result)
        assert abandoned is True, (
            "an album that really blows its budget must still be flagged, or "
            "the hang is no longer isolated to that album"
        )
        assert reason, "the abandonment must carry a reason for the log"

    def test_an_abandoned_track_phase_is_incomplete_and_a_finished_one_is_not(self):
        """Pin the contract against the shapes the helper actually receives.

        ``_execute_track_jobs_safely`` ALWAYS returns a list, so a non-list means
        the phase produced no results.  Two shapes reach here after the unwrap:
        the budget's ``abandoned`` sentinel, and ``None``/``{}`` from a call that
        RAISED.
        """
        import services.popularity.scan_stage_runner as runner

        assert runner._track_phase_is_incomplete(
            {"ok": False, "abandoned": True, "reason": "exceeded 900.0s budget"}
        ) is True, "an abandoned phase must skip only its own album"
        assert runner._track_phase_is_incomplete([{"score": 1}]) is False, (
            "a completed phase returns its list and must never be skipped"
        )
        assert runner._track_phase_is_incomplete([]) is False, (
            "an album with no eligible tracks is COMPLETE, not incomplete"
        )

    def test_a_failed_track_phase_is_also_incomplete(self):
        """⚠️ Found by probing: the failure shapes must still skip their album.

        This is NOT the same as the regression.  The caller's next step is
        ``zip(_track_jobs, _track_results_ordered)``, which raises ``TypeError``
        against ``None`` and silently yields ZERO pairs against ``{}`` — so
        classifying a failed phase as "complete" either unwinds the album loop or
        drops every track without a skip.  ``_bounded_album_phase`` unwraps a
        failure to ``report["result"]``, which is exactly ``None``.
        """
        import services.popularity.scan_stage_runner as runner

        assert runner._track_phase_is_incomplete(None) is True, (
            "a RAISED call unwraps to None; treating it as complete crashes the "
            "album loop on zip(None)"
        )
        assert runner._track_phase_is_incomplete({}) is True, (
            "the seconds-is-None failure path returns {}; treating it as "
            "complete silently drops every track of the album"
        )
        assert runner._track_phase_is_incomplete(
            {"ok": False, "result": None, "abandoned": False, "reason": "boom"}
        ) is True, "a failed (not abandoned) budget report must still skip"

    def test_a_failed_track_phase_actually_reaches_the_skip(self):
        """The end-to-end version: a RAISING phase must be reported incomplete."""
        import services.popularity.scan_stage_runner as runner

        def _explodes(**_kw):
            raise RuntimeError("track phase blew up")

        for seconds in (900.0, None):
            result = runner._bounded_album_phase(
                _explodes,
                track_jobs=[1, 2],
                max_workers=1,
                artist="A",
                album="B",
                seconds=seconds,
                section="album_track_phase",
                log_context={},
            )
            assert runner._track_phase_is_incomplete(result) is True, (
                f"seconds={seconds}: a phase that raised must skip its album, "
                "not be zipped against None"
            )


class TestTheArtistCallSiteToleratesADisabledBudget:
    """The SAME defect class, one level up, found by probing the artist loop.

    ``popularity_pipeline`` does::

        _artist_report = _bounded_call_report(_run_one_artist, seconds=...)
        if _artist_report.get("ok"):

    ``_run_one_artist`` returns ``None``, and ``_bounded_call_report`` returns
    the RAW result when ``seconds is None``.  So with
    ``artist_timeout_seconds = 0`` — the Config page's own "disable the timeout"
    setting — ``_artist_report`` was ``None`` and the whole artist loop died on
    the FIRST artist with ``AttributeError: 'NoneType' object has no attribute
    'get'``.
    """

    def test_the_independent_case_crashes_without_the_guard(self):
        """Documents the failure the guard exists to prevent."""
        import services.popularity.scan_stage_runner as runner

        raw = runner._bounded_call_report(lambda: None, seconds=None, label="x")
        assert raw is None, "seconds=None returns the raw result, here None"
        with pytest.raises(AttributeError):
            raw.get("ok")  # type: ignore[union-attr]

    def test_the_pipeline_normalises_a_missing_report(self):
        """The fix must be present as a CHECK, not merely as text."""
        source = PIPELINE.read_text(encoding="utf-8")
        idx = source.index("_artist_report = _bounded_call_report(")
        window = source[idx: idx + 1600]
        assert "isinstance(_artist_report, dict)" in window, (
            "the artist call site must normalise a non-dict report, or "
            "artist_timeout_seconds=0 crashes the full scan on artist #1"
        )
        # Anchor the fallback INSIDE the same window so an unrelated occurrence
        # elsewhere in the file cannot satisfy this.
        assert '"ok": True' in window, (
            "a missing report means the artist ran and did not raise"
        )
        assert "abandoned" in window, (
            "the normalised report must satisfy the abandonment read below it"
        )


class TestTheMissingTrackSnapshotIsNotStarvedByASkip:
    """The SECOND reported symptom: "the missing tracks were also missing after the scan".

    SAME root cause as the phase bug above.  ``get_missing_tracks`` refreshes
    the ``missing_album_tracks`` snapshot, and it runs at the END of the
    per-album loop body — while the skip ``continue`` fires EARLY.  So a
    wrongly-abandoned album never refreshed its snapshot, the album page kept
    reading the stale rows, and a prompt re-scan was then skipped as "recently
    scanned" — leaving the tracks reported missing indefinitely.

    Fixing the unwrap is what actually restores this.  These tests pin the
    REACHABILITY so a future edit cannot re-couple the snapshot to the skip path,
    and they document why the request path reads the snapshot instead of
    recomputing it.

    NOTE: the scan is no longer the snapshot's only writer — the album page's
    Compare now persists its own findings too (so they survive a reload until
    saved or discarded).  ``test_the_snapshot_has_exactly_two_writers`` states
    that contract; the one thing that must stay true is that a PAGE LOAD never
    recomputes.
    """

    def test_the_snapshot_runs_in_the_normal_path_not_the_skip_branch(self):
        """A COMPLETED album must refresh the snapshot — that is the whole point.

        The snapshot is the only writer of ``missing_album_tracks``, and the
        album it most needs to describe correctly is one that finished.  If the
        call lived inside the abandon/skip branch it would run only for albums
        that were cut short, so a normal album would never refresh its rows.

        This is a WIRING assertion (the call is positioned in the non-skipped
        part of the loop body), which is what source checks are for.  The
        behavioural half — that a completed phase is NOT skipped, and therefore
        that this call is actually reached — is covered by
        ``TestTheAlbumPhaseUnwrapsTheBudgetReport`` above.
        """
        source = SCANNER.read_text(encoding="utf-8")

        skip_idx = source.index("if _track_phase_is_incomplete(_track_results_ordered):")
        skip_window = source[skip_idx: skip_idx + 1400]
        assert "continue" in skip_window, (
            "the incomplete-phase branch must abort this album only"
        )
        assert "get_missing_tracks(" not in skip_window, (
            "the snapshot must not live in the abandon/skip branch, or a "
            "normally-completed album would never refresh it"
        )

        snapshot_idx = source.index("_missing = get_missing_tracks(artist=artist, album=album)")
        assert snapshot_idx > skip_idx, (
            "the snapshot must run in the normal path of the album loop, i.e. "
            "after the skip branch — not before it"
        )

    def test_the_snapshot_is_gated_only_by_the_scan_type(self):
        """It must not be gated on the album being skipped or on performance.

        The gate is the scan MODE: metadata and combined passes refresh album
        identity, popularity-only and singles-only passes deliberately do not.
        A per-page-load recompute was removed on purpose (see the endpoint's
        docstring), so the scan is the sole maintainer of this table.
        """
        source = SCANNER.read_text(encoding="utf-8")
        idx = source.index("_missing = get_missing_tracks(artist=artist, album=album)")
        window = source[idx - 500: idx]

        assert 'not options.get("popularity_only")' in window, (
            "the snapshot gate must still exclude popularity-only scans"
        )
        assert 'not options.get("singles_detection_only")' in window, (
            "the snapshot gate must still exclude singles-only scans"
        )
        assert "_track_phase_is_incomplete" not in window, (
            "the snapshot must not be re-coupled to the track-phase verdict"
        )

    def test_the_endpoint_stays_database_only(self):
        """Pins WHY the scan is the sole writer, so nobody 'optimises' the
        snapshot back into the request path (one MusicBrainz call per owned
        album per artist-page load)."""
        routes = (
            REPO_ROOT / "routes" / "album_routes.py"
        ).read_text(encoding="utf-8")
        idx = routes.index("def api_album_missing_tracks")
        window = routes[idx: idx + 1400]
        assert "get_missing_tracks_from_db" in window, (
            "the endpoint must read the persisted snapshot"
        )
        assert "get_missing_tracks(" not in window, (
            "the endpoint must NOT recompute — that is what made a page load "
            "fire a MusicBrainz release fetch per owned album"
        )

    def test_the_snapshot_has_exactly_two_writers(self):
        """ⓘ THIS TEST WAS DELIBERATELY CHANGED — THE INVARIANT MOVED.

        It used to assert that the SCAN was the snapshot's ONLY writer, so that
        "when does this go stale?" had one answer. The product requirement then
        changed: findings shown after a metadata import or an album-page
        release match must PERSIST until the user saves or discards them. That
        requires the album page to write the snapshot too, because the scan is
        not what produced those findings and the page cannot wait for one.

        So the invariant is now: ONE SQL writer, TWO considered callers —

          * ``get_missing_tracks`` ......... the SCAN's refresh
          * ``persist_missing_from_comparison``  the album page's Compare

        The dangerous case (a request-path recompute that fired a MusicBrainz
        release fetch on every page load) is still excluded, and is pinned by
        ``test_the_endpoint_stays_database_only`` above. This test pins the
        remaining risk: a THIRD writer appearing unnoticed, and a second
        caller being added to the scan path.
        """
        svc = (
            REPO_ROOT / "services" / "metadata" / "album_missing_service.py"
        ).read_text(encoding="utf-8")

        # The only place rows are actually written. Both callers funnel here,
        # so the DELETE/INSERT pair stays in one function and cannot drift.
        assert svc.count("INSERT INTO missing_album_tracks") == 1, (
            "the snapshot must have exactly one INSERT site"
        )
        assert svc.count("DELETE FROM missing_album_tracks") == 1, (
            "the snapshot must have exactly one DELETE site"
        )

        # Exactly two CALLERS, counted as calls to the function rather than one
        # exact argument spelling. Counting the string
        # "_persist_missing_tracks(artist, album, missing)" was mutation-tested
        # and a THIRD writer passed different arguments (`... (artist, album,
        # [])`), which evaded the guard entirely. Counting the call itself
        # cannot be evaded that way.
        call_sites = (
            svc.count("_persist_missing_tracks(")
            - svc.count("def _persist_missing_tracks(")
        )
        assert call_sites == 2, (
            f"expected exactly 2 snapshot writers, found {call_sites}: the "
            "scan's get_missing_tracks and the album page's "
            "persist_missing_from_comparison — a third writer changes when the "
            "snapshot goes stale and must be a deliberate decision"
        )
        for expected_caller in (
            "def get_missing_tracks(",
            "def persist_missing_from_comparison(",
        ):
            assert expected_caller in svc, (
                f"{expected_caller} is one of the two intended writers"
            )

        # The comparison writer must reuse the comparison it was handed rather
        # than re-deriving the missing set (which would re-pay the throttled
        # MusicBrainz release lookup the album page already made).
        idx = svc.index("def persist_missing_from_comparison(")
        # Bound the window by the NEXT definition, not a fixed character count,
        # so a longer docstring cannot silently push the body out of the window
        # and turn this into a vacuous pass.
        end = svc.index("def get_missing_tracks_from_db(", idx)
        window = svc[idx:end]
        assert "mb_title" in window, (
            "it must translate the comparison's mb_* keys, which is the "
            "already-fetched data"
        )
        assert "get_missing_tracks(" not in window, (
            "it must NOT re-derive via get_missing_tracks — that is the "
            "duplicate MusicBrainz fetch this path exists to avoid"
        )

        scanner = SCANNER.read_text(encoding="utf-8")
        assert scanner.count("get_missing_tracks(artist=artist, album=album)") == 1, (
            "the scan must still refresh the snapshot from exactly one place"
        )


class TestTheZeroTrackSummaryTellsTheTruth:
    """The message the user actually saw, and acted on.

    The reported run ended with::

        Popularity Scan - All albums were skipped (recently scanned or up to
        date). Run in Forced mode to rescan.

    Every album had in fact been ABANDONED on its per-album budget, not found
    up to date — and the advice was useless, because ``force`` bypasses only the
    freshness check::

        if not force and not album_filter:      # <- the up-to-date skip
            ...
        skip_album = ...                        # forced mode sets this False

    The budget check runs regardless, so a forced rescan abandons the same
    albums again. That is why a re-scan changed nothing and the missing tracks
    stayed missing. ``skipped_albums`` conflates the two outcomes, so the
    summary could not distinguish them.
    """

    def test_an_abandoned_run_does_not_claim_the_albums_were_up_to_date(self):
        import services.popularity.scan_stage_runner as runner

        msg = runner._zero_track_summary(abandoned_albums=3, skipped_albums=3)

        assert "recently scanned or up to date" not in msg, (
            "claiming the albums were up to date is factually wrong when they "
            "were abandoned on budget"
        )
        assert "3" in msg, "the abandon count must be reported"
        assert "budget" in msg.lower(), (
            "the summary must name the real cause so it is actionable"
        )
        assert "Forced mode does NOT bypass the budget" in msg, (
            "the old advice sent the user to a rescan that cannot help; the "
            "summary must say so explicitly"
        )

    def test_a_genuinely_up_to_date_run_keeps_the_original_message(self):
        import services.popularity.scan_stage_runner as runner

        msg = runner._zero_track_summary(abandoned_albums=0, skipped_albums=5)

        assert "recently scanned or up to date" in msg, (
            "when nothing was abandoned the original message is correct"
        )
        assert "Forced mode" in msg, (
            "and so is its advice — forced mode really does bypass the "
            "freshness check"
        )
        assert "budget" not in msg.lower(), "no budget claim when none applied"

    def test_the_summary_names_the_real_config_label(self):
        """The advice must name the control the user can actually find.

        The Config page calls it **"Per-Album Scan Timeout (seconds)"** — the
        label rendered for ``popularity.album_timeout_seconds``. That label is
        asserted against BOTH template trees so the message cannot drift into
        naming a control that does not exist (the user would search for a
        phrase the page never shows).
        """
        import services.popularity.scan_stage_runner as runner

        msg = runner._zero_track_summary(3, 3)
        assert "Per-Album Scan Timeout" in msg, (
            "the advice must use the Config page's own label"
        )

        for rel in (
            "templates/pages/config.html",
            "test_site/templates/Pages/config.html",
        ):
            html = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "Per-Album Scan Timeout (seconds)" in html, (
                f"{rel}: the label the summary refers to must exist on the "
                "Config page"
            )
            assert 'id="pop_album_timeout_seconds"' in html, (
                f"{rel}: and it must be bound to album_timeout_seconds"
            )

    def test_forced_mode_really_cannot_bypass_the_budget(self):
        """Pins the FACT behind the corrected advice, so the wording cannot be
        'simplified' back into an untrue statement."""
        source = SCANNER.read_text(encoding="utf-8")
        # The up-to-date skip is the only thing gated on `force`.
        skip_idx = source.index("if not force and not album_filter:")
        assert skip_idx, "the freshness skip must remain gated on force"
        # The budget is resolved unconditionally, outside any `force` guard.
        budget_idx = source.index("_album_budget = _resolve_album_budget()")
        assert budget_idx < skip_idx, (
            "the per-album budget must be resolved for the whole scan, not "
            "only when NOT forced — otherwise the advice would be true"
        )

    def test_the_abandon_count_is_reported_additively(self):
        """The counters feed the summary AND the run result; a second
        increment site must be counted or the message under-reports."""
        source = SCANNER.read_text(encoding="utf-8")
        assert source.count("abandoned_albums += 1") == 2, (
            "both abandon sites must count: the enrich phase and the track "
            "phase (they are separate `_bounded_album_phase` call sites)"
        )
        assert source.count("skipped_albums += 1") == 3, (
            "the up-to-date skip plus the two abandon sites"
        )
        assert '"abandoned_albums": abandoned_albums' in source, (
            "the additive result key lets the dashboard distinguish the cases"
        )



