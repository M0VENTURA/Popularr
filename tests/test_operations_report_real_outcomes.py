"""Operations must not report success when the work did not happen.

Follows the reported class of bug (an album save that reported success while the
database refused every row). An AST audit of the tree
(``api_*``/``*_service`` functions that return a literal ``"success": True``
*and* contain an ``except Exception`` + ``continue`` loop) found the same shape
in six places. This module pins each one.

⚠️ The common defect: ``success`` was a **literal**, not a function of what was
written. A batch where every unit of work failed still answered
``success: True`` (usually with HTTP 200), and the count in the payload was the
number of items *attempted*, read by the UI as the number *completed*.
"""

from __future__ import annotations

import ast
import json

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def strip_py_comments(source: str) -> str:
    """Blank out Python comments, preserving newlines.

    REQUIRED, not tidiness. This module asserts that certain tokens must NOT
    appear, and the shipped code deliberately *documents* each trap using the
    very token — the new ``bulk_delete_tracks`` comment quotes the old
    ``"success": True`` it replaced. Matching the raw text therefore matches
    the DOCUMENTATION and reports the opposite of the truth.
    """
    out = list(source)
    i, n = 0, len(source)
    in_str = None
    while i < n:
        ch = source[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
            i += 1
            continue
        if ch == "#":
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _func_body(rel: str, name: str) -> str:
    """Source of one function, located via the AST and COMMENT-STRIPPED."""
    source = strip_py_comments(_src(rel))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"{rel}: function {name}() not found")


# ---------------------------------------------------------------------------
# 1. apply_genres — album
# ---------------------------------------------------------------------------

class TestApplyGenresToAlbum:
    REL = "services/metadata/album_service.py"

    def test_success_is_conditional(self):
        body = _func_body(self.REL, "apply_genres_to_album")
        assert '"success": True' not in body, (
            "success is a literal — a wholly failed album still reports success"
        )
        assert '"success": updated > 0' in body

    def test_it_reports_the_failure_reason(self):
        body = _func_body(self.REL, "apply_genres_to_album")
        assert '"error"' in body, "a failed call must carry an error message"

    def test_it_resolves_the_stored_path_before_writing(self):
        body = _func_body(self.REL, "apply_genres_to_album")
        assert "resolve_music_file_path" in body, (
            "the DB may hold a path relative to the music root; the writer "
            "returns False for anything that does not exist on disk"
        )

    def test_a_tag_write_that_returns_false_is_not_counted_as_updated(self):
        body = _func_body(self.REL, "apply_genres_to_album")
        # The count must be driven by the writer's return value.
        assert "if update_file_tags(" in body


# ---------------------------------------------------------------------------
# 2. bulk_delete_tracks
# ---------------------------------------------------------------------------

class TestBulkDeleteTracks:
    REL = "services/metadata/album_service.py"

    def test_success_is_conditional(self):
        body = _func_body(self.REL, "bulk_delete_tracks")
        assert '"success": True' not in body, (
            "deleting nothing reported a completed delete; the UI announced "
            "'Deleted 0 track(s)' as a success"
        )
        assert '"success": deleted_count > 0' in body

    def test_a_missing_row_counts_as_a_failure(self):
        body = _func_body(self.REL, "bulk_delete_tracks")
        assert "failed_count" in body, (
            "a requested id that no longer exists is not a deletion and must "
            "not be silently skipped"
        )

    def test_the_status_code_reflects_the_outcome(self):
        body = _func_body(self.REL, "bulk_delete_tracks")
        assert "200 if deleted_count else 500" in body


# ---------------------------------------------------------------------------
# 3. apply_genres — artist (the path the rebuilt UI uses)
# ---------------------------------------------------------------------------

class TestApplyGenresArtist:
    REL = "services/metadata/artist_metadata_service.py"

    def test_success_is_conditional(self):
        body = _func_body(self.REL, "apply_genres")
        assert '"success": True' not in body
        assert '"success": updated > 0' in body

    def test_the_tag_write_is_not_bare_excepted_into_silence(self):
        body = _func_body(self.REL, "apply_genres")
        assert "except Exception:\n                        pass" not in body, (
            "the tag write was swallowed by `except Exception: pass`, so a run "
            "where every file write failed still reported the genres applied"
        )
        # It must count and log instead.
        assert "files_failed" in body

    def test_it_resolves_the_stored_path(self):
        body = _func_body(self.REL, "apply_genres")
        assert "resolve_music_file_path" in body

    def test_it_checks_the_writers_return_value(self):
        body = _func_body(self.REL, "apply_genres")
        assert "if write_tags_to_file(" in body, (
            "the writer returns False for an unresolvable path or a disabled "
            "tagging toggle; ignoring that counts a no-op as a success"
        )


# ---------------------------------------------------------------------------
# 4. fix_album_field — checks the writer's result and resolves the path
# ---------------------------------------------------------------------------

class TestFixAlbumField:
    REL = "services/metadata/correction_service.py"

    def test_it_checks_the_writers_return_value(self):
        body = _func_body(self.REL, "fix_album_field")
        assert "if write_tags_to_file(" in body, (
            "files_updated counted every call that did not RAISE, so a writer "
            "returning False was still reported as a successful write"
        )

    def test_it_resolves_the_stored_path(self):
        body = _func_body(self.REL, "fix_album_field")
        assert "resolve_music_file_path" in body

    def test_it_reports_partial_failure(self):
        body = _func_body(self.REL, "fix_album_field")
        assert "files_failed" in body


# ---------------------------------------------------------------------------
# 5. Tag writers that silently no-op'd
# ---------------------------------------------------------------------------

class TestTagWritersResolveAndReport:
    CASES = [
        ("services/metadata/album_tag_sync_service.py", "resolve_music_file_path"),
        ("services/metadata/album_name_update_service.py", "resolve_music_file_path"),
    ]

    @pytest.mark.parametrize("rel,needle", CASES)
    def test_the_path_is_resolved(self, rel: str, needle: str):
        source = _src(rel)
        assert needle in source, (
            f"{rel}: writes the DB's stored path straight to the tag writer, "
            "which returns False for a path relative to the music root"
        )

    def test_tag_fill_failure_is_not_debug_only(self):
        """A tag fill that wrote nothing is exactly the reported symptom."""
        source = _src("services/metadata/album_tag_sync_service.py")
        assert "Tag fill did not write the file" in source
        assert 'logger.debug("Tag fill failed"' not in source, (
            "the failure is logged at DEBUG and vanishes from a normal log tail"
        )


# ---------------------------------------------------------------------------
# 6. The legacy /api/genres/apply must not claim success for zero rows
# ---------------------------------------------------------------------------

class TestLegacyGenreApplyEndpoint:
    REL = "routes/misc_routes.py"

    def test_it_does_not_report_success_for_zero_rows(self):
        body = _func_body(self.REL, "api_apply_genres")
        assert "if not affected:" in body, (
            "static/js/genre-utils.js branches on data.error, not data.success, "
            "so a hard-coded success made an artist with no tracks look applied"
        )
        assert '"error"' in body


# ---------------------------------------------------------------------------
# 7. The audit itself must stay clean for the fixed functions
# ---------------------------------------------------------------------------

class TestNoLiteralSuccessWithSwallowRemains:
    """A regression guard for the whole defect class.

    Re-runs the audit heuristic over the modules this change touched: no
    write-oriented function may return a literal ``"success": True`` while also
    containing an ``except Exception`` + ``continue`` loop.
    """

    MODULES = [
        "services/metadata/album_service.py",
        "services/metadata/artist_metadata_service.py",
        "services/metadata/correction_service.py",
    ]

    def test_no_literal_success_hides_a_failed_loop(self):
        offenders = []
        for rel in self.MODULES:
            source = strip_py_comments(_src(rel))
            tree = ast.parse(source)
            lines = source.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = "\n".join(lines[node.lineno - 1: node.end_lineno])
                if '"success": True' not in body:
                    continue
                if "except Exception" in body and "continue" in body:
                    if any(k in node.name for k in ("apply", "bulk", "delete", "tag", "save", "update")):
                        offenders.append(f"{rel}::{node.name}")
        assert not offenders, (
            "these functions report literal success while swallowing per-item "
            f"failures: {offenders}"
        )
