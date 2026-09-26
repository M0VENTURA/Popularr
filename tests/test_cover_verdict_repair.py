"""The repair for cover verdicts cleared without evidence.

``track_stage`` used to clear ``is_cover`` from the result of a SHALLOW cover
check, so genuine covers were unflagged before the deep detection pass had run.
Those rows are already damaged in the database, and fixing the code does not
heal them: the verdict is stored, and nothing re-derives it.

WHAT MAKES THE REPAIR POSSIBLE
------------------------------
The removed branch wrote a distinctive reason string —
``"cover attribution removed from title"`` — and wrote it UNCONDITIONALLY, not
on evidence. Nothing else in the codebase produces it, so it is an exact
fingerprint of the damage.

⚠️ The flag alone cannot be used: ``is_cover = 0`` is the normal state of
almost every track in a library, so "every unflagged track" would re-flag the
entire catalogue as covers.

The repair only RE-FLAGS. It does not decide the cover question — the deep
detector does that on the next album pass, with real evidence, and its own
``_clear_unconfirmed_verdicts`` clears the row again when nothing confirms it.
That is the whole point: the verdict is decided once, with evidence, rather than
deleted on a shallow ``no_match``.
"""

from __future__ import annotations

import pytest

from services.enrichment import cover_verdict_repair_service as repair


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Recorder:
    """Captures the SQL + params the repair issues."""

    def __init__(self, rowcount: int = 0) -> None:
        self.sql = ""
        self.params: dict = {}
        self._rowcount = rowcount

    def execute(self, statement, params):
        self.sql = str(statement)
        self.params = dict(params)
        return _Result(self._rowcount)


@pytest.fixture()
def fake_db(monkeypatch):
    """Patch db.engine.db_session and return the recorder."""
    from contextlib import contextmanager

    recorder = _Recorder(rowcount=3)

    @contextmanager
    def _session(*_a, **_k):
        yield recorder

    import db.engine as engine

    monkeypatch.setattr(engine, "db_session", _session)
    return recorder


class TestTheFingerprintIsTheReasonNotTheFlag:
    def test_it_selects_on_the_reason_string(self, fake_db):
        repair.repair_shallowly_cleared_cover_verdicts()
        assert "is_cover_reason = :damaged_reason" in fake_db.sql, (
            "the repair must key off the reason string written by the removed "
            "branch; `is_cover = 0` alone matches almost the whole library"
        )
        assert fake_db.params["damaged_reason"] == "cover attribution removed from title"

    def test_it_only_touches_rows_that_are_currently_unflagged(self, fake_db):
        """The predicate must select UNFLAGGED rows — expressed as a boolean.

        ⚠️ This used to assert the literal text ``COALESCE(is_cover, 0) = 0``,
        which pinned the DEFECT rather than the intent. ``is_cover`` is BIGINT so
        that specific predicate is legal, but the sibling predicate on
        ``cover_manual_override`` (BOOLEAN) used the same shape and made the
        whole statement fail on PostgreSQL with
        "COALESCE types boolean and integer cannot be matched" — so the test was
        green while the repair never ran in production.

        Asserted on the COLUMN rather than an exact string, so the correct
        boolean spelling (``is_cover IS NOT TRUE``) is required but a future
        rename of the SQL formatting does not break it.
        """
        repair.repair_shallowly_cleared_cover_verdicts()
        sql = fake_db.sql
        assert "is_cover" in sql, "the repair must filter on the cover flag"
        assert "IS NOT TRUE" in sql.upper(), (
            "the unflagged-row predicate must use boolean syntax "
            "(`IS NOT TRUE`); the integer form fails on PostgreSQL"
        )
        assert "COALESCE(cover_manual_override, 0)" not in sql, (
            "cover_manual_override is BOOLEAN, so COALESCE with 0 is exactly the "
            "`COALESCE types boolean and integer cannot be matched` error that "
            "stopped this repair running in production"
        )

    def test_it_never_touches_a_manual_override(self, fake_db):
        """A user-locked verdict outranks any repair."""
        repair.repair_shallowly_cleared_cover_verdicts()
        assert "cover_manual_override" in fake_db.sql, (
            "the repair must exclude manual overrides or it can overwrite a "
            "user's explicit decision"
        )

    def test_it_sets_the_flag_and_nothing_else(self, fake_db):
        """Notably NOT the title or the genres."""
        repair.repair_shallowly_cleared_cover_verdicts()
        assert "is_cover = 1" in fake_db.sql
        assert "title" not in fake_db.sql.replace("title_had", ""), (
            "the title strip was the legitimate half of the old branch and must "
            "not be undone"
        )
        assert "genre" not in fake_db.sql, (
            "the genres are the deep detector's business, not this repair's"
        )

    def test_it_resets_cover_last_checked(self, fake_db):
        """Otherwise the deep pass SKIPS the rows this repair just re-flagged.

        The detector skips any track assessed within ``COVER_RECHECK_DAYS``
        (90). Re-flagging without clearing the timestamp would put the row in
        front of it only to be skipped as fresh — the repair would appear to do
        nothing for up to three months.
        """
        repair.repair_shallowly_cleared_cover_verdicts()
        assert "cover_last_checked = NULL" in fake_db.sql, (
            "the repair must clear cover_last_checked, or the deep detector "
            "skips the re-flagged row as already-fresh"
        )


class TestIdempotence:
    def test_the_repaired_reason_differs_from_the_fingerprint(self, fake_db):
        """Otherwise a second run re-flags the same rows forever."""
        repair.repair_shallowly_cleared_cover_verdicts()
        assert fake_db.params["repaired_reason"] != repair.DAMAGED_REASON, (
            "the repair must overwrite the fingerprint it searches for, or it "
            "is not idempotent"
        )

    def test_the_new_reason_is_distinctive(self):
        assert repair.REPAIRED_REASON.strip()
        assert repair.REPAIRED_REASON != repair.DAMAGED_REASON


class TestItReportsAndSurvives:
    def test_it_reports_the_count(self, fake_db):
        result = repair.repair_shallowly_cleared_cover_verdicts()
        assert result["repaired"] == 3

    def test_a_zero_row_result_is_not_an_error(self, monkeypatch):
        from contextlib import contextmanager

        recorder = _Recorder(rowcount=0)

        @contextmanager
        def _session(*_a, **_k):
            yield recorder

        import db.engine as engine

        monkeypatch.setattr(engine, "db_session", _session)
        result = repair.repair_shallowly_cleared_cover_verdicts()
        assert result["repaired"] == 0
        assert "error" not in result

    def test_a_failure_never_raises(self, monkeypatch):
        """A boot repair must not be able to prevent a boot."""
        from contextlib import contextmanager

        @contextmanager
        def _boom(*_a, **_k):
            raise RuntimeError("table missing")
            yield  # pragma: no cover

        import db.engine as engine

        monkeypatch.setattr(engine, "db_session", _boom)
        result = repair.repair_shallowly_cleared_cover_verdicts()
        assert result["repaired"] == 0
        assert "error" in result


class TestItIsWiredIntoBoot:
    def test_bootstrap_calls_it_on_the_immediate_path(self):
        import inspect

        import db.bootstrap as bootstrap

        source = inspect.getsource(bootstrap.init_database_and_schema)
        assert "_repair_shallowly_cleared_cover_verdicts_at_boot" in source, (
            "the repair must run on the normal startup path or already-damaged "
            "libraries are never healed"
        )

    def test_bootstrap_calls_it_on_the_deferred_path_too(self):
        import inspect

        import db.bootstrap as bootstrap

        source = inspect.getsource(bootstrap._run_deferred_startup_migrations)
        assert "_repair_shallowly_cleared_cover_verdicts_at_boot" in source, (
            "the deferred path runs when the immediate bootstrap could not, so "
            "it must repair there as well"
        )

    def test_it_runs_in_a_daemon_thread(self):
        import inspect

        import db.bootstrap as bootstrap

        source = inspect.getsource(
            bootstrap._repair_shallowly_cleared_cover_verdicts_at_boot
        )
        assert "daemon=True" in source, (
            "a boot repair must never be able to block startup"
        )
