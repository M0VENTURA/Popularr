"""Regression tests: the dead ``download_queue.max_retries`` column is gone.

``max_retries`` was removed in migration ``015_drop_download_queue_max_retries``.
It had never been enforced — ``mark_failed`` read only the retry DELAY and
ignored any ceiling, so an item was retried forever whatever the column said.
It was dead on every other surface too:

* nothing ever WROTE it (only read);
* ``GET /api/musicbrainz/downloads`` never returned it, so the download table's
  ``(Retry N/5)`` badge always fell back to its own client-side constant;
* the JS posted a ``max_retries`` field that no backend handler read;
* ``queue.max_retries`` in ``config.yaml`` had no default and no Config-page
  field, so the lookup could only ever return the hard-coded 5.

Retries are deliberately UNBOUNDED, which is also why ``mark_failed`` never
writes the terminal ``'failed'`` status. These tests pin the removal so the
column (and the phantom limit it advertised) cannot creep back in.

What is NOT removed, and is still live:
* ``retry_delay_minutes`` — the tunable backoff window, still written and read;
* ``retry_count`` — still incremented on every failure;
* ``queue.failure_retry_delay_minutes`` — the config key the delay reads;
* unrelated retry loops (Discogs HTTP, MusicBrainz HTTP, cover-metadata batch,
  Last.fm) that happen to use the same local variable name.
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

import pytest


def _dedent_source(source: str) -> str:
    """``inspect.getsource`` on a method returns an indented block, which
    ``ast.parse`` rejects. Dedent so it can be parsed as a module."""
    return textwrap.dedent(source)


# ---------------------------------------------------------------------------
# The ORM / schema / repository no longer declare the column
# ---------------------------------------------------------------------------

class TestColumnIsGoneFromTheCodebase:

    def test_orm_model_drops_it(self):
        from db.models import DownloadQueue

        assert "max_retries" not in DownloadQueue.__table__.columns

    def test_the_orm_model_keeps_the_live_retry_fields(self):
        """The delay and the counter are still real — removing them would break
        the backoff entirely."""
        from db.models import DownloadQueue

        columns = set(DownloadQueue.__table__.columns.keys())
        assert "retry_count" in columns
        assert "retry_delay_minutes" in columns
        assert "next_retry_at" in columns

    def test_repository_column_list_drops_it(self):
        """``_QUEUE_COLUMNS`` (or equivalent) drives reads/writes; a stale entry
        there would make every queue write reference a missing column."""
        from db.repositories import queue as queue_repo

        source = inspect.getsource(queue_repo)
        assert '"max_retries"' not in source
        assert "'max_retries'" not in source

    def test_schema_bootstrap_drops_it(self):
        """A leftover entry here would RE-ADD the column on boot, silently
        undoing the migration."""
        from db import schema

        source = inspect.getsource(schema)
        # The docstring/comments legitimately mention the name; assert no SQL
        # definition remains.
        assert '"max_retries": "INTEGER' not in source
        assert "'max_retries': 'INTEGER" not in source

    def test_no_migration_after_015_adds_it_back(self):
        """A later revision that re-adds the column would resurrect the phantom
        limit. Only the original create and the drop may mention it."""
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        offenders = []
        for path in sorted(versions.glob("*.py")):
            name = path.name
            if name.startswith("001_initial_schema") or name.startswith("015_"):
                continue  # the historical create, and the drop itself
            if "max_retries" in path.read_text(encoding="utf-8"):
                offenders.append(name)
        assert not offenders, f"a later migration re-introduces max_retries: {offenders}"


# ---------------------------------------------------------------------------
# The drop migration itself
# ---------------------------------------------------------------------------

class TestDropMigration:

    @staticmethod
    def _module():
        import importlib

        return importlib.import_module(
            "migrations.versions.015_drop_download_queue_max_retries"
        )

    def test_it_chains_onto_014(self):
        mod = self._module()
        assert mod.revision == "015_drop_download_queue_max_retries"
        assert mod.down_revision == "014_add_tracks_release_detail"

    def test_it_targets_the_right_column(self):
        mod = self._module()
        assert mod._TABLE == "download_queue"
        assert mod._COLUMN == "max_retries"

    def test_the_drop_is_existence_guarded(self):
        """Unguarded DDL would explode on the SQLite test engine, which creates
        ``download_queue`` without this column."""
        mod = self._module()
        source = inspect.getsource(mod)
        assert "def _has_column" in source
        assert "if _has_column():" in source
        # The inspector call must be exception-guarded for a missing table.
        assert "except Exception" in inspect.getsource(mod._has_column)

    def test_downgrade_restores_the_original_shape(self):
        """The downgrade must reproduce exactly what 001 created: INTEGER with
        server_default 5 and nullable."""
        mod = self._module()
        source = inspect.getsource(mod.downgrade)
        assert "sa.Integer()" in source
        assert 'sa.text("5")' in source
        assert "nullable=True" in source


# ---------------------------------------------------------------------------
# The retry helper no longer pretends a ceiling exists
# ---------------------------------------------------------------------------

class TestRetryHelperReturnsOnlyADelay:

    def test_there_is_no_ceiling_helper(self):
        from db.repositories import queue as queue_repo

        assert not hasattr(queue_repo, "_queue_retry_defaults"), (
            "the tuple-returning helper advertised a max_retries that was never used"
        )

    def test_the_delay_helper_exists_and_returns_an_int(self):
        from db.repositories.queue import _queue_retry_delay_minutes

        delay = _queue_retry_delay_minutes()
        assert isinstance(delay, int)
        assert delay >= 1, "a zero/negative window would busy-loop the worker"

    def test_the_delay_helper_no_longer_reads_max_retries(self):
        """Assert on the EXECUTABLE lines only.

        The docstring deliberately explains what was removed, so a naive
        ``"max_retries" not in source`` matches that prose and fails — the
        comment-matching trap. Blank out comments and docstrings first.
        """
        import ast

        from db.repositories.queue import _queue_retry_delay_minutes

        source = inspect.getsource(_queue_retry_delay_minutes)
        tree = ast.parse(_dedent_source(source))
        # Drop the docstring, then compare only the code body.
        if (
            tree.body
            and isinstance(tree.body[0], ast.FunctionDef)
            and tree.body[0].body
            and isinstance(tree.body[0].body[0], ast.Expr)
            and isinstance(tree.body[0].body[0].value, ast.Constant)
            and isinstance(tree.body[0].body[0].value.value, str)
        ):
            tree.body[0].body.pop(0)
        code_only = ast.unparse(tree)
        code_only = "\n".join(
            line.split("#", 1)[0] for line in code_only.splitlines()
        )

        assert "max_retries" not in code_only, (
            "the helper still reads a max_retries ceiling that is never enforced"
        )
        assert "failure_retry_delay_minutes" in code_only

    def test_mark_failed_still_supplies_the_delay(self):
        """``mark_failed`` must keep passing the configured window, or the
        retry schedule silently collapses to the SQL default."""
        from db.repositories.queue import mark_failed

        source = inspect.getsource(mark_failed)
        assert "_queue_retry_delay_minutes()" in source
        assert '"delay": delay' in source


# ---------------------------------------------------------------------------
# The UI's phantom "Retry N/5" has nothing left to lie about
# ---------------------------------------------------------------------------

class TestTheRouteStillDoesNotServeACeiling:

    @staticmethod
    def _read(relative: str) -> str:
        root = Path(__file__).resolve().parents[1]
        return (root / relative).read_text(encoding="utf-8")

    def test_the_downloads_route_does_not_serialize_max_retries(self):
        """It never did — the badge fell back to the client constant. Pinned so
        nobody "fixes" the display by resurrecting the server field."""
        source = self._read("routes/musicbrainz_routes.py")
        assert "max_retries" not in source

    def test_the_js_badge_still_has_its_own_fallback(self):
        """The value is client-side only, so the fallback is load-bearing: if it
        were removed the badge would render "Retry 3/undefined"."""
        for path in ("static/js/downloads.js", "test_site/static/js/pages/download-queue.js"):
            source = self._read(path)
            assert "dl.max_retries ||" in source, path
