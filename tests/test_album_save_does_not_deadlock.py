"""An album save must never write a FILE while a DB transaction is open.

REPORT

> "When saving album changes since the updates yesterday, its timing out. It
>  worked fine before the changes"

CAUSE

``update_album_ids`` copied+rewrote every file of the album from INSIDE the
``with db_session()`` block that had just run ``UPDATE tracks … WHERE album=…``.
That UPDATE holds a write lock on every row of the album until the block exits.

Every successful file write calls ``mark_local_tags_written_for_path``, which
opens its OWN session and UPDATEs those same rows. Those rows are locked by the
outer transaction, which cannot commit until the file loop returns, which cannot
finish until the inner UPDATE completes. PostgreSQL: self-deadlock, waits
forever. SQLite: 5 s busy-timeout PER FILE — a 14-track album spent ~70 s and the
request timed out.

``force=True`` is what made it a regression: without it, ``fill_missing_only``
skipped a populated MUSICBRAINZ_ALBUMID frame and returned before reaching the
nested session. ``force=True`` made every write "succeed", so every file reached
the lock.

These tests assert the STRUCTURE (no file write inside the transaction), because
a timing test is flaky and a real lock test needs a second process.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from services.metadata import album_service

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _source() -> str:
    return inspect.getsource(album_service.update_album_ids)


def _tree() -> ast.AST:
    return ast.parse(inspect.getsource(album_service.update_album_ids))


class TestNoFileWriteInsideTheTransaction:
    def test_the_file_loop_is_not_inside_a_db_session(self) -> None:
        """⭐ THE REGRESSION ITSELF.

        ``update_file_tags`` must not be reachable from within the
        ``with db_session()`` body, because each call opens a second session to
        UPDATE rows the first session has locked.
        """
        tree = _tree()
        db_with_bodies = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                "db_session" in ast.unparse(item.context_expr)
                for item in node.items
            )
        ]
        assert db_with_bodies, "the function no longer opens a db_session at all"

        for body in db_with_bodies:
            inside = ast.unparse(body)
            assert "update_file_tags" not in inside, (
                "update_file_tags is called INSIDE the open transaction; each "
                "write opens a nested session that UPDATEs the rows this "
                "transaction has locked, so the save deadlocks/timeouts"
            )

    def test_the_paths_are_collected_then_written_afterwards(self) -> None:
        src = _source()
        assert "_file_targets" in src, (
            "file paths must be collected during the transaction and written "
            "after it commits"
        )
        # The collect phase appends; the write phase iterates.
        assert "_file_targets.append" in src
        assert "for _track_id, _path in _file_targets" in src

    def test_the_write_loop_comes_after_the_with_block(self) -> None:
        """Ordering matters: a write before the commit is the bug."""
        src = _source()
        collect_at = src.index("_file_targets.append")
        # The write phase's loop header.
        write_at = src.index("for _track_id, _path in _file_targets")
        assert collect_at < write_at, (
            "the write loop must follow the collection, not precede it"
        )


class TestTheCorrectionStillWorks:
    """The fix must not lose the behaviour the fan-out was added for."""

    def test_force_is_still_used_for_an_authoritative_id(self) -> None:
        """Without ``force`` a populated frame is skipped and the bug returns."""
        assert "force=True" in _source()

    def test_every_row_is_still_stamped(self) -> None:
        """The stamp is what stops the next import reverting the id."""
        assert "_mark_local_correction(stamped_ids)" in _source()

    def test_the_stamp_covers_rows_off_the_page(self) -> None:
        """Ids come from the SAME predicate as the UPDATE, not the file loop."""
        src = _source()
        assert "SELECT id FROM tracks" in src
        assert "stamped_ids" in src


class TestOnlyChangedFilesAreRewritten:
    """``force=True`` disables the writer's own "already set?" check.

    The album page posts its release-id input on EVERY save, so without this
    skip an unrelated edit rewrote every file of the album.
    """

    def _fn(self):
        fn = getattr(album_service, "_row_already_has_ids", None)
        assert fn is not None, "missing the skip predicate"
        return fn

    def test_an_identical_id_is_skipped(self) -> None:
        assert self._fn()("ABC", "", "ABC", "") is True

    def test_case_and_whitespace_do_not_defeat_the_skip(self) -> None:
        assert self._fn()(" abc ", "", "ABC", "") is True

    def test_a_different_id_is_written(self) -> None:
        assert self._fn()("OLD", "", "NEW", "") is False

    def test_a_stale_release_group_alone_still_writes(self) -> None:
        """Half-corrected is exactly the state that split the album."""
        assert self._fn()("SAME", "OLD-RG", "SAME", "NEW-RG") is False

    def test_an_absent_field_does_not_force_a_write(self) -> None:
        """``want`` empty means "not in this payload", not "clear it"."""
        assert self._fn()("ABC", "RG1", "ABC", "") is True

    def test_the_prior_value_is_read_before_the_update(self) -> None:
        """⭐ Ordering is the whole correctness argument.

        Read AFTER the UPDATE, every row reports the new value, the skip always
        fires, and no file is ever written — silently restoring the bug.
        """
        src = _source()
        prior_at = src.index("_prior_ids[_tid]")
        update_at = src.index("UPDATE tracks SET")
        assert prior_at < update_at, (
            "the prior ids are read after the UPDATE, so every row looks "
            "already-correct and no file is written"
        )


class TestTheClearPathIsAlsoSafe:
    def test_the_clear_writes_files_after_its_session_closes(self) -> None:
        """``_clear_album_id_file_tags`` opens its own session and closes it
        before the write loop — the shape the id path now matches."""
        src = inspect.getsource(album_service._clear_album_id_file_tags)
        tree = ast.parse(src)
        db_with_bodies = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                "db_session" in ast.unparse(item.context_expr)
                for item in node.items
            )
        ]
        assert db_with_bodies
        for body in db_with_bodies:
            assert "update_file_tags" not in ast.unparse(body), (
                "the clear path writes a file inside its transaction too"
            )
