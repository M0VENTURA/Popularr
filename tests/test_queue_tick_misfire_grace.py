"""A queue tick that runs 38s long must not be discarded.

REPORTED in a popularity-scan log:

    Run time of job "Process download queue" was missed by 0:00:38.

``get_scheduler()`` sets ``job_defaults = {"coalesce": True, "max_instances": 1,
"misfire_grace_time": 300}``, and the download-queue job then overrode the grace
**down to 30** — the only job that did. APScheduler discards a run whose start
is late by more than the grace, so a tick that ran 38s long passed 30s by 8
seconds and the next run was thrown away, leaving downloads idling for a whole
extra interval.

Parsed with ``ast`` rather than matched as text, so a later hand-edit cannot
quietly reintroduce the override.
"""
from __future__ import annotations

import ast
from pathlib import Path

SCHEDULER = (
    Path(__file__).resolve().parents[1]
    / "services" / "scheduler" / "scheduler_service.py"
)


def _scheduler_source() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


def _put_call_for(job_id: str) -> ast.Call:
    tree = ast.parse(_scheduler_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_put":
            continue
        if any(
            isinstance(arg, ast.Constant) and arg.value == job_id
            for arg in node.args
        ):
            return node
    raise AssertionError(f"no _put() call for {job_id!r}")


def _job_defaults() -> dict[str, ast.AST]:
    tree = ast.parse(_scheduler_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (getattr(node.func, "id", "") or getattr(node.func, "attr", "")) != "BackgroundScheduler":
            continue
        for kw in node.keywords:
            if kw.arg == "job_defaults" and isinstance(kw.value, ast.Dict):
                return {
                    key.value: value
                    for key, value in zip(kw.value.keys, kw.value.values)
                    if isinstance(key, ast.Constant)
                }
    raise AssertionError("BackgroundScheduler(job_defaults=...) not found")


class TestTheQueueTickIsNotDiscarded:
    def test_the_download_queue_job_does_not_lower_the_grace(self):
        keywords = {kw.arg: kw.value for kw in _put_call_for("download_queue_processor").keywords}

        assert "misfire_grace_time" not in keywords, (
            "overriding the global 300s grace DOWN is what discarded the tick "
            "reported as 'Run time of job ... was missed by 0:00:38'"
        )

    def test_the_global_default_grace_covers_that_overrun(self):
        grace = _job_defaults().get("misfire_grace_time")
        assert grace is not None, "job_defaults must set a misfire grace"
        assert isinstance(grace, ast.Constant)
        assert grace.value >= 300, "a 38s overrun must fit inside the default grace"

    def test_job_defaults_still_carry_coalesce_and_max_instances(self):
        """Lateness is only safe because the backlog collapses into one run."""
        assert {"coalesce", "max_instances"} <= set(_job_defaults())

        call = _put_call_for("download_queue_processor")
        assert {kw.arg for kw in call.keywords} >= {"max_instances", "coalesce"}, (
            "the queue job sets both explicitly, so a later edit that drops "
            "them cannot silently reintroduce overlapping ticks"
        )
