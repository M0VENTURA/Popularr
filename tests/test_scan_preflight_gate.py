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


class TestArtistFormIsGuarded:
    """The artist form is server-rendered, so it opts in declaratively."""

    @pytest.mark.parametrize("template", [
        "test_site/templates/Pages/artist_detail_v2.html",
        "templates/pages/artist_detail_v2.html",
    ])
    def test_scan_form_carries_the_attribute(self, template):
        text = (REPO_ROOT / template).read_text(encoding="utf-8")
        # The attribute must be ON the scan form, not merely present somewhere.
        start = text.index("artistScanForm")
        window = text[start:start + 400]
        assert "data-scan-preflight" in window, f"attribute missing on the form in {template}"

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
    @pytest.mark.parametrize("js", [
        "test_site/static/js/pages/dashboard.js",
        "static/js/dashboard.js",
    ])
    def test_dashboard_start_functions_are_gated(self, js):
        code = _code_only((REPO_ROOT / js).read_text(encoding="utf-8"))
        for fn in ("runDashboardPopularityScan", "startNavidromeImport",
                   "startNavidromeServerScan", "startEssentiaScan"):
            assert fn in code, f"{fn} missing from {js}"
        # One call per start path (popularity is gated via scanGate()).
        assert code.count("confirmIfRunning") + code.count("scanGate(") >= 4

    def test_test_site_dashboard_gates_every_start(self):
        """Each start helper must reach the gate before its POST."""
        code = _code_only(
            (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(encoding="utf-8")
        )
        for endpoint in ("/api/popularity/run", "/api/navidrome/import",
                         "/api/navidrome/scan/start", "/api/essentia/run"):
            idx = code.index(endpoint)
            # Look back for the gate within the same function.
            before = code[max(0, idx - 700):idx]
            assert ("scanGate(" in before) or ("confirmIfRunning" in before), (
                f"{endpoint} is reachable without passing the gate"
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
    def test_metadata_refresh_is_gated(self, js):
        """`form.submit()` bypasses listeners, so this path needs its own call."""
        code = _code_only((REPO_ROOT / js).read_text(encoding="utf-8"))
        assert "confirmIfRunning" in code, f"{js} does not gate the metadata refresh"


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
