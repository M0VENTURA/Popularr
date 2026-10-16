"""Tests: remote Navidrome sync — VERIFIED completion (rewritten contract).

Reported (2026-10-03):

    "When updating the metadata using the album page … it says the metadata
    is updated on the tracks, but the next navidrome import puts the old data
    back in."

Root cause chain this file pins:

1. ``trigger_and_wait_for_scan`` used to treat ``scanning == False`` as
   "scan finished".  Navidrome's own Subsonic handler only waits ~3s for the
   scanner goroutine to start ("response may be stale" after that), so
   ``scanning`` reads False *before the scan has begun* — the old wait
   returned "finished" right there and the import then read PRE-save rows,
   whose upsert overwrote the metadata just saved.  The contract is now:
   DRAIN any running scan, record ``lastScan``, trigger, and only report
   success when ``lastScan`` CHANGES (any scan completing after that
   observation also started after it, so it saw the file writes).

2. The per-tag-write trigger helpers used to be no-ops that returned
   ``True`` anyway — callers reported ``navidrome_scan_triggered: true``
   while nothing ran.  They now DELEGATE to the coalescing rescan service
   (``services.scanning.navidrome_rescan_service``) and return whether the
   request was accepted.

Behavioural coverage of the rescan service itself and of the album-save
wiring lives in ``tests/test_navidrome_rescan_after_save.py``.
"""

from __future__ import annotations

from pathlib import Path

#: Repo root (tests/…).
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Scripted client: canned getScanStatus replies + an event log so a test can
# assert the ORDER of drain-polls vs. the startScan trigger.
# ---------------------------------------------------------------------------

class _ScriptedClient:
    def __init__(self, statuses: list[dict], start_ok: bool = True):
        self._statuses = list(statuses)
        self._start_ok = start_ok
        self.events: list[str] = []
        self._i = 0

    @property
    def status_calls(self) -> int:
        return sum(1 for e in self.events if e.startswith("status"))

    @property
    def started(self) -> int:
        return sum(1 for e in self.events if e == "start")

    def get_scan_status(self) -> dict:
        status = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        self.events.append(f"status:scanning={bool(status.get('scanning'))}")
        return dict(status)

    def start_scan(self) -> bool:
        self.events.append("start")
        return self._start_ok


def _ok(scanning: bool, last_scan: str | None = "T0") -> dict:
    status = {"success": True, "scanning": scanning, "count": 1420}
    if last_scan is not None:
        status["lastScan"] = last_scan
    return status


def _fail() -> dict:
    return {"success": False, "error": "unreachable"}


def _trigger(client, **kw):
    from api_clients.navidrome import NavidromeClient

    kw.setdefault("poll_interval_seconds", 0.0)
    kw.setdefault("max_wait_seconds", 0.05)
    return NavidromeClient.trigger_and_wait_for_scan(client, **kw)


# ===========================================================================
# trigger_and_wait_for_scan — the drain + lastScan-verification contract
# ===========================================================================

class TestTriggerAndWaitForScan:

    def test_waits_for_last_scan_to_advance_before_claiming_success(self):
        """THE regression: scanning==False with an UNCHANGED lastScan is NOT success.

        This is the exact state Navidrome reports BEFORE its scan goroutine
        starts (and after an unrelated scan) — the old code returned True
        here and the import then overwrote fresh saves with stale rows.
        """
        client = _ScriptedClient([
            # drain: idle right away, baseline lastScan=T0
            _ok(scanning=False, last_scan="T0"),
            # after startScan: still idle, still T0 — must NOT be "done"
            _ok(scanning=False, last_scan="T0"),
            _ok(scanning=False, last_scan="T0"),
        ])
        assert _trigger(client) is False, (
            "reported completion while lastScan never advanced — the import "
            "would read pre-save Navidrome rows and revert saved metadata"
        )
        assert client.started == 1

    def test_returns_true_only_after_last_scan_changes(self):
        client = _ScriptedClient([
            _ok(scanning=True, last_scan="T0"),    # drain: wait this out
            _ok(scanning=False, last_scan="T0"),   # drained → baseline T0
            _ok(scanning=False, last_scan="T0"),   # triggered, not started yet
            _ok(scanning=True, last_scan="T0"),    # our scan running
            _ok(scanning=False, last_scan="T1"),   # completed → advanced
        ])
        assert _trigger(client, max_wait_seconds=5.0) is True
        assert client.started == 1
        assert client.status_calls >= 5

    def test_drains_a_running_scan_before_triggering(self):
        """startScan is rejected (ErrAlreadyScanning) while a scan runs — and a
        scan that started BEFORE our trigger may not see the file writes, so
        the drain must complete BEFORE startScan is called."""
        client = _ScriptedClient([
            _ok(scanning=True, last_scan="T0"),
            _ok(scanning=True, last_scan="T0"),
            _ok(scanning=False, last_scan="T0"),   # drained → baseline
            _ok(scanning=True, last_scan="T0"),
            _ok(scanning=False, last_scan="T1"),   # our scan done
        ])
        assert _trigger(client, max_wait_seconds=5.0) is True
        start_idx = client.events.index("start")
        # An idle status observation must exist BEFORE the trigger …
        assert any(
            e == "status:scanning=False" for e in client.events[:start_idx]
        ), "startScan fired while Navidrome was still scanning"
        # … and the first two polls must be the busy drain, not idle.
        assert client.events[:2] == [
            "status:scanning=True", "status:scanning=True",
        ]

    def test_start_scan_failure_returns_false(self):
        client = _ScriptedClient([_ok(scanning=False)], start_ok=False)
        assert _trigger(client) is False
        assert client.started == 1

    def test_unreachable_server_fails_fast_without_triggering(self):
        """3 consecutive status failures → give up immediately, do NOT spin
        for the whole deadline against a dead server."""
        client = _ScriptedClient([_fail()])
        assert _trigger(client, max_wait_seconds=5.0) is False
        assert client.status_calls == 3, (
            "expected the consecutive-failure fast-fail after 3 polls"
        )
        assert client.started == 0

    def test_status_recovery_resets_the_failure_streak(self):
        """A transient blip (fail, ok, fail, fail) must not accumulate to the
        fast-fail threshold — the streak resets on every successful poll."""
        client = _ScriptedClient([
            _fail(),
            _ok(scanning=True, last_scan="T0"),   # ok → streak resets, still busy
            _fail(),
            _fail(),
            _ok(scanning=False, last_scan="T0"),  # drained → baseline
            # …then never advances → timeout False, but we GOT to startScan,
            # which proves the earlier failures did not trip the fast-fail.
        ])
        assert _trigger(client, max_wait_seconds=0.1) is False
        assert client.started == 1, (
            "failure streak did not reset on a successful status poll"
        )

    def test_unreachable_mid_scan_fails_fast(self):
        client = _ScriptedClient([
            _ok(scanning=False, last_scan="T0"),   # drain ok → baseline
            _fail(),
            _fail(),
            _fail(),
        ])
        assert _trigger(client, max_wait_seconds=5.0) is False
        assert client.started == 1

    def test_legacy_server_without_last_scan_needs_an_observed_run(self):
        """Servers that expose no lastScan: fall back to observing the run
        (scanning True → False), never to a bare 'not scanning'."""
        client = _ScriptedClient([
            _ok(scanning=False, last_scan=None),   # drain, no lastScan field
            _ok(scanning=True, last_scan=None),    # our run observed
            _ok(scanning=False, last_scan=None),   # …finished
        ])
        assert _trigger(client, max_wait_seconds=5.0) is True

    def test_legacy_server_never_scanning_reports_unverified(self):
        """No lastScan AND never observed running → report unverified (False)
        rather than a false success (the old behaviour)."""
        client = _ScriptedClient([
            _ok(scanning=False, last_scan=None),
            _ok(scanning=False, last_scan=None),
        ])
        assert _trigger(client, max_wait_seconds=0.05) is False

    def test_last_scan_appearing_after_first_scan_counts_as_advance(self):
        """Never-scanned server: baseline lastScan is None/absent, the first
        completed scan reports a timestamp → verified."""
        client = _ScriptedClient([
            _ok(scanning=False, last_scan=None),   # baseline: never scanned
            _ok(scanning=True, last_scan=None),
            _ok(scanning=False, last_scan="2026-10-03T10:00:00Z"),
        ])
        assert _trigger(client, max_wait_seconds=5.0) is True


# ===========================================================================
# The per-tag-write helpers now DELEGATE (they used to be lying no-ops)
# ===========================================================================

class TestTriggerHelpersDelegateToRescanService:

    def test_track_route_trigger_delegates_and_returns_the_result(self, monkeypatch):
        calls: list[str] = []
        from services.scanning import navidrome_rescan_service as svc

        monkeypatch.setattr(
            svc, "request_rescan", lambda reason="": calls.append(reason) or True
        )
        from routes.track_routes import _trigger_navidrome_scan

        assert _trigger_navidrome_scan() is True
        assert calls == ["track metadata save"], (
            "track save must request a coalesced rescan (it used to report "
            "navidrome_scan_triggered=true while firing nothing)"
        )

    def test_track_route_trigger_propagates_rejection(self, monkeypatch):
        from services.scanning import navidrome_rescan_service as svc

        monkeypatch.setattr(svc, "request_rescan", lambda reason="": False)
        from routes.track_routes import _trigger_navidrome_scan

        assert _trigger_navidrome_scan() is False

    def test_misc_route_trigger_delegates_and_returns_the_result(self, monkeypatch):
        calls: list[str] = []
        from services.scanning import navidrome_rescan_service as svc

        monkeypatch.setattr(
            svc, "request_rescan", lambda reason="": calls.append(reason) or True
        )
        from routes.misc_routes import _trigger_scan_after_tag_write

        assert _trigger_scan_after_tag_write() is True
        assert calls == ["tag/genre save"]

    def test_misc_route_trigger_propagates_rejection(self, monkeypatch):
        from services.scanning import navidrome_rescan_service as svc

        monkeypatch.setattr(svc, "request_rescan", lambda reason="": False)
        from routes.misc_routes import _trigger_scan_after_tag_write

        assert _trigger_scan_after_tag_write() is False


# ---------------------------------------------------------------------------
# The IMPORT makes an external call, and the UI has to say so
# ---------------------------------------------------------------------------


class TestTheWordingMatchesWhatTheImportActuallyDoes:
    """═══════════════════════════════════════════════════════════════════════

    Reported:

        "The Navidrome import on the dashboard is currently triggering a remote
        scan, not an import"

    ⭐ THE REPORT IS ACCURATE, and the behaviour is deliberate — so the fix was
    the WORDING, not the scan.

    ``run_navidrome_import_scan`` calls
    ``sync_remote_navidrome_before_import()`` BEFORE it reads a single row: it
    triggers a Navidrome library rescan and waits for ``lastScan`` to advance
    (up to 30 minutes) so the import cannot read rows Navidrome has not yet
    re-read. Doing otherwise is the 2026-10-03 failure recorded at the top of
    this file — the import puts pre-save metadata back.

    But the scan sheet described that type as

        "Re-import rows from Navidrome only — no external API calls and no
         scoring"

    and offered a separate button labelled "Also ask Navidrome to re-read its
    own files", which together read as "the import does not touch Navidrome".
    It does. On a large library the import therefore sits waiting on Navidrome
    for minutes with the UI insisting no external call happens — exactly the
    "it's scanning, not importing" the user saw.

    ⚠️ THIS IS A TWO-WAY CONTRACT. These tests fail if the wording stops
    matching the code in EITHER direction: if someone removes the remote sync
    from the import (the description would then over-promise a rescan), or if
    the wording reverts to claiming no external calls (it would under-state
    what the import does). The description is the user's only warning that a
    click here can wait on another server.

    ═══════════════════════════════════════════════════════════════════════
    """

    PIPELINE = REPO_ROOT / "services" / "scanning" / "pipelines" / "navidrome_pipeline.py"
    SCAN_MODAL = REPO_ROOT / "test_site" / "static" / "js" / "ui" / "scan-modal.js"
    MODE_KEY = REPO_ROOT / "test_site" / "templates" / "components" / "_scan_mode_key.html"
    SCAN_SHEET = REPO_ROOT / "test_site" / "templates" / "components" / "_scan_modal.html"
    LIVE_DASHBOARD = REPO_ROOT / "templates" / "pages" / "dashboard.html"

    def test_the_import_really_does_trigger_a_remote_scan(self) -> None:
        """The premise. If this stops being true, every description below is wrong."""
        source = self.PIPELINE.read_text(encoding="utf-8")
        body = source[source.index("def run_navidrome_import_scan("):]

        assert "sync_remote_navidrome_before_import(" in body, (
            "the import no longer triggers a remote Navidrome scan — the scan "
            "sheet's description (which now promises one) is now wrong and must "
            "be reverted to 'no external API calls'"
        )

    def test_the_sync_happens_before_the_marker_check(self) -> None:
        """Order matters: it is a PRE-step, not a follow-up.

        Asserted because the description says "rescans the library FIRST and
        waits", and a future refactor that moved the sync after the marker check
        would make that misleading — the import would decide there was nothing
        to do before Navidrome had re-read anything.
        """
        source = self.PIPELINE.read_text(encoding="utf-8")
        body = source[source.index("def run_navidrome_import_scan("):]

        sync_at = body.index("sync_remote_navidrome_before_import(")
        marker_at = body.index("get_scan_status()")
        assert sync_at < marker_at, (
            "the remote sync now runs AFTER the unchanged-marker check, so the "
            "description's 'first' is wrong"
        )

    def test_no_surface_claims_the_import_makes_no_external_calls(self) -> None:
        """The exact sentence that was wrong, banned across every surface."""
        for path in (self.SCAN_MODAL, self.MODE_KEY):
            text = path.read_text(encoding="utf-8")
            # The scan-modal keeps the old sentence INSIDE a comment explaining
            # what it used to say, so comments are stripped before checking.
            code = _strip_js_or_html_comments(text)
            assert "no external API calls" not in code, (
                f"{path.name} again claims the Navidrome import makes no "
                "external calls; it triggers and waits on a Navidrome rescan"
            )

    def test_every_navidrome_description_mentions_the_rescan(self) -> None:
        """The positive half: the warning must actually be present.

        Asserted per surface rather than by one regex, because the three places
        are edited independently and a single check would let two of them
        silently revert.

        ⚠️ COMMENTS ARE STRIPPED FIRST. The mode-key row contains a Jinja
        comment that explains the rescan, so a slice of the raw row is satisfied
        by that prose alone — verified by mutation, which escaped until this
        changed. Same trap as the `ur-queue-btn` check in
        tests/test_mobile_chrome_layout_fixes.py.
        """
        modal = self.SCAN_MODAL.read_text(encoding="utf-8")
        desc_line = next(
            line for line in modal.splitlines() if line.strip().startswith("desc:")
            and "Navidrome" in modal[: modal.index(line)][-400:]
        )
        assert "rescan" in desc_line.lower(), (
            f"the scan sheet's Navidrome description does not mention the rescan: {desc_line.strip()}"
        )

        key = _strip_js_or_html_comments(self.MODE_KEY.read_text(encoding="utf-8"))
        navidrome_row = key[key.index("<strong>Navidrome</strong>"):]
        navidrome_row = navidrome_row[: navidrome_row.index("</tr>")]
        assert "rescan" in navidrome_row.lower(), (
            "the long-form mode key does not mention the rescan"
        )

        sheet = self.SCAN_SHEET.read_text(encoding="utf-8")
        secondary = sheet[sheet.index('id="scanSecondaryAction"'):]
        secondary = secondary[: secondary.index("</div>")]
        assert "no import" in secondary.lower(), (
            "the separate rescan button must say it does NOT import — otherwise "
            "it reads as the only way to rescan, which is what was wrong"
        )

    def test_the_live_dashboard_import_button_warns_that_it_waits(self) -> None:
        """The live tree's Navidrome pane has the same Import button.

        It carried no title at all, so the button gave the user no hint that it
        can sit waiting on Navidrome before anything imports.
        """
        html = self.LIVE_DASHBOARD.read_text(encoding="utf-8")
        idx = html.index("startNavidromeImport()")
        button = html[html.rindex("<button", 0, idx): html.index("</button>", idx)]

        assert "title=" in button, "the Import button has no tooltip"
        assert "rescan" in button.lower(), (
            "the Import tooltip does not mention the Navidrome rescan it triggers"
        )


def _strip_js_or_html_comments(text: str) -> str:
    """Drop `//`, `/* */`, `{#…#}`, and `<!-- -->` comments.

    ⚠️ Needed, not tidiness: several of these files EXPLAIN the old wording in a
    comment ("It read 'no external API calls'"), so a bare substring check is
    satisfied by the prose that documents the removal.
    """
    import re

    out = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = re.sub(r"\{#.*?#\}", "", out, flags=re.S)
    out = re.sub(r"<!--.*?-->", "", out, flags=re.S)
    return re.sub(r"//[^\n]*", "", out)
