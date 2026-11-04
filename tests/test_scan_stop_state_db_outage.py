"""A DB blip must not abort a scan at the stop check.

Reported: a Navidrome import died with

    psycopg2.OperationalError: connection to server ... failed: Connection refused
    ...
    File "/app/services/scanning/pipelines/navidrome_pipeline.py", line 589,
         in run_navidrome_import_scan
        if is_stop_requested(progress_file):

``is_stop_requested`` is a CONTROL check called between artists/batches. Being
unable to read the stop flag during a transient database outage is not a reason
to fail the whole import — it must fail OPEN (keep going). The next real DB
operation surfaces a genuine outage where it can be retried or deferred.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from services.scanning import scan_state


def _outage(*_args, **_kwargs):
    raise OperationalError(
        "SELECT 1", {}, Exception("connection to server failed: Connection refused")
    )


class TestStopCheckFailsOpen:
    def test_is_stop_requested_returns_false_when_the_db_is_unavailable(
        self, monkeypatch
    ):
        monkeypatch.setattr(scan_state, "db_session", _outage)

        assert scan_state.is_stop_requested("/tmp/navidrome_scan_progress.json") is False, (
            "a transient DB outage at the stop check must not abort the scan"
        )

    def test_is_stop_requested_still_reads_the_flag_normally(self, monkeypatch):
        """CONTROL — the fail-open path must not disable a real stop."""

        class _State:
            stop_requested = True
            status = "stop_requested"

        class _Query:
            def filter_by(self, **_kwargs):
                return self

            def first(self):
                return _State()

        class _Session:
            def query(self, *_args, **_kwargs):
                return _Query()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(scan_state, "db_session", lambda *a, **k: _Session())

        assert scan_state.is_stop_requested("/tmp/navidrome_scan_progress.json") is True
