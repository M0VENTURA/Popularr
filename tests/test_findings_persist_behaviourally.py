"""Behavioural tests for the persistence wiring.

⭐ WHY THIS FILE EXISTS SEPARATELY. The static tests in
`test_findings_persist_until_saved_or_discarded.py` could not catch six
mutations:

  * removing an `import` left the CALL intact (a NameError, not a text change);
  * `problems.append("...missing-track list...")` -> `pass` still left the OTHER
    append in place;
  * `if (kind === 'warning')` -> `if (false)` and `if (data.stash_warning)` ->
    `if (false)` leave every symbol present and simply UNREACHABLE.

⭐ A SOURCE-TEXT ASSERTION CANNOT DETECT A NEUTERED BRANCH. So these CALL the
real code: `_persist_comparison_findings` in Python, and the two JS paths in
Node with stubs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
REVIEW_JS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Python: the compare persistence helper actually calls both writers
# ---------------------------------------------------------------------------

class TestPersistComparisonFindingsCallsBothWriters:
    """Drives the REAL helper with stubbed writers and records what it calls."""

    @staticmethod
    def _run(monkeypatch, *, stash_fails=False, missing_fails=False):
        """Import and call `_persist_comparison_findings`, recording the calls."""
        sys.path.insert(0, str(REPO_ROOT))
        from routes.api_v1 import albums as albums_api
        import services.metadata.album_missing_service as missing_svc
        import services.metadata.metadata_proposal_service as proposal_svc
        import services.metadata.pending_update_service as pending_svc

        calls: list[tuple[str, object]] = []

        def _fake_propose(artist, album, release_mbid, comparison_result=None):
            calls.append(("propose", release_mbid))
            calls.append(("propose_comparison_passed", comparison_result is not None))
            return {"success": True, "album_changes": [], "track_changes": []}

        def _fake_stash(artist, album, proposal):
            calls.append(("stash", artist))
            if stash_fails:
                raise RuntimeError("db down")
            return {"stashed": 1, "cleared": 0, "reason": ""}

        def _fake_missing(artist, album, comparison):
            calls.append(("missing", album))
            if missing_fails:
                raise RuntimeError("db down")
            return 2

        monkeypatch.setattr(proposal_svc, "propose_album_metadata", _fake_propose)
        monkeypatch.setattr(pending_svc, "stash_album_recommendations", _fake_stash)
        monkeypatch.setattr(
            missing_svc, "persist_missing_from_comparison", _fake_missing
        )
        # The helper imports lazily INSIDE its body, so patching the module
        # attributes above is what the imports resolve to.

        warning = albums_api._persist_comparison_findings(
            "Band", "Album", "rel-1", {"success": True, "comparison": []}
        )
        return calls, warning

    def test_both_writers_are_called(self, monkeypatch):
        calls, warning = self._run(monkeypatch)
        names = [c[0] for c in calls]
        assert "stash" in names, (
            "the compare helper must CALL the recommendation stash, not merely "
            "name it — removing the import used to leave the call intact and "
            "the tests passing"
        )
        assert "missing" in names, "and it must CALL the missing-track persistence"
        assert warning == "", "a clean run reports no warning"

    def test_the_comparison_is_handed_to_the_proposal(self, monkeypatch):
        calls, _ = self._run(monkeypatch)
        assert ("propose_comparison_passed", True) in calls, (
            "the already-computed comparison must be reused, or the proposal "
            "pays a second MusicBrainz comparison"
        )

    def test_a_failed_missing_write_is_reported(self, monkeypatch):
        """⭐ Catches `problems.append(...)` -> `pass` for the MISSING writer.

        The old static check passed because the OTHER append (for the stash)
        was still present.
        """
        _, warning = self._run(monkeypatch, missing_fails=True)
        assert warning, "a failed missing-track write must be reported"
        assert "missing" in warning.lower(), (
            "the warning must name the missing-track list specifically, or the "
            "user cannot tell which half failed"
        )

    def test_a_failed_stash_is_reported(self, monkeypatch):
        _, warning = self._run(monkeypatch, stash_fails=True)
        assert warning, "a failed recommendation stash must be reported"
        assert "recommendation" in warning.lower()

    def test_both_failures_are_reported_together(self, monkeypatch):
        _, warning = self._run(monkeypatch, stash_fails=True, missing_fails=True)
        assert "recommendation" in warning.lower()
        assert "missing" in warning.lower()


# ---------------------------------------------------------------------------
# 2. JS: the warning must actually reach the user
# ---------------------------------------------------------------------------

def _build_harness(path: Path, script_body: str) -> str:
    """Wrap a page module + a probe that runs INSIDE its IIFE."""
    source = read(path)
    assert source.rstrip().endswith("})(window);")
    module = source.rstrip()[: -len("})(window);")]
    prelude = textwrap.dedent(
        """
        const seen = [];
        const win = {};
        win.toast = {
          success: (m) => seen.push(['success', String(m)]),
          error: (m) => seen.push(['error', String(m)]),
          warning: (m) => seen.push(['warning', String(m)]),
          info: (m) => seen.push(['info', String(m)]),
        };
        win.alert = (m) => seen.push(['alert', String(m)]);
        win.document = {
          getElementById: () => null,
          querySelector: () => null,
          querySelectorAll: () => [],
          addEventListener: () => {},
          createElement: () => ({ style: {}, classList: { add() {} }, dataset: {} }),
        };
        win.location = { reload: () => {}, href: '' };
        win.ui = { confirm: async () => true };
        win.api = { postJson: async () => ({}), getJson: async () => ({}) };
        win.setTimeout = (fn) => { try { fn(); } catch (e) {} return 0; };
        win.busyPopup = null;
        globalThis.window = win;
        globalThis.document = win.document;
        globalThis.location = win.location;
        globalThis.alert = win.alert;
        const realSetTimeout = globalThis.setTimeout;
        globalThis.setTimeout = win.setTimeout;
        """
    )
    probe = textwrap.dedent(
        """
        (async () => {
          try {
            __BODY__
            globalThis.__RESULT__ = { seen };
          } catch (e) {
            globalThis.__RESULT__ = { error: String((e && e.stack) || e), seen };
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
              console.log('RESULT ' + JSON.stringify(
                globalThis.__RESULT__ || { error: 'probe never completed' }
              ));
            }, 60);
            """
        )
    )


def _run_js(path: Path, script_body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.cjs"
        script.write_text(_build_harness(path, script_body), encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)], capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    blob = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")]
    if not blob:
        raise RuntimeError(f"node produced no RESULT:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(blob[-1][len("RESULT "):])


class TestTheStashWarningReachesTheUser:
    def test_a_warning_kind_routes_to_toast_warning(self):
        """⭐ Catches `if (kind === 'warning')` -> `if (false)`.

        The symbol `toast.warning` is still present in that mutation, so only
        CALLING notify() can tell that the branch became unreachable.
        """
        result = _run_js(
            REVIEW_JS,
            """
            if (typeof notify !== 'function') throw new Error('notify unreachable');
            notify('could not save', 'warning');
            """,
        )
        assert result.get("error") is None, result.get("error")
        kinds = [k for k, _ in result["seen"]]
        assert "warning" in kinds, (
            f"a warning must reach toast.warning, not success; saw {result['seen']}"
        )
        assert "success" not in kinds, (
            "reporting a failed stash as SUCCESS is how a review silently "
            "disappears"
        )

    def test_report_stash_warning_actually_fires(self):
        result = _run_js(
            REVIEW_JS,
            """
            if (typeof reportStashWarning !== 'function') {
              throw new Error('reportStashWarning unreachable');
            }
            reportStashWarning({ stash_warning: 'could not be stored' });
            """,
        )
        assert result.get("error") is None, result.get("error")
        assert result["seen"], (
            "a present stash_warning must produce a visible message"
        )
        assert result["seen"][0][0] == "warning"

    def test_no_warning_means_no_message(self):
        result = _run_js(
            REVIEW_JS,
            """
            reportStashWarning({ success: true });
            """,
        )
        assert result["seen"] == [], "a clean response must stay quiet"


class TestTheCompareFlowSurfacesTheWarning:
    """⭐ Catches `if (data.stash_warning) { ... }` -> `if (false) { ... }`.

    Every symbol survives that mutation, so only driving the real handler with a
    response carrying `stash_warning` can tell the branch became unreachable.
    `compareWithMusicBrainz` reads the release MBID from the Edit Album form, so
    the probe stubs those inputs.
    """

    @staticmethod
    def _body(with_warning: bool) -> str:
        response = (
            '{"success":true,"stash_warning":"could not be stored",'
            '"comparison":[],"total_tracks":0}'
            if with_warning
            else '{"success":true,"comparison":[],"total_tracks":0}'
        )
        return (
            "const els = {\n"
            "  album_release_group_mbid: { value: 'rel-1' },\n"
            "  album_mbid: { value: '' },\n"
            "};\n"
            "win.document.getElementById = (id) => els[id] || null;\n"
            f"win.api.postJson = async () => ({response});\n"
            f"win.api.getJson = async () => ({response});\n"
            "if (typeof compareWithMusicBrainz !== 'function') {\n"
            "  throw new Error('compareWithMusicBrainz unreachable');\n"
            "}\n"
            "await compareWithMusicBrainz();\n"
        )

    def test_a_stash_warning_is_shown(self):
        result = _run_js(ALBUM_JS, self._body(True))
        assert result.get("error") is None, result.get("error")
        kinds = [k for k, _ in result["seen"]]
        assert kinds, (
            "a stash_warning in the comparison response must produce a visible "
            "message; ignoring it is how the diff silently disappears on reload"
        )
        assert any("could not be stored" in m for _, m in result["seen"]), (
            f"the message must carry the server's reason; saw {result['seen']}"
        )

    def test_a_clean_response_shows_nothing(self):
        result = _run_js(ALBUM_JS, self._body(False))
        assert result.get("error") is None, result.get("error")
        assert result["seen"] == [], "a clean comparison must not warn"


# ---------------------------------------------------------------------------
# 3. The propose endpoint really stashes (driven through the route)
# ---------------------------------------------------------------------------

class TestTheProposeRouteStashes:
    """⭐ Catches removing/neutering the stash in the propose handler.

    A static check could not: the identifier still appeared in the import line
    and in the explanatory comment, so deleting the call left it passing. This
    POSTs to the real route with the proposal engine stubbed and records whether
    the stash writer was invoked.
    """

    @staticmethod
    def _stub(monkeypatch, *, stash_fails=False):
        import services.metadata.metadata_proposal_service as proposal_svc
        import services.metadata.pending_update_service as pending_svc

        calls: list[str] = []

        monkeypatch.setattr(
            proposal_svc,
            "propose_album_metadata",
            lambda artist, album, release_mbid, comparison_result=None: {
                "success": True,
                "release_mbid": release_mbid,
                "album_changes": [{"field": "album_title", "label": "Album Title",
                                   "current": "A", "proposed": "B"}],
                "track_changes": [],
                "counts": {"album_changes": 1, "tracks_changed": 0, "track_changes": 0},
            },
        )

        def _stash(artist, album, proposal):
            calls.append("stash")
            if stash_fails:
                raise RuntimeError("db down")
            return {"stashed": 1, "cleared": 0, "reason": ""}

        monkeypatch.setattr(pending_svc, "stash_album_recommendations", _stash)
        return calls

    async def test_the_route_calls_the_stash_writer(self, app, client, monkeypatch):
        calls = self._stub(monkeypatch)
        resp = await client.post(
            "/api/album/musicbrainz/propose",
            json={"artist": "Band", "album": "Album", "release_mbid": "rel-1"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body.get("success") is True
        assert calls == ["stash"], (
            "the Lookup MBID review must be stashed so it survives a reload — "
            "no stash call means the review is lost on the next page view"
        )

    async def test_a_failed_stash_warns_but_still_returns_the_proposal(
        self, app, client, monkeypatch
    ):
        """A failed WRITE must not fail the review the user is looking at, but
        it must be reported."""
        self._stub(monkeypatch, stash_fails=True)
        resp = await client.post(
            "/api/album/musicbrainz/propose",
            json={"artist": "Band", "album": "Album", "release_mbid": "rel-1"},
        )
        assert resp.status_code == 200, (
            "the proposal is still valid when the STASH fails"
        )
        body = await resp.get_json()
        assert body.get("success") is True
        assert "album_changes" in body, "the review content must still be returned"
        assert body.get("stash_warning"), (
            "and the failure must be surfaced — silently losing the review on "
            "reload is the defect being fixed"
        )
