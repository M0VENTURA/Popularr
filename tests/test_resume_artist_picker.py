"""Resume-artist picker: which artist should a resumed scan start from?

Reported request
----------------
"When Starting a scan from the dashboard and restart isn't being selected, I want
a popup asking which artist to resume from. At the top of the list should be the
artist that was last scanned during a full scan, as well as the last three
artists that were manually scanned or had albums scanned. If that artist had
completed the scan (all albums scanned during that scan) it will continue from
the next artist. If it was interrupted half way through, it will restart that
artist and then continue the full scan."

The two cases that must differ
------------------------------
The scan loop skips every artist BEFORE ``resume_from`` and PROCESSES
``resume_from`` itself (``_run_full_scan_as_artist_pipeline``). That makes:

* interrupted  -> pass the artist itself  -> it is re-scanned from the top
* completed    -> pass the NEXT artist    -> the finished one is not redone

Getting this backwards silently re-scans finished artists (or skips work), so it
is asserted directly rather than left to the UI.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest


@pytest.fixture()
def svc():
    return importlib.import_module("services.scanning.resume_options_service")


# ---------------------------------------------------------------------------
# completed vs interrupted -> where the resume actually begins
# ---------------------------------------------------------------------------

@pytest.fixture()
def statuses(monkeypatch, svc):
    """Install a status table and a library order."""
    state: dict[str, str] = {}

    monkeypatch.setattr(svc, "_most_recent_status_by_artist", lambda: dict(state))
    monkeypatch.setattr(
        svc, "get_all_artists",
        lambda: ["Aardvark", "Beatles", "Coldplay", "Daft Punk"],
    )
    return state


class TestResolveResumeTarget:
    def test_completed_artist_resumes_from_the_next_one(self, svc, statuses):
        """The whole point: do not redo an artist that already finished."""
        statuses["Beatles"] = "completed"
        result = svc.resolve_resume_target("Beatles")
        assert result["mode"] == "next"
        assert result["resume_from"] == "Coldplay"

    def test_interrupted_artist_restarts_itself(self, svc, statuses):
        statuses["Beatles"] = "started"
        result = svc.resolve_resume_target("Beatles")
        assert result["mode"] == "restart"
        assert result["resume_from"] == "Beatles"

    def test_unknown_artist_starts_at_that_artist(self, svc, statuses):
        """No history: re-scanning it is the safe choice (it may be incomplete)."""
        result = svc.resolve_resume_target("Coldplay")
        assert result["mode"] == "restart"
        assert result["resume_from"] == "Coldplay"

    def test_last_artist_in_the_library_restarts_rather_than_vanishing(self, svc, statuses):
        """A completed final artist has no 'next' — the run must still do work."""
        statuses["Daft Punk"] = "completed"
        result = svc.resolve_resume_target("Daft Punk")
        assert result["mode"] == "restart"
        assert result["resume_from"] == "Daft Punk"

    def test_match_is_case_and_punctuation_tolerant(self, svc, monkeypatch):
        """A stored name can differ from the checkpoint's spelling."""
        monkeypatch.setattr(svc, "_most_recent_status_by_artist", lambda: {"D'Artagnan": "completed"})
        monkeypatch.setattr(svc, "get_all_artists", lambda: ["dArtagnan", "Eminem"])
        result = svc.resolve_resume_target("D'Artagnan")
        assert result["mode"] == "next"
        assert result["resume_from"] == "Eminem"

    def test_no_artist_selected(self, svc):
        result = svc.resolve_resume_target("")
        assert result["mode"] == "none"
        assert result["resume_from"] is None

    def test_reason_is_human_readable(self, svc, statuses):
        statuses["Beatles"] = "interrupted"
        result = svc.resolve_resume_target("Beatles")
        assert "Beatles" in result["reason"]


class TestArtistScanState:
    @pytest.mark.parametrize("status,expected", [
        ("completed", "completed"),
        ("started", "interrupted"),
        ("running", "interrupted"),
        ("stop_requested", "interrupted"),
        ("failed", "unknown"),
        ("", "unknown"),
    ])
    def test_status_mapping(self, svc, status, expected):
        assert svc.artist_scan_state("X", {"X": status}) == expected

    def test_missing_artist_is_unknown(self, svc):
        assert svc.artist_scan_state("X", {}) == "unknown"


# ---------------------------------------------------------------------------
# The option list
# ---------------------------------------------------------------------------

class TestGetResumeOptions:
    def _install(self, monkeypatch, svc, *, statuses, library, albums=None,
                 checkpoint=None):
        monkeypatch.setattr(svc, "_most_recent_status_by_artist", lambda: dict(statuses))
        monkeypatch.setattr(svc, "get_all_artists", lambda: list(library))
        monkeypatch.setattr(svc, "_album_counts", lambda: dict(albums or {}))
        monkeypatch.setattr(svc, "_full_scan_checkpoint_artist", lambda: checkpoint)
        # Keep the ordering deterministic without a DB.
        monkeypatch.setattr(svc, "_recent_album_rows", lambda limit=400: [])

    def test_full_scan_artist_is_listed_first(self, monkeypatch, svc):
        """'At the top of the list should be the artist that was last scanned
        during a full scan.'"""
        self._install(
            monkeypatch, svc,
            statuses={"Beatles": "started", "Coldplay": "completed"},
            library=["Aardvark", "Beatles", "Coldplay"],
            checkpoint="Beatles",
        )
        result = svc.get_resume_options()
        assert result["artists"][0]["artist"] == "Beatles"
        assert result["artists"][0]["source"] == "full_scan"
        assert result["recommended"] == "Beatles"

    def test_recent_artists_are_included_after_it(self, monkeypatch, svc):
        self._install(
            monkeypatch, svc,
            statuses={"Beatles": "started", "Coldplay": "completed", "Daft Punk": "started"},
            library=["Aardvark", "Beatles", "Coldplay", "Daft Punk"],
            checkpoint="Beatles",
        )
        result = svc.get_resume_options()
        names = [a["artist"] for a in result["artists"]]
        assert names[0] == "Beatles"
        assert "Coldplay" in names

    def test_at_most_three_recent_artists(self, monkeypatch, svc):
        """'the last three artists that were manually scanned'."""
        self._install(
            monkeypatch, svc,
            statuses={"B1": "started", "B2": "started", "B3": "started",
                      "B4": "started", "B5": "started"},
            library=["B1", "B2", "B3", "B4", "B5"],
            checkpoint=None,
        )
        result = svc.get_resume_options()
        recent = [a for a in result["artists"] if a["source"] == "recent"]
        assert len(recent) <= 3

    def test_the_full_scan_artist_is_not_duplicated_as_recent(self, monkeypatch, svc):
        self._install(
            monkeypatch, svc,
            statuses={"Beatles": "started", "Coldplay": "started"},
            library=["Beatles", "Coldplay"],
            checkpoint="Beatles",
        )
        result = svc.get_resume_options()
        names = [a["artist"] for a in result["artists"]]
        assert names.count("Beatles") == 1

    def test_each_option_carries_what_will_happen(self, monkeypatch, svc):
        self._install(
            monkeypatch, svc,
            statuses={"Beatles": "completed"},
            library=["Aardvark", "Beatles", "Coldplay"],
            checkpoint="Beatles",
        )
        entry = svc.get_resume_options()["artists"][0]
        assert entry["state"] == "completed"
        assert entry["mode"] == "next"
        assert entry["resume_from"] == "Coldplay"
        assert entry["reason"]

    def test_interrupted_entry_restarts_itself(self, monkeypatch, svc):
        self._install(
            monkeypatch, svc,
            statuses={"Beatles": "started"},
            library=["Aardvark", "Beatles", "Coldplay"],
            checkpoint="Beatles",
        )
        entry = svc.get_resume_options()["artists"][0]
        assert entry["state"] == "interrupted"
        assert entry["mode"] == "restart"
        assert entry["resume_from"] == "Beatles"

    def test_no_history_reports_no_options(self, monkeypatch, svc):
        """Nothing recorded -> the dashboard must start normally, not prompt."""
        self._install(monkeypatch, svc, statuses={}, library=["A", "B"], checkpoint=None)
        result = svc.get_resume_options()
        assert result["has_options"] is False
        assert result["artists"] == []

    def test_album_counts_are_reported(self, monkeypatch, svc):
        self._install(
            monkeypatch, svc,
            statuses={"Beatles": "started"},
            library=["Beatles"],
            albums={"Beatles": 12},
            checkpoint="Beatles",
        )
        assert svc.get_resume_options()["artists"][0]["album_count"] == 12

    def test_missing_from_library_is_flagged(self, monkeypatch, svc):
        self._install(
            monkeypatch, svc,
            statuses={"Ghost": "started"},
            library=["Beatles"],
            checkpoint="Ghost",
        )
        assert svc.get_resume_options()["artists"][0]["in_library"] is False

    def test_read_failure_is_reported_not_raised(self, monkeypatch, svc):
        monkeypatch.setattr(svc, "get_all_artists",
                            lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(svc, "_most_recent_status_by_artist", lambda: {})
        monkeypatch.setattr(svc, "_album_counts", lambda: {})
        monkeypatch.setattr(svc, "_full_scan_checkpoint_artist", lambda: None)
        result = svc.get_resume_options()
        assert result["success"] is False
        assert result["has_options"] is False


# ---------------------------------------------------------------------------
# Session rows must never become options
# ---------------------------------------------------------------------------

class TestSessionRowsExcluded:
    def test_query_excludes_the_session_sentinel(self, svc):
        """`_SCAN_SESSION_` rows carry no artist and would pollute the list."""
        import inspect

        code = inspect.getsource(svc._recent_album_rows)
        assert "_SESSION_ARTIST" in code or "_SCAN_SESSION_" in code

    def test_sentinel_constant_is_the_real_value(self, svc):
        assert svc._SESSION_ARTIST == "_SCAN_SESSION_"


# ---------------------------------------------------------------------------
# Endpoint + route wiring
# ---------------------------------------------------------------------------

class TestEndpointWiring:
    def test_route_is_registered(self):
        app_mod = importlib.import_module("app")
        rules = {str(r.rule) for r in app_mod.app.url_map.iter_rules()}
        assert "/api/scan/resume-options" in rules

    def test_route_is_get(self):
        app_mod = importlib.import_module("app")
        for rule in app_mod.app.url_map.iter_rules():
            if str(rule.rule) == "/api/scan/resume-options":
                assert "GET" in rule.methods
                return
        pytest.fail("route not registered")


class TestRouteHonoursChosenResumePoint:
    """An explicit resume_from must override the stored checkpoint."""

    def _source(self) -> str:
        from pathlib import Path

        return Path("routes/scan_routes/api.py").read_text(encoding="utf-8")

    def test_route_reads_resume_from_the_request(self):
        assert 'raw.get("resume_from")' in self._source()

    def test_request_value_takes_precedence_over_the_checkpoint(self):
        """The picker's choice wins — otherwise the user's pick is discarded."""
        source = self._source()
        # The explicit branch must be tested BEFORE loading the checkpoint.
        explicit = source.index("elif _requested_resume:")
        checkpoint_load = source.index("load_scan_checkpoint(_checkpoint_path)")
        assert explicit < checkpoint_load, (
            "the request value must be considered before the stored checkpoint"
        )

    def test_schema_accepts_resume_from(self):
        schemas = importlib.import_module("routes.schemas")
        assert "resume_from" in schemas.ScanRequest.model_fields


# ---------------------------------------------------------------------------
# Frontend wiring (both trees)
# ---------------------------------------------------------------------------

_TREES = [
    ("test_site/templates/Pages/dashboard.html", "js/services/scan-resume-picker.js",
     "test_site/static/js/services/scan-resume-picker.js",
     "test_site/static/js/pages/dashboard.js"),
    ("templates/pages/dashboard.html", "js/scan-resume-picker.js",
     "static/js/scan-resume-picker.js", "static/js/dashboard.js"),
]


def _code_only(source: str) -> str:
    import re

    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    without_line = re.sub(r"//[^\n]*", " ", without_block)
    return re.sub(r"<!--.*?-->", " ", without_line, flags=re.DOTALL)


class TestFrontendWiring:
    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_dashboard_loads_the_picker(self, template, ref, module, js):
        from pathlib import Path

        text = Path(template).read_text(encoding="utf-8")
        assert ref in text, f"{template} does not load {ref}"
        assert Path(module).exists(), f"{module} is missing"

    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_picker_publishes_the_global(self, template, ref, module, js):
        from pathlib import Path

        code = _code_only(Path(module).read_text(encoding="utf-8"))
        assert "ScanResumePicker" in code
        assert "chooseResumeArtist" in code

    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_picker_reads_the_options_endpoint(self, template, ref, module, js):
        from pathlib import Path

        code = _code_only(Path(module).read_text(encoding="utf-8"))
        assert "/api/scan/resume-options" in code

    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_dashboard_prompts_only_when_restart_is_unchecked(self, template, ref, module, js):
        """Restart means 'from the top' — prompting there would contradict it."""
        from pathlib import Path

        code = _code_only(Path(js).read_text(encoding="utf-8"))
        assert "ScanResumePicker" in code
        assert "!restart" in code

    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_dashboard_sends_the_chosen_resume_point(self, template, ref, module, js):
        from pathlib import Path

        code = _code_only(Path(js).read_text(encoding="utf-8"))
        assert "resume_from" in code

    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_cancelling_does_not_start_a_scan(self, template, ref, module, js):
        """A null choice must abort — not silently resume from a default."""
        from pathlib import Path

        code = _code_only(Path(js).read_text(encoding="utf-8"))
        assert "if (!choice) return;" in code

    @pytest.mark.parametrize("template,ref,module,js", _TREES)
    def test_picker_makes_the_two_outcomes_visible(self, template, ref, module, js):
        """The user must be able to see which artists restart vs continue."""
        from pathlib import Path

        code = _code_only(Path(module).read_text(encoding="utf-8"))
        assert "restart" in code
        assert "next" in code
