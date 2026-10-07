"""The artist page's Import button queues a download — it no longer fabricates rows.

REPORTED

> When tracks are added to the download queue from the artist, it's
> automatically adding them to the releases before the download has completed.

ROOT CAUSE
----------
The routed artist page (``pages/artist_detail_v2.html``) loads
``artist-releases.js``, whose ``importMissingRelease`` posted to
``/api/artist/import-release``. That endpoint is documented in
``artist_scan_service.import_release`` as *"Import a missing release as
placeholder track records"*: it writes one ``tracks`` row per track with
``file_path: NULL`` and then DELETES the ``missing_releases`` row.

The artist page builds its OWNED releases from ``SELECT * FROM tracks`` with no
file-path guard (``routes/ui_routes._build_artist_detail_payload``), so those
placeholder rows made the release appear under Releases **instantly** — before a
single file existed — while its missing entry was already gone. Nothing was
queued, yet the toast said ``Import queued for …``.

The 2026-08-19 change log already retired the endpoint from every button
(``2026-08-19-missing-releases-mb-only-and-import-via-soulseek.md``) — but only
in the files it touched. ``artist-releases.js`` (added by the artist-page
rebuild) kept the old wiring, so the placeholder behaviour survived on exactly
the page the user reported it from.

THE FIX
-------
``importMissingRelease`` now opens the shared MusicBrainz picker prepopulated
with the entry and queues the picked release through Soulseek — the same
sequence ``templates/pages/missing_releases.html`` already uses, and the flow
that actually produces playable files.

Behaviour is covered by ``tests/js/import-missing-release-probe.js``, which
extracts the SHIPPED function from both copies and drives it against stubs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "import-missing-release-probe.js"

MODULES = (
    "static/js/artist-releases.js",
    "test_site/static/js/pages/artist-releases.js",
)

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS"
)


@needs_node
def test_the_import_button_queues_a_download_not_placeholder_rows() -> None:
    """Run the shipped function against stubs, in BOTH trees."""
    assert PROBE.is_file(), f"probe missing: {PROBE.relative_to(REPO_ROOT)}"

    proc = subprocess.run(
        [shutil.which("node"), str(PROBE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    # A probe that prints nothing is a HARNESS failure, not a pass — an empty
    # diff must never be read as an all-clear.
    assert proc.stdout.strip(), (
        "the probe produced no output — it did not run\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "failed=0" in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("rel", MODULES)
def test_the_module_opens_the_shared_musicbrainz_picker(rel: str) -> None:
    """The wiring must survive: the picker is what turns a click into a queue."""
    code = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert "openGlobalMbSearch(artist, album, function (selectedRelease)" in code, (
        f"{rel}: importMissingRelease no longer opens the shared picker"
    )
    assert "downloadReleaseViaSoulseek(" in code, (
        f"{rel}: no fallback download helper — neither downloadMbRelease nor "
        "downloadReleaseViaSoulseek exists on the artist page"
    )


@pytest.mark.parametrize("rel", MODULES)
def test_the_module_no_longer_posts_to_the_placeholder_endpoint(rel: str) -> None:
    """Comments are stripped: the docstring deliberately NAMES the retired path."""
    code = (REPO_ROOT / rel).read_text(encoding="utf-8")
    code = _strip_js_block_comments(code)
    assert "api/artist/import-release" not in code, (
        f"{rel}: the placeholder-row endpoint is wired into the artist page again"
    )


class TestTheEndpointItselfIsIntentionallyKept:
    """CONTROL — the fix UNWIRES the button; it does not delete the API.

    The 2026-08-19 change log: *"The old /api/artist/import-release
    placeholder-record flow is no longer wired to any button (the route stays for
    API/backward compatibility)."* Deleting it would break that contract.
    """

    def test_the_route_still_exists(self) -> None:
        source = (REPO_ROOT / "routes" / "artist_routes.py").read_text(encoding="utf-8")
        assert '@artist_bp.route("/api/artist/import-release", methods=["POST"])' in source

    def test_the_placeholder_writer_still_exists(self) -> None:
        source = (
            REPO_ROOT / "services" / "metadata" / "artist_scan_service.py"
        ).read_text(encoding="utf-8")
        assert "def import_release(" in source
        assert "placeholder track records" in source, (
            "the docstring is what tells the next reader WHY this endpoint is "
            "no longer wired to a button"
        )


def _strip_js_block_comments(source: str) -> str:
    """Remove ``/* … */`` blocks, preserving line structure.

    ⚠️ REQUIRED, not tidiness: both modules DOCUMENT the endpoint they no longer
    call — by name — so a raw substring check would report the exact behaviour
    the fix removed. The same comment-matching trap has bitten this codebase
    repeatedly; stripping comments is the only form a "must not appear" assertion
    on this code can trust.
    """
    import re

    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
