"""A refused track write must not be silent, and must not be reported as success.

Reported (Postgres log while saving an album):

    ERROR:  invalid input syntax for type json at character 756
    DETAIL:  Token "alternative" is invalid.
    CONTEXT:  JSON data, line 1: alternative...
    STATEMENT:  INSERT INTO tracks (…, musicbrainz_genres, …)
                VALUES (…, 'alternative rock, britpop, rock', …)
                ON CONFLICT (id) DO UPDATE SET …

``musicbrainz_genres`` is JSONB and was handed a CSV string, so PostgreSQL
rejected the whole statement. That is the same root cause as the album-save bug
fixed in ``0a66dfa9`` (generic coercion) and ``c95c3f2f`` (raw-SQL writers).

⚠️ WHAT THIS MODULE COVERS THAT THOSE DID NOT — the **swallowed exception**.

``upsert_tracks_bulk`` wrapped every row in ``except Exception`` and logged at
**DEBUG**::

    logger.debug("Bulk track upsert skipped for %s: %s", payload.get("id"), exc)

so a failed batch returned ``False`` — which **no caller checks** — and a normal
log tail showed nothing at all. The scan reported success while every row of the
affected album was discarded.

``bulk_tag_tracks`` was worse: its per-track ``except`` logged an ERROR and then
``continue``d, but the function still returned ``{"success": True}``, so the
endpoint answered 200 and the UI said the tags were applied.

These tests pin the reporting behaviour at the level the caller actually sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def strip_py_comments(source: str) -> str:
    """Blank out Python comments, preserving newlines.

    REQUIRED, not tidiness: several assertions below are of the form "this token
    must NOT appear", and the shipped code deliberately *explains* the mistakes
    it avoids — the new bulk-upsert handler comments on the ``logger.debug`` it
    replaced, and ``bulk_tag_tracks`` documents the ``str(list)`` repr bug. A
    naive substring check therefore matches the documentation rather than the
    code and reports the opposite of the truth.
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


# ---------------------------------------------------------------------------
# 1. upsert_tracks_bulk must fail LOUDLY
# ---------------------------------------------------------------------------

class _FakeSession:
    """Fails every INSERT, so each payload exercises the error branch."""

    def __init__(self, fail_ids=()):
        self.fail_ids = {str(i) for i in fail_ids}
        self.attempted: list[str] = []

    def execute(self, statement, params=None, *a, **k):
        sql = str(statement)
        if "INSERT INTO tracks" in sql:
            tid = str((params or {}).get("id"))
            self.attempted.append(tid)
            if tid in self.fail_ids:
                raise ValueError('invalid input syntax for type json')
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Result:
    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def scalar_one_or_none(self):
        return None


@pytest.fixture
def bulk_harness(monkeypatch):
    """Route ``upsert_tracks_bulk`` at a fake session with a real column map."""
    from db.repositories import popularity_repository as pr

    session = _FakeSession(fail_ids=["bad-1", "bad-2"])
    monkeypatch.setattr(pr, "db_session", lambda *a, **k: session)
    # Column set/types come from the real inspector path; stub both so the
    # payload is not filtered down to an empty INSERT.
    monkeypatch.setattr(
        pr, "get_tracks_table_columns",
        lambda session=None: {"id", "title", "musicbrainz_genres", "genres"},
    )
    monkeypatch.setattr(
        pr, "get_tracks_table_column_types",
        lambda session=None: {"musicbrainz_genres": "jsonb", "genres": "text"},
    )
    return session


class TestBulkUpsertReportsFailures:
    def test_a_failed_row_is_not_logged_only_at_debug(self):
        """DEBUG is invisible in a normal log tail — this hid the reported bug.

        Asserts on COMMENT-STRIPPED source. The replacement code deliberately
        explains the old ``logger.debug`` in a comment, so a naive substring
        check matches its own documentation and reports the opposite of the
        truth.
        """
        source = strip_py_comments(
            (REPO_ROOT / "db" / "repositories" / "popularity_repository.py").read_text(
                encoding="utf-8"
            )
        )
        start = source.index("def upsert_tracks_bulk(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]
        assert "logger.debug" not in body, (
            "a refused write is logged at DEBUG and vanishes from a normal log tail"
        )
        assert "logger.warning" in body or "logger.error" in body

    def test_all_rows_failing_returns_false(self, bulk_harness):
        from db.repositories import popularity_repository as pr

        ok = pr.upsert_tracks_bulk([
            {"id": "bad-1", "musicbrainz_genres": "alternative rock, rock"},
        ])
        assert ok is False

    def test_a_mixed_batch_returns_false_and_attempts_every_row(self, bulk_harness):
        from db.repositories import popularity_repository as pr

        ok = pr.upsert_tracks_bulk([
            {"id": "bad-1", "musicbrainz_genres": "alternative rock"},
            {"id": "ok-1", "title": "Fine"},
            {"id": "bad-2", "musicbrainz_genres": "britpop, rock"},
        ])
        assert ok is False, "one refused row must fail the batch result"
        # A bad row must not abort the rest of the album.
        assert bulk_harness.attempted == ["bad-1", "ok-1", "bad-2"]

    def test_the_failure_names_the_track_and_the_reason(self, bulk_harness, caplog):
        import logging

        from db.repositories import popularity_repository as pr

        with caplog.at_level(logging.WARNING):
            pr.upsert_tracks_bulk([{"id": "bad-1", "musicbrainz_genres": "x"}])

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "bad-1" in joined, "the log must name the track that failed"
        assert "json" in joined.lower(), "the log must carry the DB's reason"

    def test_a_clean_batch_still_returns_true(self, monkeypatch):
        from db.repositories import popularity_repository as pr

        session = _FakeSession(fail_ids=[])
        monkeypatch.setattr(pr, "db_session", lambda *a, **k: session)
        monkeypatch.setattr(pr, "get_tracks_table_columns", lambda session=None: {"id", "title"})
        monkeypatch.setattr(pr, "get_tracks_table_column_types", lambda session=None: {})

        assert pr.upsert_tracks_bulk([{"id": "ok-1", "title": "Fine"}]) is True


# ---------------------------------------------------------------------------
# 2. Callers must not discard that result
# ---------------------------------------------------------------------------

class TestCallersCheckTheResult:
    """``upsert_tracks_bulk`` returned False into a void.

    Every production caller ignored it, so a wholly discarded album was
    indistinguishable from a successful one.
    """

    CALLERS = [
        ("services/popularity/scan_stage_runner.py", "Bulk-persisted track(s)"),
        ("services/scanning/navidrome_import.py", "[NAVIDROME_SCAN]"),
    ]

    @pytest.mark.parametrize("rel,marker", CALLERS)
    def test_the_return_value_is_checked(self, rel: str, marker: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert marker in source, f"{rel}: marker moved, update this test"
        # Find the call and confirm its result is inspected.
        idx = source.index("upsert_tracks_bulk(")
        window = source[max(0, idx - 400): idx + 400]
        assert "if not upsert_tracks_bulk(" in window or "= upsert_tracks_bulk(" in window, (
            f"{rel}: upsert_tracks_bulk's False return is discarded — a fully "
            "failed batch looks identical to a successful one"
        )


# ---------------------------------------------------------------------------
# 3. bulk_tag_tracks must not claim success when every track failed
# ---------------------------------------------------------------------------

class TestBulkTagTracksReportsFailures:
    def test_it_does_not_return_success_when_nothing_updated(self):
        """⚠️ ``success: True`` with ``updated_count: 0`` is the lie.

        The endpoint returns 200 and the UI reports the tags applied, while
        every row hit the per-track ``except`` and was skipped.
        """
        source = (REPO_ROOT / "services" / "metadata" / "album_service.py").read_text(encoding="utf-8")
        start = source.index("def bulk_tag_tracks(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]

        assert "failed_count" in body or "failed_tracks" in body, (
            "bulk_tag_tracks must count the tracks it skipped"
        )
        # The success flag must depend on something, not be a literal.
        assert '"success": True,\n        "updated_count": updated_count' not in body, (
            "success is hard-coded even when every track was skipped"
        )


# ---------------------------------------------------------------------------
# 4. The reported statement, end to end through the real coercer
# ---------------------------------------------------------------------------

class TestTheReportedStatementNowSucceeds:
    """The exact payload from the Postgres log must coerce to valid JSON."""

    REPORTED = "alternative rock, britpop, rock"

    def test_the_reported_value_coerces(self):
        from db.repositories.popularity_repository import coerce_json_value

        out = coerce_json_value(self.REPORTED)
        assert json.loads(out) == ["alternative rock", "britpop", "rock"]
        # And the token Postgres complained about is gone from the raw text.
        assert not out.startswith("alternative")

    def test_it_flows_through_the_column_coercer(self):
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        out = c("musicbrainz_genres", self.REPORTED, "jsonb")
        assert json.loads(out) == ["alternative rock", "britpop", "rock"]

    @pytest.mark.parametrize(
        "reported",
        [
            "rock, singer-songwriter",
            "alternative rock, glam rock, rock",
            "alternative rock, britpop, rock",
        ],
    )
    def test_every_value_from_the_log_coerces(self, reported: str):
        from db.repositories.popularity_repository import coerce_json_value

        assert json.loads(coerce_json_value(reported)) == [
            g.strip() for g in reported.split(",")
        ]
