"""Merged queue/search activity log, Navidrome import split and matching guards."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from helpers import logging_config as lc
from helpers import file_manager  # noqa: F401  (pre-created files contract)
from services import log_service


class _FakeLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.propagate = True

    def info(self, message, *args, **kwargs):
        self.lines.append(str(message))


@pytest.fixture
def fake_loggers(monkeypatch):
    loggers: dict[str, _FakeLogger] = {}
    real_get_logger = logging.getLogger

    def _get(name=None, *args, **kwargs):
        # pytest's logging plugin calls ``logging.getLogger()`` with NO name at
        # teardown; without this pass-through the module patch turns its
        # teardown into an INTERNALERROR.
        if name is None:
            return real_get_logger()
        return loggers.setdefault(name, _FakeLogger())

    monkeypatch.setattr(lc.logging, "getLogger", _get)
    return loggers


class TestActivityLog:
    def test_activity_line_has_category_and_ids(self, fake_loggers):
        lc.log_activity(
            "download",
            "Download started.",
            queue_id=7,
            attempt_id="7-a2",
            candidate_id="peer:file.flac",
        )
        line = fake_loggers["popularr.activity"].lines[0]
        assert line.startswith("[ACTIVITY] [DOWNLOAD] Download started.")
        assert "queue_id=7" in line
        assert "attempt_id='7-a2'" in line
        assert "candidate_id='peer:file.flac'" in line

    def test_queue_event_reaches_both_activity_and_legacy_file(self, fake_loggers):
        from services.queue import queue_diagnostics_service as qds

        qds.log_queue_event(
            "downloading",
            "Artist - Title → downloading from peer (file.flac)",
            queue_id=42,
            attempt_id="42-a1",
        )
        activity = fake_loggers["popularr.activity"].lines[-1]
        legacy = fake_loggers["popularr.queue"].lines[-1]
        assert "[ACTIVITY] [DOWNLOAD]" in activity
        assert "queue_id=42" in activity
        assert "attempt_id='42-a1'" in activity
        assert "[DOWNLOADING]" in legacy
        assert "queue_id=42" in legacy

    def test_search_delegates_to_activity(self, fake_loggers):
        lc.log_search("[AUTOMATIC] query → 3 results", queue_id=9)
        activity = fake_loggers["popularr.activity"].lines[-1]
        assert "[ACTIVITY] [SEARCH]" in activity
        assert "queue_id=9" in activity

    def test_manual_search_does_not_claim_zero_results(self, monkeypatch):
        from routes import download_search_routes as dsr

        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(dsr, "log_search", lambda msg, **kw: seen.append((msg, kw)))
        monkeypatch.setattr(dsr, "log_slskd_search", lambda **kw: None)

        dsr._log_manual_search_event(
            search_type="manual", query="Maphra - End of You",
            result_count=0, notes="search_started", search_id="s1",
        )
        assert seen[0][0] == "[MANUAL] Searching: Maphra - End of You"
        assert "0 results" not in seen[0][0]
        assert seen[0][1].get("search_id") == "s1"

        seen.clear()
        dsr._log_manual_search_event(
            search_type="manual", query="Maphra - End of You",
            result_count=13, notes="search_complete", search_id="s1",
        )
        assert seen[0][0] == "[MANUAL] Search complete: 13 results for Maphra - End of You"

    def test_undefined_query_is_rejected(self):
        from routes import download_search_routes as dsr

        assert dsr._normalize_slskd_query("undefined") == ""
        assert dsr._normalize_slskd_query("null") == ""
        assert dsr._normalize_slskd_query("  Artist - Title  ") == "Artist - Title"


class TestNavidromeImportSplit:
    def test_import_helper_writes_only_the_import_logger(self, fake_loggers):
        lc.log_navidrome_import("[NAVIDROME_IMPORT] Importing artist: A")
        assert fake_loggers["popularr.navidrome_import"].lines
        assert "popularr.unified" not in fake_loggers
        assert "[NAVIDROME_IMPORT] Importing artist: A" in (
            fake_loggers["popularr.navidrome_import"].lines[0]
        )

    def test_config_creates_the_split_files_and_routes(self, tmp_path):
        cfg = lc._build_logging_config("test", str(tmp_path))
        assert "activity_file" in cfg["handlers"]
        assert "navidrome_import_file" in cfg["handlers"]
        assert cfg["handlers"]["activity_file"]["filename"].endswith("activity.log")
        assert cfg["handlers"]["navidrome_import_file"]["filename"].endswith(
            "navidrome_import.log"
        )
        importer = cfg["loggers"]["services.scanning.navidrome_import"]
        assert importer["propagate"] is False
        assert importer["handlers"] == ["navidrome_import_file", "error_file"]
        pipeline = cfg["loggers"]["services.scanning.pipelines.navidrome_pipeline"]
        assert pipeline["propagate"] is False


class TestViewerMerge:
    def test_viewer_hides_legacy_queue_and_search(self, tmp_path, monkeypatch):
        for name in (
            "unified_scan.log",
            "activity.log",
            "navidrome_import.log",
            "queue.log",
            "search.log",
            "error.log",
        ):
            (tmp_path / name).write_text("", encoding="utf-8")
        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        listed = {entry["name"] for entry in log_service.list_log_files()}
        assert {"activity.log", "navidrome_import.log"} <= listed
        viewer = {entry["name"] for entry in log_service.viewer_log_files()}
        assert "activity.log" in viewer
        assert "navidrome_import.log" in viewer
        assert "queue.log" not in viewer
        assert "search.log" not in viewer

    def test_allowlist_and_export_sources_include_the_new_files(self):
        assert "activity.log" in log_service.LOG_FILE_ALLOWLIST
        assert "navidrome_import.log" in log_service.LOG_FILE_ALLOWLIST
        assert log_service._LOG_SOURCE_FILES["queue"] == "activity.log"
        assert log_service._LOG_SOURCE_FILES["soulseek"] == "activity.log"
        assert (
            log_service._LOG_SOURCE_FILES["navidrome_import"]
            == "navidrome_import.log"
        )

    def test_redundant_and_empty_logs_are_hidden_from_the_viewer(
        self, tmp_path, monkeypatch
    ):
        """The picker: one stream per real source, no duplicates or 0 B files."""
        for name in (
            "unified_scan.log",
            "activity.log",
            "navidrome_import.log",
            "error.log",
            "debug.log",
            "info.log",              # INFO is a subset of debug.log
            "client.log",            # merged into activity.log
            "access.log",            # Hypercorn access log, routinely 0 B
            "queue_processor.log",   # worker stdout redirect, routinely 0 B
        ):
            (tmp_path / name).write_text("x", encoding="utf-8")
        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        viewer = {entry["name"] for entry in log_service.viewer_log_files()}
        assert viewer == {
            "unified_scan.log",
            "activity.log",
            "navidrome_import.log",
            "error.log",
            "debug.log",
        }
        # Hidden from the picker, still allow-listed for downloads/legacy.
        listed = {entry["name"] for entry in log_service.list_log_files()}
        assert {
            "info.log", "client.log", "access.log", "queue_processor.log",
        } <= listed

    def test_client_alerts_also_reach_the_activity_stream(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_service, "resolve_log_dir", lambda: str(tmp_path))

        log_service.append_client_log("Download failed for Artist - Title")

        activity = (tmp_path / "activity.log").read_text(encoding="utf-8")
        client = (tmp_path / "client.log").read_text(encoding="utf-8")
        assert "[CLIENT] Download failed for Artist - Title" in activity, (
            "UI alerts should sit beside the queue/search events they relate to"
        )
        assert "[CLIENT] Download failed for Artist - Title" in client, (
            "the legacy client.log must still be written"
        )

    def test_the_api_uses_the_viewer_list(self):
        source = (
            Path(__file__).resolve().parents[1] / "routes" / "logs.py"
        ).read_text(encoding="utf-8")
        assert "viewer_log_files()" in source, (
            "the modal's picker reads /api/logs/list; it must not re-expose a "
            "retired source the live page hides"
        )

    def test_the_viewer_js_offers_the_activity_filter(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "test_site/static/js/pages/logs.js"
        ).read_text(encoding="utf-8")
        assert "activityCategoryFilter" in source
        assert "queue.log" in source and "search.log" in source
        modal = (
            Path(__file__).resolve().parents[1]
            / "test_site/templates/components/_log_modal.html"
        ).read_text(encoding="utf-8")
        assert "activityCategoryFilter" in modal
        assert "Retries/failures" in modal


class TestMatchingGuards:
    def test_clearly_different_title_is_rejected(self):
        from services.downloads import download_pipeline_service as dps

        rejects: dict[str, int] = {}
        score = dps._score_result(
            {
                "filename": (
                    "Eurythmics - Would I Lie To You - 01 - "
                    "I Could Give You (A Mirror).mp3"
                )
            },
            "Eurythmics",
            "Would I Lie to You?",
            None,
            None,
            None,
            rejects=rejects,
        )
        assert score == 0.0
        assert (
            rejects.get("title_mismatch") == 1
            or rejects.get("edition_mismatch") == 1
        )

    def test_requested_version_missing_is_rejected(self, monkeypatch):
        from services.downloads import download_pipeline_service as dps

        # Isolate the version guard from the broader edition annotation check.
        monkeypatch.setattr(
            "helpers.normalization_service.edition_annotations_compatible",
            lambda *_a, **_k: True,
        )
        rejects: dict[str, int] = {}
        score = dps._score_result(
            {"filename": "Maphra - End of You - 01 - End of You.mp3"},
            "Maphra",
            "End of You (Live)",
            None,
            None,
            None,
            rejects=rejects,
        )
        assert score == 0.0
        assert rejects.get("variant_mismatch") == 1
