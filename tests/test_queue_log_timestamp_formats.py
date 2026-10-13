"""Queue log timestamps: BOTH formats must parse (the 1970 regression).

THE REPORT
----------
Some rows in the Download Queue Events Log showed **01/01/1970**.

WHY
---
``queue.log`` carries TWO timestamp formats, because two logging paths write
into it:

* structlog loggers (``services.downloads.*``, ``services.queue.*`` …) go
  through ``structlog.processors.TimeStamper(fmt="iso")`` →
  ``2026-10-13T10:53:31.169453``
* ``log_queue`` / ``log_unified`` write through the ``popularr.*`` stdlib
  loggers, whose formatter uses ``%Y-%m-%d %H:%M:%S`` →
  ``2026-10-13 10:53:49``

``queue_diagnostics_service`` parsed them with a SPACE-ONLY pattern, so every
``services.*`` line yielded ``created_at=None``; the viewer then evaluated
``new Date(null)``, which is the **epoch**, and rendered 01/01/1970.

The other two parsers of these same logs
(``log_service._filter_lines_last_hour`` and ``templates/pages/logs.html``)
already accepted ``[ T]`` — this one had drifted. All three now share
``_LOG_TS_RE``, and the pattern lives in ONE place so a copy cannot accept a
different subset of formats again.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE = REPO_ROOT / "services" / "queue" / "queue_diagnostics_service.py"

#: A real structlog line (ISO `T` + microseconds) — the one that showed 1970.
T_FORMAT_LINE = (
    "2026-10-13T10:53:31.169453 [WARNING] "
    "[services.downloads.download_completion_service] slskd succeeded but no "
    "local file found (will keep polling) queue_id=6271"
)
#: A real log_queue line (space separator).
SPACE_FORMAT_LINE = (
    "2026-10-13 10:53:49 [INFO] [popularr.queue] [SEARCHING] "
    "BIGBANG - SOBER (KR ver.) → searching Soulseek"
)


def _source() -> str:
    return MODULE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The shared pattern accepts both separators
# ---------------------------------------------------------------------------

class TestTheTimestampPattern:
    def test_it_accepts_the_structlog_iso_form(self) -> None:
        from services.queue.queue_diagnostics_service import _LOG_TS_RE

        match = _LOG_TS_RE.search(T_FORMAT_LINE)
        assert match, "the ISO `T` form must match, or services.* lines read 1970"
        assert match.group(1) == "2026-10-13T10:53:31"

    def test_it_accepts_the_log_queue_space_form(self) -> None:
        from services.queue.queue_diagnostics_service import _LOG_TS_RE

        match = _LOG_TS_RE.search(SPACE_FORMAT_LINE)
        assert match, "the space form must still match"
        assert match.group(1) == "2026-10-13 10:53:49"

    def test_the_pattern_is_defined_once(self) -> None:
        """Two copies is how one viewer shows dates and another shows 1970."""
        source = _source()
        assert source.count("_LOG_TS_RE = re.compile(") == 1, (
            "the timestamp pattern must be declared exactly once"
        )

    def test_no_space_only_pattern_remains(self) -> None:
        """A space-only copy would silently reintroduce the regression."""
        source = _source()
        assert r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}" not in source, (
            "a space-only timestamp pattern remains — every ISO line will "
            "parse to None and render as 1970"
        )
        assert r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}" in source

    def test_every_parser_uses_the_shared_pattern(self) -> None:
        source = _source()
        # _tail_queue_log and _tail_search_log both bound it to a local name.
        assert source.count("_TS_RE = _LOG_TS_RE") == 2, (
            "both log-tail readers must use the shared pattern"
        )


# ---------------------------------------------------------------------------
# 2. The readers actually populate created_at for both formats
# ---------------------------------------------------------------------------

@pytest.fixture
def queue_log_dir(tmp_path, monkeypatch):
    """A temp log dir with one queue.log holding BOTH timestamp formats."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "queue.log").write_text(
        f"{SPACE_FORMAT_LINE}\n{T_FORMAT_LINE}\n",
        encoding="utf-8",
    )
    # resolve_log_dir is imported INSIDE the functions, so patching the source
    # module attribute is what bites.
    import helpers.logging_config as lc

    monkeypatch.setattr(lc, "resolve_log_dir", lambda: str(log_dir))
    return log_dir


class TestTailQueueLog:
    def test_both_formats_get_a_timestamp(self, queue_log_dir) -> None:
        from services.queue import queue_diagnostics_service as diag

        events = diag._tail_queue_log(limit=50)
        assert len(events) == 2, f"expected both lines, got {len(events)}"

        stamps = {e["created_at"] for e in events}
        assert None not in stamps, (
            "a line parsed to created_at=None — that is the 1970 regression "
            f"(stamps={stamps})"
        )
        assert "2026-10-13T10:53:31" in stamps
        assert "2026-10-13T10:53:49" in stamps

    def test_the_iso_line_is_normalised_to_a_T_separator(self, queue_log_dir) -> None:
        """The frontend does `new Date(created_at)`, so the value must be ISO."""
        from services.queue import queue_diagnostics_service as diag

        events = diag._tail_queue_log(limit=50)
        for event in events:
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", event["created_at"]), (
                f"not ISO-ish: {event['created_at']!r}"
            )


class TestTailSearchLog:
    def test_both_formats_get_a_timestamp(self, tmp_path, monkeypatch) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "search.log").write_text(
            f"{SPACE_FORMAT_LINE}\n{T_FORMAT_LINE}\n",
            encoding="utf-8",
        )
        import helpers.logging_config as lc

        monkeypatch.setattr(lc, "resolve_log_dir", lambda: str(log_dir))

        from services.queue import queue_diagnostics_service as diag

        events = diag._tail_search_log(limit=50)
        assert events, "the search log reader returned nothing"
        assert all(e["created_at"] for e in events), (
            "a search-log line parsed to None → 1970 in the viewer"
        )


class TestQueueEventsEndpoint:
    def test_events_carry_a_parseable_timestamp(self, queue_log_dir) -> None:
        from services.queue import queue_diagnostics_service as diag

        # Clear the in-memory store so the endpoint uses the log fallback.
        diag.clear_queue_events()

        payload, status = diag.queue_events({"limit": 50})
        assert status == 200
        events = payload["events"]
        assert events, "no events returned"
        assert all(e.get("created_at") for e in events), (
            "the events endpoint returned a null timestamp — the viewer will "
            "render 1970 for that row"
        )


# ---------------------------------------------------------------------------
# 3. The frontend must not turn a missing timestamp into 1970
# ---------------------------------------------------------------------------

class TestFrontendFallsBackToADash:
    RENDERERS = (
        ("live", REPO_ROOT / "static" / "js" / "downloads.js"),
        ("test_site", REPO_ROOT / "test_site" / "static" / "js" / "pages" / "download-queue.js"),
    )

    @pytest.mark.parametrize("label,path", RENDERERS)
    def test_the_epoch_is_not_used_as_a_fallback(self, label, path) -> None:
        js = path.read_text(encoding="utf-8")
        idx = js.index("async function loadQueueEvents()")
        window = js[idx: idx + 1800]
        assert "new Date(event.created_at)" not in window.replace(
            "event.created_at ? new Date(event.created_at)", ""
        ), (
            f"{label}: `new Date(null)` is the EPOCH, so an unparseable "
            "timestamp renders as 01/01/1970 instead of a dash"
        )
        assert "isNaN(" in window, f"{label}: an Invalid Date must be guarded too"

    @pytest.mark.parametrize("label,path", RENDERERS)
    def test_a_dash_is_the_fallback(self, label, path) -> None:
        js = path.read_text(encoding="utf-8")
        idx = js.index("async function loadQueueEvents()")
        window = js[idx: idx + 1800]
        assert "—" in window or "'-'" in window, (
            f"{label}: a missing timestamp should render as a dash"
        )
