"""Regression tests: queueing a release must never report a false success.

Reported symptom (universal search → Queue, and the album/artist download
buttons): the UI announced a successful queue — green pill / "Download queued"
— but nothing appeared in the download queue. Re-queueing an album the user
already owned produced the same happy toast and still nothing was added.

Root cause: the whole chain was structurally incapable of reporting "I did
nothing", because there were two independent ways for it to happen silently.

1. ``add_release_tracks_to_queue`` returned ``list[int]``. Three legitimate
   skip paths return an empty list — the release already has active queue
   items, every track is already in the library, every track is already in
   the download queue. ``[]`` cannot distinguish those from an error, and
   nothing counted them.
2. ``/api/musicbrainz/download`` only branched on ``result["success"]``,
   which ``start_release_download`` hard-codes to ``True`` even when it
   created zero queue rows. So a zero-track request answered
   ``{"success": true, "(0 tracks)"}`` with HTTP 201.

The fix reports *why* nothing was queued and treats "0 tracks" as not-success.
These tests pin both halves: the detailed adder's reason codes, and the
endpoint's refusal to claim success for an empty queue.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.queue import queue_processing_service as qps


# ---------------------------------------------------------------------------
# Fake DB session: answers the pre-flight SELECTs from an explicit fixture
# ---------------------------------------------------------------------------

class _FakeResult:
    """SQLAlchemy-Result stand-in: ``INSERT ... RETURNING id`` + SELECTs."""

    def __init__(self, value: Any = None, rows: Any = ()) -> None:
        self._value = value
        self._rows = list(rows)

    def scalar_one_or_none(self) -> Any:
        return self._value

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> Any:
        return list(self._rows)


class _FakeSession:
    """Captures INSERTs and replies to the skip checks from the fixture args."""

    def __init__(self, existing_rows: Any = (), dup_row: bool = False) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self._existing_rows = list(existing_rows)
        self._dup_row = dup_row

    def execute(self, statement: Any, params: Any = None, *a: Any, **k: Any) -> _FakeResult:
        sql = str(statement)
        if "INSERT INTO download_queue" in sql:
            self.inserted.append(dict(params or {}))
            return _FakeResult(len(self.inserted))
        if sql.strip().upper().startswith("DELETE"):
            self.deleted.append(dict(params or {}))
            return _FakeResult(None)
        # The active/stale pre-flight scan (SELECT id, status ...).
        if "status FROM download_queue" in sql:
            return _FakeResult(None, rows=self._existing_rows)
        # The per-track duplicate lookup (SELECT id FROM download_queue ...).
        if "SELECT id FROM download_queue" in sql:
            return _FakeResult(None, rows=[{"id": 4242}] if self._dup_row else [])
        return _FakeResult(None, rows=[])

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _install(monkeypatch: Any, session: _FakeSession, library_hit: bool = False) -> _FakeSession:
    monkeypatch.setattr(qps, "db_session", lambda *a, **k: session)
    monkeypatch.setattr(
        qps, "find_library_track",
        lambda **k: ({"id": "lib-1"} if library_hit else None),
    )
    monkeypatch.setattr(qps, "signal_new_item", lambda: None)
    return session


def _tracks(n: int = 2) -> list[dict[str, Any]]:
    return [
        {"title": f"Track {i}", "track_number": i, "recording_mbid": f"rec-{i}"}
        for i in range(1, n + 1)
    ]


def _add(tracks: Any) -> dict[str, Any]:
    return qps.add_release_tracks_to_queue_detailed(
        "rel-1", tracks, "Madball", "Not Your Kingdom",
    )


# ---------------------------------------------------------------------------
# 1. The detailed adder names the reason it queued nothing
# ---------------------------------------------------------------------------

class TestDetailedAdderReportsSkipReason:

    def test_success_reports_no_reason(self, monkeypatch):
        session = _install(monkeypatch, _FakeSession())
        result = _add(_tracks(2))

        assert result["queued"] is True
        assert result["reason"] is None
        assert result["message"] == ""
        assert result["queue_ids"] == [1, 2]
        assert len(session.inserted) == 2

    def test_empty_tracklist_reports_no_tracks(self, monkeypatch):
        _install(monkeypatch, _FakeSession())
        result = _add([])

        assert result["queued"] is False
        assert result["reason"] == "no_tracks"
        assert result["total_tracks"] == 0
        assert "no tracks" in result["message"].lower()

    def test_all_tracks_in_library_reports_all_in_library(self, monkeypatch):
        """The exact reported case: the album is already owned."""
        session = _install(monkeypatch, _FakeSession(), library_hit=True)
        result = _add(_tracks(3))

        assert result["queued"] is False
        assert result["reason"] == "all_in_library"
        assert result["in_library"] == 3
        assert result["queue_ids"] == []
        assert session.inserted == []
        assert "library" in result["message"].lower()

    def test_all_tracks_already_queued_reports_already_queued(self, monkeypatch):
        _install(monkeypatch, _FakeSession(dup_row=True))
        result = _add(_tracks(2))

        assert result["queued"] is False
        assert result["reason"] == "already_queued"
        assert result["already_queued"] == 2
        assert "queue" in result["message"].lower()

    def test_active_queue_items_report_already_active(self, monkeypatch):
        """A release mid-download must not be silently re-queued as a success."""
        session = _install(
            monkeypatch,
            _FakeSession(existing_rows=[("q-1", "queued"), ("q-2", "downloading")]),
        )
        result = _add(_tracks(2))

        assert result["queued"] is False
        assert result["reason"] == "already_active"
        assert result["already_active"] == 2
        assert session.inserted == []

    def test_mixed_library_and_queue_reports_all_present(self, monkeypatch):
        """Half owned, half queued → still nothing new, and that is explainable."""
        # find_library_track is called per track; make the first hit, second miss.
        calls = {"n": 0}

        def _find(**k: Any) -> Any:
            calls["n"] += 1
            return {"id": "lib-1"} if calls["n"] == 1 else None

        session = _FakeSession(dup_row=True)
        monkeypatch.setattr(qps, "db_session", lambda *a, **k: session)
        monkeypatch.setattr(qps, "find_library_track", _find)
        monkeypatch.setattr(qps, "signal_new_item", lambda: None)

        result = _add(_tracks(2))

        assert result["queued"] is False
        assert result["reason"] == "all_present"
        assert result["in_library"] == 1
        assert result["already_queued"] == 1

    def test_repeated_tracks_report_all_duplicate(self, monkeypatch):
        """A release listing that repeats one track queues it once, not zero."""
        _install(monkeypatch, _FakeSession())
        repeated = [
            {"title": "Same", "track_number": 1, "recording_mbid": "rec-x"},
            {"title": "Same", "track_number": 2, "recording_mbid": "rec-x"},
        ]
        result = _add(repeated)

        # One row was created, so this is still a success — duplicate only
        # becomes the reason when nothing at all could be queued.
        assert result["queued"] is True
        assert result["reason"] is None
        assert result["duplicate"] == 1
        assert len(result["queue_ids"]) == 1

    def test_duplicate_is_reported_when_a_queued_row_blocks_and_one_is_duplicate(self, monkeypatch):
        _install(monkeypatch, _FakeSession(dup_row=True))
        repeated = [
            {"title": "Same", "track_number": 1, "recording_mbid": "rec-x"},
            {"title": "Same", "track_number": 2, "recording_mbid": "rec-x"},
        ]
        result = _add(repeated)

        assert result["queued"] is False
        # already_queued wins the reason, but the duplicate is still counted.
        assert result["reason"] == "already_queued"
        assert result["duplicate"] == 1
        assert result["already_queued"] == 1

    def test_every_failure_message_is_human_readable(self, monkeypatch):
        """No reason may surface an empty string to the user."""
        cases = [
            ([], _FakeSession()),
            (["lib"], _FakeSession()),
        ]
        for _spec, session in cases:
            _install(monkeypatch, session, library_hit=bool(_spec))
            result = _add([] if not _spec else _tracks(1))
            if not result["queued"]:
                assert result["message"], f"empty message for reason {result['reason']!r}"


# ---------------------------------------------------------------------------
# 2. The list-returning wrapper keeps its contract
# ---------------------------------------------------------------------------

class TestLegacyWrapperStillReturnsIds:

    def test_wrapper_returns_ids(self, monkeypatch):
        _install(monkeypatch, _FakeSession())
        assert qps.add_release_tracks_to_queue(
            "rel-1", _tracks(2), "Madball", "Not Your Kingdom"
        ) == [1, 2]

    def test_wrapper_returns_empty_list_when_nothing_queued(self, monkeypatch):
        _install(monkeypatch, _FakeSession(), library_hit=True)
        assert qps.add_release_tracks_to_queue(
            "rel-1", _tracks(2), "Madball", "Not Your Kingdom"
        ) == []

    def test_wrapper_result_matches_detailed_queue_ids(self, monkeypatch):
        """The two entry points must not drift apart."""
        _install(monkeypatch, _FakeSession())
        detailed = qps.add_release_tracks_to_queue_detailed(
            "rel-1", _tracks(2), "Madball", "Not Your Kingdom"
        )
        # A fresh session so the returned ids restart from 1 in both calls.
        _install(monkeypatch, _FakeSession())
        legacy = qps.add_release_tracks_to_queue(
            "rel-1", _tracks(2), "Madball", "Not Your Kingdom"
        )
        assert legacy == detailed["queue_ids"]


# ---------------------------------------------------------------------------
# 3. The pipeline propagates the reason instead of a bare count
# ---------------------------------------------------------------------------

def _fake_pipeline(monkeypatch: Any, queue_result: dict[str, Any]) -> dict[str, Any]:
    from services.downloads import download_pipeline_service as dps

    monkeypatch.setattr(dps, "resolve_release_id", lambda rid: rid)
    monkeypatch.setattr(dps, "fetch_musicbrainz_release_metadata", lambda rid: None)
    monkeypatch.setattr(
        dps, "fetch_release_metadata",
        lambda rid: {
            "release_title": "Abyss",
            "release_year": 2024,
            "artist": "Ad Infinitum",
            "tracks": _tracks(2),
        },
    )
    monkeypatch.setattr(dps, "upsert_musicbrainz_release", lambda *a, **k: 99)
    monkeypatch.setattr(dps, "create_monitoring_folder", lambda *a, **k: "/tmp/x")
    monkeypatch.setattr(dps, "add_release_tracks_to_queue_detailed", lambda *a, **k: queue_result)
    return dps.start_release_download("rg-1", "Abyss", "Ad Infinitum", method="slskd")


def _zero_result(reason: str, message: str, **counts: Any) -> dict[str, Any]:
    base = {
        "queue_ids": [],
        "queued": False,
        "reason": reason,
        "message": message,
        "total_tracks": 2,
        "already_active": 0,
        "in_library": 0,
        "already_queued": 0,
        "duplicate": 0,
    }
    base.update(counts)
    return base


class TestPipelinePropagatesReason:

    def test_zero_tracks_sets_queued_false_and_carries_the_reason(self, monkeypatch):
        result = _fake_pipeline(
            monkeypatch,
            _zero_result(
                "all_in_library",
                "Every track in this release is already in your library.",
                in_library=2,
            ),
        )

        assert result["queue_items_created"] == 0
        assert result["queued"] is False
        assert result["queue_reason"] == "all_in_library"
        assert "library" in result["queue_message"].lower()
        assert result["queue_skipped"]["in_library"] == 2
        # The keys existing callers already read must survive.
        assert result["queue_ids"] == []
        assert result["total_tracks"] == 2

    def test_success_has_no_reason(self, monkeypatch):
        result = _fake_pipeline(
            monkeypatch,
            {
                "queue_ids": [1, 2],
                "queued": True,
                "reason": None,
                "message": "",
                "total_tracks": 2,
                "already_active": 0,
                "in_library": 0,
                "already_queued": 0,
                "duplicate": 0,
            },
        )

        assert result["queue_items_created"] == 2
        assert result["queued"] is True
        assert result["queue_reason"] is None


# ---------------------------------------------------------------------------
# 4. The endpoint must not claim success for an empty queue
# ---------------------------------------------------------------------------

class TestDownloadEndpointRefusesFalseSuccess:

    @staticmethod
    def _patch(monkeypatch: Any, result: dict[str, Any]) -> None:
        import routes.musicbrainz_routes as routes

        monkeypatch.setattr(routes, "start_release_download", lambda *a, **k: result)
        # The route refuses to proceed unless slskd is enabled in config; the
        # test config does not enable it, so bypass the gate rather than
        # writing a real config file.
        monkeypatch.setattr(
            routes, "_normalize_download_method", lambda *a, **k: ("slskd", None)
        )

    async def test_zero_tracks_is_not_success(self, client, monkeypatch):
        self._patch(
            monkeypatch,
            {
                "success": True,
                "mb_release_db_id": 99,
                "queue_items_created": 0,
                "queue_ids": [],
                "queued": False,
                "queue_reason": "all_in_library",
                "queue_message": "Every track in this release is already in your library.",
                "queue_skipped": {"in_library": 11},
                "total_tracks": 11,
            },
        )

        response = await client.post(
            "/api/musicbrainz/download",
            json={"release_id": "rel-1", "release_title": "Abyss", "artist": "Ad Infinitum"},
        )
        data = await response.get_json()

        # 200, NOT 4xx: the rebuilt UI's postJson throws on non-2xx, which
        # would hide this specific reason behind a generic HTTP error.
        assert response.status_code == 200
        assert data["success"] is False
        assert data["queued"] is False
        assert data["reason"] == "all_in_library"
        assert data["message"] == "Every track in this release is already in your library."
        assert data["error"] == data["message"]
        assert data["tracking_id"] is None
        assert data["queued_tracks"] == 0
        assert data["total_tracks"] == 11
        assert data["skipped"]["in_library"] == 11

    async def test_tracks_queued_is_still_201_success(self, client, monkeypatch):
        self._patch(
            monkeypatch,
            {
                "success": True,
                "mb_release_db_id": 99,
                "queue_items_created": 2,
                "queue_ids": [7, 8],
                "queued": True,
                "queue_reason": None,
                "queue_message": "",
                "queue_skipped": {},
                "total_tracks": 2,
            },
        )

        response = await client.post(
            "/api/musicbrainz/download",
            json={"release_id": "rel-1", "release_title": "Abyss", "artist": "Ad Infinitum"},
        )
        data = await response.get_json()

        assert response.status_code == 201
        assert data["success"] is True
        assert data["queued"] is True
        assert data["queued_tracks"] == 2
        assert data["total_tracks"] == 2
        assert data["tracking_id"] == 99

    async def test_tracking_id_falls_back_to_first_queue_id(self, client, monkeypatch):
        """Concrete release with no folder-group row still tracks via queue id."""
        self._patch(
            monkeypatch,
            {
                "success": True,
                "mb_release_db_id": None,
                "queue_items_created": 1,
                "queue_ids": [51],
                "queued": True,
                "queue_reason": None,
                "queue_message": "",
                "queue_skipped": {},
                "total_tracks": 1,
            },
        )

        response = await client.post(
            "/api/musicbrainz/download",
            json={"release_id": "rel-1", "release_title": "Abyss", "artist": "Ad Infinitum"},
        )
        data = await response.get_json()

        assert response.status_code == 201
        assert data["tracking_id"] == 51

    async def test_missing_reason_still_refuses_success(self, client, monkeypatch):
        """Even an older/partial result shape must not report a false success."""
        self._patch(
            monkeypatch,
            {
                "success": True,
                "mb_release_db_id": 99,
                "queue_items_created": 0,
                "queue_ids": [],
            },
        )

        response = await client.post(
            "/api/musicbrainz/download",
            json={"release_id": "rel-1", "release_title": "Abyss", "artist": "Ad Infinitum"},
        )
        data = await response.get_json()

        assert response.status_code == 200
        assert data["success"] is False
        assert data["queued"] is False
        assert data["reason"] == "nothing_queued"
        assert data["message"]


# ---------------------------------------------------------------------------
# 5. Guard the guard: the skip-message table must cover every reason we emit
# ---------------------------------------------------------------------------

def test_every_emitted_reason_has_a_message():
    emitted = {
        "already_active", "no_tracks", "all_in_library",
        "already_queued", "all_present", "all_duplicate", "nothing_queued",
    }
    assert emitted <= set(qps._QUEUE_SKIP_MESSAGES), (
        "a reason code without a message would surface an empty toast"
    )


def test_reason_codes_are_distinct_except_the_catch_all():
    """The specific reasons must map to different text — that is the point."""
    specific = ["already_active", "all_in_library", "already_queued"]
    messages = [qps._QUEUE_SKIP_MESSAGES[r] for r in specific]
    assert len(set(messages)) == len(messages)


# ---------------------------------------------------------------------------
# 6. The frontend must not claim success either
# ---------------------------------------------------------------------------
#
# The endpoint telling the truth is only half the fix: ``queueRelease`` used to
# await the POST and then immediately settle(true) + toast.queued(...) without
# reading a single field of the response, so a zero-track answer still showed a
# green pill and marked the button "Queued".
#
# That is exercised behaviourally by ``tests/js/queue-zero-track-probe.js``,
# which brace-matches the SHIPPED function out of search-flyout.js and drives it
# with stub collaborators. It is mutation-tested by
# ``tests/js/mutate-queue-zero-track.js`` (all 3 mutations detected).

import json  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "queue-zero-track-probe.js"
REBUILT_SEARCH = "test_site/static/js/ui/search-flyout.js"


def _node_available() -> bool:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, shell=True
        ).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run_probe(search_rel: str) -> dict[str, Any]:
    out = subprocess.run(
        ["node", str(PROBE), str(REPO_ROOT / search_rel)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), shell=True,
        encoding="utf-8",
    )
    assert out.returncode == 0, f"probe failed:\n{out.stdout}\n{out.stderr}"
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")]
    assert lines, f"probe produced no JSON:\n{out.stdout}\n{out.stderr}"
    return json.loads(lines[-1])


@needs_node
def test_frontend_probe_flags_zero_track_as_not_queued():
    """The shipped queueRelease must surface a no-op instead of celebrating it."""
    result = _run_probe(REBUILT_SEARCH)
    assert "error" not in result, result.get("error")
    failed = [c["name"] for c in result["checks"] if not c["pass"]]
    assert not failed, f"failed checks: {failed}"
    assert result["total"] >= 15


@needs_node
def test_frontend_probe_covers_the_original_success_true_zero_track_shape():
    """Guard the guard: the probe must include the exact buggy response shape.

    Without the ``success:true + queued_tracks:0`` case the probe passed even
    against a variant that trusted ``success`` over the track count — that is
    how the precedence bug in the first draft of this fix was caught.
    """
    result = _run_probe(REBUILT_SEARCH)
    names = {c["name"] for c in result["checks"]}
    assert any("success:true + 0 tracks" in n for n in names), (
        "the original response shape must be asserted explicitly"
    )
    assert any("queued:false alone" in n for n in names)
