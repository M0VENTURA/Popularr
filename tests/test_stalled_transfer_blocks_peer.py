"""A peer that never sends must not be picked again next cycle.

REPORTED (queue log): ATEEZ "HIGHER" and "Seeker" from `roqinghejing`:

    18:11:04 [DOWNLOADING] ATEEZ - HIGHER → downloading from roqinghejing
    18:26:22 Cancelled stalled transfer … progress=0
    18:26:23 Failed queue item for stalled transfer queue_id=5454
    … 18:57:21 [DOWNLOADING] ATEEZ - HIGHER → downloading from roqinghejing
    … 19:46:56 [DOWNLOADING] ATEEZ - HIGHER → downloading from roqinghejing

Every other failure path already blocks the ``(peer, file)`` pair before the
next search: no free upload slots, ``download_file`` returning False, and a
mismatched download. **The stall path did not** — the download *request*
succeeds (so nothing blocks), the transfer then sits at 0% for
``STALL_ZERO_PROGRESS_MINUTES``, the reaper cancels it, and the next search
finds the same peer again. That is the loop: a 15-minute stall every cycle,
forever, on a peer that was never going to send anything.

Blocking here uses the same key and TTL as everywhere else, so the existing
``_filter_blocked_peers`` in the automatic-search path does the rest: the next
cycle either selects a different file or reports ``no_results`` and backs off.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.downloads import download_pipeline_service as dps
from services.downloads import slskd_reaper_service as reaper

USERNAME = "roqinghejing"
FILENAME = "music/手动下载/ATEEZ/HIGHER.flac"


def _started(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _transfer(*, progress: int, minutes: int, speed: int = 0) -> dict:
    return {
        "id": "e8454e3f-d20a-4973-b319-8e263b9dc254",
        "username": USERNAME,
        "filename": FILENAME,
        "progress": progress,
        "averageSpeed": speed,
        "startedAt": _started(minutes),
    }


class _FakeSlskd:
    def __init__(self, transfers):
        self._transfers = transfers
        self.cancelled: list[tuple[str, str]] = []

    def get_active_downloads(self):
        return list(self._transfers)

    def cancel_download(self, username, transfer_id, remove=False):
        self.cancelled.append((username, transfer_id))
        return True


@pytest.fixture(autouse=True)
def _clean_blocked_peers():
    """``_blocked_peers`` is module-global by design — isolate every test."""
    dps._blocked_peers.clear()
    yield
    dps._blocked_peers.clear()


@pytest.fixture
def reap(monkeypatch):
    def _reap(transfers: list[dict]) -> tuple[dict, _FakeSlskd]:
        fake = _FakeSlskd(transfers)
        # No queue rows in the test DB: the stall is what we are testing, not
        # the ownership lookup (and the table may not exist here).
        monkeypatch.setattr(reaper, "get_active_queue", lambda limit=300: [])
        monkeypatch.setattr(
            "api_clients.slskd_http.get_slskd_client", lambda: object(),
        )
        monkeypatch.setattr(
            "services.downloads.slskd_service.SlskdService",
            lambda http_client=None: fake,
        )
        return reaper.reap_stalled_transfers(), fake

    return _reap


class TestAStalledTransferIsRemembered:
    def test_a_zero_progress_stall_blocks_that_peer_and_file(self, reap):
        stats, fake = reap([_transfer(progress=0, minutes=20)])

        assert stats["cancelled_transfers"] == 1
        assert fake.cancelled, "the transfer must still be cancelled"
        assert dps._is_peer_blocked(USERNAME, FILENAME), (
            "without this the next search re-selects the same dead peer and "
            "stalls for another 15 minutes — the reported loop"
        )

    def test_a_mid_transfer_stall_is_blocked_too(self, reap):
        """Progressing to 5% then dying is the same dead peer."""
        _, _ = reap([_transfer(progress=5, minutes=70, speed=0)])

        assert dps._is_peer_blocked(USERNAME, FILENAME)

    def test_a_progressing_transfer_is_left_alone(self, reap):
        """A healthy long transfer must never be blocked."""
        stats, fake = reap([_transfer(progress=50, minutes=70, speed=1_000_000)])

        assert stats["cancelled_transfers"] == 0
        assert fake.cancelled == []
        assert dps._is_peer_blocked(USERNAME, FILENAME) is False

    def test_a_young_transfer_is_left_alone(self, reap):
        """Under STALL_ZERO_PROGRESS_MINUTES it may simply be slow to start."""
        stats, _ = reap([_transfer(progress=0, minutes=5)])

        assert stats["cancelled_transfers"] == 0
        assert dps._is_peer_blocked(USERNAME, FILENAME) is False

    def test_an_unknown_age_is_never_stalled(self, reap):
        """``startedAt`` missing → conservative, no cancel, no block."""
        stats, _ = reap([{**_transfer(progress=0, minutes=20), "startedAt": None}])

        assert stats["cancelled_transfers"] == 0
        assert dps._is_peer_blocked(USERNAME, FILENAME) is False


class TestTheLoopActuallyBreaks:
    def test_the_next_search_drops_that_result(self, reap):
        """The block only matters if the search path honours it."""
        reap([_transfer(progress=0, minutes=20)])

        results = [
            {"username": USERNAME, "filename": FILENAME, "bitrate": 1000},
            {"username": "someoneelse", "filename": FILENAME, "bitrate": 320},
        ]
        kept = dps._filter_blocked_peers(results)

        assert [r["username"] for r in kept] == ["someoneelse"], (
            "the reaper's block must reach the automatic-search filter, "
            "otherwise the loop continues untouched"
        )

    def test_a_different_file_from_that_peer_is_still_allowed(self, reap):
        """The key is (peer, file) — one dead file must not ban the peer."""
        reap([_transfer(progress=0, minutes=20)])

        assert dps._filter_blocked_peers([
            {"username": USERNAME, "filename": "music/ATEEZ/Seeker.flac"},
        ]), "a healthy file from the same peer must remain selectable"
