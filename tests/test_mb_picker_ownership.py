"""The MusicBrainz picker must own the MB globals on the Test Site.

REPORTED: "On the downloads page, when clicking match on the matched and
unmatched folders, when I select match it brings up MusicBrainz matches, but
when I select apply match, nothing happens."

TWO PIPELINES, ONE SET OF GLOBALS
---------------------------------
Two files each ship a COMPLETE MusicBrainz search pipeline and they are not
interchangeable:

  static/js/downloads.js                          (legacy)
  test_site/static/js/services/musicbrainz-picker.js   (reconciled)

The picker is loaded by ``base.html``; ``downloads.js`` by a page's
``{% block scripts %}``, which renders AFTER. So ``downloads.js`` always won
the race and replaced the picker's globals with its own "use strict"-era
pipeline.

They disagree on where the pending selection lives:

  picker       module scope: ``pendingRelease`` / ``selectionCallback``
  downloads.js ``window._mbPendingRelease`` / ``window._mbSearchCallback``

and on the result-button contract:

  picker       ``data-index`` + bound ``.mb-select-match`` listeners
  downloads.js inline ``onclick="handleGlobalMbSelect('<encoded>')"``

so ``confirmReleaseSelection`` read ``window._mbPendingRelease``, which the
picker never sets, hit ``if (!release) return``, and did nothing — silently.

THE FIX: an ownership guard. ``downloads.js`` stands down from every MB global
when the picker is loaded, and keeps working when it is not (the cutover-off
tree has no ``static/js/services/`` or ``static/js/utils/`` at all, so the
picker CANNOT load there).

WHY A GUARD AND NOT A DELETION
------------------------------
Deleting the legacy copies outright would break the tree that runs with the
Test-Site cutover OFF, because those shared helpers do not exist there. The
guard retires them on the Test Site while keeping that tree functional.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

LEGACY = "static/js/downloads.js"
PICKER = "test_site/static/js/services/musicbrainz-picker.js"

#: Every global the two pipelines BOTH define. Each one is a place where the
#: legacy file could silently override the picker.
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


def _legacy() -> str:
    return (REPO_ROOT / LEGACY).read_text(encoding="utf-8")


def _picker() -> str:
    return (REPO_ROOT / PICKER).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The picker really is a complete, competing pipeline
# ---------------------------------------------------------------------------


class TestThePickerIsASeparatePipeline:
    """If this stops being true the guard below is pointless — fail loudly."""

    def test_the_picker_publishes_its_own_public_api(self):
        """The guard keys off ``window.mbSearch``; it must exist."""
        assert "window.mbSearch" in _picker() or "global.mbSearch" in _picker(), (
            "the picker must publish global.mbSearch — the ownership guard "
            "detects it by that name"
        )

    @pytest.mark.parametrize("name", ["confirm", "open", "search", "clear"])
    def test_the_picker_exports_the_confirm_contract(self, name: str):
        assert re.search(rf"\bmbSearch\s*=\s*\{{", _picker()), (
            "the picker's mbSearch export block was not found"
        )
        assert re.search(rf"^\s*{name}:", _picker(), re.M), (
            f"the picker's mbSearch export must include {name!r}"
        )

    def test_the_picker_keeps_its_pending_release_in_module_scope(self):
        """THE ROOT CAUSE.

        The picker must NOT publish the selection on ``window`` — the whole bug
        was that ``downloads.js`` read a window global the picker never set.
        """
        src = _picker()
        for module_scope in ("let pendingRelease", "let selectionCallback"):
            assert module_scope in src, (
                f"{PICKER} should keep {module_scope!r} in module scope"
            )

    def test_the_two_files_agree_on_nothing_state_wise(self):
        """Documents the incompatibility the guard exists to resolve."""
        legacy = _legacy()
        picker = _picker()

        # The legacy file talks to window; the picker to module scope.
        assert "window._mbPendingRelease" in legacy
        assert "window._mbPendingRelease" not in picker, (
            "the picker must not adopt the legacy window contract — the guard "
            "is what reconciles them"
        )


# ---------------------------------------------------------------------------
# 2. The guard exists and covers every shared global
# ---------------------------------------------------------------------------


class TestTheOwnershipGuard:
    def test_the_guard_is_declared(self):
        assert re.search(r"\bvar _pickerOwnsMbGlobals\b", _legacy()), (
            "downloads.js must declare the picker-ownership guard"
        )

    def test_the_guard_tests_the_pickers_public_api(self):
        """It must detect the picker by its own export, not a loose global."""
        src = _legacy()
        idx = src.index("_pickerOwnsMbGlobals = !!(")
        window = src[idx: idx + 260]

        assert "window.mbSearch" in window, (
            "the guard must test window.mbSearch (the picker's public API)"
        )
        assert "mbSearch.confirm" in window, (
            "the guard should require the confirm method, so a half-loaded or "
            "unrelated mbSearch object cannot disable the legacy pipeline"
        )

    @pytest.mark.parametrize("name", SHARED_GLOBALS)
    def test_every_shared_global_is_guarded(self, name: str):
        """Each competing global must stand down when the picker is loaded."""
        src = _legacy()
        assert re.search(
            rf"if \(!_pickerOwnsMbGlobals\)\s+window\.{re.escape(name)}\b", src
        ), (
            f"{LEGACY} still assigns window.{name} unconditionally, so on the "
            f"Test Site it overrides the picker's pipeline (the reported "
            f"'Apply Match does nothing' bug)"
        )

    def test_the_pending_release_global_is_guarded(self):
        """This one is the direct cause of the silent no-op."""
        assert re.search(
            r"if \(!_pickerOwnsMbGlobals\)\s+window\._mbPendingRelease\s*=", _legacy()
        ), (
            "window._mbPendingRelease must be guarded — the picker owns its own "
            "selection state, and this file's copy is what confirmReleaseSelection "
            "read while the picker had set nothing"
        )

    def test_the_guard_uses_var_not_const(self):
        """A classic top-level script must not throw on a double include."""
        assert not re.search(r"\bconst _pickerOwnsMbGlobals\b", _legacy()), (
            "use `var`: downloads.js is a classic top-level script, so a const "
            "binding throws 'already been declared' if the file is ever "
            "included twice"
        )


# ---------------------------------------------------------------------------
# 3. The legacy pipeline still works when the picker is absent
# ---------------------------------------------------------------------------


class TestTheLegacyPipelineSurvivesWithoutThePicker:
    """The cutover-off tree has no picker, so these must still be implemented."""

    @pytest.mark.parametrize("name", SHARED_GLOBALS)
    def test_the_handler_body_still_exists(self, name: str):
        src = _legacy()
        # The assignment is guarded, but the function body must remain.
        assert re.search(rf"window\.{re.escape(name)}\s*=", src), (
            f"{LEGACY} dropped window.{name} entirely — the tree running with "
            f"the Test-Site cutover OFF would lose its MusicBrainz pipeline "
            f"(it has no static/js/services/ or utils/, so the picker cannot "
            f"load there)"
        )

    def test_the_legacy_pipeline_is_self_contained(self):
        """It must not depend on the Test-Site-only helper modules."""
        src = _legacy()
        for helper in ("global.api", "global.toast", "global.buttonState"):
            assert helper not in src, (
                f"{LEGACY} must not call {helper} — those live in "
                f"test_site/static/js/{{utils,ui}}/ which do NOT exist in the "
                f"live tree, so it would break with the cutover off"
            )


# ---------------------------------------------------------------------------
# 4. The component markup matches the pipeline that will own it
# ---------------------------------------------------------------------------


class TestTheMarkupUsesThePickerContract:
    def test_the_apply_match_button_calls_the_global_by_name(self):
        """Both pipelines expose confirmReleaseSelection, so this stays valid.

        The button uses an inline ``onclick`` rather than a bound listener, so
        whichever file owns the global at click time handles it. That is what
        makes the guard sufficient for THIS button.
        """
        for rel in (
            "templates/components/_musicbrainz_search_component.html",
            "test_site/templates/components/_musicbrainz_search_component.html",
        ):
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "onclick=\"confirmReleaseSelection()\"" in src, (
                f"{rel} must call confirmReleaseSelection() from Apply Match"
            )

    def test_the_picker_binds_its_own_result_buttons(self):
        """The picker uses data-index + listeners; it must not rely on onclick."""
        src = _picker()
        assert "mb-select-match" in src, (
            "the picker must mark its match buttons so it can bind listeners — "
            "its renderer and handler are a matched pair"
        )
