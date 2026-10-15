"""Choosing WHICH edition to queue, from the search results.

REPORTED

> When using the search for an album, then selecting queue, it sometimes comes up
> with an option to select a version, but the version can't be selected.

The screenshot showed exactly that: the *Select Version: End the Silence* flyout
listing two editions (29 tracks, and 14 tracks with "3 editions" folded in), each
with a **Preview Tracks** button and — for the second one — nothing else. The
instruction above it read *"Select the exact version you want to queue:"* and
there was no control that queued anything.

THE DEAD END

``openReleasePicker`` probes the release group and only auto-queues when the
probe comes back with **exactly one** release::

    if (releases && releases.length === 1) { ... queue it ... }
    window.openSlideOver(url, 'Select Version: ' + title)

Everything else opened the flyout. And the flyout renders a button only for
``mode=select``, which is the **album page's link flow** — its handler
(``.js-pick-release`` → ``applyAlbumMbid``) lives on that page, so it must not be
reused here. A route comment claimed the queue path "auto-selects (one release,
or every edition sharing a track count) and is driven by the caller", but the
JSON probe never applied the edition grouping — so for any group whose editions
genuinely differ, the flyout was a dead end. That is the report.

WHAT WAS BUILT

* ``GET /api/musicbrainz/release-picker`` gained **``mode=queue``**, rendering a
  *Queue this version* button (``.js-queue-release``) per card. ``mode=select``
  is unchanged, and the two are mutually exclusive so a card never offers both.
* ``openReleasePicker`` (both trees) now asks for ``mode=queue``.
* A **delegated** document listener (both trees) turns the button into a
  ``queueSpecificRelease(releaseId, title, artist)`` call — the same function the
  auto-queue path uses, so the queue POST, the success toast, the ``onQueued``
  callback and closing the slide-over all behave identically.

The artist rides on the button as a data attribute because the queue POST needs
it and the slide-over has no other source for it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ROUTE = REPO_ROOT / "routes" / "musicbrainz_routes.py"
LIVE_MAIN_JS = REPO_ROOT / "static" / "js" / "main.js"
TEST_SITE_MAIN_JS = REPO_ROOT / "test_site" / "static" / "js" / "main.js"

MAIN_JS = [LIVE_MAIN_JS, TEST_SITE_MAIN_JS]


def _route_source() -> str:
    return ROUTE.read_text(encoding="utf-8")


def _inline_picker_template() -> str:
    """The Jinja string the release-picker route renders for the slideover."""
    source = _route_source()
    start = source.index("return await _render(")
    open_at = source.index('"""', start) + 3
    close_at = source.index('"""', open_at)
    return source[open_at:close_at]


def _release(**overrides) -> dict:
    row = {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "title": "End the Silence",
        "status": "Official",
        "disambiguation": "",
        "country": "NZ",
        "date": "2006-05-29",
        "track_count": 14,
        "format": "CD",
        "artist": "Blindspott",
        "edition_count": 1,
    }
    row.update(overrides)
    return row


def _render(
    *,
    queue_mode: bool,
    releases: list[dict] | None = None,
    artist: str = "Blindspott",
) -> str:
    from jinja2 import Environment

    template = Environment().from_string(_inline_picker_template())
    return template.render(
        releases=releases if releases is not None else [_release()],
        album="End the Silence",
        artist=artist,
        select_mode=False,
        queue_mode=queue_mode,
    )


# ===========================================================================
# 1. The route renders a queue button
# ===========================================================================


class TestTheQueueModeRendersAButton:
    def test_the_mode_is_parsed(self):
        source = _route_source()
        assert 'queue_mode = mode == "queue"' in source, (
            "the route must accept mode=queue"
        )

    def test_the_flag_reaches_the_renderer(self):
        assert "queue_mode=queue_mode" in _route_source(), (
            "without this the template's queue_mode is undefined and the button "
            "silently never renders"
        )

    def test_a_button_is_rendered_per_release(self):
        """THE FIX: the reported flyout had nothing to click."""
        rows = [_release(id="r1", track_count=29), _release(id="r2", track_count=14)]
        html = _render(queue_mode=True, releases=rows)
        assert html.count("js-queue-release") == len(rows), (
            "every version needs its own selectable control"
        )

    def test_the_button_carries_the_release_identity(self):
        html = _render(queue_mode=True)
        assert 'data-release-id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"' in html
        assert 'data-release-title="End the Silence"' in html

    def test_the_button_carries_the_ARTIST(self):
        """The queue POST requires an artist and the slide-over has no other
        source for it — the route only receives it as a query parameter."""
        html = _render(queue_mode=True, artist="Blindspott")
        assert 'data-release-artist="Blindspott"' in html

    def test_it_says_queue_not_link(self):
        html = _render(queue_mode=True)
        assert "Queue this version" in html
        assert "Use this release" not in html

    def test_the_preview_button_survives(self):
        """CONTROL — the fix adds a control, it does not replace one."""
        html = _render(queue_mode=True)
        assert "Preview Tracks" in html

    def test_the_edition_badge_survives(self):
        """CONTROL — '3 editions' is how the user knows pressings were folded."""
        html = _render(queue_mode=True, releases=[_release(edition_count=3)])
        assert "3 editions" in html

    def test_the_instruction_matches_the_action(self):
        """CONTROL — the heading already said "queue"; now a button agrees."""
        assert "Select the exact version you want to queue" in _render(queue_mode=True)


# ===========================================================================
# 2. Both trees ask for it and wire it up
# ===========================================================================


@pytest.mark.parametrize("path", MAIN_JS, ids=["live", "test_site"])
class TestTheQueueFlowWiresTheButton:
    def test_the_picker_requests_queue_mode(self, path):
        source = path.read_text(encoding="utf-8")
        assert "mode=queue" in source, (
            "openReleasePicker must ask for the mode that renders a button — "
            "without it the flyout is the reported dead end"
        )

    def test_the_mode_is_on_the_url_the_flyout_opens(self, path):
        """⚠️ Scoped to openReleasePicker's own window.

        A bare ``mode=queue`` anywhere in the file would satisfy the check above
        while the flyout still opened without it — the exact shape of the bug.
        """
        source = path.read_text(encoding="utf-8")
        start = source.index("openReleasePicker")
        window = source[start : start + 4000]
        assert re.search(
            r"release-picker\?[^'\"`]*mode=queue", window
        ), "the flyout URL does not carry mode=queue"

    def test_a_delegated_handler_exists(self, path):
        """The slide-over is SERVER-rendered, so its buttons cannot be bound at
        load time — the whole app delegates for these.

        ⚠️ Asserted as a ``closest(...)`` CALL, not a bare selector string. A
        bare substring is satisfied by the comment above the handler, so
        breaking the lookup would leave the test green — measured: mutating the
        selector inside ``closest()`` went uncaught until this was tightened.
        """
        source = path.read_text(encoding="utf-8")
        assert re.search(
            r"closest\(\s*['\"]\.js-queue-release['\"]\s*\)", source
        ), "nothing looks up .js-queue-release on a click target"
        assert re.search(
            r"addEventListener\(\s*['\"]click['\"]", source
        ), "no delegated click listener"

    def test_the_handler_queues_the_chosen_release(self, path):
        source = path.read_text(encoding="utf-8")
        start = source.index(".js-queue-release")
        window = source[start : start + 1400]
        assert re.search(r"queueSpecificRelease\(", window), (
            "the button must go through queueSpecificRelease so the POST, the "
            "toast, the onQueued callback and closing the slide-over are the "
            "same as the auto-queue path"
        )

    def test_the_handler_reads_the_data_attributes(self, path):
        source = path.read_text(encoding="utf-8")
        start = source.index(".js-queue-release")
        window = source[start : start + 1400]
        for attr in ("data-release-id", "data-release-title", "data-release-artist"):
            assert attr in window, f"the handler never reads {attr}"

    def test_the_button_is_disabled_while_in_flight(self, path):
        """A queue POST takes seconds; a second click would queue it twice."""
        source = path.read_text(encoding="utf-8")
        start = source.index(".js-queue-release")
        window = source[start : start + 1400]
        assert "btn.disabled = true" in window
        assert "btn.disabled = false" in window, (
            "the button must come back on FAILURE, or the user cannot retry — "
            "the slide-over only closes on success"
        )


# ===========================================================================
# 3. The stale claim that caused it
# ===========================================================================


class TestTheStaleClaimIsGone:
    def test_the_route_no_longer_claims_the_queue_path_needs_no_button(self):
        """⚠️ The comment said "the queue path auto-selects (one release, or
        every edition sharing a track count) and is driven by the caller". The
        probe auto-queues only a group with ONE release, so the claim was false
        and it is what justified leaving the flyout without a button."""
        source = _route_source()
        assert "Only the\n    # second one needs a button" not in source
        assert "BOTH need a button" in source, (
            "the correction must be recorded where the next reader will look"
        )

    def test_the_route_explains_why_the_two_modes_are_distinct(self):
        """The link button's handler lives on the album page, so reusing
        ``mode=select`` here would render a button that does nothing."""
        source = _route_source()
        assert "its handler lives on that page" in source


# ===========================================================================
# 4. The REAL route, end to end
# ===========================================================================
#
# The tests above render the Jinja string directly, which proves the markup but
# not that the route WIRES the query parameter through to it. These go through
# the app, so the whole path is exercised: query parameter → flag → HTML.

RG_ID = "11111111-1111-4111-8111-111111111111"


def _mb_release(rid: str, title: str, tracks: int, country: str = "NZ") -> dict:
    return {
        "id": rid,
        "title": title,
        "status": "Official",
        "disambiguation": "",
        "country": country,
        "date": "2006-05-29",
        "media": [{"format": "CD", "track-count": tracks}],
    }


class _FakeMbClient:
    """The reported album: one 29-track edition and two 14-track pressings."""

    def browse_releases_for_group(self, rg_id, inc="media", limit=100):
        return [
            _mb_release("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "End the Silence (Tour Edition)", 29),
            _mb_release("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "End the Silence", 14),
            _mb_release("cccccccc-cccc-cccc-cccc-cccccccccccc", "End the Silence", 14, "US"),
        ]

    def get_release(self, rid, inc=None):
        return {}


@pytest.fixture
def mb_client(monkeypatch):
    from routes import musicbrainz_routes as mbr

    monkeypatch.setattr(mbr, "_mb_client", _FakeMbClient())
    return mbr


@pytest.mark.asyncio
class TestTheRealRoute:
    async def _html(self, client, query: str) -> str:
        resp = await client.get(
            f"/api/musicbrainz/release-picker?{query}"
            f"&rg_id={RG_ID}&artist=Blindspott&album=End+the+Silence"
        )
        assert resp.status_code == 200, f"route returned {resp.status_code}"
        return await resp.get_data(as_text=True)

    async def test_queue_mode_renders_a_selectable_button(self, client, mb_client):
        """THE REPORTED BUG, end to end: the flyout opened with nothing to click."""
        html = await self._html(client, "mode=queue")

        assert "Queue this version" in html, "the version still cannot be selected"
        assert "js-queue-release" in html
        assert 'data-release-artist="Blindspott"' in html, (
            "the queue POST needs an artist and the slide-over has no other source"
        )

    async def test_queue_mode_offers_one_button_per_edition(self, client, mb_client):
        """29 tracks and the two 14s fold to 2 rows — each needs its own control."""
        html = await self._html(client, "mode=queue")

        assert html.count("Queue this version") == 2, (
            "every surviving row must be selectable"
        )
        assert "2 editions" in html, "the folded pressings must be disclosed"

    async def test_queue_mode_keeps_preview(self, client, mb_client):
        html = await self._html(client, "mode=queue")
        assert "Preview Tracks" in html

    async def test_select_mode_is_unchanged(self, client, mb_client):
        """CONTROL — the album page's link flow still gets its own button."""
        html = await self._html(client, "mode=select")

        assert "Use this release" in html
        assert "js-pick-release" in html
        assert "Queue this version" not in html

    async def test_no_mode_renders_neither_action(self, client, mb_client):
        """CONTROL — the list is still browsable without either action."""
        html = await self._html(client, "")

        assert "Preview Tracks" in html
        assert "js-queue-release" not in html
        assert "js-pick-release" not in html

    async def test_the_json_probe_is_unaffected(self, client, mb_client):
        """CONTROL — ``format=json`` still answers the auto-queue probe.

        It returns UNGROUPED releases on purpose: the caller's identical-track-count
        check needs the real count per release, and grouping is a display concern.
        """
        resp = await client.get(
            f"/api/musicbrainz/release-picker?format=json&rg_id={RG_ID}"
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 3
        assert [r["track_count"] for r in body["releases"]] == [29, 14, 14]
