"""Missing tracks must be LISTED on the album page, not just counted.

Reported
--------
> It currently flags missing tracks too, but doesn't show them on the album page
> to select to download them

The count was real and accurate; the list was simply never rendered.

* ``missing_album_tracks`` holds the per-album missing set, and the SCAN
  refreshes it — but the only thing that ever RENDERED it was
  ``_injectMissingTrackRows``, which runs exclusively from the manual
  "Compare with MusicBrainz" result. So the artist page advertised "3 missing"
  on a row, the user opened the album to download them, and the album page
  listed nothing to click.
* ``#albumMissingHeaderBadge`` already existed in BOTH album templates and no
  script had ever populated it — a dead element, which is what the count would
  have gone into.
* ``GET /api/album/missing-tracks`` already served the persisted list. The album
  page simply never called it.

Two things are proven here, and they are different in kind:

1. **The loader behaves** — exercised in Node against the REAL page scripts
   (both trees), so the assertions are about rendered rows rather than about
   source text that merely looks right.
2. **The wiring exists** — the endpoint is called on page load, and the row
   builder is the SAME one the Compare path uses, so the per-row controls
   cannot drift between the two.

A source-level pin accompanies each behavioural one: the behaviour tests would
still pass if the loader were never CALLED, which is exactly the defect class
being fixed (working code that nothing invokes).
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
LIVE_STATIC = REPO_ROOT / "static" / "js"
REBUILT_STATIC = REPO_ROOT / "test_site" / "static" / "js"
LIVE_TEMPLATES = REPO_ROOT / "templates"
REBUILT_TEMPLATES = REPO_ROOT / "test_site" / "templates"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS"
)


# ---------------------------------------------------------------------------
# DOM stub — only the slice the loader touches
# ---------------------------------------------------------------------------

_DOM_STUB = r"""
const calls = { fetch: [], listeners: {} };
const elements = {};

function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    attributes: {},
    dataset: {},
    style: {},
    _classes: new Set(),
    _text: '',
    _html: '',
    disabled: false,
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
      toggle(c, on) { if (on) el._classes.add(c); else el._classes.delete(c); },
    },
    setAttribute(k, v) { el.attributes[k] = String(v); },
    getAttribute(k) { return k in el.attributes ? el.attributes[k] : null; },
    removeAttribute(k) { delete el.attributes[k]; },
    appendChild(child) { el.children.push(child); child.parentNode = el; return child; },
    insertAdjacentElement(_pos, child) {
      // ⚠️ Inserts into the PARENT, not into `el`. The page does
      //     lastTrackRow.insertAdjacentElement('afterend', missingRow)
      // so appending to `el` would bury every missing row INSIDE the last track
      // row — leaving the tbody looking empty to every query.
      const parent = el.parentNode || el;
      parent.children.push(child);
      child.parentNode = parent;
      return child;
    },
    querySelector(sel) { return queryAll(sel, el)[0] || null; },
    querySelectorAll(sel) { return queryAll(sel, el); },
    addEventListener(ev, fn) { (el._listeners ||= {})[ev] = fn; },
    remove() { el._removed = true; },
    closest() { return null; },
    focus() {},
  };

  // ⚠️ innerHTML must POPULATE children. The page builds its rows by assigning
  // an HTML string and then querying the buttons inside it:
  //     row.innerHTML = `...<button class="mb-queue-missing">...`;
  //     row.querySelector('.mb-queue-missing').addEventListener(...)
  // A stub that only stores the string makes that querySelector return null and
  // the script throws — which would look like a bug in the PAGE rather than a
  // gap in the harness.
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html; },
    set(value) {
      el._html = String(value);
      el.children = parseHtml(el._html);
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return el._text; },
    set(value) { el._text = String(value); },
  });
  // The page sets `row.className = 'text-muted missing-track-row mb-missing-row'`
  // directly (not via classList), so this must feed the same class set or the
  // row would be invisible to every class-based query in this harness.
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el._classes).join(' '); },
    set(value) {
      el._classes = new Set(String(value).split(/\s+/).filter(Boolean));
    },
  });
  return el;
}

/** Extract elements from an HTML string: tag + class + data-* attributes. */
function parseHtml(html) {
  const out = [];
  const tagRe = /<(\w+)([^>]*)>/g;
  let m;
  while ((m = tagRe.exec(html))) {
    const el = makeEl(m[1]);
    const attrs = m[2] || '';
    const cls = /class\s*=\s*"([^"]*)"/.exec(attrs);
    if (cls) cls[1].split(/\s+/).forEach((c) => { if (c) el._classes.add(c); });
    const attrRe = /([\w-]+)\s*=\s*"([^"]*)"/g;
    let am;
    while ((am = attrRe.exec(attrs))) {
      el.attributes[am[1]] = am[2];
      if (am[1].startsWith('data-')) el.dataset[am[1].slice(5)] = am[2];
    }
    out.push(el);
  }
  return out;
}

function descendants(root) {
  const out = [];
  (root.children || []).forEach((c) => { out.push(c); out.push(...descendants(c)); });
  return out;
}

function matches(el, sel) {
  const parts = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
  return parts.some((p) => {
    if (p.startsWith('tr[data-track-id]')) return el.tagName === 'TR' && 'data-track-id' in el.attributes;
    if (p === '.mb-missing-row') return el._classes.has('mb-missing-row');
    if (p === '.mb-update-row') return el._classes.has('mb-update-row');
    if (p.startsWith('.')) return el._classes.has(p.slice(1));
    return el.tagName === p.toUpperCase();
  });
}

function queryAll(sel, root) {
  return descendants(root || document.body).filter((el) => matches(el, sel));
}

const document = {
  body: makeEl('body'),
  listeners: {},
  getElementById(id) {
    if (!(id in elements)) {
      const el = makeEl('div');
      el.attributes.id = id;
      // Model a real input: `_getLinkedReleaseMbid` reads `.value` and calls
      // `.trim()` on it, which a bare stub element would not support.
      el.value = '';
      elements[id] = el;
    }
    return elements[id];
  },
  querySelector(sel) { return queryAll(sel, document.body)[0] || null; },
  querySelectorAll(sel) { return queryAll(sel, document.body); },
  createElement(tag) { return makeEl(tag); },
  addEventListener(ev, fn) { document.listeners[ev] = fn; },
};

global.document = document;
global.CSS = { escape: (v) => String(v) };
// The page scripts are IIFEs invoked with `window`, so alias it — Node has no
// global `window`, and without this the script throws ReferenceError at its
// closing `})(window);` and NOTHING inside it is ever evaluated.
global.window = global;
global.fetch = function (url, opts) {
  calls.fetch.push({ url: String(url), opts: opts || {} });
  return global.__RESPOND(url);
};
global.__RESPOND = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
global.confirm = () => true;
global.alert = () => {};
global.escapeHtml = (v) => String(v == null ? '' : v);
global.ui = { confirm: async () => true };
global.toast = { success: () => {}, error: () => {}, info: () => {} };

// ⚠️ `setTimeout` is deliberately NOT stubbed. An immediate, synchronous
// version would run the test's assertions BEFORE the loader's promise settles
// (so every row count reads 0) and, worse, a try/catch inside it would SWALLOW
// the resulting error — turning a genuine failure into a passing empty result.
// The real timer keeps the async ordering the page actually has.

// Surface async failures. Without this an unhandled rejection in the loader
// just leaves zero rows, and a "renders nothing" assertion would PASS on a
// broken loader — the worst possible outcome for these tests.
process.on('unhandledRejection', (err) => {
  console.log(JSON.stringify({ __rejection: String((err && err.stack) || err) }));
  process.exit(1);
});

// ── Load the page script under test ─────────────────────────────────────
const MODULE = __MODULE_PATH__;
const source = require('fs').readFileSync(MODULE, 'utf8');
// The page scripts are IIFEs that expect window/global wiring; evaluate them
// with `global` as the enclosing scope.
//
// ⚠️ The load event is NOT fired here. The test must be able to install its
// `_pageData` / response stub FIRST — firing it at load time means the
// initialisers run against an empty page and every assertion below would be
// measuring nothing.
(function () { eval(source); })();
global.__fireLoad = function () {
  if (document.listeners['DOMContentLoaded']) document.listeners['DOMContentLoaded']();
  if (global.listeners && global.listeners['DOMContentLoaded']) global.listeners['DOMContentLoaded']();
};
"""


def _run(script: str, module: Path) -> dict:
    """Run *script* against the REAL page module in Node; return its JSON.

    The program goes into a temp FILE rather than ``node -e``: with ``-e`` Node
    shifts ``process.argv``, so a path passed as an argument lands in
    ``argv[1]`` and a ``require`` of ``argv[2]`` receives ``undefined``.
    """
    program = _DOM_STUB.replace("__MODULE_PATH__", json.dumps(str(module))) + "\n" + script
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(program, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(harness)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: The endpoint every tree must call on load.
MISSING_ENDPOINT = "/api/album/missing-tracks"

_MISSING_PAYLOAD = {
    "missing_tracks": [
        {"title": "Track One", "track_number": "1", "disc_number": 1,
         "recording_mbid": "rec-1", "duration": 180, "year": "2026",
         "release_id": "rel-1", "track_artist": "Some Artist"},
        {"title": "Track Two", "track_number": "2", "disc_number": 1,
         "recording_mbid": "rec-2", "duration": 200, "year": "2026",
         "release_id": "rel-1", "track_artist": "Some Artist"},
    ],
    "missing_count": 2,
}


# ===========================================================================
# 1. The LIVE tree
# ===========================================================================

class TestAlbumDetailJsLoadsMissingTracks:
    MODULE = LIVE_STATIC / "album_detail.js"

    def _load(self, payload=_MISSING_PAYLOAD):
        return _run(
            f"""
            const tbody = document.getElementById('albumTracksTbody');
            const real = document.createElement('tr');
            real.setAttribute('data-track-id', 'real-1');
            tbody.appendChild(real);
            // The real template renders the badge HIDDEN (class="d-none"), so
            // model that: otherwise "the badge is shown" would be true before
            // the loader runs and the assertion would prove nothing.
            document.getElementById('albumMissingHeaderBadge').classList.add('d-none');

            global.__RESPOND = () => Promise.resolve({{
              ok: true, json: () => Promise.resolve({json.dumps(payload)}),
            }});
            global._pageData = {{ artistName: 'Test Artist', albumName: 'Test Album' }};

            // State is installed, so NOW let the page's initialisers run.
            global.__fireLoad();

            setTimeout(() => {{
              const rows = tbody.querySelectorAll('.mb-missing-row');
              const badge = document.getElementById('albumMissingHeaderBadge');
              console.log(JSON.stringify({{
                fetchCount: calls.fetch.length,
                urls: calls.fetch.map((c) => c.url),
                rowCount: rows.length,
                titles: rows.map((r) => r.innerHTML),
                badgeShown: !badge._classes.has('d-none'),
                badgeText: badge.textContent,
              }}));
            }}, 0);
            """,
            self.MODULE,
        )

    def test_it_calls_the_missing_tracks_endpoint_on_load(self):
        out = self._load()
        assert out["fetchCount"] >= 1, (
            "loadAlbumMissingTracks did not run — the album page still never "
            "asks for its persisted missing tracks"
        )
        assert any(MISSING_ENDPOINT in u for u in out["urls"]), out["urls"]

    def test_the_endpoint_is_asked_for_this_artist_and_album(self):
        urls = self._load()["urls"]
        target = next(u for u in urls if MISSING_ENDPOINT in u)
        assert "artist=Test%20Artist" in target
        assert "album=Test%20Album" in target

    def test_every_missing_track_gets_a_row(self):
        out = self._load()
        assert out["rowCount"] == 2, (
            "the flagged missing tracks are still not rendered as selectable rows"
        )

    def test_the_rendered_rows_carry_the_titles(self):
        out = self._load()
        joined = " ".join(out["titles"])
        assert "Track One" in joined and "Track Two" in joined

    def test_the_header_badge_is_populated(self):
        """#albumMissingHeaderBadge existed in both templates, always empty."""
        out = self._load()
        assert out["badgeShown"] is True
        assert "2" in out["badgeText"] and "missing" in out["badgeText"]

    def test_an_empty_list_renders_nothing_and_hides_the_badge(self):
        out = self._load({"missing_tracks": [], "missing_count": 0})
        assert out["rowCount"] == 0
        assert out["badgeShown"] is False

    def test_the_rows_include_a_queue_control(self):
        """The whole point: the tracks must be SELECTABLE to download."""
        out = self._load()
        assert all("queueMissingTrack" in t for t in out["titles"])

    def test_a_duplicate_position_and_title_renders_once(self):
        dup = {
            "missing_tracks": [
                {"title": "Same", "track_number": "1", "disc_number": 1},
                {"title": "Same", "track_number": "1", "disc_number": 1},
            ],
        }
        assert self._load(dup)["rowCount"] == 1


# ===========================================================================
# 2. The REBUILT tree
# ===========================================================================

class TestRebuiltAlbumPageLoadsMissingTracks:
    MODULE = REBUILT_STATIC / "pages" / "album.js"

    def _load(self, payload=_MISSING_PAYLOAD):
        return _run(
            f"""
            const tbody = document.getElementById('albumTracksTbody');
            const real = document.createElement('tr');
            real.setAttribute('data-track-id', 'real-1');
            tbody.appendChild(real);
            // The real template renders the badge HIDDEN (class="d-none").
            document.getElementById('albumMissingHeaderBadge').classList.add('d-none');

            global.__RESPOND = () => Promise.resolve({{
              ok: true, json: () => Promise.resolve({json.dumps(payload)}),
            }});
            global._pageData = {{ artistName: 'Test Artist', albumName: 'Test Album' }};
            global.api = {{
              getJson: (url) => {{ calls.fetch.push({{ url: String(url) }});
                                   return global.__RESPOND(url).then((r) => r.json()); }},
            }};

            // State is installed, so NOW let the page's initialisers run.
            global.__fireLoad();

            setTimeout(() => {{
              const rows = tbody.querySelectorAll('.mb-missing-row');
              const badge = document.getElementById('albumMissingHeaderBadge');
              console.log(JSON.stringify({{
                fetchCount: calls.fetch.length,
                urls: calls.fetch.map((c) => c.url),
                rowCount: rows.length,
                html: rows.map((r) => r.innerHTML),
                badgeShown: !badge._classes.has('d-none'),
                badgeText: badge.textContent,
              }}));
            }}, 0);
            """,
            self.MODULE,
        )

    def test_it_calls_the_missing_tracks_endpoint_on_load(self):
        out = self._load()
        assert any(MISSING_ENDPOINT in u for u in out["urls"]), out["urls"]

    def test_every_missing_track_gets_a_row(self):
        assert self._load()["rowCount"] == 2

    def test_the_header_badge_is_populated(self):
        out = self._load()
        assert out["badgeShown"] is True
        assert "2" in out["badgeText"]

    def test_the_rows_include_a_queue_control(self):
        out = self._load()
        assert all("mb-queue-missing" in h for h in out["html"])

    def test_an_empty_list_renders_nothing(self):
        out = self._load({"missing_tracks": [], "missing_count": 0})
        assert out["rowCount"] == 0
        assert out["badgeShown"] is False


# ===========================================================================
# 3. Wiring — the loader must actually be CALLED
# ===========================================================================
#
# The behaviour tests above invoke the loader directly, so they would pass even
# if nothing ever called it — which is precisely the defect being fixed (a
# working code path nothing reached).

class TestTheLoaderIsWiredToPageLoad:

    def _album_js(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    def test_live_tree_wires_it(self):
        src = self._album_js(LIVE_STATIC / "album_detail.js")
        assert "window.loadAlbumMissingTracks = function" in src
        assert re.search(
            r"DOMContentLoaded[\s\S]{0,400}loadAlbumMissingTracks\(\)", src
        ), "loadAlbumMissingTracks is defined but never called on page load"

    def test_rebuilt_tree_wires_it(self):
        src = self._album_js(REBUILT_STATIC / "pages" / "album.js")
        assert "async function loadAlbumMissingTracks" in src
        assert re.search(
            r"DOMContentLoaded[\s\S]{0,400}loadAlbumMissingTracks\(\)", src
        ), "loadAlbumMissingTracks is defined but never called on page load"

    @pytest.mark.parametrize(
        "live_rel,rebuilt_rel",
        [
            # ⚠️ The rebuilt tree capitalises this directory ("Pages"), and the
            # two trees genuinely differ. Path literals must match the real
            # casing or these would pass on Windows (case-insensitive) and fail
            # on Linux CI.
            ("pages/album_detail.html", "Pages/album_detail.html"),
        ],
    )
    def test_both_trees_carry_the_header_badge_the_loader_fills(
        self, live_rel: str, rebuilt_rel: str
    ):
        """The loader writes to an id the template must actually render."""
        checked = 0
        for root, rel in ((LIVE_TEMPLATES, live_rel), (REBUILT_TEMPLATES, rebuilt_rel)):
            path = root / rel
            assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
            body = path.read_text(encoding="utf-8", errors="replace")
            assert 'id="albumMissingHeaderBadge"' in body, (
                f"{path.relative_to(REPO_ROOT)} has no #albumMissingHeaderBadge, "
                "so the missing-track count has nowhere to render"
            )
            checked += 1
        assert checked == 2, "both album templates must be checked"


# ===========================================================================
# 4. The loader reuses the Compare path's row builder
# ===========================================================================

class TestTheLoaderReusesTheExistingRowBuilder:
    """One row builder, so per-row controls cannot drift between the two paths.

    A second, hand-written row here is exactly how the "Download Missing
    Tracks" button and the per-row queue button drifted apart before.
    """

    def test_live_tree_reuses_the_builders(self):
        src = (LIVE_STATIC / "album_detail.js").read_text(encoding="utf-8")
        loader = src[src.index("window.loadAlbumMissingTracks"):]
        loader = loader[: loader.index("\n};")] if "\n};" in loader else loader
        assert "_buildMissingTrackRow(" in loader, (
            "the loader must reuse _buildMissingTrackRow, not hand-roll a row"
        )
        assert "ignoreMissingTrack" not in loader or "onclick=" not in loader

    def test_rebuilt_tree_reuses_the_builders(self):
        src = (REBUILT_STATIC / "pages" / "album.js").read_text(encoding="utf-8")
        loader = src[src.index("async function loadAlbumMissingTracks"):]
        loader = loader[: loader.index("\n  }")] if "\n  }" in loader else loader
        assert "buildMissingRow(" in loader, (
            "the loader must reuse buildMissingRow, not hand-roll a row"
        )
