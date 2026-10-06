"""Queue and search decisions must actually reach ``queue.log`` / ``search.log``.

Reported
--------
> Check through the queue and search and make sure all parts of it are logged
> including the matching of songs. They should be going to the search.log or
> the queue.log.

How routing works (`helpers/logging_config.py::dictConfig`)
-----------------------------------------------------------
============================================ ==============================
queue.log                                    ``services.queue``,
                                             ``services.downloads``,
                                             ``db.repositories.queue``,
                                             ``db.repositories.queue_admin``,
                                             and ``log_queue()`` →
                                             ``popularr.queue``
search.log                                   **only** ``log_search()`` →
                                             ``popularr.search`` (two call
                                             sites in the whole codebase)
============================================ ==============================

So a statement reaches the right file only if its *module logger* is routed or
it calls the helper. These tests pin the three decisions that used to be
invisible:

1. **A successful match was never logged.** ``Best result found`` was
   ``logger.debug`` in a module whose level is INFO — so the failure path
   (a WARNING naming the gates, counts, ``top_score`` and ``top_candidate``)
   was visible, but what the pipeline *chose* when it succeeded was dropped.
   That is "the matching of songs" being absent from the log.
2. **The queue processor's cycle failures only reached unified** —
   ``services/scheduler`` is not in the routing table, so
   ``download_queue_processor cycle failed`` never reached ``queue.log``.
3. The two search summaries (manual + automatic) must keep going to
   ``search.log`` — they are the only thing that does.

The per-candidate rejection lines stay DEBUG on purpose: they fire hundreds of
times per search. The aggregate WARNING carries their counts per gate, which is
what a log tail needs.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = REPO_ROOT / "services" / "downloads" / "download_pipeline_service.py"
SCHEDULER = REPO_ROOT / "services" / "scheduler" / "scheduler_service.py"
SEARCH_ROUTES = REPO_ROOT / "routes" / "download_search_routes.py"
LOGGING_CONFIG = REPO_ROOT / "helpers" / "logging_config.py"


class TestAMatchIsLoggedWhenItSucceeds:
    def test_the_selected_candidate_is_logged_at_info(self):
        src = PIPELINE.read_text(encoding="utf-8")

        assert "Selected download candidate" in src, (
            "a successful match is never recorded — the queue log can say why "
            "nothing matched but not what it chose"
        )
        idx = src.index("Selected download candidate")
        window = src[max(0, idx - 400): idx]

        assert "logger.info(" in window, (
            "must be INFO: the module's level is INFO, so DEBUG is dropped"
        )
        assert 'logger.debug("Best result found"' not in src, (
            "the old DEBUG line is still the one being emitted"
        )

    def test_the_line_names_the_track_not_just_the_file(self):
        """A bare filename cannot be attributed to a queue item in a log tail."""
        src = PIPELINE.read_text(encoding="utf-8")
        idx = src.index("Selected download candidate")
        body = src[idx: idx + 500]

        assert "expected_artist" in body and "expected_title" in body

    def test_failures_still_name_the_gates(self):
        """CONTROL — the WARNING summary must survive (it is the other half)."""
        src = PIPELINE.read_text(encoding="utf-8")
        assert "No result met min_score" in src
        for gate in ("year_mismatch", "no_artist_evidence", "title_mismatch",
                     "album_mismatch"):
            assert f'_note_reject(rejects, "{gate}")' in src, (
                f"the {gate} gate no longer reports why it rejected"
            )
        # ``below_floor`` is counted directly rather than through the helper.
        assert 'rejects["below_floor"] = below_floor' in src


class TestTheQueueProcessorReachesQueueLog:
    def test_cycle_and_spawn_failures_are_written_to_queue_log(self):
        src = SCHEDULER.read_text(encoding="utf-8")

        assert "log_queue(" in src, (
            "services/scheduler is not in the routing table, so without the "
            "helper the queue processor's failures only reach unified"
        )
        assert "download_queue_processor cycle failed" in src, (
            "the cycle-failure message moved — the log_queue call must move with it"
        )
        assert "from helpers.logging_config import" in src


class TestSearchSummariesReachSearchLog:
    def test_both_search_paths_log_a_summary(self):
        """CONTROL — search.log has exactly two feeders; losing either is a gap."""
        for path in (SEARCH_ROUTES, PIPELINE):
            src = path.read_text(encoding="utf-8")
            assert "log_search(" in src, (
                f"{path.name} no longer writes to search.log"
            )

    def test_the_search_logger_is_routed(self):
        src = LOGGING_CONFIG.read_text(encoding="utf-8")
        assert '"popularr.search"' in src
        assert '"search_file"' in src
