"""The manual-search Search button must work on EVERY page that shows it.

Reported
--------
> The button still doesn't work on the test_site.

The click was dead on ``/downloads/monitor`` in test_site mode, and for a
non-obvious reason that earlier probes (including mine) kept missing.

``test_site/templates/components/modals/_soulseek_manual_search.html`` is
deliberately markup-only: it carries **no inline ``onclick``** and relies on
``services/slskd.js`` to bind the button on ``DOMContentLoaded``. That is correct
on the pages that load skld.js — but the monitor PAGE is **shadowed to the live
tree** (``helpers/test_site_mode.py::_SHADOWED_TEMPLATES``), because the rebuilt
copy was a stray artist-page snapshot. So in test_site mode the monitor page was
rendered from ``templates/pages/downloads/monitor.html``, which loads
``static/js/downloads.js`` and **not** ``services/slskd.js``.

Measured before the fix::

    /downloads/monitor  inline onclick=False  loads slskd.js=False

Neither binding path existed, so the click did nothing — silently, because the
page still returned 200.

The trap in testing this
------------------------
Rendering pages with the DEFAULT config inspects the LIVE tree, where the button
works. Every probe must therefore **enable ``features.use_test_site``** first, or
it verifies the wrong tree. That is exactly how this was missed twice.

The fix keeps the partial self-contained: a single inline handler that resolves
``window.runSoulseekManualSearch`` at CLICK time, so it uses whichever
implementation the page actually loaded — ``services/slskd.js`` on the queue
page, ``downloads.js`` on the monitor page — and reports plainly when neither is
present rather than appearing dead.
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

#: The rebuilt partial — the one that had no inline handler.
PARTIAL = REPO_ROOT / "test_site/templates/components/modals/_soulseek_manual_search.html"
LIVE_PARTIAL = REPO_ROOT / "templates/components/modals/_soulseek_manual_search.html"

QUEUE_PAGE = REPO_ROOT / "test_site/templates/Pages/downloads/queue.html"
LIVE_MONITOR_PAGE = REPO_ROOT / "templates/pages/downloads/monitor.html"

SLSKD_JS = REPO_ROOT / "test_site/static/js/services/slskd.js"
BUTTON_STATE_JS = REPO_ROOT / "test_site/static/js/ui/button-state.js"
DOWNLOADS_JS = REPO_ROOT / "static/js/downloads.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to drive the button"
)


def _button_onclick() -> str:
    """The SHIPPED handler body for the Search button.

    The button calls the NAMED ``runSoulseekManualSearchFromModal``, which the
    partial defines in an inline <script>. The test invokes that body directly so
    the click exercises the real shipped code rather than a copy of it.
    """
    body = PARTIAL.read_text(encoding="utf-8")
    anchor = "window.runSoulseekManualSearchFromModal = function"
    start = body.index(anchor)
    brace = body.index("{", start)
    depth = 0
    for i in range(brace, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                # Include the parameter list from `function`.
                fn_start = body.index("function", start)
                return body[fn_start:i + 1]
    raise AssertionError("unbalanced braces in runSoulseekManualSearchFromModal")


# ---------------------------------------------------------------------------
# Node harness: a real click fires the inline handler AND every listener
# ---------------------------------------------------------------------------

_STUB = r"""
const calls = []; const alerts = []; const elements = {};
function makeEl(tag) {
  const el = { tagName: String(tag||'div').toUpperCase(), children: [], attributes: {},
    style:{}, dataset:{}, _cls:new Set(), _html:'', _text:'', value:'', disabled:false,
    classList:{add(c){el._cls.add(c);},remove(c){el._cls.delete(c);},contains(c){return el._cls.has(c);},toggle(){}},
    setAttribute(k,v){el.attributes[k]=String(v);},
    getAttribute(k){return k in el.attributes?el.attributes[k]:null;},
    removeAttribute(k){delete el.attributes[k];},
    appendChild(c){el.children.push(c); c.parentNode=el; return c;},
    insertAdjacentHTML(_p,h){el._html+=String(h);},
    insertAdjacentElement(_p,c){(el.parentNode||el).children.push(c);return c;},
    querySelector(){return null;}, querySelectorAll(){return [];},
    addEventListener(ev,fn){(el._l||={});(el._l[ev]||=[]).push(fn);},
    closest(){return null;}, focus(){}, remove(){},
  };
  Object.defineProperty(el,'innerHTML',{get(){return el._html;},set(v){el._html=String(v);}});
  Object.defineProperty(el,'textContent',{get(){return el._text;},set(v){el._text=String(v);}});
  return el;
}
const document = {
  body: makeEl('body'), listeners: {},
  getElementById(id){ if(!(id in elements)){const e=makeEl('div'); e.attributes.id=id; elements[id]=e;} return elements[id]; },
  querySelector(){return null;}, querySelectorAll(){return [];},
  createElement(t){return makeEl(t);},
  addEventListener(ev,fn){(document.listeners[ev]||=[]).push(fn);},
};
global.window = global;
global.document = document;
global.CSS = { escape: v => String(v) };
global.alert = (m) => { alerts.push(String(m)); };
global.confirm = () => true;
global.setTimeout = setTimeout; global.clearTimeout = clearTimeout;
global.AbortController = class { constructor(){this.signal={};} abort(){} };
global.AbortSignal = { any: () => undefined };
global.bootstrap = { Modal: { getOrCreateInstance: () => ({show(){},hide(){}}) },
                     Tab: { getOrCreateInstance: () => ({show(){}}) } };
global.fetch = (url, opts) => {
  calls.push({ m: (opts && opts.method) || 'GET', url: String(url) });
  return Promise.resolve({ ok: true, status: 200,
    text: () => Promise.resolve(JSON.stringify({ searchId: 'sid-1', results: [], isComplete: true })) });
};
// slskd.js goes through global.api.*, not raw fetch.
global.api = {
  postJson: (url) => { calls.push({ m:'POST', url:String(url) });
                       return Promise.resolve({ searchId:'sid-1', results:[], isComplete:true }); },
  getJson:  (url) => { calls.push({ m:'GET',  url:String(url) });
                       return Promise.resolve({ results:[], isComplete:true, slotFree:true }); },
};
global.poller = { create: ({ onTick }) => ({ start(){ Promise.resolve().then(()=>onTick({stop(){}})); }, stop(){} }) };
// An async rejection is NOT caught by a synchronous try/catch, so an escaping
// error would look exactly like the silent no-op under investigation.
process.on('unhandledRejection', (e) => {
  console.log(JSON.stringify({ __rejection: String((e && e.stack) || e) })); process.exit(1);
});
"""


def _click_count(scripts: list[Path]) -> dict:
    """Load *scripts*, fire a real click, and report the search POSTs made."""
    load = ""
    for index, s in enumerate(scripts):
        # Unique const per script: a repeated name is a SyntaxError that takes
        # the whole harness down (and looks nothing like the bug under test).
        load += ("\nconst _src" + str(index) + " = require('fs').readFileSync("
                 + json.dumps(str(s)) + ", 'utf8');\n(0, eval)(_src" + str(index) + ");\n")
    load += "(document.listeners['DOMContentLoaded']||[]).forEach(f=>f());\n"

    program = (
        _STUB + load
        + "\nconst btn = document.getElementById('soulseekManualSearchBtn');\n"
        + "const inp = document.getElementById('soulseekManualQuery');\n"
        + "inp.value = 'Artist - Track';\n"
        + "const inline = (" + _button_onclick() + ");\n"
        + """
// A real click runs the inline handler AND every registered listener.
inline.call(btn, btn);
(btn._l && btn._l['click'] ? btn._l['click'] : []).forEach(f => f.call(btn));
setTimeout(() => {
  const posts = calls.filter(c => c.m === 'POST' && c.url.indexOf('/api/slskd/search') !== -1);
  console.log(JSON.stringify({
    searchPosts: posts.length,
    alerts,
    status: document.getElementById('soulseekManualStatus')._text,
  }));
  process.exit(0);
}, 500);
"""
    )
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp) / "h.js"
        h.write_text(program, encoding="utf-8")
        proc = subprocess.run(["node", str(h)], capture_output=True, text=True,
                              encoding="utf-8", timeout=90)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ===========================================================================
# 1. The partial must be self-contained
# ===========================================================================

class TestThePartialCarriesItsOwnHandler:

    def test_the_button_has_an_inline_handler(self):
        assert 'onclick="' in PARTIAL.read_text(encoding="utf-8"), (
            "the rebuilt partial's Search button has no inline handler, so any "
            "page that does not load services/slskd.js (the SHADOWED monitor "
            "page) has no way to bind it and the click is dead"
        )

    def test_the_handler_resolves_the_implementation_at_click_time(self):
        handler = _button_onclick()
        assert "window.runSoulseekManualSearch" in handler, (
            "the handler must look the implementation up when CLICKED, not "
            "capture one at render time"
        )

    def test_both_providers_publish_that_name(self):
        """Otherwise the delegate resolves on one page and not the other."""
        for path in (SLSKD_JS, DOWNLOADS_JS):
            body = path.read_text(encoding="utf-8", errors="replace")
            assert "runSoulseekManualSearch" in body, (
                f"{path.relative_to(REPO_ROOT)} does not provide "
                "runSoulseekManualSearch, so the button would be dead on a page "
                "that loads it"
            )

    def test_the_handler_reports_when_no_implementation_loaded(self):
        """A dead button and an explanatory message are different failures."""
        handler = _button_onclick()
        assert "alert(" in handler, (
            "with no implementation available the handler must SAY so; silently "
            "doing nothing is the exact symptom being fixed"
        )


# ===========================================================================
# 2. The two page scenarios actually fire a search
# ===========================================================================

@needs_node
class TestTheButtonFiresOnBothPageKinds:

    def test_queue_page_which_loads_slskd_js(self):
        """The queue page loads services/slskd.js (+ button-state.js)."""
        out = _click_count([BUTTON_STATE_JS, SLSKD_JS])
        assert out["searchPosts"] == 1, (
            f"expected exactly one search POST on the queue page, got "
            f"{out['searchPosts']} (alerts={out['alerts']}) — two bindings must "
            "not both run, and one binding must run"
        )

    def test_monitor_page_which_loads_downloads_js_instead(self):
        """⚠️ The reported failure: the monitor page loads downloads.js only."""
        out = _click_count([DOWNLOADS_JS])
        assert out["searchPosts"] == 1, (
            f"expected exactly one search POST on the monitor page, got "
            f"{out['searchPosts']} (alerts={out['alerts']}) — this is the page "
            "whose button did nothing"
        )

    def test_a_page_with_neither_script_tells_the_user(self):
        out = _click_count([])
        assert out["searchPosts"] == 0
        assert out["alerts"], (
            "with no search implementation loaded the button must explain "
            "itself rather than appearing dead"
        )


# ===========================================================================
# 3. The premise: the monitor PAGE is shadowed, and loads only downloads.js
# ===========================================================================

class TestTheShadowingPremiseHolds:
    """If this ever changes, the delegate can be removed — so pin it."""

    def test_the_monitor_page_is_shadowed_to_live(self):
        from helpers import test_site_mode

        shadowed = {s.replace("\\", "/") for s in test_site_mode._SHADOWED_TEMPLATES}
        assert "pages/downloads/monitor.html" in shadowed, (
            "the rebuilt monitor page is no longer shadowed, so the monitor page "
            "now comes from test_site/ — re-check whether the modal still needs "
            "its own binding fallback"
        )

    def test_the_live_monitor_page_does_not_load_slskd_js(self):
        body = LIVE_MONITOR_PAGE.read_text(encoding="utf-8", errors="replace")
        assert "slskd.js" not in body, (
            "the live monitor page now loads skld.js, which would bind the "
            "button itself — the delegate is then redundant"
        )

    def test_the_live_monitor_page_does_load_downloads_js(self):
        body = LIVE_MONITOR_PAGE.read_text(encoding="utf-8", errors="replace")
        assert "js/downloads.js" in body, (
            "the monitor page must load the script that provides "
            "runSoulseekManualSearch, or the button is dead there"
        )

    def test_the_queue_page_still_loads_slskd_js(self):
        body = QUEUE_PAGE.read_text(encoding="utf-8", errors="replace")
        assert "services/slskd.js" in body

    def test_both_partials_keep_the_same_element_ids(self):
        """The delegate is markup-level, so the ids slskd.js reads must not drift."""
        rebuilt = PARTIAL.read_text(encoding="utf-8")
        for element_id in (
            "soulseekManualSearchModal",
            "soulseekManualQuery",
            "soulseekManualSearchBtn",
            "soulseekManualStatus",
            "soulseekManualResults",
        ):
            assert f'id="{element_id}"' in rebuilt, f"#{element_id} missing"
