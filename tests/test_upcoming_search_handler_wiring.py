"""Regression tests: the upcoming-releases Search button must have its handler.

Reported symptom (dashboard → Upcoming Releases → the per-row Search button):

    MusicBrainz search is unavailable on this page.

That string is ``test_site/static/js/services/upcoming-releases.js``, whose
``search()`` delegates to ``global.searchMusicBrainzRelease`` and only falls
back to the error when the global is missing::

    if (typeof global.searchMusicBrainzRelease === 'function') { … }
    console.warn('[upcoming] searchMusicBrainzRelease is unavailable on this page');
    notifyError('MusicBrainz search is unavailable on this page.')

Each tree wires this differently, and the page must load its own half:

* test_site — ``js/services/musicbrainz-queue.js`` publishes
  ``global.searchMusicBrainzRelease``. ``Pages/downloads/upcoming.html`` loaded
  it explicitly (which is why the dedicated page worked) but
  ``Pages/dashboard.html`` did not, so every Search click on the dashboard
  reported the error while the same table rendered fine.
* live      — ``js/upcoming_releases.js`` goes through
  ``window.searchMusicBrainzReleaseFromEncoded``, published by ``js/main.js``
  (global via base.html) and backed by ``js/downloads.js``.

These tests pin the wiring AND assert the named module actually publishes the
handler — a script tag alone proves nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _script_srcs(template: Path) -> list[str]:
    """Script src arguments in document order (``versioned_static`` unwrapped)."""
    body = template.read_text(encoding="utf-8")
    srcs = re.findall(
        r"""<script\s+src=["'][^"']*?versioned_static\(\s*['"]([^'"]+)['"]""", body
    )
    srcs += re.findall(
        r"""<script\s+src=["'](?![^"']*versioned_static)([^"']+)["']""", body
    )
    return srcs


#: (label, page template) for the pages that render the upcoming table.
TEST_SITE_CASES = (
    (
        "test_site/dashboard",
        REPO_ROOT / "test_site" / "templates" / "Pages" / "dashboard.html",
    ),
    (
        "test_site/upcoming",
        REPO_ROOT / "test_site" / "templates" / "Pages" / "downloads" / "upcoming.html",
    ),
)

PUBLISHER = "js/services/musicbrainz-queue.js"
CONSUMER = "js/services/upcoming-releases.js"


@pytest.mark.parametrize("label,template", TEST_SITE_CASES)
def test_test_site_pages_that_render_the_table_load_the_handler(
    label: str, template: Path
) -> None:
    assert template.is_file(), f"{template} is missing"
    srcs = _script_srcs(template)

    assert CONSUMER in srcs, (
        f"[{label}] does not load the upcoming-releases service, so the table "
        "the Search button lives in is not rendered at all"
    )
    assert PUBLISHER in srcs, (
        f"[{label}] loads the upcoming-releases service (whose search() calls "
        f"global.searchMusicBrainzRelease) but never loads {PUBLISHER} — the "
        "module that publishes it. Every Search click then reports "
        "'MusicBrainz search is unavailable on this page.'"
    )


def test_the_test_site_publisher_loads_before_the_consumer() -> None:
    """Order matters: the publisher must be parsed before the table calls it."""
    template = REPO_ROOT / "test_site" / "templates" / "Pages" / "dashboard.html"
    srcs = _script_srcs(template)
    assert srcs.index(PUBLISHER) < srcs.index(CONSUMER), (
        "musicbrainz-queue.js must load BEFORE upcoming-releases.js, or the "
        "handler is still undefined when the table wires its buttons"
    )


def test_the_test_site_publisher_really_publishes_the_handler() -> None:
    """A script tag proves the file loaded, not that it exports the function."""
    module = REPO_ROOT / "test_site" / "static" / PUBLISHER
    body = module.read_text(encoding="utf-8")
    assert re.search(r"global\.searchMusicBrainzRelease\s*=", body), (
        "musicbrainz-queue.js does not assign searchMusicBrainzRelease, so the "
        "dashboard would still report the error"
    )


def test_live_tree_handler_chain_is_intact() -> None:
    """Live goes through main.js's encoded wrapper, backed by downloads.js."""
    srcs = _script_srcs(REPO_ROOT / "templates" / "pages" / "dashboard.html")
    assert "js/upcoming_releases.js" in srcs
    assert "js/downloads.js" in srcs, (
        "live dashboard.html must load downloads.js — it defines "
        "searchMusicBrainzRelease for the live tree"
    )

    main_js = (REPO_ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert re.search(r"window\.searchMusicBrainzReleaseFromEncoded\s*=", main_js), (
        "main.js must publish searchMusicBrainzReleaseFromEncoded; the live "
        "upcoming_releases.js calls it via searchFromEncoded"
    )

    downloads = (REPO_ROOT / "static" / "js" / "downloads.js").read_text(encoding="utf-8")
    assert re.search(r"function searchMusicBrainzRelease\s*\(", downloads), (
        "downloads.js no longer defines searchMusicBrainzRelease; retarget this "
        "test at whatever provides the live dashboard's handler"
    )


def test_upcoming_service_reports_the_error_only_when_the_handler_is_missing() -> None:
    """Pin the exact failure string so this file keeps naming the reported bug."""
    service = REPO_ROOT / "test_site" / "static" / "js" / "services" / "upcoming-releases.js"
    body = service.read_text(encoding="utf-8")
    assert "MusicBrainz search is unavailable on this page." in body
    assert "global.searchMusicBrainzRelease" in body, (
        "upcoming-releases.js no longer calls global.searchMusicBrainzRelease; "
        "if the handler moved, retarget these tests at the new name"
    )
