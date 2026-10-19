"""The dashboard's two alert surfaces: abandoned artists, and the queue processor.

REPORTED
--------
> This is showing on the dashboard from an earlier scan, but there is no way to
> dismiss the message.
>
> Artists skipped / abandoned — investigate
>   Visions Of Atlantis  abandoned  exceeded 2700.0s budget
>
> ...when the queue processor fails, it shows it with the option to restart in
> both the scanner queue and the config page, but if it's failed, it should also
> show on the dashboard and a restart on failure should happen automatically.

TWO SEPARATE DEFECTS, ONE SURFACE
---------------------------------
1. **The banner had no way out.** ``abandonedBanner()`` was rebuilt from
   ``/api/scan-progress`` on every 5-second poll, so removing the element
   brought it straight back. A scan whose progress row is left ``is_running``
   after a crash can keep saying so until the next restart
   (``reset_stale_scan_states`` only runs at BOOT), which is exactly the
   "showing from an earlier scan" report.

2. **The dashboard knew nothing about the queue processor.** The Downloads page
   and the Config page both draw a Download Queue Processor card with a Restart
   button; the dashboard — the page the operator leaves open — had no view of it
   at all, so a stopped processor could go unnoticed indefinitely.

The dismissal is keyed on a FINGERPRINT OF THE CONTENT (scan type + the artists
and reasons), not on the scan type and not as a boolean: "I have seen this" must
not silence the NEXT abandonment. ``tests/js/dashboard-alerts-probe.js`` drives
the shipped renderers to prove both directions, and this module adds the wiring
assertions a probe cannot make — the container the renderer writes into, and the
config controls behind the auto-restart.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "dashboard-alerts-probe.js"

LIVE_JS = REPO_ROOT / "static" / "js" / "dashboard.js"
LIVE_HTML = REPO_ROOT / "templates" / "pages" / "dashboard.html"
LIVE_CONFIG_JS = REPO_ROOT / "static" / "js" / "config.js"
LIVE_CONFIG_HTML = REPO_ROOT / "templates" / "pages" / "config.html"

REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "dashboard.js"
REBUILT_HTML = REPO_ROOT / "test_site" / "templates" / "Pages" / "dashboard.html"
REBUILT_CONFIG_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js"
REBUILT_CONFIG_HTML = REPO_ROOT / "test_site" / "templates" / "Pages" / "config.html"

DASHBOARDS = (LIVE_JS, REBUILT_JS)
DASHBOARD_TEMPLATES = (LIVE_HTML, REBUILT_HTML)

#: The one storage key both trees must agree on. A second literal is how one
#: page dismisses an alert and the other keeps showing it.
DISMISSED_KEY = "popularr.dismissedDashboardAlerts"


def _node_available() -> bool:
    try:
        return subprocess.run(["node", "--version"], capture_output=True).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run_probe(source: pathlib.Path) -> dict:
    result = subprocess.run(
        ["node", str(PROBE), str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"probe crashed on {source.name}: rc={result.returncode} stderr={result.stderr}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in payload, f"probe could not drive {source}: {payload['error']}"
    return payload


@pytest.fixture(scope="module")
def probe_results() -> dict[pathlib.Path, dict]:
    if not _node_available():
        pytest.skip("node is required")
    return {source: _run_probe(source) for source in DASHBOARDS}


# ===========================================================================
# 1. The abandoned-artists banner is dismissible
# ===========================================================================
class TestTheAbandonedBannerCanBeDismissed:
    def test_a_fresh_abandonment_is_shown(self, probe_results):
        for source, result in probe_results.items():
            assert result["banner_shown"], f"{source.name}: the banner is not rendered"
            assert result["banner_is_alert"], f"{source.name}: not an alert element"
            assert result["banner_has_alert_id"], f"{source.name}: no data-alert-id"

    def test_it_offers_a_dismiss_control(self, probe_results):
        """The reported symptom: a message with no way out."""
        for source, result in probe_results.items():
            assert result["banner_has_dismiss_button"], (
                f"{source.name}: the banner has no dismiss button — this is the "
                "reported 'no way to dismiss the message'"
            )

    def test_dismissing_hides_it_and_it_stays_hidden(self, probe_results):
        """The poll re-renders every 5s, so a DOM-only removal is undone."""
        for source, result in probe_results.items():
            assert result["dismissed_state_recorded"], f"{source.name}: dismissal not recorded"
            assert result["banner_hidden_after_dismiss"], (
                f"{source.name}: the banner came back immediately after dismissal"
            )

    def test_a_different_abandonment_is_not_swallowed(self, probe_results):
        """CONTROL on the key: 'seen this' must not become 'never tell me again'."""
        for source, result in probe_results.items():
            assert result["other_fingerprint_differs"], (
                f"{source.name}: the fingerprint does not include the artist/reason, "
                "so one dismissal would silence every future abandonment"
            )
            assert result["other_abandonment_still_shown"], (
                f"{source.name}: dismissing one abandonment hid a later, different one"
            )

    def test_dismissing_removes_the_element_without_waiting_for_the_poll(
        self, probe_results
    ):
        for source, result in probe_results.items():
            assert result["dismiss_removes_element"], (
                f"{source.name}: dismissed banner left on screen until the next poll"
            )

    def test_a_quoted_artist_name_cannot_break_out_of_the_attribute(
        self, probe_results
    ):
        """`escapeHtml`/`esc` do not escape quotes; only `escapeAttr` does.

        The id is built from artist names, so `Al "Test" Band` would otherwise
        close the attribute and inject markup.
        """
        for source, result in probe_results.items():
            assert result["quoted_attr_is_wellformed"], (
                f"{source.name}: data-alert-id was truncated at a quote in the artist name"
            )
            assert result["quoted_attr_is_escaped"], (
                f"{source.name}: the quote reached the attribute unescaped"
            )


# ===========================================================================
# 2. The queue processor is visible on the dashboard
# ===========================================================================
class TestTheQueueProcessorIsVisibleOnTheDashboard:
    def test_nothing_is_drawn_while_the_processor_is_healthy(self, probe_results):
        """A permanent green banner on every load is noise, not information."""
        for source, result in probe_results.items():
            assert result["queue_running_hidden"], f"{source.name}: alert shown while running"
            assert result["queue_restarting_hidden"], (
                f"{source.name}: alert shown while restarting — a cycle in flight is "
                "not a failure"
            )

    def test_a_stopped_processor_is_shown_with_a_restart(self, probe_results):
        """The report: it shows on the queue/config pages but not the dashboard."""
        for source, result in probe_results.items():
            assert result["queue_stopped_shown"], f"{source.name}: stopped processor not shown"
            assert result["queue_stopped_says_not_running"], (
                f"{source.name}: the alert does not say the processor is not running"
            )
            assert result["queue_stopped_has_restart"], (
                f"{source.name}: the alert offers no way to restart the processor"
            )

    def test_it_says_what_auto_restart_is_doing(self, probe_results):
        for source, result in probe_results.items():
            assert result["queue_stopped_reports_auto_restart"], (
                f"{source.name}: the alert does not report the automatic restarts"
            )
            assert result["queue_auto_off_explained"], (
                f"{source.name}: with auto-restart off the alert does not say so, so it "
                "implies help is coming"
            )

    @pytest.mark.parametrize("template", DASHBOARD_TEMPLATES, ids=lambda p: p.name)
    def test_each_dashboard_renders_the_alert_into_a_container(self, template: pathlib.Path):
        """⚠️ The renderer bails out when the host is missing.

        A container-less page would make a stopped processor invisible again with
        nothing logged anywhere, which is the bug being fixed.
        """
        src = template.read_text(encoding="utf-8")
        assert 'id="queueProcessorAlert"' in src, (
            f"{template}: dashboard.js writes #queueProcessorAlert, but the page has no "
            "such element — the alert can never appear"
        )

    @pytest.mark.parametrize("source", DASHBOARDS, ids=lambda p: p.name)
    def test_the_poll_is_wired_into_the_dashboard_refresh(self, source: pathlib.Path):
        """Unwired, the renderer would only ever run if something else called it."""
        src = source.read_text(encoding="utf-8")
        assert "updateQueueProcessorAlert()" in src, (
            f"{source.name}: updateQueueProcessorAlert is never called"
        )
        assert "updateAll" in src, f"{source.name}: no refresh entry point to hook into"


# ===========================================================================
# 3. Both trees agree
# ===========================================================================
class TestBothTreesUseTheSameDismissalKey:
    def test_one_storage_key(self):
        """Two literals would mean one page dismissing an alert the other keeps."""
        seen = set()
        for source in DASHBOARDS:
            src = source.read_text(encoding="utf-8")
            match = re.search(r"DISMISSED_ALERTS_KEY\s*=\s*'([^']+)'", src) or re.search(
                r'DISMISSED_ALERTS_KEY\s*=\s*"([^"]+)"', src
            )
            assert match, f"{source.name}: no dismissed-alerts storage key"
            seen.add(match.group(1))
        assert seen == {DISMISSED_KEY}, (
            f"the two dashboards disagree about the storage key: {seen}"
        )

    def test_the_two_dashboards_agree_behaviourally(self, probe_results):
        """⚠️ THE POINT OF RUNNING ONE PROBE TWICE.

        Every key the probe emits must match between the trees, so a fix applied
        to one of them fails here. `error` is excluded because `_run_probe`
        already fails on it.
        """
        live, rebuilt = (probe_results[LIVE_JS], probe_results[REBUILT_JS])
        assert set(live) == set(rebuilt), "the probe emitted different keys per tree"
        differences = {k: (live[k], rebuilt[k]) for k in live if live[k] != rebuilt[k]}
        assert not differences, f"the rebuilt dashboard drifted from the live one: {differences}"


# ===========================================================================
# 4. The auto-restart is configurable from the Config page, in both trees
# ===========================================================================
class TestAutoRestartIsConfigurable:
    @pytest.mark.parametrize(
        "template", (LIVE_CONFIG_HTML, REBUILT_CONFIG_HTML), ids=lambda p: p.parent.name
    )
    def test_the_config_page_exposes_the_controls(self, template: pathlib.Path):
        """The Config page is the source of truth: no option without a control."""
        src = template.read_text(encoding="utf-8")
        assert 'id="queue_auto_restart_enabled"' in src, (
            f"{template}: no on/off control for the queue auto-restart"
        )
        assert 'id="queue_auto_restart_cooldown_seconds"' in src, (
            f"{template}: no cooldown control for the queue auto-restart"
        )
        assert "'auto_restart'" in src or '"auto_restart"' in src, (
            f"{template}: the controls do not read queue.auto_restart from the config"
        )

    @pytest.mark.parametrize(
        "script", (LIVE_CONFIG_JS, REBUILT_CONFIG_JS), ids=lambda p: p.parent.name
    )
    def test_the_config_page_saves_the_controls(self, script: pathlib.Path):
        """A control that is rendered but not collected silently reverts."""
        src = script.read_text(encoding="utf-8")
        assert "queue_auto_restart_enabled" in src, (
            f"{script}: the auto-restart switch is never collected on save"
        )
        assert "queue_auto_restart_cooldown_seconds" in src, (
            f"{script}: the auto-restart cooldown is never collected on save"
        )
        assert "auto_restart" in src, (
            f"{script}: nothing writes queue.auto_restart into the saved config"
        )

    @pytest.mark.parametrize(
        "script", (LIVE_CONFIG_JS, REBUILT_CONFIG_JS), ids=lambda p: p.parent.name
    )
    def test_the_cooldown_is_floored_on_the_client_too(self, script: pathlib.Path):
        """0 would mean 'restart in a tight loop'; the server clamps, so must this.

        Asserted on the source because the value never round-trips through a
        Python call — the config page POSTs it straight to /api/config.
        """
        src = script.read_text(encoding="utf-8")
        assert "Math.max(30," in src, (
            f"{script}: the cooldown has no client-side floor, so a typed 0 would be "
            "saved and then silently clamped on load"
        )
