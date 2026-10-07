"""Queue and search lines must live ONLY in their own log files.

Reported:

> download logs are being added to the info log, these should only be in
> queue or search logs

Auditing the current build found the routing CORRECT — ``log_search`` →
``search.log`` only, ``log_queue`` → ``queue.log`` only — and the reported
line format (``2026-10-05 17:16:29 INFO popularr.search …``, no brackets)
matches only the renderer that lived between 2026-08-06 and 2026-08-23, i.e.
the pasted lines come from a stale build/log volume, not from this code.

This suite pins the guarantee so it cannot regress:

1. the dictConfig contract routes each dedicated logger to ITS file with
   ``propagate=False``;
2. behaviourally, each emitter reaches only its own file — with a control
   proving the info log IS being written (so the negatives are not vacuous);
3. the emit-time guard in ``log_search``/``log_queue`` re-asserts
   ``propagate=False`` even when something flipped it back on.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from helpers import logging_config as lc  # noqa: E402

SEARCH_LINE = "[AUTOMATIC] routing probe :: Artist | Title -> 1 results in 20.9s (download_started)"
QUEUE_LINE = "[QUEUE] routing probe"
UNIFIED_LINE = "[NAVIDROME_IMPORT] routing probe"
CONTROL_LINE = "control info line"


# ---------------------------------------------------------------------------
# 1. The dictConfig contract
# ---------------------------------------------------------------------------

class TestTheDictConfigContract:
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path):
        self.config = lc._build_logging_config("RoutingProbe", str(tmp_path))

    def test_search_goes_to_search_file_only(self):
        entry = self.config["loggers"]["popularr.search"]
        assert entry["handlers"] == ["search_file"]
        assert entry["propagate"] is False
        assert "info_file" not in entry["handlers"]
        assert "debug_file" not in entry["handlers"]

    def test_queue_goes_to_queue_file_only(self):
        entry = self.config["loggers"]["popularr.queue"]
        assert entry["handlers"] == ["queue_file"]
        assert entry["propagate"] is False

    def test_unified_goes_to_unified_file_only(self):
        entry = self.config["loggers"]["popularr.unified"]
        assert entry["handlers"] == ["unified_file"]
        assert entry["propagate"] is False

    def test_the_root_logger_still_carries_the_info_file(self):
        """CONTROL — info.log must exist and receive *something*, or the
        behavioural negatives below would pass vacuously."""
        assert "info_file" in self.config["loggers"][""]["handlers"]


# ---------------------------------------------------------------------------
# 2. Behaviour: the files receive exactly their own lines
# ---------------------------------------------------------------------------

class TestTheFilesReceiveOnlyTheirOwnLines:
    @pytest.fixture(autouse=True)
    def _routing(self, monkeypatch, tmp_path):
        """Point every handler at a temp log dir and reconfigure logging."""
        original_log_path = os.environ.get("LOG_PATH")
        monkeypatch.setenv("LOG_PATH", str(tmp_path))

        lc.setup_logging("RoutingProbe")
        yield tmp_path

        # Restore the suite's original log destination for the tests after this.
        if original_log_path is None:
            monkeypatch.delenv("LOG_PATH", raising=False)
        else:
            monkeypatch.setenv("LOG_PATH", original_log_path)
        lc.setup_logging("WebUI")

        # Close the temp-dir handlers so Windows can release them.
        for handler in list(logging.getLogger().handlers) + [
            h for name in ("popularr.search", "popularr.queue", "popularr.unified")
            for h in logging.getLogger(name).handlers
        ]:
            try:
                handler.close()
            except Exception:
                pass

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""

    def test_each_line_lands_in_its_own_file_only(self, tmp_path):
        lc.log_search(SEARCH_LINE)
        lc.log_queue(QUEUE_LINE)
        lc.log_unified(UNIFIED_LINE)
        logging.getLogger("control.info.probe").info(CONTROL_LINE)

        for handler in list(logging.getLogger().handlers) + [
            h for name in ("popularr.search", "popularr.queue", "popularr.unified")
            for h in logging.getLogger(name).handlers
        ]:
            handler.flush()

        search = self._read(tmp_path / "search.log")
        queue = self._read(tmp_path / "queue.log")
        unified = self._read(tmp_path / "unified_scan.log")
        info = self._read(tmp_path / "info.log")

        # The three dedicated files got their lines…
        assert SEARCH_LINE in search, "the search line must reach search.log"
        assert QUEUE_LINE in queue, "the queue line must reach queue.log"
        assert UNIFIED_LINE in unified, "the unified line must reach unified_scan.log"

        # …and NONE of them leaked into the info log (the report).
        assert SEARCH_LINE not in info, "a search line leaked into info.log"
        assert QUEUE_LINE not in info, "a queue line leaked into info.log"
        assert UNIFIED_LINE not in info, "a unified line leaked into info.log"
        assert SEARCH_LINE not in unified, "a search line leaked into unified_scan.log"
        assert SEARCH_LINE not in queue, "a search line leaked into queue.log"

        # CONTROL: the info log IS being written by an ordinary propagating
        # logger — the assertions above are not vacuous.
        assert CONTROL_LINE in info, "the control line must reach info.log"
        # …and this build's renderer brackets the level/name, which the
        # reported paste format (bare "INFO popularr.search") does not —
        # evidence the pasted lines came from a stale build's logs.
        assert "[control.info.probe]" in info


# ---------------------------------------------------------------------------
# 3. The emit-time guard
# ---------------------------------------------------------------------------

class TestTheEmitTimeGuard:
    @pytest.fixture(autouse=True)
    def _routing(self, monkeypatch, tmp_path):
        original_log_path = os.environ.get("LOG_PATH")
        monkeypatch.setenv("LOG_PATH", str(tmp_path))
        lc.setup_logging("RoutingProbe")
        yield tmp_path
        if original_log_path is None:
            monkeypatch.delenv("LOG_PATH", raising=False)
        else:
            monkeypatch.setenv("LOG_PATH", original_log_path)
        lc.setup_logging("WebUI")

    def test_a_flipped_propagate_is_re_asserted_before_the_emit(self, tmp_path):
        """A third-party reconfiguration that re-enables propagation must not
        duplicate search lines into the info/debug logs."""
        search_logger = logging.getLogger("popularr.search")
        search_logger.propagate = True  # simulate the flip

        caught: list[str] = []
        root = logging.getLogger()

        class _Spy(logging.Handler):
            def emit(self, record):
                caught.append(record.name)

        spy = _Spy()
        root.addHandler(spy)
        try:
            lc.log_search(SEARCH_LINE)
        finally:
            try:
                root.handlers.remove(spy)
            except ValueError:
                pass

        assert search_logger.propagate is False, "the emit-time guard must re-assert separation"
        assert "popularr.search" not in caught, "the record escaped to the root (info/debug path)"
        assert SEARCH_LINE in (tmp_path / "search.log").read_text(
            encoding="utf-8", errors="ignore"
        )
