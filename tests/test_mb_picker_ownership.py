"""ONE MusicBrainz pipeline now — the picker owns the shared globals.

The old two-tree world shipped a second, complete MB pipeline in
``downloads.js`` and needed an ownership guard so it would stand down when the
picker was loaded. The legacy tree is gone, so the guard is gone with it: the
served page script no longer redefines those globals at all.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

PICKER = REPO_ROOT / "test_site/static/js/services/musicbrainz-picker.js"
PAGE = REPO_ROOT / "test_site/static/js/pages/download-queue.js"
#: One downloads page now — queue.html/search.html redirect into monitor.html.
MONITOR = REPO_ROOT / "test_site/templates/Pages/downloads/monitor.html"

SHARED_GLOBALS = [
    "mbDerivedCategory",
    "doLookup",
    "clearLookup",
    "performMbSearch",
    "clearMbSearch",
    "handleGlobalMbSelect",
    "confirmReleaseSelection",
    "performMbDownloadSearch",
]


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_picker_publishes_every_shared_global():
    source = _read(PICKER)
    for name in SHARED_GLOBALS:
        assert f"global.{name} =" in source, f"the picker no longer publishes {name}"


def test_the_page_script_does_not_redefine_them():
    source = _read(PAGE)
    for name in SHARED_GLOBALS:
        assert f"global.{name} =" not in source, (
            f"pages/download-queue.js redefines {name}; the picker owns it"
        )
        assert f"window.{name} =" not in source, (
            f"pages/download-queue.js redefines {name}; the picker owns it"
        )


def test_the_picker_loads_before_the_page_script():
    for page in (MONITOR,):
        text = _read(page)
        picker = text.index("musicbrainz-picker.js")
        script = text.index("js/pages/download-queue.js")
        assert picker < script, f"{page.name} loads the page script before the picker"


def test_the_apply_match_button_calls_the_global_by_name():
    src = _read(REPO_ROOT / "test_site/templates/components/_musicbrainz_search_component.html")
    assert 'onclick="confirmReleaseSelection()"' in src, (
        "Apply Match must call the picker's global confirmReleaseSelection()"
    )


def test_the_picker_binds_its_own_result_buttons():
    src = _read(PICKER)
    assert "mb-select-match" in src, (
        "the picker must mark its match buttons so it can bind listeners"
    )
