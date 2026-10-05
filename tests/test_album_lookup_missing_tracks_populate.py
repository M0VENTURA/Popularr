"""After a Lookup MBID, the album page must actually populate missing tracks.

REPORTED: *"Missing tracks are still not populating on test_site after the
lookup_mbid."*

Two separate ways that happened, both in `test_site/static/js/pages/album.js`
and both silent:

1. **`applyAlbumMbid()` never refreshed the findings.** Live's
   `applyAlbumMbid` has always fired `_refreshAlbumTrackFindings(mbid)` right
   after switching to the Edit tab; test_site's copy did field → preview →
   notify and stopped, so any caller arriving with a concrete release id
   (the release picker's release path, an inline handler) produced **no
   missing rows at all**.
2. **`refreshAlbumTrackFindings()` refused to run without a release id.**
   Its body was `if (releaseMbid) { … }`, and `applyAlbumMatch` passes
   `resolvedReleaseId`, which stays `''` whenever the best-release probe fails
   (throttled MusicBrainz, an offline group browse). The proposal in the same
   function falls back to `release.id` in that situation, so the orange bars
   appeared while the tracklist quietly stayed blank — and the failure was
   swallowed by an empty `catch`. The server can be asked with no id at all:
   `get_missing_tracks` falls back to the stored album MBID, then a name
   search.

These tests drive the REAL module in node (same harness idea as
`test_findings_persist_behaviourally.py`), so a renderer that stops rendering
fails them too.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEST_SITE_ALBUM = REPO / "test_site" / "static" / "js" / "pages" / "album.js"
LIVE_ALBUM = REPO / "static" / "js" / "album_detail.js"


def _build_harness(path: Path, script_body: str) -> str:
    """Wrap a page module + a probe that runs INSIDE its IIFE."""
    source = path.read_text(encoding="utf-8")
    assert source.rstrip().endswith("})(window);"), f"{path} is not an IIFE"
    module = source.rstrip()[: -len("})(window);")]

    prelude = textwrap.dedent(
        """
        const seen = [];
        const fetches = [];
        const warns = [];
        // Capture console.warn/error so a swallowed failure is ASSERTABLE —
        // the trailer writes to stdout directly, so this cannot eat the RESULT
        // line.
        globalThis.console = {
          warn: (m) => warns.push(String(m)),
          error: (m) => warns.push(String(m)),
          log: (m) => warns.push(String(m)),
          info: () => {}, debug: () => {}, trace: () => {},
        };
        const win = {};
        win.toast = {
          success: (m) => seen.push(['success', String(m)]),
          error: (m) => seen.push(['error', String(m)]),
          warning: (m) => seen.push(['warning', String(m)]),
          info: (m) => seen.push(['info', String(m)]),
        };
        win.alert = (m) => seen.push(['alert', String(m)]);
        win.console = console;
        // The page under test resolves artist/album from here.
        win._pageData = { artistName: 'Probe Artist', albumName: 'Probe Album' };

        const fakeField = () => ({
          value: '',
          style: {},
          classList: { add() {}, remove() {}, contains: () => false },
          querySelector: () => null,
          querySelectorAll: () => [],
          insertAdjacentElement() {},
          appendChild() {},
          setAttribute() {},
          addEventListener() {},
        });
        // The tracklist tbody: enough surface for the row builders and the
        // placement helpers to run without a real DOM.
        const tbody = {
          children: [],
          cells: [],
          querySelector: () => null,
          querySelectorAll: () => [],
          appendChild(row) { this.children.push(row); },
          insertBefore(row) { this.children.push(row); },
        };
        win.document = {
          getElementById: (id) => (id === 'albumTracksTbody' ? tbody : fakeField()),
          querySelector: () => null,
          querySelectorAll: () => [],
          addEventListener: () => {},
          createElement: () => fakeField(),
        };
        win.location = { reload: () => {}, href: '' };
        win.ui = { confirm: async () => true };
        win.confirm = () => true;
        win.busyPopup = null;
        win.api = {
          getJson: async (url) => {
            fetches.push(String(url));
            return win.__getJsonResult(String(url));
          },
          postJson: async (url) => {
            fetches.push('POST ' + String(url));
            return win.__postJsonResult(String(url));
          },
        };
        win.__getJsonResult = () => ({
          mb_total: 0, missing_count: 0, missing_tracks: [], duplicates: [],
        });
        win.__postJsonResult = () => ({});
        win.setTimeout = (fn) => { try { fn(); } catch (e) {} return 0; };
        globalThis.window = win;
        globalThis.document = win.document;
        globalThis.location = win.location;
        globalThis.alert = win.alert;
        const realSetTimeout = globalThis.setTimeout;
        globalThis.setTimeout = win.setTimeout;
        globalThis.__realSetTimeout = realSetTimeout;
        """
    )

    probe = textwrap.dedent(
        """
        (async () => {
          try {
            __BODY__
            globalThis.__RESULT__ = { seen, fetches, warns, tbodyRows: tbody.children.length };
          } catch (e) {
            globalThis.__RESULT__ = {
              error: String((e && e.stack) || e), seen, fetches, warns,
            };
          }
        })();
        """
    ).replace("__BODY__", textwrap.indent(textwrap.dedent(script_body), " " * 4))

    return (
        prelude
        + module
        + probe
        + "\n})(window);\n"
        + textwrap.dedent(
            """
            realSetTimeout(() => {
              process.stdout.write('RESULT ' + JSON.stringify(
                globalThis.__RESULT__ || { error: 'probe never completed' }
              ));
            }, 80);
            """
        )
    )


def _run_js(path: Path, script_body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.cjs"
        script.write_text(_build_harness(path, script_body), encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    blob = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")]
    if not blob:
        raise RuntimeError(f"node produced no RESULT:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(blob[-1][len("RESULT "):])


def _missing_urls(result: dict) -> list[str]:
    return [u for u in result.get("fetches", []) if "missing-tracks" in u]


class TestTheMbidPathPopulatesMissingTracks:
    """test_site's `applyAlbumMbid` must do what live's has always done."""

    def test_it_asks_the_server_for_this_release(self):
        result = _run_js(
            TEST_SITE_ALBUM,
            """
            win.__getJsonResult = (url) => url.includes('missing-tracks')
              ? { mb_total: 12, missing_count: 2, missing_tracks: [] }
              : { duplicates: [] };
            await win.applyAlbumMbid('release-abc');
            """,
        )
        assert "error" not in result, result.get("error")

        urls = _missing_urls(result)
        assert urls, (
            "applyAlbumMbid never requested /api/album/missing-tracks — this is "
            "the reported symptom: a lookup that leaves the tracklist with no "
            "missing rows at all"
        )
        assert "release_mbid=release-abc" in urls[0], (
            f"the request must be scoped to the picked release, got {urls[0]!r}"
        )

    def test_it_survives_a_failing_proposal_still_fetching(self):
        """The preview is optional; the findings are not."""
        result = _run_js(
            TEST_SITE_ALBUM,
            """
            win.albumMetadataReview = {
              applyProposal: async () => { throw new Error('preview exploded'); },
            };
            win.__getJsonResult = (url) => url.includes('missing-tracks')
              ? { mb_total: 12, missing_count: 1, missing_tracks: [] }
              : { duplicates: [] };
            await win.applyAlbumMbid('release-abc');
            """,
        )
        assert "error" not in result, result.get("error")
        assert _missing_urls(result), (
            "a broken metadata preview must not take the missing-track refresh "
            "down with it"
        )


class TestAnUnresolvedReleaseStillPopulatesMissingTracks:
    """`refreshAlbumTrackFindings` must run even with no release id.

    `applyAlbumMatch` passes `resolvedReleaseId`, which is `''` whenever the
    best-release probe fails — and the proposal in the same function falls back
    to `release.id`, so the bars appeared while the tracklist stayed blank.
    """

    def test_an_empty_id_still_asks_the_server(self):
        result = _run_js(
            TEST_SITE_ALBUM,
            """
            win.__getJsonResult = (url) => url.includes('missing-tracks')
              ? { mb_total: 8, missing_count: 1, missing_tracks: [] }
              : { duplicates: [] };
            await win.refreshAlbumTrackFindings('');
            """,
        )
        assert "error" not in result, result.get("error")

        urls = _missing_urls(result)
        assert urls, (
            "an empty release id skipped the fetch entirely; the server can "
            "compute this without an id (stored album MBID, then a name search)"
        )
        assert "release_mbid=" not in urls[0], (
            f"no id must mean no release_mbid param, got {urls[0]!r}"
        )

    def test_a_release_id_is_still_passed_through(self):
        result = _run_js(
            TEST_SITE_ALBUM,
            """
            win.__getJsonResult = (url) => url.includes('missing-tracks')
              ? { mb_total: 8, missing_count: 1, missing_tracks: [] }
              : { duplicates: [] };
            await win.refreshAlbumTrackFindings('release-xyz');
            """,
        )
        assert "error" not in result, result.get("error")
        urls = _missing_urls(result)
        assert urls and "release_mbid=release-xyz" in urls[0]

    def test_a_failed_request_is_reported_not_swallowed(self):
        """An empty catch is what hid the blank tracklist in the first place."""
        result = _run_js(
            TEST_SITE_ALBUM,
            """
            win.__getJsonResult = () => { throw new Error('network down'); };
            await win.refreshAlbumTrackFindings('');
            """,
        )
        assert "error" not in result, result.get("error")
        assert _missing_urls(result), "the request must still be attempted"
        assert any(
            "Could not refresh album tracklist findings" in w
            for w in result.get("warns", [])
        ), (
            "a failed refresh must be reported: an empty catch is what hid the "
            f"blank tracklist in the first place; warns={result.get('warns')!r}"
        )


class TestLiveAndTestSiteAgreeOnTheLookupPath:
    """Both trees must refresh findings from their MBID-apply path.

    A source-level parity guard: live is the reference behaviour, and the
    defect here was test_site quietly diverging from it.
    """

    def test_live_applies_the_mbid_and_refreshes_findings(self):
        src = LIVE_ALBUM.read_text(encoding="utf-8")
        assert "_refreshAlbumTrackFindings(mbid)" in src

    def test_test_site_applies_the_mbid_and_refreshes_findings(self):
        src = TEST_SITE_ALBUM.read_text(encoding="utf-8")
        start = src.index("function applyAlbumMbid(mbid) {")
        end = src.index("function linkedReleaseMbid()", start)
        body = src[start:end]
        assert "refreshAlbumTrackFindings(mbid)" in body, (
            "test_site's applyAlbumMbid must refresh the tracklist findings, "
            "exactly as live's does"
        )

    def test_the_findings_function_is_reachable_for_callers(self):
        """Exported so both entry points (and tests) can call it."""
        src = TEST_SITE_ALBUM.read_text(encoding="utf-8")
        assert "global.refreshAlbumTrackFindings = refreshAlbumTrackFindings;" in src
