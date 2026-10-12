"""The "succeeded but never imported" loops are self-limiting and prefix-tolerant.

Reported log (2026-10-12): three stuck-forever patterns in one queue log —

* ``queue_id=6271``: ``slskd succeeded but no local file found (will keep
  polling)`` re-logged every ~13-minute cycle for 45+ minutes. The queue's
  ``found_filename`` uses a DISC+TRACK number style (``music/Moonspell/.../
  0108 - Spring of Rage.flac``) whose leading digits can differ on disk
  (``08 - ...``, ``1-08 - ...``) — the punct-stripped basename key keeps the
  digits, so the file is never found. And the 15-minute wall-clock stale
  gate keys on ``updated_at``, which ``update_queue_item`` re-stamps on every
  update — a refreshed row never trips it, so the item polled forever.
* ``queue_id=6704/6705/6707``: ``Transfer file appeared within the grace
  window`` re-logged every cycle — the file exists, the caller's landed-path
  claim silently skipped it (already claimed by another row / moved) and the
  item was never imported nor failed.

Fixes under test:

1. **Track-prefix-tolerant basename keys** — ``_basename_without_track_prefix``
   strips a leading 1-4 digit disc/track number so ``0108 - Spring of Rage``,
   ``08 - Spring of Rage`` and ``1-08. Spring of Rage`` collapse to the same
   key. Secondary key only; exact/punct keys stay primary and the caller
   verifies before importing.
2. **Consecutive-cycle grace** — ``_SUCCEEDED_NOT_FOUND_CYCLES`` /
   ``_LANDED_NOT_IMPORTED_CYCLES`` fail the item after N consecutive
   observations, immune to ``updated_at`` refreshes.
3. **Loud landed-path skip** — the claimed/moved case logs a warning and
   fails for retry after 3 cycles instead of silently re-logging grace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.downloads import download_completion_service as dcs  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Track-number-prefix tolerance
# ---------------------------------------------------------------------------

class TestTrackPrefixKeys:

    def test_prefix_collapses_disc_track_styles(self):
        base = dcs._track_prefix_stripped_key("0108 - Spring of Rage.flac")
        assert base == dcs._track_prefix_stripped_key("08 - Spring of Rage.flac")
        assert base == dcs._track_prefix_stripped_key("1-08. Spring of Rage.flac")
        assert base == dcs._track_prefix_stripped_key("108 - Spring of Rage.flac")
        assert base == dcs._track_prefix_stripped_key("Spring of Rage.flac"), (
            "a file with NO number must match the prefixed remote name too"
        )
        assert "springofrage" in base

    def test_two_total_ways_to_number_still_equal(self):
        # THE reported Moonspell file: remote "0108" (disc 01 + track 08),
        # on-disk "08".
        assert dcs._track_prefix_stripped_key(
            "0108 - Spring of Rage.flac"
        ) == dcs._track_prefix_stripped_key("08 - Spring of Rage.flac")

    def test_non_numeric_leading_words_are_untouched(self):
        base = dcs._track_prefix_stripped_key("We Are the Champions.mp3")
        assert base == dcs._track_prefix_stripped_key(
            "We Are the Champions.mp3"
        ), "a title that merely STARTS with words is unchanged"

    def test_three_digit_album_year_is_not_stripped_from_middle(self):
        # Only a LEADING number is treated as a track/disc prefix.
        assert "1999" in dcs._track_prefix_stripped_key("Verse - 1999 (remix).flac")

    def test_basename_without_prefix_is_used_by_the_prefix_key(self):
        assert dcs._basename_without_track_prefix(
            "0108 - Spring of Rage.flac"
        ).strip() == "Spring of Rage.flac"


# ---------------------------------------------------------------------------
# 2. Consecutive-cycle counters (immune to updated_at refreshes)
# ---------------------------------------------------------------------------

class TestCycleCounters:

    def teardown_method(self):
        dcs._SUCCEEDED_NOT_FOUND_CYCLES.clear()
        dcs._LANDED_NOT_IMPORTED_CYCLES.clear()

    def test_bump_increments_and_returns(self):
        assert dcs._bump_cycle_counter(dcs._SUCCEEDED_NOT_FOUND_CYCLES, 6271) == 1
        assert dcs._bump_cycle_counter(dcs._SUCCEEDED_NOT_FOUND_CYCLES, 6271) == 2
        # Different items are independent.
        assert dcs._bump_cycle_counter(dcs._SUCCEEDED_NOT_FOUND_CYCLES, 6704) == 1

    def test_clear_resets(self):
        dcs._bump_cycle_counter(dcs._SUCCEEDED_NOT_FOUND_CYCLES, 6271)
        dcs._clear_cycle_counter(dcs._SUCCEEDED_NOT_FOUND_CYCLES, 6271)
        assert dcs._bump_cycle_counter(dcs._SUCCEEDED_NOT_FOUND_CYCLES, 6271) == 1

    def test_thresholds(self):
        assert dcs._SLSKD_SUCCEEDED_NOT_FOUND_MAX_CYCLES == 3
        assert dcs._LANDED_NOT_IMPORTED_MAX_CYCLES == 3


# ---------------------------------------------------------------------------
# 3. A succeeded-but-unfound transfer FAILS after N cycles, not forever
# ---------------------------------------------------------------------------

class _FakeSearchSlskd:
    FAILED_STATES = frozenset(["Completed, Errored", "Completed, TimedOut"])
    ACTIVE_STATES = frozenset(["Requested", "Queued, Remotely", "InProgress"])
    STATE_QUEUED_REMOTELY = "Queued, Remotely"
    STATE_SUCCEEDED = "Completed, Succeeded"

    def __init__(self):
        self.cancelled = []

    @staticmethod
    def state_text(raw):
        return str(raw or "").strip()

    def is_success_state(self, raw):
        return self.state_text(raw) == self.STATE_SUCCEEDED

    def cancel_download(self, username, transfer_id, remove=True):
        self.cancelled.append((username, transfer_id, remove))
        return True


class TestSucceededNotFoundFailsAfterCycles:
    """THE reported 6271 loop: slskd reports success, no file on disk, and
    the item polls forever because ``updated_at`` keeps getting refreshed.
    The new consecutive-cycle grace fails it after the 3rd observation."""

    def setup_method(self):
        dcs._SUCCEEDED_NOT_FOUND_CYCLES.clear()
        dcs._LANDED_NOT_IMPORTED_CYCLES.clear()

    def _run(self, monkeypatch, slskd, item, transfer):
        marked: list[tuple[int, str]] = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )
        # The file NEVER appears — force the not-found outcome.
        monkeypatch.setattr(dcs, "_wait_for_transfer_file", lambda *a, **k: None)
        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: "/downloads/Music")

        result = dcs._reconcile_transfer_state(
            item, slskd, active=[transfer], now=dcs._db_now_naive()
        )
        return result, marked

    def test_stays_downloading_then_fails_on_third_observation(self, monkeypatch):
        slskd = _FakeSearchSlskd()
        # FRESH item — the 15-min wall-clock gate never trips (updated_at is
        # refreshed every cycle in production), so ONLY the cycle counter can
        # end the polling.
        item = {"id": 6271, "found_filename": "/Peer/file.mp3", "updated_at": dcs._db_now_naive()}
        transfer = {
            "id": "t1",
            "username": "peer",
            "filename": "/Peer/file.mp3",
            "localFilePath": "",
            "state": "Completed, Succeeded",
        }

        first, marked1 = self._run(monkeypatch, slskd, item, transfer)
        assert first is False
        assert marked1 == [], "first observation stays in the grace window"

        second, marked2 = self._run(monkeypatch, slskd, item, transfer)
        assert second is False
        assert marked2 == [], "second observation still stays in the grace window"

        third, marked3 = self._run(monkeypatch, slskd, item, transfer)
        assert third is True, "the third consecutive observation must fail for retry"
        assert marked3 and marked3[0][0] == 6271
        assert "succeeded but local file not found" in marked3[0][1]
        assert slskd.cancelled, "the stale transfer is removed on failure"

    def test_a_fresh_item_recovers_if_the_file_lands(self, monkeypatch):
        # The grace mechanism must not be sticky: once the file appears, the
        # counter resets and the NEXT succeeded state imports.
        slskd = _FakeSearchSlskd()
        item = {"id": 70, "found_filename": "/Peer/song.mp3", "updated_at": dcs._db_now_naive()}
        transfer = {
            "id": "t1", "username": "peer", "filename": "/Peer/song.mp3",
            "localFilePath": "", "state": "Completed, Succeeded",
        }
        marked: list[tuple[int, str]] = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )
        monkeypatch.setattr(dcs, "_wait_for_transfer_file", lambda *a, **k: None)
        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: "/downloads/Music")

        assert dcs._reconcile_transfer_state(item, slskd, active=[transfer], now=dcs._db_now_naive()) is False
        # Now the file lands.
        monkeypatch.setattr(dcs, "_wait_for_transfer_file", lambda *a, **k: "/downloads/Music/song.mp3")
        result = dcs._reconcile_transfer_state(item, slskd, active=[transfer], now=dcs._db_now_naive())
        assert result == "/downloads/Music/song.mp3", "the landed path comes through"
        # And the counter must have been reset by the found file.
        assert dcs._SUCCEEDED_NOT_FOUND_CYCLES.get(70, 0) == 0


# ---------------------------------------------------------------------------
# 4. The downloads-walk finds the disc-prefixed file on disk
# ---------------------------------------------------------------------------

class TestWaitForTransferFileFindsPrefixedBasename:

    def test_finds_disc_prefix_drift(self, tmp_path, monkeypatch):
        """Queue found_filename "0108 - Spring of Rage.flac"; the file landed
        as "08 - Spring of Rage.flac" under a nested remote-style folder. The
        walk must find it via the prefix-tolerance key."""

        downloads_dir = tmp_path / "downloads"
        nested = downloads_dir / "music" / "Moonspell" / "Moonspell - Album - 2008 - Night Eternal"
        nested.mkdir(parents=True)
        on_disk = nested / "08 - Spring of Rage.flac"
        on_disk.write_bytes(b"fake flac")

        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: str(downloads_dir))

        found = dcs._wait_for_transfer_file(
            "music/Moonspell/Moonspell - Album - 2008 - Night Eternal/0108 - Spring of Rage.flac",
            "",
        )
        assert found is not None
        assert os.path.exists(found)
        assert found == str(on_disk)

    def test_exact_remote_path_still_wins(self, tmp_path, monkeypatch):
        downloads_dir = tmp_path / "downloads"
        exact = downloads_dir / "music" / "Moonspell" / "0108 - Spring of Rage.flac"
        exact.parent.mkdir(parents=True)
        exact.write_bytes(b"fake")
        # A differently-prefixed decoy must NOT shadow the exact remote path.
        decoy = downloads_dir / "music" / "Moonspell" / "08 - Spring of Rage.flac"
        decoy.write_bytes(b"decoy")

        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: str(downloads_dir))

        found = dcs._wait_for_transfer_file(
            "music/Moonspell/0108 - Spring of Rage.flac", ""
        )
        assert os.path.normpath(found) == os.path.normpath(str(exact))

    def test_non_prefixed_titles_are_unaffected(self, tmp_path, monkeypatch):
        downloads_dir = tmp_path / "downloads"
        nested = downloads_dir / "Album"
        nested.mkdir(parents=True)
        on_disk = nested / "Cannonball.flac"
        on_disk.write_bytes(b"fake")

        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: str(downloads_dir))

        found = dcs._wait_for_transfer_file("Album/Cannonball.flac", "")
        assert os.path.normpath(found) == os.path.normpath(str(on_disk))