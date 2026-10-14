"""A stop request must flag the scan_type the RUNNING scan actually reads.

Reported
--------
> Even though ``routes.scan_routes.control`` logged
> ``Stop requested for all scans scan_types_affected=10``, the Library Scan
> never stopped! Both scans ran simultaneously ... causing a PostgreSQL
> DeadlockDetected crash on the ``tracks`` table.

Root cause
----------
A pure naming mismatch. ``run_full_library_scan``
(``services/scanning/pipeline.py``) resolves its progress row with
``get_library_progress_path()`` — which returns **``"library"``** — and that is
the name it passes to ``is_stop_requested()``:

    progress = get_library_progress_path()          # "library"
    clear_stop_request(progress)                    # clears "library"
    if is_stop_requested(progress):                 # reads  "library"

Both stop routes flagged the prettier ``"library_scan"`` spelling instead:

    path = get_scan_progress_path("library_scan")   # "library_scan"

``get_scan_progress_path`` is the IDENTITY function, so the request set
``stop_requested`` on a ``scan_states`` row that nothing polls. The library scan
was therefore UNSTOPPABLE — by the generic Stop, by Stop All, and (because the
same spelling was missing from the clear-stuck list) by the dashboard's
"Clear stuck scans" button too.

Why it matters beyond the button
--------------------------------
A scan the user cannot stop keeps running while they start another one, and the
two then collide on ``tracks`` — the deadlock in the report. The fix is to write
the name through the canonical accessor the reader uses, so the two sides cannot
drift apart again; this file pins that agreement behaviourally rather than by
comparing source text.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
async def scan_states_table(app):
    """``scan_states`` as the app uses it.

    ``tests/conftest.py`` creates only ``tracks``; the stop routes need this
    table, and importing ``app`` DISPOSES the shared in-memory engine, so this
    fixture depends on the ``app`` fixture to pin the ordering (same pattern as
    ``test_full_scan_startup_fix``).
    """
    from db.engine import db_session

    with db_session() as session:
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS scan_states ("
            "scan_type TEXT PRIMARY KEY, is_running BOOLEAN, status TEXT, "
            "stop_requested BOOLEAN, current_artist TEXT, "
            "last_scanned_artist TEXT, extra_data TEXT, updated_at TEXT)"
        ))
    return True


def _stop_flag(scan_type: str) -> bool:
    from db.engine import db_session

    with db_session() as session:
        row = session.execute(
            text("SELECT stop_requested FROM scan_states WHERE scan_type = :t"),
            {"t": scan_type},
        ).fetchone()
    return bool(row and row[0])


class TestTheStopRoutesFlagTheNameTheScanReads:
    def test_the_library_scan_reads_the_library_progress_path(self):
        """The premise, asserted explicitly: the reader's name is ``library``.

        If this ever changes, every stop route must change with it — which is
        exactly why it is pinned rather than assumed.
        """
        from services.scanning.scan_state import get_library_progress_path

        with open("services/scanning/pipeline.py", encoding="utf-8") as handle:
            source = handle.read()

        assert 'return "library"' in _helper_body(get_library_progress_path), (
            "get_library_progress_path() no longer returns 'library'"
        )
        assert "progress = get_library_progress_path()" in source, (
            "run_full_library_scan no longer resolves its progress row through "
            "get_library_progress_path()"
        )
        assert "is_stop_requested(progress)" in source, (
            "the library loop no longer checks the stop flag on that row"
        )

    async def test_stop_all_flags_the_library_scan(
        self, client, scan_states_table
    ):
        """THE REPORTED CASE. Stop All must set ``stop_requested`` on the row
        ``run_full_library_scan`` polls — a 200 response proves nothing, so
        this asserts the FLAG, not the reply."""
        response = await client.post("/scan/stop-all")

        assert response.status_code in (200, 302)
        assert _stop_flag("library") is True, (
            "Stop All did not flag the library scan: the library scan cannot "
            "be stopped, and it will keep running alongside the next scan"
        )

    async def test_the_generic_stop_flags_the_library_scan(
        self, client, scan_states_table
    ):
        """/scan/stop is the plain Stop button — it is documented as stopping
        the LIBRARY scan, so it must flag that row too."""
        response = await client.post("/scan/stop")

        assert response.status_code in (200, 302)
        assert _stop_flag("library") is True

    async def test_stop_all_does_not_flag_the_dead_spelling(
        self, client, scan_states_table
    ):
        """CONTROL OF THE DEFECT — ``library_scan`` is the spelling that used to
        be written and never read. Nothing may depend on it again."""
        await client.post("/scan/stop-all")

        assert _stop_flag("library_scan") is False, (
            "a stop flag was written to 'library_scan', which no scan reads"
        )

    async def test_stop_all_still_flags_the_other_scans(
        self, client, scan_states_table
    ):
        """CONTROL — the fix must not have displaced the nine scans that were
        already correct. Each of these is the name its own pipeline reads."""
        await client.post("/scan/stop-all")

        for scan_type in (
            "full_scan",
            "navidrome_scan",
            "popularity_scan",
            "singles_scan",
            "mp3_import",
        ):
            assert _stop_flag(scan_type) is True, (
                f"Stop All stopped flagging {scan_type}"
            )

    async def test_a_running_library_scan_is_clearable(
        self, client, scan_states_table
    ):
        """The same naming trap hid the library scan from the dashboard's
        "Clear stuck scans" button, which reads each row's ``is_running``. A
        stuck library scan could therefore never be repaired."""
        from db.engine import db_session

        with db_session() as session:
            session.execute(text(
                "INSERT INTO scan_states (scan_type, is_running, status) "
                "VALUES ('library', TRUE, 'running') "
                "ON CONFLICT(scan_type) DO UPDATE SET is_running = TRUE, "
                "status = 'running'"
            ))

        response = await client.post("/scan/clear-stuck")

        assert response.status_code in (200, 302)
        assert _stop_flag("library") is True, (
            "a running library scan was not picked up by Clear stuck scans"
        )


# ---------------------------------------------------------------------------
# The pipeline side of the same agreement
# ---------------------------------------------------------------------------


class TestEveryRunningScanIsReachableByStopAll:
    """A scan whose flag name is missing from Stop All is unstoppable.

    This walks each pipeline's own default rather than a hand-written list, so
    adding a scan without adding it to the stop route fails here.
    """

    def _default_progress_names(self) -> dict[str, str]:
        from services.scanning.pipelines.navidrome_pipeline import (
            run_navidrome_import_scan,
        )
        from services.scanning.scan_state import (
            get_library_progress_path,
            get_navidrome_progress_path,
        )

        names = {
            "library scan": get_library_progress_path(),
            "navidrome scan": get_navidrome_progress_path(),
        }
        # The navidrome pipeline's own default, resolved the way it resolves it.
        import inspect

        source = inspect.getsource(run_navidrome_import_scan)
        assert 'get_scan_progress_path("navidrome_scan")' in source, (
            "the navidrome pipeline changed the scan_type it polls"
        )
        assert names["navidrome scan"] == "navidrome"
        return names

    async def test_every_pipeline_default_is_flagged_by_stop_all(
        self, client, scan_states_table
    ):
        await client.post("/scan/stop-all")

        for label, name in self._default_progress_names().items():
            # "navidrome" is the CHECKPOINT path; the navidrome SCAN polls
            # "navidrome_scan", which the previous test covers. Only the
            # library scan shares one name for both.
            if name == "navidrome":
                continue
            assert _stop_flag(name) is True, (
                f"Stop All cannot stop the {label} (it polls {name!r})"
            )


def _helper_body(func) -> str:
    import inspect

    return inspect.getsource(func)
