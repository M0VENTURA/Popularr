"""`itemGroups.bindActions` must bind `this` to the element it dispatches for.

Reported symptom
----------------
"Manual search on soulseek doesn't seem to work when clicking on it on the
download queue."

Root cause — ONE call shape, ten dead buttons
---------------------------------------------
``services/item-groups.js::bindActions`` invoked its handler as a BARE call::

    el.addEventListener('click', function (event) {
      handler(this, event);          # <- binds nothing
    });

Inside that arrow-free callback ``this`` IS the element, so the fix is
``handler.call(this, this, event)``. But with a bare call the handler's ``this``
is ``undefined`` (strict) or ``globalThis`` (sloppy) — and ``this.dataset``
throws either way.

Every caller in the rebuilt tree was written in the ``this.dataset`` style::

    '.queue-manual-search': function () {
      manualQueueSearch(this.dataset.query, parseInt(this.dataset.queueId, 10) || null);
    },

so **all ten** selectors in ``pages/download-queue.js``'s handler map were dead
— manual search, cancel, retry, organize, delete, and all four group actions
plus the organize-group modal — along with the folder actions in
``pages/monitor.js``. A ``TypeError`` in a click listener is invisible: no
console-facing error, no toast, nothing happens.

Why this test drives the real code
----------------------------------
A structural assertion ("the source contains `.call(this`") would pass on any
rearrangement that still happens to contain the text. ``tests/js/probe-bindactions-this.js``
instead loads the REAL ``bindActions`` out of its IIFE and the REAL handler map
out of ``download-queue.js``, then dispatches a click at each selector and
asserts the collaborator was reached.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "probe-bindactions-this.js"
ITEM_GROUPS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "item-groups.js"
DOWNLOAD_QUEUE = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "download-queue.js"
MONITOR = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "monitor.js"


def _node_available() -> bool:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True
        ).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run_probe() -> tuple[int, str]:
    out = subprocess.run(
        ["node", str(PROBE)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return out.returncode, (out.stdout or "") + (out.stderr or "")


# ---------------------------------------------------------------------------
# Behavioural — the actual regression
# ---------------------------------------------------------------------------


@needs_node
class TestHandlersActuallyRun:
    def test_every_queue_handler_fires(self):
        """The reported bug: clicking the manual-search icon does nothing."""
        code, output = _run_probe()
        assert code == 0, (
            "a handler bound through itemGroups.bindActions did not run.\n"
            "bindActions must call the handler with `this` bound to the element "
            "(`handler.call(this, this, event)`), or every `this.dataset` "
            "handler throws a TypeError that nothing surfaces.\n\n" + output
        )

    def test_the_probe_reports_the_selectors_it_checked(self):
        """Guards against the probe silently checking nothing."""
        _code, output = _run_probe()
        for selector in (
            ".queue-manual-search",
            ".queue-cancel",
            ".queue-retry",
            ".queue-organize",
            ".queue-delete",
        ):
            assert selector in output, f"{selector} was not exercised"

    def test_the_probe_checked_at_least_ten_handlers(self):
        _code, output = _run_probe()
        assert "FIRED" in output
        fired = output.count("FIRED")
        assert fired >= 10, f"only {fired} handlers fired; expected all 10"


# ---------------------------------------------------------------------------
# The two call styles that must BOTH keep working
# ---------------------------------------------------------------------------


class TestBothCallStylesSurvive:
    """The fix must not pick a side — the param is the contract, `this` is used."""

    def test_bind_this_to_the_element(self):
        src = ITEM_GROUPS.read_text(encoding="utf-8")
        assert "handler.call(this, this, event)" in src, (
            "bindActions no longer binds `this` to the element — every "
            "`this.dataset` handler in download-queue.js and monitor.js will "
            "throw a TypeError on click"
        )

    def test_the_parameter_contract_is_kept(self):
        """`(element, event)` is what the JSDoc promises."""
        src = ITEM_GROUPS.read_text(encoding="utf-8")
        # The element must still be passed as the FIRST ARGUMENT.
        assert "handler.call(this, this, event)" in src
        assert "handlers receive (element, event)" in src.lower() or (
            "@param {Object<string, Function>} map selector -> handler(element, event)"
            in src
        )

    def test_a_bare_call_does_not_come_back(self):
        src = ITEM_GROUPS.read_text(encoding="utf-8")
        # `handler(this, event);` as a STATEMENT (with the semicolon) is the bug.
        import re
        assert not re.search(r"^\s*handler\(this,\s*event\);\s*$", src, re.M), (
            "bindActions reverted to a bare `handler(this, event)` call — that "
            "binds nothing and kills ten queue buttons"
        )

    def test_the_danger_is_documented(self):
        """This is a silent failure, so the reason must live next to the code."""
        src = ITEM_GROUPS.read_text(encoding="utf-8")
        assert "this.dataset" in src, (
            "the comment explaining WHY `this` is bound has gone; without it the "
            "next reader will 'simplify' this back into the bug"
        )


# ---------------------------------------------------------------------------
# Both callers really do use the this-style
# ---------------------------------------------------------------------------


class TestCallersUseThisStyle:
    def test_download_queue_handlers_read_this_dataset(self):
        src = DOWNLOAD_QUEUE.read_text(encoding="utf-8")
        map_block = src[
            src.index("const handlers = {") : src.index("global.itemGroups.bindActions")
        ]
        assert "this.dataset" in map_block, (
            "download-queue.js no longer uses this.dataset — re-check whether "
            "bindActions still needs to bind `this`"
        )
        for selector in (
            ".queue-manual-search",
            ".queue-cancel",
            ".queue-retry",
            ".queue-organize",
            ".queue-delete",
            ".group-organize-modal",
        ):
            assert selector in map_block, f"{selector} vanished from the handler map"

    def test_monitor_folder_actions_read_this_dataset(self):
        src = MONITOR.read_text(encoding="utf-8")
        idx = src.index("global.itemGroups.bindActions")
        block = src[idx : idx + 2000]
        assert "this.dataset" in block, (
            "monitor.js's folder actions no longer use this.dataset"
        )

    def test_the_manual_search_handler_passes_query_and_queue_id(self):
        """The specific reported path."""
        src = DOWNLOAD_QUEUE.read_text(encoding="utf-8")
        idx = src.index("'.queue-manual-search'")
        block = src[idx : idx + 300]
        assert "manualQueueSearch(" in block
        assert "this.dataset.query" in block
        assert "this.dataset.queueId" in block


# ---------------------------------------------------------------------------
# The modal the handler opens must exist and be reachable
# ---------------------------------------------------------------------------


class TestManualSearchModalIsWired:
    """The click was dead, so nothing downstream was ever exercised."""

    @pytest.mark.parametrize(
        "partial",
        ["test_site/templates/components/modals/_soulseek_manual_search.html"],
    )
    def test_the_modal_partial_exists(self, partial: str):
        assert (REPO_ROOT / partial).is_file()

    def test_the_modal_ids_match_what_slskd_js_reads(self):
        modal = (
            REPO_ROOT / "test_site/templates/components/modals/_soulseek_manual_search.html"
        ).read_text(encoding="utf-8")
        slskd = (
            REPO_ROOT / "test_site/static/js/services/slskd.js"
        ).read_text(encoding="utf-8")
        for element_id in (
            "soulseekManualSearchModal",
            "soulseekManualQuery",
            "soulseekManualSearchBtn",
            "soulseekManualStatus",
            "soulseekManualResults",
        ):
            assert f'id="{element_id}"' in modal, f"#{element_id} missing from the modal"
            assert f"getElementById('{element_id}')" in slskd or element_id in slskd, (
                f"#{element_id} is not read by slskd.js"
            )

    def test_the_queue_page_loads_both_prerequisites(self):
        """slskd.js AND the modal partial — either missing and the click is inert."""
        queue = (
            REPO_ROOT / "test_site/templates/Pages/downloads/queue.html"
        ).read_text(encoding="utf-8")
        assert "services/slskd.js" in queue, "queue.html does not load slskd.js"
        assert "_soulseek_manual_search.html" in queue, (
            "queue.html does not include the manual-search modal partial"
        )

    def test_the_monitor_page_also_gets_a_working_button(self):
        """⚠️ THE GAP THIS CLASS MISSED. The click was dead on /downloads/monitor.

        The previous guard checked only the QUEUE page, and it asserted that
        slskd.js was loaded there — which stopped being sufficient the moment
        this partial dropped its inline ``onclick`` and relied on skld.js to
        bind the button.

        The monitor page is the one that breaks, and for a non-obvious reason:
        in test_site mode its PAGE is SHADOWED to the live tree
        (``helpers/test_site_mode.py::_SHADOWED_TEMPLATES``), so it loads
        ``static/js/downloads.js`` and NOT ``services/slskd.js``. Measured
        before the fix:

            /downloads/monitor  inline onclick=False  loads slskd.js=False

        i.e. neither binding path existed and the button did nothing at all.
        """
        live_monitor = (
            REPO_ROOT / "templates/pages/downloads/monitor.html"
        ).read_text(encoding="utf-8")
        # The monitor page really does NOT load slskd.js — that is the premise.
        assert "slskd.js" not in live_monitor, (
            "the live monitor page now loads slskd.js; re-check whether the "
            "modal still needs its own binding fallback"
        )

        partial = (
            REPO_ROOT / "test_site/templates/components/modals/_soulseek_manual_search.html"
        ).read_text(encoding="utf-8")
        assert 'onclick="' in partial, (
            "the modal's Search button has no inline handler, and the monitor "
            "page does not load skld.js to bind one — so the button is dead "
            "there. Either restore a self-contained handler or load skld.js on "
            "every page that includes this partial."
        )

    def test_the_inline_handler_delegates_to_whichever_script_loaded(self):
        """One handler, two possible providers — never a duplicate implementation."""
        partial = (
            REPO_ROOT / "test_site/templates/components/modals/_soulseek_manual_search.html"
        ).read_text(encoding="utf-8")
        assert "window.runSoulseekManualSearch" in partial, (
            "the inline handler must resolve the implementation at CLICK time, "
            "so the same markup works whether the page loaded services/slskd.js "
            "(queue) or downloads.js (monitor)"
        )
        # Both providers must publish the same name, or the delegate is a lie.
        for rel in ("test_site/static/js/services/slskd.js", "static/js/downloads.js"):
            body = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
            assert "runSoulseekManualSearch" in body, (
                f"{rel} does not provide runSoulseekManualSearch, so the modal's "
                "delegate cannot resolve on a page that loads it"
            )

    def test_the_opener_is_published_as_a_global(self):
        slskd = (
            REPO_ROOT / "test_site/static/js/services/slskd.js"
        ).read_text(encoding="utf-8")
        assert "global.openSoulseekManualSearchModal = openManualSearchModal;" in slskd
