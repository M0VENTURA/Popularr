"""Pre-flight gate: warn before starting a scan while one is already running.

Reported request
----------------
"When a scan is selected to run and a scan is already running, I want a popup to
appear before running the scan advising 'A scan is currently running. Would you
like to cancel this scan and start the new scan?' It should list the name of the
scan thats running that it's asking to cancel. Whether its a full scan, artist
scan, the current artist being scanned."

Why this needs behavioural tests
--------------------------------
The server already rejects a duplicate start with a 409, so a gate that merely
SHOWED a dialog would still leave the user stuck. The feature only works if the
order is exact:

    ask -> (if accepted) stop -> WAIT for the scan to actually go idle -> start

Stopping is asynchronous: ``/scan/stop-all`` sets a flag that the running scan
checks between artists/albums, so it keeps running briefly. Starting immediately
would trip the duplicate guard. A structural test ("the file mentions
/scan/stop-all") cannot distinguish that from a broken implementation, so
``tests/js/scan-preflight-probe.js`` runs the real module against stubs.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "scan-preflight-probe.js"

# (module, template tree) pairs — the rebuilt tree is served first when the
# cutover is on, so a change to one tree only is invisible to the other user.
TREES = [
    ("static/js/scan-preflight.js", "templates/pages"),
    ("test_site/static/js/services/scan-preflight.js", "test_site/templates/Pages"),
]

MODULES = [t[0] for t in TREES]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for the JS probe"
)


def _run_probe(scenario: str, module: str = MODULES[1]) -> dict:
    # encoding is pinned: the dialog names scans with an em dash, and letting
    # the platform default decode node's stdout mangles it into mojibake, which
    # then fails an assertion the shipped code actually satisfies.
    result = subprocess.run(
        ["node", str(PROBE), scenario, str(REPO_ROOT / module)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), shell=False,
    )
    line = (result.stdout or "").strip().splitlines()
    assert line, f"probe produced no output: {result.stderr}"
    return json.loads(line[-1])


# ---------------------------------------------------------------------------
# Behaviour of the gate itself
# ---------------------------------------------------------------------------

class TestGateBehaviour:
    def test_idle_start_is_not_interrupted(self):
        """No running scan -> no dialog, no stop call, no delay."""
        out = _run_probe("idle")
        assert out["ok"] is True
        assert out["proceed"] is True
        assert out["confirmCalls"] == 0
        assert out["posts"] == []

    def test_running_scan_prompts_before_starting(self):
        out = _run_probe("cancel")
        assert out["confirmCalls"] == 1
        # Declining must not start anything.
        assert out["proceed"] is False
        assert out["posts"] == []

    def test_dialog_uses_the_requested_wording(self):
        out = _run_probe("cancel")
        message = out["confirmMessage"]
        assert "A scan is currently running" in message
        assert "cancel this scan and start the new scan" in message

    def test_dialog_names_the_running_scan(self):
        """The whole point of the request: say WHICH scan is being cancelled."""
        out = _run_probe("cancel")
        assert len(out["confirmItems"]) == 1
        item = out["confirmItems"][0]
        # Type, the artist being scanned, and the progress.
        assert "Full Scan" in item
        assert "Madball" in item
        assert "40%" in item

    def test_accepting_stops_then_waits_then_proceeds(self):
        out = _run_probe("proceed")
        assert out["proceed"] is True
        assert out["posts"] == ["/scan/stop-all"]
        # 1 probe + at least one wait-poll: proving it did NOT start on the
        # strength of the stop request alone.
        assert out["progressGets"] >= 3

    def test_never_start_while_the_old_scan_is_still_running(self):
        """A scan that will not stop must ABORT, not race the duplicate guard."""
        out = _run_probe("wait-timeout")
        assert out["proceed"] is False
        assert out["alerts"], "the user must be told why nothing started"
        assert "still stopping" in out["alerts"][0]

    def test_status_probe_failure_fails_open(self):
        """A status hiccup must not block a legitimate scan start."""
        out = _run_probe("probe-failed")
        assert out["proceed"] is True
        assert out["confirmCalls"] == 0


@pytest.mark.parametrize("module", MODULES)
class TestGateBehaviourBothTrees:
    @pytest.mark.parametrize("scenario", ["idle", "cancel", "proceed", "wait-timeout"])
    def test_scenario_matches_in_every_tree(self, module, scenario):
        out = _run_probe(scenario, module)
        assert out["ok"] is True, out.get("error")
        assert out["proceed"] is (scenario in {"idle", "proceed"})


# ---------------------------------------------------------------------------
# Wiring — every scan-start path must be gated
# ---------------------------------------------------------------------------

def _code_only(source: str) -> str:
    """Strip comments so a guard tests code, not prose.

    These files deliberately EXPLAIN the endpoints they call; asserting on raw
    text would let a comment satisfy (or break) the check.
    """
    import re

    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    without_line = re.sub(r"//[^\n]*", " ", without_block)
    return re.sub(r"<!--.*?-->", " ", without_line, flags=re.DOTALL)


class TestModuleIsLoaded:
    """The gate is useless if the page never loads it."""

    @pytest.mark.parametrize("template,expected", [
        ("test_site/templates/Pages/dashboard.html", "js/services/scan-preflight.js"),
        ("templates/pages/dashboard.html", "js/scan-preflight.js"),
        ("test_site/templates/Pages/artist_detail_v2.html", "js/services/scan-preflight.js"),
        ("templates/pages/artist_detail_v2.html", "js/scan-preflight.js"),
        ("test_site/templates/Pages/artist_list.html", "js/services/scan-preflight.js"),
        ("templates/pages/artist_list.html", "js/scan-preflight.js"),
    ])
    def test_page_loads_the_gate(self, template, expected):
        text = (REPO_ROOT / template).read_text(encoding="utf-8")
        assert expected in text, f"{template} does not load {expected}"

    @pytest.mark.parametrize("module", MODULES)
    def test_module_exists_and_publishes_the_global(self, module):
        code = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))
        assert "ScanPreflight" in code
        assert "confirmIfRunning" in code

    @pytest.mark.parametrize("module", MODULES)
    def test_module_calls_the_stop_all_endpoint(self, module):
        code = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))
        assert "/scan/stop-all" in code

    @pytest.mark.parametrize("module", MODULES)
    def test_module_reads_the_progress_endpoint(self, module):
        code = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))
        assert "/api/scan-progress" in code


class TestArtistPageScansThroughTheSheet:
    """⚠️ THE ARTIST PAGE'S SCAN ENTRY POINT MOVED, so this test moved with it.

    It used to be a server-rendered `<form id="artistScanForm"
    data-scan-preflight>` — a scan-type <select> plus Run and Force in the hero.
    That toolbar was replaced by the action row (Play / Scan / ⋮), whose Scan
    button opens the SHARED scan sheet, so there is no per-page form to opt in
    and no `artistScanForm` to find.

    **The property is unchanged: a scan started from the artist page still passes
    the pre-flight gate before it starts.** Only the mechanism moved — from a
    declarative attribute on a form to the sheet's own `run()`, which calls
    `confirmIfRunning` before either dispatch (the JSON path and the targeted
    FORM path). Asserting the old attribute would now pass on prose alone, so it
    is asserted as "the button reaches the sheet, and the sheet gates".
    """

    ARTIST_TEMPLATES = [
        "test_site/templates/Pages/artist_detail_v2.html",
        "templates/pages/artist_detail_v2.html",
    ]
    ARTIST_MODULES = [
        "test_site/static/js/pages/artist-detail-extras.js",
        "static/js/artist-page.js",
    ]

    @pytest.mark.parametrize("template", ARTIST_TEMPLATES)
    def test_the_hero_scan_button_opens_the_sheet(self, template):
        text = (REPO_ROOT / template).read_text(encoding="utf-8")

        assert "openArtistScanSheet()" in text, (
            f"{template} has no Scan control, so the artist page cannot start a scan"
        )
        assert "_scan_modal.html" in text, (
            f"{template} does not include the scan sheet the button opens"
        )

    @pytest.mark.parametrize("template", ARTIST_TEMPLATES)
    def test_the_old_inline_toolbar_is_gone(self, template):
        """CONTROL: two scan UIs on one page is the problem the sheet removes."""
        text = (REPO_ROOT / template).read_text(encoding="utf-8")

        assert "artistScanForm" not in text
        assert "artistForceScan" not in text, (
            "the old Force checkbox is back — its force flag belongs in the "
            "sheet's Advanced options now"
        )

    @pytest.mark.parametrize("module", ARTIST_MODULES)
    def test_the_module_delegates_to_the_sheet(self, module):
        code = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))

        assert "ScanModal.open" in code, f"{module} does not open the sheet"
        assert "scope: 'artist'" in code, (
            f"{module} opens the sheet without the artist scope, so Run would "
            "scan the whole library"
        )

    @pytest.mark.parametrize("sheet_js", [
        "test_site/static/js/ui/scan-modal.js",
        "static/js/ui/scan-modal.js",
    ])
    def test_the_sheet_gates_both_dispatch_paths(self, sheet_js):
        """⭐ The inherited guard. The sheet must gate the FORM path as well as
        the JSON one — the targeted branch returns early, so a gate placed only
        in the busy wrapper would never run for an artist scan.

        ⚠️ BOTH COPIES. The sheet is served by both trees, and this check used to
        read only the rebuilt one — so a change to the LIVE copy's dispatch order
        went unnoticed. Verified by mutation, which escaped before this was
        parametrised.
        """
        code = _code_only((REPO_ROOT / sheet_js).read_text(encoding="utf-8"))

        submit = code[code.index("function submitTargetedScan"):]
        submit = submit[: submit.index("function run(")]
        assert "confirmIfRunning" not in submit, (
            "the gate belongs in run(), before the branch — not inside the "
            "helper that builds the form"
        )

        run = code[code.index("async function run("):]
        run = run[: run.index("withBusy")]
        assert "confirmIfRunning" in run, (
            "the targeted dispatch returns before the busy wrapper, so the gate "
            "must be in the branch itself"
        )
        assert run.index("confirmIfRunning") < run.index("submitTargetedScan"), (
            "the gate must run BEFORE the form is submitted"
        )

    @pytest.mark.parametrize("module", MODULES)
    def test_module_auto_attaches_declared_forms(self, module):
        code = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))
        assert "data-scan-preflight" in code
        assert "attachDeclaredForms" in code

    @pytest.mark.parametrize("module", MODULES)
    def test_form_guard_does_not_use_the_dead_cleared_flag(self, module):
        """`form.submit()` fires no submit event, so a "cleared" flag is never
        consumed — it stays set and silently SKIPS the gate on the next click.
        A re-entrancy WeakSet is the correct guard."""
        code = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))
        assert "scanPreflightCleared" not in code
        assert "_inFlight" in code


class TestJSEntryPointsCallTheGate:
    """Every scan START path must pass the gate before its POST.

    ⚠️ THE REBUILT TREE'S START PATHS MOVED, SO THIS TEST MOVED WITH THEM.
    `test_site/static/js/pages/dashboard.js` used to define four start helpers
    and this class asserted each one was gated. They now live in the shared scan
    sheet (`test_site/static/js/ui/scan-modal.js`), so the dashboard assertions
    are kept for the LIVE tree only — and inverted for the rebuilt one, because
    a starter reappearing there is the regression to catch, not the gate.
    """

    def test_live_dashboard_start_functions_are_gated(self):
        """The LIVE tree is unchanged: its dashboard still owns the starters."""
        js = "static/js/dashboard.js"
        code = _code_only((REPO_ROOT / js).read_text(encoding="utf-8"))
        for fn in ("runDashboardPopularityScan", "startNavidromeImport",
                   "startNavidromeServerScan", "startEssentiaScan"):
            assert fn in code, f"{fn} missing from {js}"
        # One call per start path (popularity is gated via scanGate()).
        assert code.count("confirmIfRunning") + code.count("scanGate(") >= 4

    def test_rebuilt_dashboard_no_longer_owns_any_starter(self):
        """The rebuilt dashboard delegates to the sheet and keeps nothing.

        Asserted as an ABSENCE. A leftover starter that nothing calls cannot be
        caught behaviourally, and it reads as if the dashboard still owned the
        control — which is the whole reason the block was removable in the first
        place (every `getElementById` in it resolved to null, so it could never
        throw and never announce itself).
        """
        code = _code_only(
            (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(encoding="utf-8")
        )
        for fn in ("runDashboardPopularityScan", "startNavidromeImport",
                   "startNavidromeServerScan", "startEssentiaScan",
                   "startPopularityScan", "pollPopularityStatus",
                   "pollNavidromeStatus", "pollEssentiaStatus",
                   "checkNavidromeConnectivity"):
            assert fn not in code, (
                f"{fn} is back in the rebuilt dashboard — scan starts belong to "
                f"test_site/static/js/ui/scan-modal.js"
            )

    def test_rebuilt_dashboard_template_owns_the_shared_sheet(self):
        """Removing the starters is only safe if the sheet is actually there.

        Checked on the TEMPLATE, not on dashboard.js: the script never names
        `scan-modal.js`, it just never posts a scan endpoint. Asserting the
        include AND the script load is what proves the replacement is wired —
        and each is asserted separately, because an include with no script (or
        the reverse) leaves the sheet's controls inert.
        """
        html = (REPO_ROOT / "test_site/templates/Pages/dashboard.html").read_text(encoding="utf-8")
        assert "components/_scan_modal.html" in html, "the scan sheet is not included"
        assert "js/ui/scan-modal.js" in html, "the scan sheet's script is not loaded"

    def test_rebuilt_scan_sheet_gates_every_start(self):
        """Each start path in the sheet reaches the gate before its POST.

        Scoped to the ENCLOSING FUNCTION rather than a character window: the
        sheet has two start paths in one file, so a single file-wide
        "a gate appears somewhere before this endpoint" check would pass with
        one of them unguarded.
        """
        js = "test_site/static/js/ui/scan-modal.js"
        code = _code_only((REPO_ROOT / js).read_text(encoding="utf-8"))

        def enclosing_function_start(idx: int) -> int:
            fn = code.rfind("function ", 0, idx)
            assert fn != -1, "endpoint call is not inside a function"
            return fn

        for endpoint in ("/api/popularity/run", "/api/navidrome/import",
                         "/api/navidrome/scan/start", "/api/essentia/run"):
            idx = code.index(endpoint)
            start = enclosing_function_start(idx)
            before = code[start:idx]
            assert "confirmIfRunning" in before, (
                f"{endpoint} is reachable without passing the gate "
                f"(function starting at {start})"
            )

    def test_test_site_dashboard_gates_every_start(self):
        """Every start helper must reach the gate before its POST."""
        code = _code_only(
            (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(encoding="utf-8")
        )
        for endpoint in ("/api/popularity/run", "/api/navidrome/import",
                         "/api/navidrome/scan/start", "/api/essentia/run"):
            assert endpoint not in code, (
                f"{endpoint} is posted from the rebuilt dashboard — the sheet owns it"
            )

    @pytest.mark.parametrize("js", [
        "test_site/static/js/pages/artists.js",
        "static/js/artist_list.js",
    ])
    def test_letter_scan_is_gated(self, js):
        code = _code_only((REPO_ROOT / js).read_text(encoding="utf-8"))
        assert "confirmIfRunning" in code, f"{js} does not gate the letter scan"
        # And it must gate BEFORE posting.
        assert code.index("confirmIfRunning") < code.index("/api/scan/from-artist")

    @pytest.mark.parametrize("js", [
        "test_site/static/js/pages/artist-detail-extras.js",
        "static/js/artist-page.js",
    ])
    def test_metadata_refresh_delegates_to_the_sheet(self, js):
        """⚠️ This replaces the direct-call assertion, because the PATH changed.

        `forceArtistMetadataRefresh` used to find `#artistScanForm`, set the
        select to metadata and `form.submit()` — bypassing submit listeners, so
        it had to call the gate itself. That form is gone; the function now opens
        the sheet with Metadata preselected, and the SHEET calls the gate. So the
        guard is still made, one layer down, once rather than twice.
        """
        code = _code_only((REPO_ROOT / js).read_text(encoding="utf-8"))

        assert "preselect: 'metadata'" in code, (
            f"{js} does not open the sheet in metadata mode — the refresh would "
            "ask the user to choose a type they already chose"
        )
        assert "force: true" in code, (
            f"{js}: a Force Metadata Refresh must arrive with Force ticked"
        )
        assert "artistScanForm" not in code, (
            f"{js} still targets the removed form"
        )
        assert "confirmIfRunning" not in code, (
            f"{js} gates as well as the sheet, so the user would be prompted twice"
        )


class TestLabels:
    """The dialog must name a scan in words, not as a raw scan_type."""

    def test_all_progress_scan_types_have_labels(self):
        import importlib

        module = _run_probe.__globals__  # noqa: F841  (keep import local/simple)
        # Read the labels the module ships and compare with the server's list.
        src = (REPO_ROOT / MODULES[1]).read_text(encoding="utf-8")
        server = (
            REPO_ROOT / "services/scanning/pipelines/progress_service.py"
        ).read_text(encoding="utf-8")

        import re

        server_types = re.findall(r'^\s{4}"([a-z_]+)",\s*$', server, re.MULTILINE)
        server_types = [t for t in server_types if t.endswith(("scan", "import"))]
        assert server_types, "could not parse SCAN_TYPES"

        missing = [t for t in server_types if f"{t}:" not in src]
        assert not missing, f"scan types with no label in the gate: {missing}"
