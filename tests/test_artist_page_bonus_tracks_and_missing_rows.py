"""The artist page must not call bonus tracks "missing", and must LIST them.

Reported (screenshots): the album page for Black Sails at Midnight showed
"3 Bonus to download" with #11/#12/#13 badged **Bonus**, while the artist page
showed the same album with a yellow "3 tracks missing" badge — and expanding the
album showed only the 10 library tracks, with no sign of the three at all.

Two independent faults, both client-side. The server has always sent the split
(``missing_count``, ``bonus_count``, ``missing_core_count``) and it has always
sent the rows (``missing_tracks`` with ``is_bonus``):

1. The artist page read ``data.missing_count`` RAW — it never subtracted
   ``bonus_count``, unlike the album page.
2. The artist page's expanded tracklist rendered only ``/api/album/tracklist``
   (the collection's rows). It had no concept of a missing row at all, so the
   badge said "3 missing" above a list that showed none of them.

There was also a SECOND badge writer in ``artist.js`` doing the same wrong sum
with different wording, racing ``artist-releases.js`` on the same span.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ARTIST_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist.js"
RELEASES_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-releases.js"
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
HELPER_JS = REPO_ROOT / "test_site" / "static" / "js" / "utils" / "missing-tracks.js"
BASE_HTML = REPO_ROOT / "test_site" / "templates" / "base.html"
RELEASE_SECTION = REPO_ROOT / "test_site" / "templates" / "components" / "_release_section.html"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_helper(payload: dict) -> dict:
    """Execute the shared JS helper in node and return its answers."""
    node = _find_node()
    if node is None:
        pytest.skip("node is not available")
    payload_json = json.dumps(payload)
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const fs = require('fs');
        const sandbox = {{ window: {{}} }};
        sandbox.window = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(
          fs.readFileSync({str(HELPER_JS)!r}, 'utf8'),
          sandbox
        );
        const data = {payload_json};
        const out = sandbox.missingTracks.splitMissingCounts(data);
        out.badge = sandbox.missingTracks.missingBadgeText(data);
        console.log(JSON.stringify(out));
        """
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _find_node() -> str | None:
    import shutil

    return shutil.which("node")


# ---------------------------------------------------------------------------
# 1. The shared helper does the split the album page always did
# ---------------------------------------------------------------------------

class TestTheSplitRule:
    """These run only with node; the source guards below always run."""

    def test_bonus_rows_are_not_counted_as_missing(self):
        out = _run_helper({
            "missing_count": 3,
            "bonus_count": 3,
            "missing_core_count": 0,
        })
        assert out["missing"] == 0
        assert out["bonus"] == 3
        # The reported badge text for an album owned in full.
        assert out["badge"] == "3 bonus"

    def test_a_mixed_album_reports_both(self):
        out = _run_helper({
            "missing_count": 5,
            "bonus_count": 2,
            "missing_core_count": 3,
        })
        assert out["missing"] == 3
        assert out["badge"] == "3 tracks missing · 2 bonus"

    def test_a_single_missing_track_is_singular(self):
        out = _run_helper({
            "missing_count": 1,
            "bonus_count": 0,
            "missing_core_count": 1,
        })
        assert out["badge"] == "1 track missing"

    def test_nothing_missing_produces_no_badge(self):
        out = _run_helper({"missing_count": 0, "bonus_count": 0})
        assert out["badge"] == ""

    def test_an_older_payload_without_the_split_derives_it(self):
        """missing_core_count is authoritative; fall back to the rows."""
        out = _run_helper({
            "missing_count": 3,
            "missing_tracks": [
                {"title": "a", "is_bonus": True},
                {"title": "b", "is_bonus": True},
                {"title": "c", "is_bonus": True},
            ],
        })
        assert out["missing"] == 0
        assert out["bonus"] == 3


# ---------------------------------------------------------------------------
# 2. The artist page uses the helper instead of the raw count
# ---------------------------------------------------------------------------

class TestTheArtistPageNoLongerCountsBonusAsMissing:
    def test_the_helper_is_loaded_globally(self):
        assert "js/utils/missing-tracks.js" in _src(BASE_HTML), (
            "the helper must be loaded before the page modules"
        )

    def test_the_badge_comes_from_the_helper(self):
        body = _src(RELEASES_JS)
        assert "missingTracks.missingBadgeText(" in body
        assert "missingTracks.splitMissingCounts(" in body

    def test_the_raw_missing_count_is_gone(self):
        body = _src(RELEASES_JS)
        assert 'var count = data && data.missing_count;' not in body, (
            "reading missing_count raw is the reported bug"
        )
        assert "count + ' missing'" not in body

    def test_bonus_only_albums_are_green_not_warning(self):
        body = _src(RELEASES_JS)
        assert "bonusOnly" in body
        assert "classList.toggle('bg-success', bonusOnly)" in body


# ---------------------------------------------------------------------------
# 3. The duplicate writer in artist.js is gone
# ---------------------------------------------------------------------------

class TestThereIsOnlyOneBadgeWriter:
    def test_artist_js_no_longer_fills_the_badge(self):
        body = _src(ARTIST_JS)
        # The span name and endpoint survive only inside the explanatory
        # comment; no lookup+assignment may remain.
        assert "album-missing-tracks-badge')" not in body, (
            "artist.js must not write the missing-tracks badge — "
            "artist-releases.js is the single writer"
        )
        assert "data.missing_count} tracks missing" not in body

    def test_artist_js_no_longer_refetches_missing_counts(self):
        body = _src(ARTIST_JS)
        assert "/api/album/missing-tracks" not in body

    def test_the_removal_is_documented(self):
        assert "SECOND, undocumented writer" in _src(ARTIST_JS)

    def test_the_template_names_one_writer(self):
        body = _src(RELEASE_SECTION)
        assert "ONE WRITER" in body


# ---------------------------------------------------------------------------
# 4. Missing tracks are listed under the artist page's tracklist
# ---------------------------------------------------------------------------

class TestTheArtistTracklistListsMissingTracks:
    def test_the_tracklist_fetches_missing_rows(self):
        body = _src(RELEASES_JS)
        assert "appendMissingTracks" in body
        assert "renderMissingInto" in body

    def test_it_is_called_for_owned_albums_only(self):
        body = _src(RELEASES_JS)
        assert "if (!isMissing) appendMissingTracks(item, contentEl);" in body

    def test_bonus_rows_are_badged_bonus_not_missing(self):
        body = _src(RELEASES_JS)
        assert ">Bonus</span>" in body, "a bonus cut must not say Missing"
        assert "is_bonus" in body

    def test_a_queued_row_shows_its_queue_status(self):
        body = _src(RELEASES_JS)
        assert "queue_status" in body

    def test_the_compilation_performer_is_shown(self):
        body = _src(RELEASES_JS)
        assert "track_artist" in body, (
            "on a compilation the performer is what tells the user which track "
            "this is — the album artist is 'Various Artists'"
        )


# ---------------------------------------------------------------------------
# 5. The album page reads the same rule (no drift)
# ---------------------------------------------------------------------------

class TestTheAlbumPageReadsTheSameRule:
    def test_the_album_badge_uses_the_helper(self):
        body = _src(ALBUM_JS)
        assert "missingTracks.splitMissingCounts(" in body

    def test_the_server_still_sends_the_split(self):
        src = (REPO_ROOT / "services" / "metadata" / "album_missing_service.py").read_text(
            encoding="utf-8"
        )
        assert '"bonus_count": sum(1 for m in missing if m.get("is_bonus"))' in src
        assert '"missing_core_count": sum(1 for m in missing if not m.get("is_bonus"))' in src
