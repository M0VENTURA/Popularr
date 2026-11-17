"""The Matched & Unmatched Folders panel must not be a one-shot render.

REPORT

> "On the Active Queue and Monitor Page, the discovered files section isn't
> always active to select"

DIAGNOSIS

`renderUnmatchedFolders()` ran from `DOMContentLoaded` and from the section's own
buttons — nothing else ever called it.

`download-queue.js` runs a 10s poller on this page, but it only re-renders the
QUEUE lists; it never touches `#unmatchedFoldersList`. So the panel showed the
folder list exactly as it was at page load:

  * a download finishing after load never appeared;
  * a scan run from another tab never appeared;
  * and because the hide-when-empty branch had already run, a panel that came
    up empty stayed collapsed — with no way to make it re-evaluate short of a
    full reload.

THE FIX

A managed poller re-renders the section on an interval. Because the endpoint
walks the downloads directory and reads tags from every audio file, the interval
is deliberately slower than the queue's 10s, and an overlap guard prevents a
slow response from racing a newer one.

Re-rendering replaces `list.innerHTML`, which would collapse a tracklist the
user had opened, so expansion is remembered by release id and restored.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MONITOR_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "monitor.js"


def _source() -> str:
    return MONITOR_JS.read_text(encoding="utf-8")


class TestTheSectionIsRefreshedAfterLoad:
    def test_a_poller_is_started(self) -> None:
        src = _source()
        assert "unmatchedPoller" in src, (
            "nothing refreshes the unmatched-folder section after page load"
        )
        assert "UNMATCHED_REFRESH_MS" in src

    def test_the_poller_is_started_for_that_section(self) -> None:
        src = _source()
        # The poller must be gated on the section existing, so pages without it
        # do not poll endpoint they cannot render.
        assert "document.getElementById('unmatchedFoldersSection')" in src
        start = src.index("unmatchedPoller = global.poller.create")
        window = src[start - 400:start + 400]
        assert "unmatchedFoldersSection" in window

    def test_it_uses_the_managed_poller_not_a_bare_interval(self) -> None:
        """The managed poller pauses when hidden and never overlaps a tick."""
        src = _source()
        assert "global.poller.create(" in src, (
            "a bare setInterval would poll in a background tab and overlap"
        )

    def test_the_refresh_is_slower_than_the_queue_poll(self) -> None:
        """The endpoint walks the directory and reads every file's tags."""
        src = _source()
        start = src.index("const UNMATCHED_REFRESH_MS")
        line = src[start:start + 80]
        ms = int(line.split("=")[1].split(";")[0].strip())
        assert ms >= 10000, (
            "refreshing a directory-and-tags scan faster than the queue poll "
            "would hammer the disk"
        )


class TestRefreshingDoesNotDisturbTheUser:
    def test_overlapping_refreshes_are_guarded(self) -> None:
        src = _source()
        assert "unmatchedInFlight" in src, (
            "a slow request could be overtaken by a newer one and repaint a "
            "stale list"
        )

    def test_an_action_is_never_dropped_by_the_guard(self) -> None:
        """Confirm/delete must still refresh, even mid-poll.

        A poll that wins the race must not swallow the handler's refresh —
        otherwise the panel keeps showing a folder the user just deleted.
        """
        src = _source()
        assert "options.force" in src or "force: true" in src
        assert "renderUnmatchedFolders({ force: true })" in src

    def test_open_tracklists_are_restored(self) -> None:
        src = _source()
        assert "restoreOpenTracklists" in src
        assert "openTracklists" in src


class TestTheEmptyBranchStillHides:
    def test_hide_when_empty_survives(self) -> None:
        """The section stays a data-driven panel, not an always-visible card."""
        src = _source()
        assert "section.style.display = 'none'" in src
        assert "section.style.display = 'block'" in src
