"""Tests for the busy popup (``ui/busy-popup.js``) and its wiring.

Two things need proving, and they are different in kind:

1. **The module behaves.** The popup must be a counter (so overlapping actions
   cannot have one close another's), pointer-transparent (so it cannot swallow
   a click meant for the modal underneath), always released (so it cannot
   strand), and safe outside a browser.

2. **The pages actually call it, and the buttons it was added for exist.**
   Adding ("Auto-Link MBIDs") turned out to be a DEAD BUTTON — its four
   ``onclick="autoLinkAllMbids()"`` call sites across both trees had no
   definition anywhere. A template has no compiler, so nothing caught it. These
   tests pin both the implementation and every handler the album template
   calls, so the widget and its wiring cannot silently come apart again.

The module is exercised in Node rather than a DOM emulator: it only touches a
small, well-defined slice of DOM (``createElement`` / ``appendChild`` /
``classList`` / ``addEventListener``), so a tiny stub is both sufficient and
far more honest about what is being asserted than a headless browser would be.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE = REPO_ROOT / "static" / "js"
REBUILT = REPO_ROOT / "test_site" / "static" / "js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS module"
)


# ---------------------------------------------------------------------------
# DOM stub
# ---------------------------------------------------------------------------

# A deliberately small DOM. It records what the module does so the assertions
# are about observable effects (classes, listeners, element identity) rather
# than about the module's internals.
_DOM_STUB = r"""
const listeners = {};
const created = [];

function makeEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    attributes: {},
    style: {},
    _classes: new Set(),
    textContent: '',
    innerHTML: '',
    isConnected: true,
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
    },
    setAttribute(k, v) { el.attributes[k] = String(v); },
    getAttribute(k) { return k in el.attributes ? el.attributes[k] : null; },
    removeAttribute(k) { delete el.attributes[k]; },
    appendChild(child) { el.children.push(child); child.parentNode = el; return child; },
    querySelector(sel) {
      // Only the one selector the module uses.
      if (sel === '.popularr-busy-label') {
        return el.children.find((c) => c._classes.has('popularr-busy-label')) || null;
      }
      return null;
    },
  };
  // The module assigns innerHTML to build the spinner + label pair, so parse
  // the two spans out of it into real child elements. Without this the label
  // would be unreadable and the stub would be testing nothing.
  let _html = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return _html; },
    set(v) {
      _html = String(v);
      el.children = [];
      const re = /<span class="([^"]+)"/g;
      let m;
      while ((m = re.exec(_html)) !== null) {
        const child = makeBareEl('span');
        m[1].split(/\s+/).forEach((c) => c && child._classes.add(c));
        el.children.push(child);
      }
    },
  });
  created.push(el);
  return el;
}

function makeBareEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    attributes: {},
    style: {},
    _classes: new Set(),
    textContent: '',
    isConnected: true,
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
    },
    setAttribute(k, v) { el.attributes[k] = String(v); },
    getAttribute(k) { return k in el.attributes ? el.attributes[k] : null; },
    removeAttribute(k) { delete el.attributes[k]; },
    appendChild(c) { el.children.push(c); return c; },
    querySelector() { return null; },
  };
  return el;
}

const head = makeEl('head');
const body = makeEl('body');
const registry = {};

global.document = {
  head,
  body,
  getElementById(id) { return registry[id] || null; },
  createElement(tag) { return makeEl(tag); },
  addEventListener() {},
};
// appendChild must register by id so getElementById finds the popup.
const origHeadAppend = head.appendChild.bind(head);
head.appendChild = function (child) {
  if (child.id) registry[child.id] = child;
  return origHeadAppend(child);
};
const origBodyAppend = body.appendChild.bind(body);
body.appendChild = function (child) {
  if (child.id) registry[child.id] = child;
  return origBodyAppend(child);
};

global.window = global;
global.addEventListener = function (name, fn) {
  (listeners[name] = listeners[name] || []).push(fn);
};

require(__MODULE_PATH__);

function popup() { return registry['popularrBusyPopup']; }
function label() {
  const el = popup();
  const l = el && el.children.find((c) => c._classes.has('popularr-busy-label'));
  return l ? l.textContent : null;
}
function visible() { return !!(popup() && popup()._classes.has('popularr-busy-visible')); }
function out(obj) { console.log(JSON.stringify(obj)); }
"""


def _run_module(script: str) -> dict:
    """Run a snippet against the real module in Node and return its JSON output.

    The program goes into a temp FILE rather than ``node -e``: with ``-e``,
    Node shifts ``process.argv``, so a path passed as an argument lands in
    ``argv[1]`` and ``require(process.argv[2])`` receives ``undefined``.
    """
    module_path = (REBUILT / "ui" / "busy-popup.js").resolve()
    program = (
        _DOM_STUB.replace("__MODULE_PATH__", json.dumps(str(module_path)))
        + "\n"
        + script
    )
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(program, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(harness)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# 1. Module behaviour
# ---------------------------------------------------------------------------

class TestPopupBehaviour:

    def test_hidden_until_shown(self):
        result = _run_module("""
          out({ visibleBefore: visible() });
        """)
        assert result["visibleBefore"] is False

    def test_show_makes_it_visible_with_the_label(self):
        result = _run_module("""
          busyPopup.show('Looking up MusicBrainz match…');
          out({ visible: visible(), label: label() });
        """)
        assert result["visible"] is True
        assert result["label"] == "Looking up MusicBrainz match…"

    def test_hide_removes_it(self):
        result = _run_module("""
          const h = busyPopup.show('x');
          busyPopup.hide(h);
          out({ visible: visible() });
        """)
        assert result["visible"] is False

    def test_overlapping_claims_keep_it_up(self):
        """A counter, not a flag: releasing one of two claims must NOT hide it."""
        result = _run_module("""
          const a = busyPopup.show('first');
          const b = busyPopup.show('second');
          busyPopup.hide(a);
          const afterFirstRelease = { visible: visible(), active: busyPopup.active };
          busyPopup.hide(b);
          out({ afterFirstRelease, afterSecondRelease: { visible: visible(), active: busyPopup.active } });
        """)
        assert result["afterFirstRelease"] == {"visible": True, "active": 1}
        assert result["afterSecondRelease"] == {"visible": False, "active": 0}

    def test_hide_is_idempotent(self):
        """Double hide must not drive the counter negative and desync it."""
        result = _run_module("""
          const h = busyPopup.show('x');
          busyPopup.hide(h);
          busyPopup.hide(h);
          const afterDoubleHide = busyPopup.active;
          const other = busyPopup.show('still works');
          out({ afterDoubleHide, visibleWithOneClaim: visible(), active: busyPopup.active });
        """)
        assert result["afterDoubleHide"] == 0
        assert result["visibleWithOneClaim"] is True
        assert result["active"] == 1

    def test_hide_tolerates_a_missing_handle(self):
        """``hide(maybeHandle)`` at the end of a branch must need no guard."""
        result = _run_module("""
          busyPopup.hide(null);
          busyPopup.hide(undefined);
          busyPopup.show('x');
          busyPopup.hide(null);   // must not affect the live claim
          out({ visible: visible(), active: busyPopup.active });
        """)
        assert result["visible"] is True
        assert result["active"] == 1

    def test_update_relabels_without_changing_the_count(self):
        result = _run_module("""
          const h = busyPopup.show('Looking up MusicBrainz match…');
          busyPopup.update(h, 'Saving…');
          out({ label: label(), active: busyPopup.active });
        """)
        assert result["label"] == "Saving…"
        assert result["active"] == 1

    def test_show_and_run_releases_on_success(self):
        result = _run_module("""
          (async () => {
            const value = await busyPopup.showAndRun('Working…', async () => 42);
            out({ value, visible: visible(), active: busyPopup.active });
          })();
        """)
        assert result["value"] == 42
        assert result["visible"] is False
        assert result["active"] == 0

    def test_show_and_run_releases_when_the_function_throws(self):
        """The important one: a throw must not strand the popup."""
        result = _run_module("""
          (async () => {
            let caught = null;
            try {
              await busyPopup.showAndRun('Working…', async () => { throw new Error('boom'); });
            } catch (e) { caught = e.message; }
            out({ caught, visible: visible(), active: busyPopup.active });
          })();
        """)
        assert result["caught"] == "boom"
        assert result["visible"] is False
        assert result["active"] == 0

    def test_show_and_run_rejects_a_missing_function(self):
        result = _run_module("""
          (async () => {
            let caught = null;
            try { await busyPopup.showAndRun('no fn'); } catch (e) { caught = e.name; }
            out({ caught, visible: visible() });
          })();
        """)
        assert result["caught"] == "TypeError"
        assert result["visible"] is False

    def test_release_all_clears_everything(self):
        result = _run_module("""
          busyPopup.show('a');
          busyPopup.show('b');
          busyPopup.releaseAll();
          out({ visible: visible(), active: busyPopup.active });
        """)
        assert result["visible"] is False
        assert result["active"] == 0

    def test_a_navigation_cannot_strand_it(self):
        """pagehide/beforeunload must reset the count, since a navigation is the
        one exit path that no `.finally` can cover.

        The guard is registered lazily on first ``show()``, so the listener
        counts are read AFTER showing.
        """
        result = _run_module("""
          busyPopup.show('x');
          const pagehideRegistered = (listeners['pagehide'] || []).length;
          const beforeunloadRegistered = (listeners['beforeunload'] || []).length;
          (listeners['pagehide'] || []).forEach((f) => f());
          out({
            pagehideRegistered,
            beforeunloadRegistered,
            visibleAfterPagehide: visible(),
            active: busyPopup.active,
          });
        """)
        assert result["pagehideRegistered"] >= 1
        assert result["beforeunloadRegistered"] >= 1
        assert result["visibleAfterPagehide"] is False
        assert result["active"] == 0

    def test_the_guard_is_only_bound_once(self):
        """Repeated shows must not stack listeners."""
        result = _run_module("""
          busyPopup.show('a');
          busyPopup.hide(null);
          busyPopup.releaseAll();
          busyPopup.show('b');
          out({ pagehide: (listeners['pagehide'] || []).length });
        """)
        assert result["pagehide"] == 1

    def test_the_same_element_is_reused(self):
        """A second show must not append a duplicate popup to the page."""
        result = _run_module("""
          busyPopup.show('first');
          busyPopup.hide(null);
          busyPopup.releaseAll();
          busyPopup.show('second');
          const popups = [];
          for (let i = 0; i < created.length; i++) {
            if (created[i].id === 'popularrBusyPopup') popups.push(created[i]);
          }
          out({ popupElementsCreated: popups.length });
        """)
        assert result["popupElementsCreated"] == 1

    def test_it_is_pointer_transparent_and_above_modals(self):
        """Non-negotiable: the user must still be able to click the modal
        underneath, and the popup must not sit behind it."""
        result = _run_module("""
          busyPopup.show('x');
          const styles = registry['popularrBusyPopupStyles'];
          const text = styles ? styles.textContent : '';
          out({ text: text });
        """)
        text = result["text"]
        assert "pointer-events: none" in text, (
            "the popup would swallow clicks meant for the page/modal behind it"
        )
        z = re.search(r"z-index:\s*(\d+)", text)
        assert z, "the popup must set an explicit stacking order"
        assert int(z.group(1)) >= 1080, (
            "Bootstrap modals sit at 1055, so the popup must be above them"
        )

    def test_styles_are_injected_only_once(self):
        result = _run_module("""
          busyPopup.show('a');
          busyPopup.show('b');
          let count = 0;
          for (let i = 0; i < created.length; i++) {
            if (created[i].id === 'popularrBusyPopupStyles') count++;
          }
          out({ styleElements: count });
        """)
        assert result["styleElements"] == 1

    def test_it_is_announced_to_assistive_tech(self):
        result = _run_module("""
          busyPopup.show('Adding to download queue…');
          const el = popup();
          out({ role: el.getAttribute('role'), live: el.getAttribute('aria-live') });
        """)
        assert result["role"] == "status"
        assert result["live"] == "polite"


# ---------------------------------------------------------------------------
# 2. The copies must not drift
# ---------------------------------------------------------------------------

class TestTheTwoCopiesAgree:
    """The module ships in BOTH trees, so divergence changes behaviour in one
    mode only. (``test_artist_page_contract`` guards the same pair; this keeps
    the reason next to the module.)"""

    @staticmethod
    def _body(path: Path) -> str:
        text = path.read_text(encoding="utf-8").lstrip()
        if text.startswith("/*"):
            end = text.find("*/")
            if end != -1:
                return text[end + 2:].strip()
        return text.strip()

    def test_both_copies_exist(self):
        assert (LIVE / "busy-popup.js").is_file()
        assert (REBUILT / "ui" / "busy-popup.js").is_file()

    def test_bodies_are_identical(self):
        assert self._body(LIVE / "busy-popup.js") == self._body(REBUILT / "ui" / "busy-popup.js"), (
            "The live (static/js/busy-popup.js) and rebuilt "
            "(test_site/static/js/ui/busy-popup.js) copies have drifted. Both "
            "trees load this module, so port the change to both."
        )

    def test_both_tree_bases_load_it(self):
        for rel in ("templates/base.html", "test_site/templates/base.html"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "busy-popup.js" in source, f"{rel} does not load the popup"


# ---------------------------------------------------------------------------
# 3. The pages actually call it
# ---------------------------------------------------------------------------

class TestPagesCallIt:

    @staticmethod
    def _read(rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def test_album_page_shows_a_lookup_popup(self):
        source = self._read("test_site/static/js/pages/album.js")
        assert "busyPopup" in source
        assert "Looking up MusicBrainz match" in source

    def test_album_page_releases_the_lookup_popup_on_dismissal(self):
        """Cancelling the picker must release it, or it stays up forever."""
        source = self._read("test_site/static/js/pages/album.js")
        assert "endLookupBusy" in source
        # The dismissal branch (no selection) must call the release.
        assert re.search(r"else\s*\{[^}]*endLookupBusy\(\)", source), (
            "the 'dismissed without a pick' branch does not release the popup"
        )

    def test_album_page_releases_the_popup_even_if_applying_throws(self):
        source = self._read("test_site/static/js/pages/album.js")
        assert ".finally(endLookupBusy)" in source, (
            "applyAlbumMatch is async and not awaited, so a throw would strand "
            "the popup without a .finally"
        )

    def test_live_album_page_does_the_same(self):
        source = self._read("static/js/album_detail.js")
        assert "busyPopup" in source
        assert "Looking up MusicBrainz match" in source
        assert "_endAlbumLookupBusy" in source
        assert ".finally(_endAlbumLookupBusy)" in source

    def test_batch_queue_shows_a_popup(self):
        source = self._read("test_site/static/js/services/musicbrainz-queue.js")
        assert "showAndRun('Adding to download queue" in source

    def test_live_search_flyout_shows_a_popup_when_queueing(self):
        source = self._read("static/js/unified_search.js")
        assert "busyPopup.show('Adding to download queue" in source
        assert "busyPopup.hide(busied)" in source, (
            "the popup is shown but never hidden on the fetch path"
        )

    def test_live_album_queue_button_shows_a_popup(self):
        source = self._read("static/js/album_detail.js")
        assert "Adding to download queue" in source


# ---------------------------------------------------------------------------
# 4. Every handler the album template calls must exist
# ---------------------------------------------------------------------------

class TestAlbumTemplateHandlersAreDefined:
    """The defect that produced the ``autoLinkAllMbids`` fix.

    The template called a function defined NOWHERE, so the click threw
    ``ReferenceError``. Templates are not compiled, so nothing noticed. This
    harvests every bare ``fn()`` from the template's inline handlers and
    requires each to be defined in the JS loaded by that page.
    """

    #: Handlers deliberately provided by other modules/browser globals, or
    #: whose absence is correct. Each entry must be justified.
    KNOWN_EXTERNAL = {
        "void": "javascript:void(0) — the browser's own no-op",
        "alert": "browser global",
        "confirm": "browser global",
    }

    @staticmethod
    def _page_scripts(page_rel: str) -> str:
        """Concatenate every JS file the page tree loads for the album page."""
        root = REPO_ROOT / ("test_site" if page_rel.startswith("test_site") else "")
        js_root = root / "static" / "js"
        # base.html's global bundle + the album page's own scripts. The album
        # handler set spans both, so gather the whole directory (cheap, and it
        # errs toward finding a definition rather than reporting a false miss).
        chunks = []
        for path in sorted(js_root.rglob("*.js")):
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                pass
        return "\n".join(chunks)

    def _handlers(self, page_rel: str) -> set[str]:
        source = (REPO_ROOT / page_rel).read_text(encoding="utf-8")
        found = set()
        for match in re.finditer(r'on(?:click|change|submit|input)\s*=\s*"([^"]*)"', source):
            body = match.group(1)
            # Bare `name(` calls only — skip property access (obj.fn()).
            for call in re.finditer(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(", body):
                found.add(call.group(1))
        return found

    def test_auto_link_handler_is_defined_in_both_trees(self):
        """Direct pin for the reported-relevant defect."""
        for page, js in (
            ("test_site/templates/Pages/album_detail.html", "test_site/static/js/pages/album.js"),
            ("templates/pages/album_detail.html", "static/js/album_detail.js"),
        ):
            page_source = (REPO_ROOT / page).read_text(encoding="utf-8")
            if "autoLinkAllMbids" not in page_source:
                continue
            js_source = (REPO_ROOT / js).read_text(encoding="utf-8")
            assert "autoLinkAllMbids" in js_source, (
                f"{page} calls autoLinkAllMbids() but {js} does not define it — "
                "the button throws ReferenceError"
            )
            assert re.search(
                r"(?:window\.)?autoLinkAllMbids\s*=", js_source
            ), f"{js} mentions autoLinkAllMbids but never assigns it"

    def test_it_calls_the_endpoint_that_exists(self):
        """The handler must target the real route, not an invented one."""
        for js in ("static/js/album_detail.js", "test_site/static/js/pages/album.js"):
            source = (REPO_ROOT / js).read_text(encoding="utf-8")
            assert "/api/musicbrainz/link-album-mbids" in source, js

        routes = (REPO_ROOT / "routes" / "musicbrainz_routes.py").read_text(encoding="utf-8")
        assert '"/link-album-mbids"' in routes, (
            "the endpoint the handler calls is not registered"
        )

    def test_every_album_template_handler_resolves(self):
        """The general guard: no inline handler may be unreachable, in EITHER tree.

        This is the test that found the dead buttons. The album page called
        `autoLinkAllMbids` (plus four more) from its inline handlers while
        nothing defined them — a template has no compiler, so the buttons threw
        ReferenceError and shipped.

        ``tests/test_rebuilt_pages_have_no_dead_buttons.py`` already owns the
        authoritative ``_OBSOLETE_BUTTONS`` register of knowingly-dead handlers
        (each with a written justification and a staleness assertion). This
        imports that register rather than duplicating it, so a handler either
        resolves, or is consciously registered there — and a newly-broken one
        fails HERE.
        """
        from tests.test_rebuilt_pages_have_no_dead_buttons import _OBSOLETE_BUTTONS

        registered = {
            name for (reg_page, name) in _OBSOLETE_BUTTONS
            if reg_page == "album_detail.html"
        }

        problems = []
        for page in (
            "test_site/templates/Pages/album_detail.html",
            "templates/pages/album_detail.html",
        ):
            handlers = self._handlers(page)
            if not handlers:
                problems.append(f"{page}: no inline handlers found (extractor broken)")
                continue

            js = self._page_scripts(page)
            for name in sorted(handlers):
                if name in self.KNOWN_EXTERNAL or name in registered:
                    continue
                # Defined either as `function name(` or as an assignment
                # (`name =`, `window.name =`, `global.name =`).
                pattern = (
                    rf"(?:function\s+{re.escape(name)}\s*\()"
                    rf"|(?:[\w.]*{re.escape(name)}\s*=\s*(?:function|async|\())"
                )
                if not re.search(pattern, js):
                    problems.append(f"{page}: {name}()")

        assert not problems, (
            "These inline handlers are defined nowhere in the JS their page "
            "loads and are not in _OBSOLETE_BUTTONS, so each throws "
            f"ReferenceError on click: {problems}"
        )

    def test_the_five_previously_dead_album_handlers_are_all_implemented(self):
        """Pins the specific set that was broken, in BOTH trees.

        Each of these was called by the album template and defined nowhere. The
        register previously excused them, so the guard suite stayed green while
        the buttons threw. They are now implemented, so the register entries are
        gone and this test is what keeps them implemented.
        """
        handlers = (
            "autoLinkAllMbids",
            "downloadMissingTracks",
            "renameAlbumFiles",
            "openAlbumArtModal",
            "alignTracklist",
        )
        for js in (
            "test_site/static/js/pages/album.js",
            "static/js/album_detail.js",
        ):
            source = (REPO_ROOT / js).read_text(encoding="utf-8")
            for name in handlers:
                assert re.search(
                    rf"(?:window\.|global\.)?{re.escape(name)}\s*=", source
                ), f"{js} does not define {name}() — its button will throw"

    def test_the_five_handlers_are_not_in_the_obsolete_register(self):
        """A register entry for a now-implemented handler is stale, and the
        register's own staleness test would fail on it. Pinned here so the two
        suites cannot disagree about what is dead."""
        from tests.test_rebuilt_pages_have_no_dead_buttons import _OBSOLETE_BUTTONS

        registered = {name for (page, name) in _OBSOLETE_BUTTONS if page == "album_detail.html"}
        still_claimed_dead = sorted(
            registered & {
                "autoLinkAllMbids", "downloadMissingTracks",
                "renameAlbumFiles", "openAlbumArtModal", "alignTracklist",
            }
        )
        assert not still_claimed_dead, (
            f"these are implemented now, so remove them from _OBSOLETE_BUTTONS: "
            f"{still_claimed_dead}"
        )


# ---------------------------------------------------------------------------
# 1b. Guarded calls must not guard a function that does not exist
# ---------------------------------------------------------------------------
#
# ⭐ THE `typeof x === 'function'` TRAP. Guards of the form
#
#     if (typeof window.refreshAlbumPage === 'function') window.refreshAlbumPage();
#
# read as defensive, but when the function is definition-less they are a SILENT
# no-op: no ReferenceError, no console noise, the feature just does nothing.
# `tests/test_rebuilt_pages_have_no_dead_buttons.py` cannot see these, because
# it only harvests `onclick=` handlers from templates — a guarded call from
# inside a JS module is invisible to it.
#
# This was a live defect: `refreshAlbumPage()` was called from BOTH trees and
# defined in NEITHER, so changing the album art or auto-linking MBIDs left the
# page showing stale state and reported success.

_GUARDED_GLOBAL_RE = re.compile(
    r"typeof\s+(?:window|global)\.([A-Za-z_]\w*)\s*===\s*['\"]function['\"]"
)
_GLOBAL_DEF_RE = re.compile(
    # ⭐ `=(?!=)` is load-bearing. Plain `\s*=` also matches the first `=` of a
    # `===` comparison, so the guarded CALL
    #     if (typeof window.refreshAlbumPage === 'function') window.refreshAlbumPage();
    # would itself be read as a DEFINITION — and deleting the real definition
    # then went undetected. Proven by mutation.
    r"(?:window|global)\.([A-Za-z_]\w*)\s*=(?!=)"      # window.foo = / global.foo =
    r"|^\s*(?:var|let|const)\s+([A-Za-z_]\w*)\s*=(?!=)"  # const foo =
    r"|function\s+([A-Za-z_]\w*)\s*\(",                # function foo(
    re.M,
)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _strip_js_comments(source: str) -> str:
    """Remove JS comments before pattern-matching the code.

    ⭐ THIS IS LOAD-BEARING — without it the guard is defeated by the fix's own
    explanation. The docstrings above quote the very patterns being searched
    for, e.g. a comment containing ``window.refreshAlbumPage === 'function'``
    satisfies ``_GLOBAL_DEF_RE`` (``window.refreshAlbumPage\\s*=`` matches the
    first ``=`` of ``===``). A probe that matched that would keep believing the
    function is defined even after it was deleted, which is exactly the
    false-negative this class of test must not have. Proven by mutation.

    A ``//`` is only treated as a comment start when it is not inside a quoted
    string and not preceded by ``:`` — that keeps ``'https://…'`` literals
    (which appear throughout these files) from truncating real code.
    """
    without_blocks = _BLOCK_COMMENT_RE.sub("", source)
    out: list[str] = []
    for line in without_blocks.splitlines():
        quote: str | None = None
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                if i == 0 or line[i - 1] != ":":
                    cut = i
                    break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)


def _tree_js_sources(tree_root: Path) -> list[str]:
    """Every JS body the tree can execute: its modules AND template inline JS.

    ⭐ Template inline scripts are NOT optional here. ``addSelectedTrack`` and
    ``openReplacementTrackModal`` are defined in an inline ``<script>`` in
    ``templates/playlists/importer.html``; a probe that only read ``*.js``
    files reported both as dead — a false positive that would have had the
    register excusing a function that plainly works.
    """
    bodies: list[str] = []
    for path in (tree_root / "static" / "js").rglob("*.js"):
        bodies.append(path.read_text(encoding="utf-8", errors="replace"))
    for path in (tree_root / "templates").rglob("*.html"):
        body = path.read_text(encoding="utf-8", errors="replace")
        bodies.extend(_SCRIPT_BLOCK_RE.findall(body))
    return bodies


def _defined_globals(tree_root: Path) -> set[str]:
    """Globals defined anywhere the tree can run JS from (after comment strip)."""
    found: set[str] = set()
    for source in _tree_js_sources(tree_root):
        for match in _GLOBAL_DEF_RE.finditer(_strip_js_comments(source)):
            found.add(next(g for g in match.groups() if g))
    return found


def _guard_has_else(source: str, pos: int) -> bool:
    """True when the guarded `if` at *pos* has an `else` branch.

    ⭐ THIS is what separates a SILENT no-op from a deliberate optional
    dependency.

        if (typeof X === 'function') X(); else toast('not available');

    tells the user something, so a missing X degrades loudly and acceptably.
    ``if (typeof X === 'function') X();`` with no else does NOTHING AT ALL and
    says nothing — that is the defect class here. Flagging the former would
    demand a definition for coupling that is intentionally optional, and would
    make the test wrong about two functions (``addSelectedTrack``,
    ``openReplacementTrackModal``) that are deliberately optional on a page
    that does not define them.
    """
    brace = source.find("{", pos)
    semi = source.find(";", pos)
    if brace != -1 and (semi == -1 or brace < semi):
        depth = 0
        i = brace
        while i < len(source):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        return source[i + 1 : i + 80].lstrip().startswith("else")
    if semi != -1:
        return source[semi + 1 : semi + 80].lstrip().startswith("else")
    return False


def _guard_sites(tree_root: Path) -> list[tuple[str, bool]]:
    """[(guarded global, whether its guard has an else)] for a whole tree."""
    sites: list[tuple[str, bool]] = []
    for source in _tree_js_sources(tree_root):
        clean = _strip_js_comments(source)
        for match in _GUARDED_GLOBAL_RE.finditer(clean):
            sites.append((match.group(1), _guard_has_else(clean, match.end())))
    return sites


def _guarded_globals(tree_root: Path) -> set[str]:
    """Every global referenced behind a `typeof ... === 'function'` guard."""
    return {name for name, _ in _guard_sites(tree_root)}


def _silent_guarded_globals(tree_root: Path) -> set[str]:
    """Guarded globals whose guard has NO else — the true silent no-ops."""
    return {name for name, has_else in _guard_sites(tree_root) if not has_else}


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------
#
# Same two-rule discipline as test_rebuilt_pages_have_no_dead_buttons.py:
#   1. every entry must still be dead (the staleness test below), and
#   2. a NEW ghost fails the build — so it cannot rot into a blanket skip.
#
# ⚠️ EVERY ENTRY HERE IS PRE-EXISTING, verified not to be on a line added by
# the change set that introduced this test. They are recorded rather than fixed
# because each is a MISSING FEATURE (not a typo): the definition was never
# written, so making the guard resolve means writing that feature. Doing so
# silently as part of an unrelated UI change would be wrong.
_KNOWN_DEAD_GUARDS: dict[str, str] = {
    "loadFolderGroups": (
        "Pre-existing, in BOTH trees. Callers expect monitor.js to publish the "
        "Download-Queue folder renderer, and monitor.js does not — it only "
        "publishes csvInlineReset/csvInlineImport. So `keepVisibleOnEmpty` "
        "refreshes after a folder match or download silently do nothing and the "
        "queue list keeps its stale contents. Needs the renderer hoisted into "
        "a shared module both monitor.js and downloads.js can reach."
    ),
    "searchMusicBrainzReleases": (
        "Pre-existing, in BOTH trees. unified_search.js / search-flyout.js "
        "prefer it for a plain query so the flyout shows releases, but it is "
        "defined nowhere — so every plain search silently falls through to the "
        "generic path. Needs the release search exposed as a global."
    ),
    "loadUpcomingReleases": (
        "Pre-existing, rebuilt tree. musicbrainz-queue.js refreshes the "
        "Upcoming Releases panel by calling it, but the panel's own module "
        "never publishes that name, so the list does not refresh after a queue "
        "change. Needs the module to expose its refresh function."
    ),
    "showSlskdResults": (
        "Pre-existing, rebuilt tree. download-queue.js calls it to reopen the "
        "Slskd results for a queue row, but nothing defines it, so the "
        "'view results' affordance does nothing. Needs the search results "
        "viewer exposed from the slskd module."
    ),
}


def guard_problems(tree_root: Path) -> list[str]:
    """SILENT guarded globals in *tree_root* that resolve nowhere and are
    unregistered. A guard with an `else` is intentionally optional coupling and
    is deliberately not reported."""
    return sorted(
        (_silent_guarded_globals(tree_root) - _defined_globals(tree_root))
        - set(_KNOWN_DEAD_GUARDS)
    )


def _ghost_message(problems: list[str]) -> str:
    return (
        "these are called behind a bare `typeof ... === 'function'` guard (no "
        "else) but are DEFINED NOWHERE in this tree, so the guard silently "
        "skips the work and the feature does nothing: " + str(problems) + "\n\n"
        "Fix by defining the function, or — if it is a pre-existing missing "
        "feature that needs a product decision — add it to _KNOWN_DEAD_GUARDS "
        "with the evidence. Do NOT weaken this test."
    )


class TestGuardedCallsAreNotGuardingAGhost:
    """A `typeof` guard around a definition-less function is a silent no-op."""

    def test_the_comment_stripper_removes_prose_but_keeps_code(self):
        """⭐ The guard's own correctness. This is what makes the probe able to
        see a DELETED definition when a comment still quotes it."""
        # A quoted pattern in a comment must not read as a definition.
        stripped = _strip_js_comments(
            "// see `window.refreshAlbumPage === 'function'`\nwindow.other = 1;\n"
        )
        assert "refreshAlbumPage" not in stripped
        assert "window.other = 1" in stripped

        # Block comments, including multi-line.
        stripped = _strip_js_comments("/* window.ghost = 1\n   more */\nwindow.real = 2;")
        assert "ghost" not in stripped
        assert "window.real = 2" in stripped

        # A URL in a string literal must NOT be mistaken for a line comment.
        kept = _strip_js_comments("const u = 'https://example.com/x';window.after = 3;")
        assert "window.after = 3" in kept, "a URL literal truncated real code"
        # …and neither must the `//` inside a real definition line.
        kept = _strip_js_comments('const msg = "a // b"; window.tail = 4;')
        assert "window.tail = 4" in kept

    def test_the_definition_scan_reads_template_inline_scripts(self):
        """⭐ Guards against a whole class of FALSE POSITIVE.

        ``addSelectedTrack`` really is defined — in an inline ``<script>`` in
        ``templates/playlists/importer.html``. A probe that only read ``*.js``
        files reported it dead, which would have made every failure here
        suspect and had the register excusing a function that works.

        (The rebuilt tree has NO importer page, so there the same name is
        correctly an unused optional dependency — see
        ``test_a_guard_with_an_else_is_not_a_silent_noop``.)
        """
        defined = _defined_globals(REPO_ROOT)
        for name in ("addSelectedTrack", "openReplacementTrackModal"):
            assert name in defined, (
                f"{name} is defined in templates/playlists/importer.html but the "
                "probe did not see it — it is not reading template inline scripts"
            )

    def test_a_guard_with_an_else_is_not_a_silent_noop(self):
        """⭐ The classification that makes this test correct rather than noisy.

        ``if (typeof X === 'function') X(); else toast(...)`` degrades LOUDLY;
        ``if (typeof X === 'function') X();`` with no else does nothing at all.
        Only the latter is the defect class. On the rebuilt search page
        ``addSelectedTrack`` / ``openReplacementTrackModal`` are optional and
        both branches speak to the user, so demanding a definition would be
        wrong about working code.
        """
        base = "if (typeof global.dep === 'function') { global.dep(); }"
        with_else = base + " else { global.toast.warning('nope'); }"
        assert _guard_has_else(base + ";", 0) is False
        assert _guard_has_else(with_else, 0) is True

        # The concrete pair must be classified as LOUD in the rebuilt tree.
        loud = {name for name, has_else in _guard_sites(REPO_ROOT / "test_site") if has_else}
        for name in ("addSelectedTrack", "openReplacementTrackModal"):
            assert name in loud, (
                f"{name} has an else branch in the rebuilt tree, so it is "
                "intentionally optional and must not be reported as a ghost"
            )
            assert name not in _silent_guarded_globals(REPO_ROOT / "test_site")

    def test_the_probe_is_sensitive(self):
        """⭐ Non-vacuousness. A probe that always returned nothing would let
        every assertion below pass while the bug is present.

        Proves all three moving parts: the definition is FOUND, the guarded
        CALL is seen as SILENT, and removing the definition makes the probe
        report it.
        """
        defined = _defined_globals(REPO_ROOT)
        silent = _silent_guarded_globals(REPO_ROOT)

        assert "refreshAlbumPage" in defined, (
            "the probe found no definition for refreshAlbumPage, which the live "
            "tree defines — so the probe is not reading the files"
        )
        assert "refreshAlbumPage" in silent, (
            "the probe did not classify the guarded CALL as a silent no-op — it "
            "is broken"
        )

        # Simulate the defect: without the definition the probe must report it.
        simulated = sorted((silent - (defined - {"refreshAlbumPage"})) - set(_KNOWN_DEAD_GUARDS))
        assert "refreshAlbumPage" in simulated, (
            "removing the definition did not make the probe report a silent "
            "no-op, so the assertions below are vacuous"
        )

    def test_no_guarded_call_is_a_silent_noop_in_the_live_tree(self):
        problems = guard_problems(REPO_ROOT)
        assert not problems, _ghost_message(problems)

    def test_no_guarded_call_is_a_silent_noop_in_the_rebuilt_tree(self):
        problems = guard_problems(REPO_ROOT / "test_site")
        assert not problems, _ghost_message(problems)

    def test_the_register_has_no_stale_entries(self):
        """An entry that is no longer dead must be deleted, so this cannot
        quietly become a blanket suppression of every future ghost."""
        live_dead = _silent_guarded_globals(REPO_ROOT) - _defined_globals(REPO_ROOT)
        rebuilt_dead = (
            _silent_guarded_globals(REPO_ROOT / "test_site")
            - _defined_globals(REPO_ROOT / "test_site")
        )
        stale = sorted(set(_KNOWN_DEAD_GUARDS) - (live_dead | rebuilt_dead))
        assert not stale, (
            "these _KNOWN_DEAD_GUARDS entries now resolve somewhere, so the "
            "guard works and the entry must go:\n"
            + "\n".join(f"    {name}" for name in stale)
        )

    def test_refresh_album_page_is_defined_in_both_trees(self):
        """Direct pin for the concrete defect, so a rename or removal of the
        definition cannot quietly reintroduce the silent no-op."""
        for label, js in (
            ("live", LIVE / "album_detail.js"),
            ("rebuilt", REBUILT / "pages" / "album.js"),
        ):
            source = _strip_js_comments(js.read_text(encoding="utf-8"))
            assert re.search(
                r"(?:window|global)\.refreshAlbumPage\s*=(?!=)|function\s+refreshAlbumPage\s*\(",
                source,
            ), f"the {label} tree calls refreshAlbumPage() but {js.name} defines neither"
            assert not re.search(
                r"(?:window|global)\.refreshAlbumPage\s*=\s*(?:undefined|null)\s*;",
                source,
            ), f"{js.name} appears to stub refreshAlbumPage out"
