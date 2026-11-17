"""Upcoming releases: per-track queueing, unreleased albums, and search parity.

REPORTED

> "Upcoming releases when hitting the queue button doesn't queue each of the
> songs into the download queue under the album, it adds the album as a single
> item."

> "It also doesn't allow to queue the album if it's not released yet, if matched
> on musicbrainz this should work."

> "The search button next to each release also doesn't search the same way as the
> universal search at the top of the page, so the results don't show the same way
> from musicbrainz to add to the queue."

THREE DISTINCT DEFECTS

1. **The stored id is a release-GROUP id.** ``upcoming_releases.release_group_mbid``
   is filled from the release-group search, but ``start_release_download``
   fetches a concrete RELEASE. A group id yields no tracklist, so nothing could
   be queued per track and the route fell through to one album-typed row.
2. **The table row gated the Queue button on the release date.** ``else if
   (released)`` meant an unreleased album had no button at all.
3. **The row's search used a different endpoint.** It called
   ``/api/upcoming-releases/search-musicbrainz`` (raw release-GROUP rows, no
   concrete release, no tracklist) instead of ``/api/musicbrainz/search`` — the
   one the universal search uses, and the one whose shape this renderer reads.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE_ROUTE = REPO_ROOT / "routes" / "downloads.py"
UPCOMING_JS = REPO_ROOT / "test_site/static/js/services/upcoming-releases.js"
MB_QUEUE_JS = REPO_ROOT / "test_site/static/js/services/musicbrainz-queue.js"


class TestTheQueueRouteResolvesTheGroupToARelease:
    def test_it_resolves_before_queueing(self) -> None:
        src = QUEUE_ROUTE.read_text(encoding="utf-8")
        start = src.index("async def api_queue_upcoming")
        body = src[start:start + 6000]
        assert "resolve_release_id" in body, (
            "the route queues the stored id directly; a release-GROUP id has no "
            "tracklist, so nothing can be queued per track"
        )
        assert "concrete_release_id" in body

    def test_it_prefers_the_concrete_release(self) -> None:
        src = QUEUE_ROUTE.read_text(encoding="utf-8")
        start = src.index("async def api_queue_upcoming")
        body = src[start:start + 6000]
        assert "concrete_release_id or release_mbid" in body, (
            "the group id must only be used when no concrete release resolved"
        )

    def test_the_single_item_fallback_says_which_shape_it_used(self) -> None:
        """A caller must be able to tell one album row from a per-track queue."""
        src = QUEUE_ROUTE.read_text(encoding="utf-8")
        assert '"queue_shape": "album"' in src
        assert '"queue_shape": "tracks"' in src


class TestTheQueueButtonIsOfferedForUnreleasedAlbums:
    def test_the_row_no_longer_gates_on_the_release_date(self) -> None:
        """``else if (released)`` meant an upcoming album had NO Queue button."""
        src = UPCOMING_JS.read_text(encoding="utf-8")
        assert "} else if (released) {" not in src, (
            "the Queue button is still gated on the release date"
        )

    def test_the_queue_button_is_rendered_for_any_unqueued_release(self) -> None:
        src = UPCOMING_JS.read_text(encoding="utf-8")
        # Find the row renderer's action block.
        start = src.index("if (release.in_queue)")
        block = src[start:start + 1200]
        assert "ur-queue-btn" in block


class TestTheRowSearchMatchesUniversalSearch:
    def test_it_calls_the_universal_endpoint(self) -> None:
        src = MB_QUEUE_JS.read_text(encoding="utf-8")
        assert "const SEARCH_ENDPOINT = '/api/musicbrainz/search';" in src, (
            "the row search still uses the release-group-only endpoint"
        )

    def test_it_no_longer_uses_the_release_group_search(self) -> None:
        src = MB_QUEUE_JS.read_text(encoding="utf-8")
        assert "/api/upcoming-releases/search-musicbrainz" not in src

    def test_it_reads_the_universal_payload_shape(self) -> None:
        """The universal endpoint answers ``{releases: [...]}``."""
        src = MB_QUEUE_JS.read_text(encoding="utf-8")
        assert "data.releases" in src
        assert "data.results" not in src, (
            "the release-group shape is gone; reading data.results yields nothing"
        )

    def test_it_asks_for_tracklists(self) -> None:
        """Per-track selection needs each concrete release's tracklist."""
        src = MB_QUEUE_JS.read_text(encoding="utf-8")
        assert "with_tracklists" in src
