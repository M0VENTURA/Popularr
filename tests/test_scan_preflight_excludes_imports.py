"""A Navidrome import must not block starting a full scan.

REPORTED: "I also can't start a full scan while a Navidrome import is happening
in the background." The dialog offered "Cancel scan & start new", which calls
``/scan/stop-all`` and aborts the import mid-flight — for a scan that was never
actually blocked.

CAUSE

``scan-preflight.js::fetchRunningScans`` filtered ``active_scans`` on
``is_running`` alone. ``navidrome_scan`` is in ``progress_service.SCAN_TYPES``, so
a running IMPORT publishes an entry there — and the preflight gate treated it as
a blocking scan. The dashboard card already excludes imports
(``IMPORT_ONLY_TYPES``); the gate never got the same list.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "test_site" / "static" / "js" / "services" / "scan-preflight.js"
DASHBOARD = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "dashboard.js"


class TestAnImportIsNotABlockingScan:
    def test_the_preflight_excludes_imports(self):
        src = PREFLIGHT.read_text(encoding="utf-8")
        assert "NON_BLOCKING_SCANS" in src, (
            "the preflight gate must know which scan types are not scans"
        )
        assert "navidrome_scan" in src, (
            "a Navidrome import publishes navidrome_scan with is_running=true, so "
            "the gate must exclude it explicitly"
        )

    def test_the_filter_consults_the_exclusion_set(self):
        src = PREFLIGHT.read_text(encoding="utf-8")
        idx = src.index("function fetchRunningScans")
        body = src[idx: idx + 900]
        assert "NON_BLOCKING_SCANS.has" in body, (
            "the is_running filter must also drop non-blocking types"
        )
        assert "scan_type" in body

    def test_it_agrees_with_the_dashboard_card(self):
        """Both surfaces must use the same exclusion, or they disagree again."""
        dash = DASHBOARD.read_text(encoding="utf-8")
        assert "IMPORT_ONLY_TYPES" in dash
        assert "navidrome_scan" in dash
