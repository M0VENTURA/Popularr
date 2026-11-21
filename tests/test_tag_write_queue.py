"""File tag writes run in the background, off the scan path.

REPORTED

> "When writing genres to files, this is taking a long time and also stopping
> another scan from running at the same time. Could this run as a separate
> background task that doesn't stop the popularity scan running."

Two distinct costs were being paid inline:

1. **Volume.** A tag write is a read-modify-write of a whole audio file (copy,
   rewrite, byte-compare, atomic replace). The scan ran it per track inside the
   album loop, so its wall clock was dominated by disk I/O the scan does not need
   to finish — the playlists are built from the `genres` COLUMN, which is already
   committed by then.

2. **Mutual exclusion.** ``apply_genre_playlist_resync`` took the cross-process
   scan-start lock for its whole run, so while genres were being written every
   scan start was refused with "A scan is running".

What these tests pin
--------------------
1. The scan pass returns as soon as the writes are QUEUED, and never calls the
   writer itself.
2. The queue eventually performs exactly the same writes the inline path does.
3. The queue never takes the scan-start lock.
4. A second enqueue for the same album coalesces rather than racing a second
   thread over the same files.
5. The rescan is drained by the JOB, after the writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.metadata import album_tag_sync_service as svc
from services.metadata import tag_write_queue as queue

REPO_ROOT = Path(__file__).resolve().parents[1]
QUEUE_SOURCE = (REPO_ROOT / "services" / "metadata" / "tag_write_queue.py").read_text(
    encoding="utf-8"
)
RESYNC_SOURCE = (
    REPO_ROOT / "services" / "enrichment" / "genre_playlist_resync_service.py"
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_queue():
    queue.reset_tag_write_state()
    yield
    queue.wait_for_tag_writes(timeout=10.0)
    queue.reset_tag_write_state()


@pytest.fixture
def deferred(monkeypatch, deferred_tag_writes):
    """Turn the background path ON for this test.

    ``deferred_tag_writes`` is the conftest marker that tells the suite-wide
    "write inline" fixture to stand down; without it, that fixture's patch would
    be applied first and this one would be overwritten.
    """
    monkeypatch.setattr(svc, "_defer_file_writes", lambda: True)
    return True


def _track(track_id: str = "t1") -> dict:
    return {
        "id": track_id,
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "album_artist": "Artist",
        "file_path": f"/tmp/{track_id}.mp3",
        "genres": "Rock, Metal",
        "year": "2020",
        "track_number": "1",
        "disc_number": "1",
    }


#: What ``_write_album_track_file_tags`` returns: (files, corrections, skipped).
NO_WRITE = (0, 0, "")
ONE_WRITE = (1, 0, "")


class TestTheScanPassOnlyQueues:
    def test_sync_returns_before_writing_anything(self, monkeypatch, deferred) -> None:
        """⭐ The reported symptom: the scan waited on the disk.

        ⚠️ The write is recorded with the THREAD that performed it. Both paths
        call the same helper, and the background job starts immediately, so a
        bare "was it called?" assertion would pass or fail on a race. The
        question is which THREAD wrote — the scan's or the queue's.
        """
        import threading

        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda a, b: [_track()])
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda t: ("", {}, 0))

        threads: list[str] = []
        monkeypatch.setattr(
            svc,
            "_write_album_track_file_tags",
            lambda **kw: threads.append(threading.current_thread().name) or NO_WRITE,
        )

        result = svc.sync_album_file_tags("Artist", "Album")

        assert result.get("deferred") is True
        assert result.get("queued") is True
        assert threading.current_thread().name not in threads, (
            "the scan pass wrote the file itself instead of queueing"
        )

        queue.wait_for_tag_writes(timeout=10.0)
        assert "tag-write" in threads, (
            f"the queued job never performed the write: {threads}"
        )

    def test_the_queue_then_performs_the_write(self, monkeypatch, deferred) -> None:
        """The work is not lost — it moves."""
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda a, b: [_track()])
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda t: ("", {}, 0))

        calls: list[dict] = []

        def _fake_write(**kw):
            calls.append(kw)
            return ONE_WRITE

        monkeypatch.setattr(svc, "_write_album_track_file_tags", _fake_write)

        svc.sync_album_file_tags("Artist", "Album")
        assert queue.wait_for_tag_writes(timeout=10.0) is True

        assert calls, "the queued job never ran the file writer"
        assert calls[0]["artist"] == "Artist"
        assert calls[0]["album"] == "Album"
        assert [t["id"] for t in calls[0]["file_tracks"]] == ["t1"]

    def test_deferring_off_still_writes_inline(self, monkeypatch) -> None:
        """CONTROL — the switch restores the synchronous behaviour."""
        monkeypatch.setattr(svc, "_defer_file_writes", lambda: False)
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda a, b: [_track()])
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda t: ("", {}, 0))

        calls: list[dict] = []
        monkeypatch.setattr(
            svc,
            "_write_album_track_file_tags",
            lambda **kw: calls.append(kw) or ONE_WRITE,
        )

        result = svc.sync_album_file_tags("Artist", "Album")

        assert calls, "deferring off must write inline"
        assert not result.get("deferred")
        assert result["files_updated"] == 1

    def test_a_queueing_failure_falls_back_to_inline(self, monkeypatch, deferred) -> None:
        """A broken queue must not silently cost the album its tags."""
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda a, b: [_track()])
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda t: ("", {}, 0))

        def _boom(**kw):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(queue, "enqueue_tag_tracks", _boom)

        calls: list[dict] = []
        monkeypatch.setattr(
            svc,
            "_write_album_track_file_tags",
            lambda **kw: calls.append(kw) or ONE_WRITE,
        )

        result = svc.sync_album_file_tags("Artist", "Album")

        assert calls, "the fallback never wrote the tags"
        assert result["files_updated"] == 1

    def test_a_declined_job_also_falls_back_to_inline(self, monkeypatch, deferred) -> None:
        """⭐ ``queued: False`` must NOT be reported as deferred.

        The queue answers ``queued: False`` when it declines a job. Reporting
        that as "deferred" would silently drop the album's file tags — the scan
        would move on and nothing would ever write them.
        """
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda a, b: [_track()])
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda t: ("", {}, 0))
        monkeypatch.setattr(
            queue, "enqueue_tag_tracks", lambda **kw: {"queued": False, "tracks": 1}
        )

        calls: list[dict] = []
        monkeypatch.setattr(
            svc,
            "_write_album_track_file_tags",
            lambda **kw: calls.append(kw) or ONE_WRITE,
        )

        result = svc.sync_album_file_tags("Artist", "Album")

        assert calls, "a declined job left the tags unwritten"
        assert not result.get("deferred")
        assert result["files_updated"] == 1


class TestTheQueueNeverBlocksAScan:
    def test_it_never_takes_the_scan_start_lock(self) -> None:
        """⭐ This is the reported "stopping another scan from running"."""
        import re

        code = "\n".join(
            line.split("#", 1)[0] for line in QUEUE_SOURCE.splitlines()
        )
        assert not re.search(r"try_acquire_scan_start_lock\s*\(", code), (
            "the tag queue must not acquire the scan-start lock — that is what "
            "made a genre write mutually exclusive with a scan"
        )

    def test_the_resync_apply_no_longer_takes_it_either(self) -> None:
        """The other half: the genre apply held it for its whole run."""
        import re

        body = RESYNC_SOURCE.split("def apply_genre_playlist_resync", 1)[1]
        body = body.split("\ndef ", 1)[0]
        # Strip comments so a docblock mentioning the lock is not mistaken for
        # an actual acquisition.
        code = "\n".join(
            line.split("#", 1)[0] for line in body.splitlines()
        )
        assert not re.search(r"try_acquire_scan_start_lock\s*\(", code), (
            "the genre apply must not take the scan-start lock"
        )

    def test_two_applies_are_still_serialised(self) -> None:
        """The lock that MATTERS for this job is its own claim."""
        from services.enrichment import genre_playlist_resync_service as svc2

        assert svc2._claim_apply() is True
        try:
            result = svc2.apply_genre_playlist_resync()
            assert result.get("ok") is False
            assert "already running" in str(result.get("error") or "")
        finally:
            svc2._release_apply()


class TestCoalescing:
    def test_a_second_enqueue_replaces_the_payload(self, monkeypatch) -> None:
        """Two threads writing the same files from two payloads is the failure.

        The first job is held open with an ``Event`` so the second enqueue
        genuinely lands while it is running — otherwise a fast mocked write
        finishes before the second call and there is nothing to coalesce onto.
        """
        import threading

        release = threading.Event()
        started = threading.Event()
        seen: list[list[str]] = []

        def _slow(payload):
            seen.append([t["id"] for t in payload["tracks"]])
            started.set()
            release.wait(timeout=10.0)
            return {"files_updated": len(payload["tracks"])}

        monkeypatch.setattr(svc, "run_deferred_tag_write", _slow)

        first = queue.enqueue_tag_tracks(
            artist="A", album="B", tracks=[_track("t1")],
        )
        assert first["queued"] is True
        assert started.wait(timeout=10.0) is True, "the first job never started"

        second = queue.enqueue_tag_tracks(
            artist="A", album="B", tracks=[_track("t2")],
        )
        assert second["coalesced"] is True, (
            "a second enqueue while the first is running must coalesce, not "
            "start a second thread over the same files"
        )

        release.set()
        queue.wait_for_tag_writes(timeout=10.0)
        # Whatever ran, the LAST payload's track is what got written.
        assert any("t2" in batch for batch in seen), seen

    def test_an_empty_track_list_is_a_noop(self) -> None:
        assert queue.enqueue_tag_tracks(artist="A", album="B", tracks=[]) == {
            "queued": False, "tracks": 0,
        }

    def test_different_albums_get_their_own_jobs(self, monkeypatch) -> None:
        ran: list[str] = []
        monkeypatch.setattr(
            svc,
            "run_deferred_tag_write",
            lambda payload: ran.append(payload["album"]) or {"files_updated": 0},
        )
        queue.enqueue_tag_tracks(artist="A", album="One", tracks=[_track()])
        queue.enqueue_tag_tracks(artist="A", album="Two", tracks=[_track()])
        queue.wait_for_tag_writes(timeout=10.0)
        assert set(ran) == {"One", "Two"}, ran


class TestTheJobOwnsTheRescan:
    def test_the_rescan_runs_after_the_writes(self, monkeypatch) -> None:
        """Navidrome reads the FILES, so a rescan must not precede them."""
        order: list[str] = []

        monkeypatch.setattr(
            svc, "_write_album_track_file_tags",
            lambda **kw: order.append("write") or ONE_WRITE,
        )
        monkeypatch.setattr(
            "services.scanning.navidrome_rescan_service.flush_rescan_if_required",
            lambda *a, **k: order.append("rescan") or True,
        )

        svc.run_deferred_tag_write({
            "artist": "A", "album": "B", "tracks": [_track()],
        })

        assert order == ["write", "rescan"], order

    def test_it_drains_the_flag_the_writer_set(self) -> None:
        """⚠️ It must DRAIN, not force a scan.

        ``tag_file_service`` marks a rescan required on every write, and the
        scan flushes that flag at its boundary — which has already passed when
        the writes are deferred. Calling ``request_rescan`` here instead would
        bypass the flag and could start a scan for an album that wrote nothing.
        """
        import re

        source = (
            REPO_ROOT / "services" / "metadata" / "album_tag_sync_service.py"
        ).read_text(encoding="utf-8")
        fn = source.split("def run_deferred_tag_write", 1)[1].split("\ndef ", 1)[0]
        code = "\n".join(line.split("#", 1)[0] for line in fn.splitlines())
        assert "flush_rescan_if_required" in code
        assert not re.search(r"\brequest_rescan\s*\(", code), (
            "the job must drain the required flag, not force a scan per album"
        )

    def test_an_empty_payload_does_nothing(self) -> None:
        assert svc.run_deferred_tag_write({"artist": "A", "album": "B", "tracks": []}) == {
            "files_updated": 0, "corrections_recorded": 0, "tracks": 0,
        }
