"""Dashboard health surfaces that were missing, and the two retry loops.

Reported from a real activity log:

* the same ``Fuel — Pendulum`` row was re-fetched every ~35s for hours
  (``Album-typed queue row left pending_release``), because
  ``update_queue_item`` left ``next_retry_at`` NULL — which the ready query
  treats as DUE — and nothing incremented its attempt count;
* the same impossible library-copy candidates (Korn, Bush) were re-verified on
  every cycle, because a rejected copy left no trace on the row;
* the dashboard said nothing about discarded downloads, a dead scheduler, low
  disk space, or the queue's workload.
"""

from __future__ import annotations

import importlib
import time

import pytest


# ---------------------------------------------------------------------------
# 1. pending_release must park, not spin
# ---------------------------------------------------------------------------

class TestPendingReleaseParks:
    def test_a_release_row_without_an_mbid_gets_a_real_retry_time(self, monkeypatch):
        pipeline = importlib.import_module(
            "services.downloads.download_pipeline_service"
        )
        calls: dict = {}

        def _schedule(queue_id, status, next_retry_at, reason=""):
            calls.update(
                queue_id=queue_id, status=status,
                next_retry_at=next_retry_at, reason=reason,
            )
            return {"success": True, "status": status}

        monkeypatch.setattr(
            "db.repositories.queue.schedule_queue_retry", _schedule, raising=False,
        )

        result = pipeline._process_release_request(
            {"artist": "Fuel", "album": "Pendulum", "title": "Pendulum"},
            9594,
        )

        assert result["status"] == "pending_release"
        assert calls["status"] == "pending_release"
        assert calls["next_retry_at"], (
            "a NULL next_retry_at is treated as DUE, which re-fetched this row "
            "on every cycle for hours"
        )
        assert calls["queue_id"] == 9594


# ---------------------------------------------------------------------------
# 2. A rejected library copy must not be re-verified for ever
# ---------------------------------------------------------------------------

class TestLibraryCopyRemembersRejections:
    def test_a_length_mismatch_is_recorded_on_the_row(self, monkeypatch):
        libcopy = importlib.import_module("services.downloads.library_copy_service")
        recorded: list[tuple] = []

        monkeypatch.setattr(
            "db.repositories.queue.record_rejected_candidate",
            lambda qid, user, name, reason="": recorded.append((qid, name, reason)),
            raising=False,
        )
        monkeypatch.setattr(
            "db.repositories.queue.get_rejected_candidate_keys",
            lambda item: set(),
            raising=False,
        )
        monkeypatch.setattr(
            libcopy, "_candidate_rows",
            lambda item: [{
                "album": "Life Is Peachy",
                "file_path": "/music/Korn/1996 - Life Is Peachy/11 - A.D.I.D.A.S..mp3",
                "duration": 153.6,
            }],
        )
        monkeypatch.setattr(libcopy, "_quality_ok", lambda path: (True, ""))
        monkeypatch.setattr(
            libcopy, "_file_duration_seconds", lambda path, db: 153.6,
        )

        result = libcopy.try_library_copy({
            "id": 9637,
            "artist": "Korn",
            "title": "A.D.I.D.A.S.",
            "album": "Greatest Hits, Vol. 1",
            "duration": "159.2",
        })

        assert result is None, "the mismatched copy must still be refused"
        assert recorded, "the refusal must be remembered, not re-tested forever"
        assert "/music/Korn" in recorded[0][1]

    def test_an_already_rejected_candidate_is_not_re_read(self, monkeypatch):
        libcopy = importlib.import_module("services.downloads.library_copy_service")
        reads: list[str] = []

        monkeypatch.setattr(
            "db.repositories.queue.get_rejected_candidate_keys",
            lambda item: {
                importlib.import_module("db.repositories.queue")._candidate_key(
                    "", "/music/Korn/1996 - Life Is Peachy/11 - A.D.I.D.A.S..mp3"
                )
            },
            raising=False,
        )
        monkeypatch.setattr(
            libcopy, "_candidate_rows",
            lambda item: [{
                "album": "Life Is Peachy",
                "file_path": "/music/Korn/1996 - Life Is Peachy/11 - A.D.I.D.A.S..mp3",
                "duration": 153.6,
            }],
        )
        monkeypatch.setattr(libcopy, "_quality_ok", lambda path: (True, ""))
        monkeypatch.setattr(
            libcopy, "_file_duration_seconds",
            lambda path, db: reads.append(path) or 153.6,
        )

        assert libcopy.try_library_copy({
            "id": 9637, "artist": "Korn", "title": "A.D.I.D.A.S.",
            "album": "Greatest Hits, Vol. 1", "duration": "159.2",
        }) is None
        assert reads == [], (
            "a candidate already rejected for this row must not be re-read"
        )


# ---------------------------------------------------------------------------
# 3. New status payload fields
# ---------------------------------------------------------------------------

class TestTheStatusPayloadGainsFields:
    def test_rejections_and_queue_counts_are_reported(self, monkeypatch):
        service = importlib.import_module("services.queue.queue_diagnostics_service")

        monkeypatch.setattr(
            service, "_queue_processor_stats",
            lambda: {"queued": 3, "active": 1, "failed": 2, "ready": 0,
                     "pending_release": 4, "backed_off": 0},
        )
        monkeypatch.setattr(
            "db.repositories.queue_rejections.rejection_counts",
            lambda: {"wrong_length": 7, "wrong_artist": 2},
            raising=False,
        )

        payload, _status = service.queue_processor_status()

        assert payload["queue_stats"]["pending_release"] == 4
        assert payload["rejections"]["total"] == 9
        assert payload["rejections"]["top_reason"] == "wrong_length"

    def test_scheduler_and_disk_are_reported(self):
        service = importlib.import_module("services.queue.queue_diagnostics_service")

        payload, _status = service.queue_processor_status()

        assert "scheduler" in payload
        assert "disk_space" in payload
        assert "level" in payload["disk_space"]


class TestDiskSpace:
    def test_level_is_derived_from_the_worst_volume(self, monkeypatch):
        disk = importlib.import_module("services.infrastructure.disk_space_service")

        monkeypatch.setattr(
            disk, "_space_for",
            lambda label, path: {
                "label": label, "path": path, "known": True,
                "free_bytes": 1, "total_bytes": 100, "free_percent": 1.0,
            },
        )
        monkeypatch.setattr(
            "helpers.settings.get_settings",
            lambda: type("S", (), {"music_root": "/music"})(),
        )

        payload = disk.read_disk_space()

        assert payload["known"] is True
        assert payload["level"] == "critical"
        assert payload["worst_percent"] == 1.0

    def test_an_unmeasurable_volume_is_unknown_not_an_error(self, monkeypatch):
        disk = importlib.import_module("services.infrastructure.disk_space_service")
        monkeypatch.setattr(
            "helpers.settings.get_settings",
            lambda: type("S", (), {"music_root": "/does/not/exist"})(),
        )
        monkeypatch.setattr(
            "services.infrastructure.filesystem_service.resolve_downloads_dir",
            lambda: "", raising=False,
        )

        payload = disk.read_disk_space()

        assert payload["known"] is False
        assert payload["level"] == "unknown"


# ---------------------------------------------------------------------------
# 4. The dashboard actually renders them
# ---------------------------------------------------------------------------

class TestTheDashboardRendersTheNewAlerts:
    def test_every_new_host_and_renderer_exists(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        template = (
            root / "test_site" / "templates" / "Pages" / "dashboard.html"
        ).read_text(encoding="utf-8")
        script = (
            root / "test_site" / "static" / "js" / "pages" / "dashboard.js"
        ).read_text(encoding="utf-8")

        for host in (
            "searchServiceAlert", "schedulerAlert", "diskSpaceAlert",
            "discardedDownloadsAlert", "queueSummaryStrip",
        ):
            assert f'id="{host}"' in template, f"missing host: {host}"

        for renderer in (
            "renderSearchServiceAlert", "renderSchedulerAlert",
            "renderDiskSpaceAlert", "renderDiscardedDownloadsAlert",
            "renderQueueSummary",
        ):
            assert f"function {renderer}" in script, f"missing renderer: {renderer}"
            # Each must be driven by the shared poll.
            assert f"{renderer}(" in script.split("async function updateQueueProcessorAlert")[1]

    def test_the_dead_template_flags_are_gone(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        template = (
            root / "test_site" / "templates" / "Pages" / "dashboard.html"
        ).read_text(encoding="utf-8")

        # `dashboardFlags` itself must STAY: main.js uses it to stand down its
        # own scan-bar writer on this page.
        assert 'id="dashboardFlags"' in template
        # Assert on the ATTRIBUTE form: the explanatory comment above the div
        # names both, which is why a bare substring check is wrong here.
        assert "data-launch-on-startup=" not in template
        assert "data-first-full-scan-done=" not in template
