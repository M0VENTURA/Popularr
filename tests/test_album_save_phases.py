"""The album save's per-track writes are three explicit phases.

Step 1 of splitting the save into separately-confirmed phases (so a modal can
report progress per phase and retry only the one that failed). The extraction
is deliberately **behaviour-preserving**: the route's loop still calls the
three writes in the same order, for the same tracks, with the same counters.

## The invariant that must survive any split

``routes/ui_routes.py`` (the loop):

    # album-level values first …
    for field, value in release_values.items():
        if value: payload[field] = value
    # … then the user's per-track review on top
    _staged_for_track = _staged_updates.get(str(track_id))

A per-track value the user reviewed and kept must WIN over the album-wide
default. Nothing pinned that ordering before this change — a refactor could
reverse it and every existing suite would stay green — so it is asserted here
by position.

## Why genres stay one phase

The genre write touches **both** stores (the JSONB columns and the file's
genre tag) and both live inside one block. Splitting them across a DB pass and
a file pass would silently drop the file half — the kind of defect that only
shows up as "my genre edit didn't stick to the files".
"""

from __future__ import annotations

from pathlib import Path

import pytest

import routes.ui_routes as ui

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_SOURCE = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
#: The save handler only — the module has a second writer of ``payload`` and
#: helpers whose names repeat these patterns, so whole-file searches lied.
ROUTE_SOURCE = UI_SOURCE[UI_SOURCE.index("async def album_detail("):]


# ===========================================================================
# 1. Structure — the ordering that a split must not break
# ===========================================================================
class TestThePhasesRunInAnOrderThatPreservesTheReview:
    def test_album_wide_values_are_written_before_the_staged_review(self):
        """Pin the field that ACTUALLY collides — ``writer``.

        ``writer`` is in ``_STAGED_WRITABLE`` and is set album-wide by
        ``payload["writer"] = track_composer``; if the review ran first, saving
        a composer would overwrite the writer the user just kept. (The
        ``release_values`` loop sits after the review, but it writes a
        disjoint set — the release/extended fields — so it is not the
        invariant.)
        """
        album_first = ROUTE_SOURCE.index('payload["writer"] = track_composer')
        staged_next = ROUTE_SOURCE.index("_staged_for_track = _staged_updates.get(")

        assert album_first < staged_next, (
            "the staged per-track review must be applied AFTER the album-wide "
            "values it can override — otherwise an album-level value would "
            "overwrite a value the user explicitly kept during the review"
        )

    def test_the_three_phases_are_called_in_one_pass(self):
        genres = ROUTE_SOURCE.index("_apply_album_track_genres(")
        persist = ROUTE_SOURCE.index("_persist_album_track_payload(track_id, payload)")
        file_tags = ROUTE_SOURCE.index("_write_album_track_file_tags(")

        assert genres < persist < file_tags, (
            "the loop's write order changed (genres → persist → file tags)"
        )

    def test_the_route_does_no_writing_inline(self):
        """The writes must live in the phases, not be duplicated in the route."""
        route = ROUTE_SOURCE

        assert "insert_or_update_track(track_id, payload)" not in route, (
            "the DB write is inline in the route again — the phase is bypassed"
        )
        assert "build_tag_updates(payload)" not in route, (
            "the file-tag write is inline in the route again"
        )
        assert "update_track_genres(" not in route, (
            "the genre write is inline in the route again"
        )


# ===========================================================================
# 2. Phase behaviour
# ===========================================================================
class TestPersistPhase:
    def test_success_reports_ok(self, monkeypatch):
        monkeypatch.setattr(ui, "insert_or_update_track", lambda tid, payload: True)
        assert ui._persist_album_track_payload("t1", {"title": "x"}) == (True, "")

    def test_failure_is_reported_not_swallowed(self, monkeypatch, caplog):
        def _boom(_tid, _payload):
            raise ValueError("invalid input syntax for type json")

        monkeypatch.setattr(ui, "insert_or_update_track", _boom)

        ok, error = ui._persist_album_track_payload("t1", {"title": "x"})

        assert ok is False, "the caller counts this as db_failures"
        assert "invalid input syntax" in error, (
            "the DB's own reason must reach the caller — that is what makes "
            "the failure diagnosable instead of a silent no-op save"
        )


class TestGenresPhase:
    def test_rows_and_failures_are_reported_separately(self, monkeypatch):
        monkeypatch.setattr(
            ui, "resolve_music_file_path", lambda p: "",
        )
        _patch_genres(monkeypatch, rows=3)

        rows, failed = ui._apply_album_track_genres("t1", {"file_path": "x"}, "Rock, Punk")

        assert (rows, failed) == (3, 0)
        assert ui._apply_album_track_genres("t1", {"file_path": "x"}, "  ") == (0, 0), (
            "no genres → nothing to write"
        )

    def test_a_rejected_write_is_counted(self, monkeypatch):
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "")
        _patch_genres(monkeypatch, rows=0, raise_error=True)

        rows, failed = ui._apply_album_track_genres("t1", {"file_path": "x"}, "Rock")

        assert (rows, failed) == (0, 1), (
            "a JSONB rejection must be counted, or a genres-only save looks "
            "like a successful no-op"
        )

    def test_the_file_tag_is_written_too(self, monkeypatch):
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "/music/a.mp3")
        monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: seen.append((path, tags)) or True)
        _patch_genres(monkeypatch, rows=1)

        ui._apply_album_track_genres("t1", {"file_path": "x"}, "Rock; Punk")

        assert seen and seen[0][0] == "/music/a.mp3"
        assert seen[0][1] == {"genres": ["Rock", "Punk"]}, (
            "the genre file write lives in THIS phase — splitting it out "
            "would drop it silently"
        )


class TestFileTagPhase:
    def test_no_file_path_is_reported_as_not_written(self, monkeypatch):
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "")

        ok, path = ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y"},
            strip_disc_numbers=False, disc_staged=False,
        )

        assert (ok, path) == (False, "")
        assert ok is False, "the caller counts this as file_sync_failures"

    def test_the_cover_genre_convention_survives_the_extraction(self, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "/music/a.mp3")
        monkeypatch.setattr(ui, "build_tag_updates", lambda payload: {"title": "y"})
        monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: seen.append(tags) or True)

        ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y", "is_cover": True},
            strip_disc_numbers=False, disc_staged=False,
        )

        assert seen and seen[0].get("genre") == "Cover"

    def test_the_disc_clear_only_applies_to_an_unreviewed_single_disc(self, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "/music/a.mp3")
        monkeypatch.setattr(ui, "build_tag_updates", lambda payload: {"title": "y"})
        monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: seen.append(tags) or True)

        ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y"},
            strip_disc_numbers=True, disc_staged=False,
        )
        ui._write_album_track_file_tags(
            "t1", {"file_path": "x"}, {"title": "y"},
            strip_disc_numbers=True, disc_staged=True,
        )

        assert "disc_number" in seen[0], "single-disc album → clear the frame"
        assert "disc_number" not in seen[1], (
            "a staged disc number is a confirmed correction and must not be "
            "cleared by the album-level heuristic"
        )


def _patch_genres(monkeypatch: pytest.MonkeyPatch, *, rows: int, raise_error: bool = False):
    """Replace the DB genre writer with a controllable one."""

    def _write(*, track_id, genres_str):
        if raise_error:
            raise ValueError("invalid input syntax for type json")
        return rows

    import db.repositories.metadata as meta

    monkeypatch.setattr(meta, "update_track_genres", _write)
