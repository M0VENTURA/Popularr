"""A running Navidrome scan must DELAY the import, not cancel it.

REPORTED

> "the Navidrome import doesn't seem to be happening while another scan is
> happening anymore."

CAUSE

``sync_remote_navidrome_before_import`` deferred the WHOLE import whenever
``get_scan_status()`` reported ``scanning``. A popularity/full scan requests a
rescan at its end and holds a 300 s cooldown (``navidrome_rescan_service``), so
Navidrome was still scanning for five minutes after every scan — and the import
deferred every time, importing nothing.

``trigger_and_wait_for_scan`` already drains a running scan (its Phase 1 waits for
``scanning == False`` before requesting its own), so deferring was both redundant
and wrong: the correct behaviour is to wait.

The deferral that MUST survive is "Navidrome is unreachable / its state cannot be
verified" — that still returns False, because importing an unverified view is what
wrote stale release ids back to the DB.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


@pytest.fixture()
def pipeline():
    return importlib.import_module("services.scanning.pipelines.navidrome_pipeline")


def _source(fn) -> str:
    return inspect.getsource(fn)


class TestARunningScanDoesNotCancelTheImport:
    def test_the_deferral_for_a_running_scan_is_gone(self, pipeline):
        src = _source(pipeline.sync_remote_navidrome_before_import)
        assert "still scanning or " not in src, (
            "the 'deferred — still scanning' abort is back, so an import can no "
            "longer run while Navidrome scans"
        )

    def test_it_logs_that_it_waits_instead(self, pipeline):
        src = _source(pipeline.sync_remote_navidrome_before_import)
        assert "waiting for it to" in src, (
            "a running scan no longer produces a wait message"
        )

    def test_the_wait_happens_before_any_import(self, pipeline):
        """The wait note must precede the actual trigger call.

        Anchored on the LOG STRING, not the phrase, because the docstring above
        also says "waiting" — a plain substring search matched the prose.
        """
        src = _source(pipeline.sync_remote_navidrome_before_import)
        wait_at = src.index("is scanning; waiting for it to")
        trigger_at = src.index("nav_client.trigger_and_wait_for_scan()")
        assert wait_at < trigger_at, (
            "the 'waiting' note is after the trigger call; the ordering matters"
        )


class TestAnUnverifiedNavidromeStillDefers:
    def test_an_exception_still_refuses(self, pipeline):
        src = _source(pipeline.sync_remote_navidrome_before_import)
        assert "refusing to import stale state" in src, (
            "an unreachable Navidrome must still refuse — importing an unverified "
            "view is what reverted corrected release ids"
        )

    def test_a_failed_scan_still_returns_false(self, pipeline):
        src = _source(pipeline.sync_remote_navidrome_before_import)
        assert "Remote Navidrome scan FAILED" in src
        assert "return remote_sync_ok" in src


class TestTheDrainAlreadyExists:
    def test_trigger_and_wait_drains_a_running_scan(self):
        """Why deferring was redundant in the first place."""
        from api_clients import navidrome as nav

        src = _source(nav.NavidromeClient.trigger_and_wait_for_scan)
        assert "drain" in src.lower(), (
            "trigger_and_wait_for_scan no longer drains a running scan, so the "
            "import would now start its own while one is in flight"
        )
        assert "if not status.get(\"scanning\")" in src
