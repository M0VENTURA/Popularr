"""Missing tracks must line up with the album table — same cells, track order.

Reported
--------
> The lookup mbid on the album page isn't always populating the missing tracks
> inline with the rest of the album matched by track number order.
>
> It should be with the same fields as the other tracks such as duration. So
> it's lined up with the other tracks on the table.

Two independent defects:

1. **The row had SIX cells for a FIVE column table.** The header is
   ``# | Title | Duration | Rating | Actions``; the missing row emitted an
   empty ``#`` cell, then the number (so it rendered under *Title*), then a
   ``colspan=3`` title (so the title rendered under *Duration*), then the
   buttons as a sixth cell hanging outside the table. Duration was never
   displayed at all — it only rode along as a raw millisecond ``data-duration``
   attribute on the queue button.

2. **Placement ignored track order.** The Compare path walked backwards
   through ``data.comparison`` (MusicBrainz *tracklist* order) looking for the
   previous matched entry, and the page-load path appended every persisted
   missing row after the *last* track. Neither consulted the numbers the table
   is actually sorted by.

Both entry points now share one placer keyed on ``disc × 10000 + track number``,
and both trees carry the same row.

The **order key is a plain number on purpose** so the placement decision is
assertable without a DOM, while the DOM assertions below still prove the rows
land where they are placed.
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
LIVE_JS = REPO_ROOT / "static" / "js" / "album_detail.js"
REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
LIVE_TEMPLATE = REPO_ROOT / "templates" / "pages" / "album_detail.html"
REBUILT_TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS"
)

# The exact shape the header defines: # , Title , Duration , Rating , Actions.
EXPECTED_COLUMNS = 5


# ---------------------------------------------------------------------------
# DOM stub — only the slice the row builder and the placer touch.
# insertAdjacentElement is POSITION-AWARE, which is the whole point: the
# existing shared harness appends into the parent, which cannot express
# "insert before this row" and would make every ordering assertion vacuous.
# ---------------------------------------------------------------------------
_DOM = r"""
function detach(child) {
  if (child.parentNode) {
    const kids = child.parentNode.children;
    const i = kids.indexOf(child);
    if (i >= 0) kids.splice(i, 1);
    child.parentNode = null;
  }
}

function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    attributes: {},
    dataset: {},
    style: {},
    children: [],
    parentNode: null,
    _classes: new Set(),
    _html: '',
    _text: '',
    disabled: false,
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
      toggle(c, on) { if (on) el._classes.add(c); else el._classes.delete(c); },
    },
    setAttribute(k, v) {
      el.attributes[k] = String(v);
      if (k.startsWith('data-')) el.dataset[k.slice(5)] = String(v);
    },
    getAttribute(k) { return k in el.attributes ? el.attributes[k] : null; },
    removeAttribute(k) { delete el.attributes[k]; },
    appendChild(child) { detach(child); el.children.push(child); child.parentNode = el; return child; },
    insertAdjacentElement(position, child) {
      detach(child);
      if (position === 'beforebegin') {
        const parent = el.parentNode;
        if (!parent) { el.appendChild(child); return child; }
        parent.children.splice(parent.children.indexOf(el), 0, child);
        child.parentNode = parent;
      } else if (position === 'afterend') {
        const parent = el.parentNode;
        if (!parent) { el.appendChild(child); return child; }
        parent.children.splice(parent.children.indexOf(el) + 1, 0, child);
        child.parentNode = parent;
      } else {
        el.children.push(child);
        child.parentNode = el;
      }
      return child;
    },
    querySelector(sel) { return queryAll(sel, el)[0] || null; },
    querySelectorAll(sel) { return queryAll(sel, el); },
    addEventListener() {},
    remove() { detach(el); el._removed = true; },
    closest() { return null; },
    focus() {},
  };

  Object.defineProperty(el, 'nextElementSibling', {
    get() {
      if (!el.parentNode) return null;
      const kids = el.parentNode.children;
      const i = kids.indexOf(el);
      return i >= 0 && i + 1 < kids.length ? kids[i + 1] : null;
    },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html; },
    set(value) { el._html = String(value); el.children = parseHtml(el._html); },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return el._text; },
    set(value) { el._text = String(value); },
  });
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el._classes).join(' '); },
    set(value) { el._classes = new Set(String(value).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

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
  return String(sel).split(',').map((s) => s.trim()).some((p) => {
    if (p === 'tr[data-track-id]') return el.tagName === 'TR' && 'data-track-id' in el.attributes;
    if (p.startsWith('.')) return el._classes.has(p.slice(1));
    if (p.startsWith('[')) return true;
    return el.tagName === p.toUpperCase();
  });
}

function queryAll(sel, root) {
  return descendants(root || document.body).filter((el) => matches(el, sel));
}

const elements = {};
const document = {
  body: makeEl('body'),
  listeners: {},
  getElementById(id) {
    if (!(id in elements)) {
      const el = makeEl(id === 'albumTracksTbody' ? 'tbody' : 'div');
      el.attributes.id = id;
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
global.window = global;
global.CSS = { escape: (v) => String(v) };
global.escapeHtml = (v) => String(v == null ? '' : v);
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
global.confirm = () => true;
global.alert = () => {};
global.toast = { success() {}, error() {}, info() {} };
global.ui = { confirm: async () => true };
"""

# Probe shared by both trees; __BUILD__/__INJECT__/__KEY__ are the tree's names.
_PROBE = r"""
const tbody = document.getElementById('albumTracksTbody');
function libRow(n, disc) {
  const tr = document.createElement('tr');
  tr.setAttribute('data-track-id', 'lib-' + n);
  tr.dataset.trackNumber = String(n);
  tr.dataset.discNumber = String(disc);
  tbody.appendChild(tr);
  return tr;
}
libRow(1, 1); libRow(3, 1); libRow(5, 1);

const data = { comparison: [], mb_year: '2006', release_mbid: 'rel-1' };
// Deliberately OUT of order: the comparison array never matches the table.
const missing = [
  { matched: false, mb_title: 'Four', mb_track_number: 4, mb_disc_number: 1,
    mb_recording_mbid: 'r4', mb_duration: 240000 },
  { matched: false, mb_title: 'Two', mb_track_number: 2, mb_disc_number: 1,
    mb_recording_mbid: 'r2', mb_duration: 180000 },
  { matched: false, mb_title: 'Nine', mb_track_number: 9, mb_disc_number: 1,
    mb_recording_mbid: 'r9', mb_duration: 300000 },
];

__INJECT__(missing, data);

const order = tbody.children.map((c) => c.dataset.trackNumber || '?');

const built = __BUILD__(missing[2], data);
const html = built.innerHTML;
const cells = (html.match(/<td[\s>]/g) || []).length;
const parts = html.split(/<\/td>/);

console.log(JSON.stringify({
  order: order,
  cells: cells,
  hasColspan: /colspan/i.test(html),
  numberCell: parts[0] || '',
  titleCell: parts[1] || '',
  durationCell: parts[2] || '',
  keys: {
    d1n2: __KEY__(1, 2),
    d1n3: __KEY__(1, 3),
    d1n7: __KEY__(1, 7),
    d1n9: __KEY__(1, 9),
    d2n1: __KEY__(2, 1),
    noNumber: __KEY__(1, null),
    badDisc: __KEY__(null, 7),
  },
}));
"""


def _probe(module: Path, *, strip_iife: bool, build: str, inject: str, key: str) -> dict:
    source = module.read_text(encoding="utf-8").rstrip()
    if strip_iife:
        assert source.endswith("})(window);"), f"{module} no longer ends with the IIFE close"
        body = source[: -len("})(window);")]
        program = _DOM + "\n" + body + "\n" + (
            _PROBE.replace("__BUILD__", build)
            .replace("__INJECT__", inject)
            .replace("__KEY__", key)
        ) + "\n})(window);\n"
    else:
        program = _DOM + "\n" + source + "\n" + (
            _PROBE.replace("__BUILD__", build)
            .replace("__INJECT__", inject)
            .replace("__KEY__", key)
        )

    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(program, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(harness)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, f"probe produced no JSON:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[-1])


def _live() -> dict:
    return _probe(
        LIVE_JS, strip_iife=False,
        build="_buildMissingTrackRow", inject="_injectMissingTrackRows", key="_missingOrderKey",
    )


def _rebuilt() -> dict:
    return _probe(
        REBUILT_JS, strip_iife=True,
        build="buildMissingRow", inject="injectMissingRows", key="missingOrderKey",
    )


# ===========================================================================
# 1. The row must have the table's columns, and carry duration
# ===========================================================================
class TestTheMissingRowHasTheTablesColumns:
    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_it_has_exactly_five_cells(self, run):
        assert run()["cells"] == EXPECTED_COLUMNS, (
            "the missing row must emit one cell per <th>; six cells shift every "
            "column after the first and push the action buttons outside the table"
        )

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_no_cell_spans_several_columns(self, run):
        assert run()["hasColspan"] is False, (
            "a colspan on the title is what put the title in the Duration column"
        )

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_number_is_in_the_first_cell(self, run):
        assert re.search(r"\b9\b", run()["numberCell"]), (
            "the track number belongs in the '#' column"
        )

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_title_is_in_the_second_cell(self, run):
        cell = run()["titleCell"]
        assert "Nine" in cell and "Missing" in cell, cell

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_duration_is_shown_in_the_duration_column(self, run):
        """The reported missing field: ``mb_duration`` is milliseconds."""
        cell = run()["durationCell"]
        assert "5:00" in cell, (
            f"the Duration cell reads {cell!r}; MusicBrainz length is in "
            "milliseconds and must render m:ss like the library rows do"
        )


# ===========================================================================
# 2. Rows must be placed by track number, from both entry points
# ===========================================================================
class TestMissingRowsArePlacedByTrackNumber:
    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_rows_interleave_in_track_order(self, run):
        """Library 1,3,5 + missing 4,2,9 -> 1,2,3,4,5,9."""
        assert run()["order"] == ["1", "2", "3", "4", "5", "9"], (
            "missing tracks were placed by comparison position instead of by "
            "the track number the table is sorted on"
        )

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_order_key_puts_disc_2_after_disc_1(self, run):
        keys = run()["keys"]
        assert keys["d1n2"] < keys["d1n3"] < keys["d1n9"]
        assert keys["d2n1"] > keys["d1n9"], "disc must sort before track number"

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_a_track_with_no_number_sorts_last_rather_than_first(self, run):
        keys = run()["keys"]
        assert keys["noNumber"] > keys["d1n9"], (
            "an unknown number must not jump to the top of the table"
        )

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_an_unknown_disc_defaults_to_disc_one(self, run):
        """Matches the template's ``track.disc_number or 1``."""
        keys = run()["keys"]
        assert keys["badDisc"] == keys["d1n7"], keys


# ===========================================================================
# 3. Source pins — the table must expose what the placer reads
# ===========================================================================
class TestTheTableExposesOrderingKeys:
    @pytest.mark.parametrize("template", [LIVE_TEMPLATE, REBUILT_TEMPLATE])
    def test_library_rows_carry_track_and_disc_numbers(self, template):
        html = template.read_text(encoding="utf-8")
        assert 'data-track-number="{{ track.track_number or loop.index }}"' in html, (
            f"{template.name} does not publish the ordering key the placer reads"
        )
        assert 'data-disc-number="{{ track.disc_number or 1 }}"' in html

    @pytest.mark.parametrize("template", [LIVE_TEMPLATE, REBUILT_TEMPLATE])
    def test_the_tracks_header_is_five_columns(self, template):
        """Guards the cell count the missing row must match."""
        html = template.read_text(encoding="utf-8")
        header = re.search(r"<thead>(.*?)</thead>", html, re.S)
        assert header, "no tracks <thead>"
        assert header.group(1).count("<th") == EXPECTED_COLUMNS
