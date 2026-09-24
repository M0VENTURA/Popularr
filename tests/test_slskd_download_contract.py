"""Regression tests: the slskd download/retry endpoints keep their contracts.

Three separate defects, all in the manual Soulseek download path:

1. **The batch ``files`` payload contract was lost.** The legacy endpoint
   (``old_system/app.py``) accepted ``{"files": [{username, filename, size}]}``
   and dispatched it through ``SlskdClient.download_files``. When the routes
   were split out into ``routes/download_search_routes.py`` that branch was
   dropped, so ``slskd_download`` read only ``username``/``filename`` and
   rejected everything else with "username and filename required".

   Two frontends still send the batch shape — ``static/js/downloads_page.js``
   (``downloadSlskdBatch``, used by "Download selected" / whole-album download)
   and ``test_site/static/js/services/slskd.js`` (``download()``) — so every
   multi-file download and every rebuilt-page single download broke.

2. **A rejected download reported success.** ``slskd_download`` called
   ``download_file`` (which returns ``bool``, never ``None``) and then guarded
   the fallback with ``if result is None:``, an impossible branch. The route
   returned ``{"success": True, "result": False}`` — a silent no-op served to
   the UI as an enqueued download.

3. **``/api/slskd/retry`` passed a filename where slskd requires a transfer
   id.** slskd scopes a transfer by username + server-assigned transfer id
   (``POST /transfers/downloads/{username}/{transfer_id}/retry``), so a
   filename could never match.

The tests drive the real handlers and the real ``SlskdService``; only the HTTP
client is faked, so the contract is exercised end-to-end within the process.
"""

from __future__ import annotations

import pytest

from routes import download_search_routes as dsr
from services.downloads.slskd_service import SlskdService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _RecordingHttpClient:
    """Minimal stand-in for ``SlskdHttpClient`` that records enqueue calls.

    Mirrors the real signature (including the ``raise_on_error`` flag) so a
    drift in how the service calls the client shows up as a test failure
    rather than a silently swallowed ``TypeError``.
    """

    def __init__(self, *, fail: bool = False, enabled: bool = True):
        self.enabled = enabled
        self.fail = fail
        self.calls: list[dict] = []
        self.retry_calls: list[tuple[str, str]] = []

    def enqueue_downloads(self, username, files, timeout=15, raise_on_error=False):
        self.calls.append({
            "username": username,
            "files": [dict(f) for f in files],
            "timeout": timeout,
            "raise_on_error": raise_on_error,
        })
        if self.fail:
            if raise_on_error:
                raise RuntimeError("slskd rejected the request")
            return []
        return [f"transfer-{username}-{i}" for i in range(len(files))]

    def retry_download(self, username, transfer_id, timeout=10):
        self.retry_calls.append((username, transfer_id))
        return not self.fail


class _NoopSearchLog:
    """Swallow search-log writes so no DB is required."""

    @staticmethod
    def install(monkeypatch) -> None:
        monkeypatch.setattr(dsr, "log_slskd_search", lambda **kwargs: None)
        monkeypatch.setattr(dsr, "log_search", lambda *a, **k: None)
        monkeypatch.setattr(dsr, "log_queue", lambda *a, **k: None)


def _patch_route(monkeypatch, http_client):
    """Patch the route's collaborators, keeping the REAL ``SlskdService``."""
    monkeypatch.setattr(
        dsr,
        "get_config",
        lambda: {"slskd": {"enabled": True, "web_url": "http://slskd.test", "api_key": "k"}},
    )
    monkeypatch.setattr(dsr, "SlskdHttpClient", lambda *a, **k: http_client)
    monkeypatch.setattr(
        dsr,
        "update_queue_item",
        lambda *a, **k: {"success": True},
    )
    _NoopSearchLog.install(monkeypatch)
    monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)


# ---------------------------------------------------------------------------
# 1. Batch contract — /api/slskd/download
# ---------------------------------------------------------------------------

class TestBatchFilesPayloadIsAccepted:
    """The ``files`` shape is what the download page actually posts."""

    @pytest.mark.asyncio
    async def test_batch_of_files_is_enqueued(self, client, monkeypatch):
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"files": [
                {"username": "peerA", "filename": "Music/01.flac", "size": 111},
                {"username": "peerA", "filename": "Music/02.flac", "size": 222},
            ]},
        )

        assert response.status_code == 200, await response.get_data(as_text=True)
        body = await response.get_json()
        assert body["success"] is True
        assert body["requested"] == 2
        # Both files went to the same peer in ONE request.
        assert len(http.calls) == 1
        assert http.calls[0]["username"] == "peerA"
        assert [f["filename"] for f in http.calls[0]["files"]] == [
            "Music/01.flac",
            "Music/02.flac",
        ]
        assert [f["size"] for f in http.calls[0]["files"]] == [111, 222]

    @pytest.mark.asyncio
    async def test_batch_is_grouped_per_peer(self, client, monkeypatch):
        """Files from different peers become one request each, not one per file."""
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"files": [
                {"username": "peerA", "filename": "a.flac", "size": 1},
                {"username": "peerB", "filename": "b.flac", "size": 2},
                {"username": "peerA", "filename": "a2.flac", "size": 3},
            ]},
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["requested"] == 3
        by_user = {c["username"]: c["files"] for c in http.calls}
        assert sorted(by_user) == ["peerA", "peerB"]
        assert len(by_user["peerA"]) == 2
        assert len(by_user["peerB"]) == 1

    @pytest.mark.asyncio
    async def test_a_single_file_batch_still_works(self, client, monkeypatch):
        """``services/slskd.js`` sends a one-element batch rather than the
        legacy single-file shape, so a batch of one must not be special-cased
        into failure."""
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"files": [{"username": "peer", "filename": "solo.flac", "size": 9}]},
        )

        assert response.status_code == 200
        assert (await response.get_json())["requested"] == 1

    @pytest.mark.asyncio
    async def test_single_payload_still_works(self, client, monkeypatch):
        """The legacy single shape must keep working — downloads.js and
        artist_detail.js use it."""
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"username": "peer", "filename": "one.flac", "size": 5},
        )

        assert response.status_code == 200
        assert (await response.get_json())["requested"] == 1
        assert http.calls[0]["files"] == [{"filename": "one.flac", "size": 5}]

    @pytest.mark.asyncio
    async def test_malformed_entries_are_rejected(self, client, monkeypatch):
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"files": [{"username": "peer"}]},  # no filename
        )

        assert response.status_code == 400
        assert http.calls == []

    @pytest.mark.asyncio
    async def test_empty_files_list_is_rejected(self, client, monkeypatch):
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post("/api/slskd/download", json={"files": []})

        assert response.status_code == 400
        assert http.calls == []

    @pytest.mark.asyncio
    async def test_non_list_files_is_rejected(self, client, monkeypatch):
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download", json={"files": "not-a-list"}
        )

        assert response.status_code == 400
        assert http.calls == []


# ---------------------------------------------------------------------------
# 2. A rejected download must NOT report success
# ---------------------------------------------------------------------------

class TestRejectedDownloadDoesNotReportSuccess:
    """The UI branches on ``data.success``; a false True hides a dead download."""

    @pytest.mark.asyncio
    async def test_batch_failure_returns_error_and_non_2xx(self, client, monkeypatch):
        http = _RecordingHttpClient(fail=True)
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"files": [{"username": "peer", "filename": "x.flac", "size": 1}]},
        )

        assert response.status_code >= 400, "a rejected download must not be a 200"
        body = await response.get_json()
        assert body["success"] is not True
        assert body["error"]
        assert body["requested"] == 0

    @pytest.mark.asyncio
    async def test_single_failure_returns_error_and_non_2xx(self, client, monkeypatch):
        http = _RecordingHttpClient(fail=True)
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/download",
            json={"username": "peer", "filename": "x.flac", "size": 1},
        )

        assert response.status_code >= 400
        body = await response.get_json()
        assert body["success"] is not True
        assert body["error"]

    @pytest.mark.asyncio
    async def test_queue_download_does_not_mark_row_downloading_on_failure(
        self, client, monkeypatch
    ):
        """A failed enqueue must not flip the queue row to 'downloading'.

        That status tells the completion matcher to wait for a transfer that
        was never requested, leaving the row apparently active forever.
        """
        http = _RecordingHttpClient(fail=True)
        _patch_route(monkeypatch, http)

        updates: list[dict] = []
        monkeypatch.setattr(
            dsr,
            "update_queue_item",
            lambda qid, **kw: updates.append({"qid": qid, **kw}) or {"success": True},
        )

        response = await client.post(
            "/api/slskd/queue-download",
            json={"queue_id": 42, "username": "peer", "filename": "x.flac", "size": 1},
        )

        assert response.status_code >= 400
        assert updates == [], "the queue row must be left untouched on a failed enqueue"

    @pytest.mark.asyncio
    async def test_queue_download_marks_row_on_success(self, client, monkeypatch):
        """The positive case still links the queue row to the transfer."""
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        updates: list[dict] = []
        monkeypatch.setattr(
            dsr,
            "update_queue_item",
            lambda qid, **kw: updates.append({"qid": qid, **kw}) or {"success": True},
        )

        response = await client.post(
            "/api/slskd/queue-download",
            json={"queue_id": 42, "username": "peer", "filename": "x.flac", "size": 1},
        )

        assert response.status_code == 200
        assert len(updates) == 1
        assert updates[0]["qid"] == 42
        assert updates[0]["status"] == "downloading"
        assert updates[0]["slskd_username"] == "peer"
        assert updates[0]["is_manual_download"] is True


# ---------------------------------------------------------------------------
# 3. /api/slskd/retry must use a transfer id
# ---------------------------------------------------------------------------

class TestRetryUsesTransferId:
    """slskd's retry route is scoped by username + transfer id, never filename."""

    @pytest.mark.asyncio
    async def test_a_filename_is_not_accepted_as_a_transfer_id(self, client, monkeypatch):
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/retry",
            json={"username": "peer", "filename": "Music/song.flac"},
        )

        assert response.status_code == 400
        assert http.retry_calls == []

    @pytest.mark.asyncio
    async def test_transfer_id_is_forwarded(self, client, monkeypatch):
        http = _RecordingHttpClient()
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/retry",
            json={"username": "peer", "transfer_id": "abc-123"},
        )

        assert response.status_code == 200
        assert (await response.get_json())["success"] is True
        assert http.retry_calls == [("peer", "abc-123")]

    @pytest.mark.asyncio
    async def test_rejected_retry_is_not_reported_as_success(self, client, monkeypatch):
        http = _RecordingHttpClient(fail=True)
        _patch_route(monkeypatch, http)

        response = await client.post(
            "/api/slskd/retry",
            json={"username": "peer", "transfer_id": "abc-123"},
        )

        assert response.status_code >= 400
        body = await response.get_json()
        assert body["success"] is False


# ---------------------------------------------------------------------------
# Service-level: download_files
# ---------------------------------------------------------------------------

class TestDownloadFilesService:
    """``SlskdService.download_files`` is the single funnel for both shapes."""

    def test_batches_by_username_and_reports_requested(self):
        http = _RecordingHttpClient()
        results = SlskdService(http_client=http).download_files([
            {"username": "a", "filename": "1.flac", "size": 10},
            {"username": "a", "filename": "2.flac", "size": 20},
            {"username": "b", "filename": "3.flac", "size": 30},
        ])

        assert len(http.calls) == 2
        assert sum(r["requested"] for r in results) == 3
        assert all(r["success"] is True for r in results)

    def test_entries_missing_keys_are_skipped(self):
        http = _RecordingHttpClient()
        results = SlskdService(http_client=http).download_files([
            {"username": "", "filename": "x.flac"},
            {"username": "a", "filename": ""},
            {"username": "a", "filename": "good.flac", "size": 1},
        ])

        assert sum(r["requested"] for r in results) == 1
        assert len(http.calls) == 1

    def test_size_is_coerced_to_int(self):
        http = _RecordingHttpClient()
        SlskdService(http_client=http).download_files([
            {"username": "a", "filename": "x.flac", "size": "1234"},
            {"username": "a", "filename": "y.flac"},  # size absent -> 0
        ])

        sizes = [f["size"] for f in http.calls[0]["files"]]
        assert sizes == [1234, 0]

    def test_disabled_client_requests_nothing(self):
        http = _RecordingHttpClient(enabled=False)
        results = SlskdService(http_client=http).download_files([
            {"username": "a", "filename": "x.flac", "size": 1},
        ])

        assert results == []
        assert http.calls == []

    def test_a_rejected_peer_is_reported_as_unsuccessful(self):
        """Silence must not be mistaken for acceptance."""
        http = _RecordingHttpClient(fail=True)
        results = SlskdService(http_client=http).download_files([
            {"username": "a", "filename": "x.flac", "size": 1},
        ])

        assert results[0]["success"] is False
        assert results[0]["requested"] == 0
        assert results[0]["error"]

    def test_the_client_is_asked_to_surface_errors(self):
        """The service relies on ``raise_on_error`` — without it a failed POST
        is indistinguishable from an older slskd that returns no body."""
        http = _RecordingHttpClient()
        SlskdService(http_client=http).download_files([
            {"username": "a", "filename": "x.flac", "size": 1},
        ])

        assert http.calls[0]["raise_on_error"] is True


# ---------------------------------------------------------------------------
# Guard: enqueue_downloads' new flag keeps its default
# ---------------------------------------------------------------------------

def test_raise_on_error_defaults_to_false():
    """Existing best-effort callers pass only (username, files) and rely on an
    empty list rather than an exception."""
    import inspect

    from api_clients.slskd_http import SlskdHttpClient

    signature = inspect.signature(SlskdHttpClient.enqueue_downloads)
    assert signature.parameters["raise_on_error"].default is False


# ---------------------------------------------------------------------------
# Guard: the two frontends still post the batch shape we now accept
# ---------------------------------------------------------------------------

class TestFrontendsStillSendTheBatchShape:
    """If a frontend switches back to a bare single payload the batch tests
    above would keep passing while the page stayed broken, so pin the callers."""

    @staticmethod
    def _read(relative_path: str) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return (root / relative_path).read_text(encoding="utf-8")

    def test_downloads_page_posts_files(self):
        source = self._read("static/js/downloads_page.js")
        assert "body: JSON.stringify({ files })" in source

    def test_rebuilt_slskd_service_posts_files(self):
        source = self._read("test_site/static/js/services/slskd.js")
        assert "{ files: files }" in source

    def test_both_point_at_the_download_endpoint(self):
        for path in (
            "static/js/downloads_page.js",
            "test_site/static/js/services/slskd.js",
        ):
            source = self._read(path)
            assert "/api/slskd/download" in source, path
