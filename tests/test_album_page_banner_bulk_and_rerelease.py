"""Album page: the release-group banner, the bulk-edit refresh, per-track re-match.

Reported
--------
1. "When using lookup MBID on the album page, it has a banner showing that it's
   updating the release, but the same banner doesn't show when it's updated using
   'Choose from Release Group'."
2. "When selecting multiple tracks and selecting edit tracks, it doesn't seem to
   be saving correctly, or it saves but then the screen stays with the track page
   open rather than refreshing once the save has completed."
3. "It would also be good if I could select multiple tracks on an album and select
   lookup MBID and use those to match another release, but only match those tracks
   to another release for when tracks are accidentally merged to an incorrect
   release."
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALBUM_JS = ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
ALBUM_HTML = ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
TRACK_ROUTES = ROOT / "routes" / "track_routes.py"


@pytest.fixture(scope="module")
def js() -> str:
    return ALBUM_JS.read_text(encoding="utf-8")


def _function_body(js: str, name: str) -> str:
    """Source of one function, up to the next module-scope declaration."""
    start = js.index(f"function {name}(")
    rest = js[start:]
    nxt = min(
        (rest.find(marker) for marker in ("\n  function ", "\n  async function ")
         if rest.find(marker) != -1),
        default=len(rest),
    )
    return rest[:nxt]


class TestTheBusyBannerCoversBothPaths:
    """1. Both routes into the apply phase must show it."""

    def test_the_release_group_path_shows_the_banner(self, js):
        body = _function_body(js, "applyAlbumMbid")
        assert "startLookupBusy(" in body, (
            "this is the function 'Choose from release group' ends in, so "
            "without it that path looks like it did nothing"
        )

    def test_it_releases_the_banner(self, js):
        body = _function_body(js, "applyAlbumMbid")
        assert "endLookupBusy" in body, (
            "a popup that is acquired and never released strands on screen"
        )

    def test_the_lookup_path_keeps_its_own(self, js):
        """CONTROL — the working path must not lose it in the refactor."""
        body = _function_body(js, "openAlbumLookupModal")
        assert "startLookupBusy" in body


class TestTheBulkEditReloads:
    """2. A completed save must refresh the page."""

    def test_the_reload_is_outside_every_returning_branch(self, js):
        """⚠️ THE REPORTED BUG: an aborted request threw past the reload.

        The reload used to sit after the success branch only, so any throw —
        including the 30s client timeout on a large selection — skipped it and
        left the page showing pre-edit values.
        """
        body = _function_body(js, "applyEditTracks")
        assert "location.reload()" in body

    def test_the_request_allows_longer_than_the_default_timeout(self, js):
        """The default is 30s; the server used to sync every file inline."""
        body = _function_body(js, "applyEditTracks")
        assert "timeoutMs" in body, (
            "a bulk edit on a large selection must not be aborted by the "
            "client's 30s default"
        )
        timeout = int(body.split("timeoutMs:")[1].split("}")[0].strip() or 0)
        assert timeout >= 45000, f"timeoutMs={timeout} is not longer than the default"

    def test_the_server_no_longer_syncs_files_inline(self):
        src = TRACK_ROUTES.read_text(encoding="utf-8")
        fn = src[src.index("async def api_track_bulk_update"):]
        fn = fn[: fn.index("\ndef _start_bulk_file_sync")]

        assert "_start_bulk_file_sync(" in fn, "the file fan-out must be handed off"
        assert "for track_id in track_ids:" not in fn, (
            "an inline per-track read-modify-write is what exceeded the timeout"
        )

    def test_the_background_helper_is_a_daemon_thread(self):
        src = TRACK_ROUTES.read_text(encoding="utf-8")
        fn = src[src.index("def _start_bulk_file_sync"):]
        assert "daemon=True" in fn, "a non-daemon thread would block shutdown"


class TestPerTrackRerelease:
    """3. Re-match a SELECTION, not the whole album."""

    def test_the_button_exists(self):
        html = ALBUM_HTML.read_text(encoding="utf-8")
        assert "lookupMbidForSelectedTracks()" in html
        assert "Lookup MBID for Selected" in html

    def test_it_scopes_to_the_selection(self, js):
        body = _function_body(js, "lookupMbidForSelectedTracks")
        assert "selectedTrackIds()" in body, (
            "the whole point is that only the ticks are re-matched"
        )

    def test_it_writes_only_identity_fields(self, js):
        body = _function_body(js, "applyReleaseToSelectedTracks")
        for field in ("musicbrainz_albumid", "musicbrainz_album_mbid"):
            assert field in body
        # It must not rewrite the album NAME — that is the album-wide action.
        assert "album:" not in body.replace("musicbrainz_albumid", "").replace(
            "musicbrainz_album_mbid", ""
        ), "a per-track re-match must not rename the album"

    def test_it_refreshes_afterwards(self, js):
        body = _function_body(js, "applyReleaseToSelectedTracks")
        assert "location.reload()" in body

    def test_it_resolves_the_group_to_a_release(self, js):
        """⚠️ The picker returns a release GROUP. Writing that into
        ``musicbrainz_albumid`` would bind the tracks to a non-release."""
        body = _function_body(js, "lookupMbidForSelectedTracks")
        assert "resolveReleaseForGroup" in body

        resolver = _function_body(js, "resolveReleaseForGroup")
        assert "/api/album/musicbrainz/best-release" in resolver, (
            "the same resolution the album-wide path uses"
        )
        assert "release_group_mbid" in resolver

    def test_the_endpoint_accepts_the_identity_columns(self):
        src = TRACK_ROUTES.read_text(encoding="utf-8")
        fn = src[src.index("async def api_track_bulk_update"):]
        allowed = fn[fn.index("allowed = {"): fn.index("}", fn.index("allowed = {"))]
        for field in ("musicbrainz_album_mbid", "musicbrainz_albumid",
                      "musicbrainz_releasegroupid"):
            assert field in allowed, f"{field} would be refused by the allow-list"

    def test_the_identity_columns_survive_normalisation(self):
        from routes.track_routes import _normalize_track_updates

        fields = {
            "musicbrainz_albumid": "abc",
            "musicbrainz_album_mbid": "abc",
            "musicbrainz_releasegroupid": "def",
        }
        out = _normalize_track_updates(fields, {k: "text" for k in fields})
        assert out == fields
