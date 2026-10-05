"""An undelivered track must be VISIBLE (with its queue state), not swallowed.

Reported
--------
> The issue with the missing tracks is still outstanding.

The album page's Lookup MBID recomputes the list through four gates. Two of
them could remove a track with nothing on screen to explain it:

* **queue** — any queue row in ``queued``/``searching``/``downloading`` hid the
  track *permanently*. The user's queue stalls (multi-minute searches, a
  transfer stuck at ``progress=0``), so a track they never received simply
  stopped appearing: *"missing tracks 10 and 11 are still outstanding"*.
* **rejected** — ``ignored = TRUE`` is set by the reject button and **nothing
  in the codebase ever sets it back**, so a dismissed pair disappears for good.

MusicBrainz ground truth for the reported release
(``0f1ae6af``, *The Power and the Passion: A Tribute to Midnight Oil*) is a
13-track edition whose tracks 10/11 are *Forgotten Years* and *Power and the
Passion* — both with normal titles — and the pasted table shows library files
numbered 1-9, 12, 13 only. No library row occupies 10/11, so ``in_library``
cannot be what hid them; it had to be a gate with no visibility.

Now:

* a not-yet-delivered track is **returned with ``queue_status``** and renders a
  blue ``Queued`` badge with the download button disabled — listed, honest, and
  not re-queueable by accident;
* a DELIVERED row (``imported``/``completed``/``matched``/``in_collection``)
  is still hidden — it is in the library;
* the header badge gains **"· N not shown"** so a withheld track can never look
  like a release that never had it.

The DOM assertions run the real functions through node (same harness style as
``test_album_missing_rows_line_up``), so a comment quoting the badge markup
cannot satisfy them.
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

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS"
)

# ---------------------------------------------------------------------------
# Minimal DOM — enough for the row builder and the header badge.
# ---------------------------------------------------------------------------
_DOM = r"""
function makeEl(tag) {
  const el = {
    tagName: tag, children: [], style: {}, dataset: {}, attrs: {},
    className: '', _html: '', textContent: '', title: '',
    classList: { add() {}, remove() {}, contains() { return false; },
                 toggle() { return false; } },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); },
    addEventListener() {},
    querySelector() { return { addEventListener() {} }; },
    querySelectorAll() { return []; },
    insertAdjacentElement() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = v; },
  });
  return el;
}
const store = {};
store['albumMissingHeaderBadge'] = makeEl('span');
store['.album-search-missing-btn'] = makeEl('button');
global.document = {
  getElementById(id) { return store[id] || null; },
  querySelector(sel) { return store[sel] || null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
  // Both trees register top-level listeners while the module loads.
  addEventListener() {},
  removeEventListener() {},
  body: makeEl('body'),
};
global.window = global;
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.CSS = { escape: (v) => String(v) };
global.escapeHtml = (v) => String(v == null ? '' : v);
global.esc = (v) => String(v == null ? '' : v);
global.pageArtist = () => 'Various Artists';
global.pageAlbum = () => 'Tribute';
global.queueMissingTrack = () => {};
global.openMatchModal = () => {};
global.openAlbumMatchModal = () => {};
global.confirm = () => true;
"""

# __BUILD__ / __BADGE__ / __CONVERT__ are substituted per tree.
_PROBE = r"""
const data = { mb_year: '2001', release_mbid: 'rel-1' };
const base = {
  matched: false, mb_disc_number: 1, mb_recording_mbid: 'rec',
  mb_duration: 240000, mb_track_number: 10,
};
const queuedComp = Object.assign({}, base, {
  mb_title: 'Forgotten Years', queue_status: 'queued',
});
const plainComp = Object.assign({}, base, {
  mb_title: 'Power and the Passion', mb_track_number: 11,
});

const queuedRow = __BUILD__(queuedComp, data);
const plainRow = __BUILD__(plainComp, data);

function firstButton(html) {
  const m = html.match(/<button[^>]*mb-queue-missing[\s\S]*?<\/button>/i)
    || html.match(/<button[^>]*btn-outline-success[\s\S]*?<\/button>/i);
  return m ? m[0] : '';
}
function titleCell(html) {
  const parts = html.split(/<\/td>/);
  return parts[1] || '';
}

// The converter must carry the queue state from the response into the row.
const converted = __CONVERT__({ title: 'Forgotten Years', track_number: 10,
                               disc_number: 1, queue_status: 'searching' });

// Header badge: count + whatever the gates withheld.
__BADGE__(2, { in_library: 1, queued: 0, rejected: 1 });
const withExcluded = {
  text: document.getElementById('albumMissingHeaderBadge').textContent,
  title: document.getElementById('albumMissingHeaderBadge').title,
};
__BADGE__(0, { rejected: 2 });
const onlyExcluded = document.getElementById('albumMissingHeaderBadge').textContent;
__BADGE__(0, {});
const cleared = document.getElementById('albumMissingHeaderBadge').textContent;

console.log(JSON.stringify({
  queuedTitle: titleCell(queuedRow.innerHTML),
  plainTitle: titleCell(plainRow.innerHTML),
  queuedBtn: firstButton(queuedRow.innerHTML),
  plainBtn: firstButton(plainRow.innerHTML),
  convertedStatus: converted && converted.queue_status,
  withExcluded: withExcluded,
  onlyExcluded: onlyExcluded,
  cleared: cleared,
}));
"""


def _probe(module: Path, *, strip_iife: bool, build: str, badge: str, convert: str) -> dict:
    source = module.read_text(encoding="utf-8").rstrip()
    tail = ""
    if strip_iife:
        assert source.endswith("})(window);"), f"{module} no longer ends with the IIFE close"
        body = source[: -len("})(window);")]
        tail = "\n})(window);\n"
    else:
        body = source
    program = (
        _DOM + "\n" + body + "\n" + _PROBE
        .replace("__BUILD__", build)
        .replace("__BADGE__", badge)
        .replace("__CONVERT__", convert)
        + tail
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
        build="_buildMissingTrackRow", badge="_updateMissingHeaderBadge",
        convert="_missingRowToTrackComp",
    )


def _rebuilt() -> dict:
    return _probe(
        REBUILT_JS, strip_iife=True,
        build="buildMissingRow", badge="updateMissingHeaderBadge",
        convert="missingRowToTrackComp",
    )


# ===========================================================================
# 1. An undelivered track is listed, with its state
# ===========================================================================
class TestAnUndeliveredTrackIsVisible:
    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_a_queued_track_carries_its_state_not_the_missing_badge(self, run):
        title = run()["queuedTitle"]
        assert "Forgotten Years" in title, title
        assert re.search(r"bg-info", title), (
            "a queued track needs its own badge — rendering the plain 'Missing' "
            "state hides why it is not offered for download again"
        )
        assert "Queued" in title, title
        assert not re.search(r"bg-warning", title), title

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_its_download_button_is_disabled(self, run):
        btn = run()["queuedBtn"]
        assert "disabled" in btn, (
            f"a track already in the queue must not be re-queueable: {btn}"
        )
        assert "download queue" in btn, btn

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_an_ordinary_missing_track_is_unchanged(self, run):
        """CONTROL — the feature must not repaint tracks that are truly missing."""
        result = run()
        assert re.search(r"bg-warning", result["plainTitle"]), result["plainTitle"]
        assert "Missing" in result["plainTitle"], result["plainTitle"]
        assert "disabled" not in result["plainBtn"], (
            f"an unqueued track must stay queueable: {result['plainBtn']}"
        )

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_converter_forwards_the_queue_status(self, run):
        assert run()["convertedStatus"] == "searching", (
            "the refresh response's queue_status must survive the row conversion, "
            "or the badge can never appear"
        )


# ===========================================================================
# 2. Withheld tracks are reported, so absence is never ambiguous
# ===========================================================================
class TestWhatWasWithheldIsReported:
    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_badge_says_how_many_were_not_shown(self, run):
        text = run()["withExcluded"]["text"]
        assert "2 tracks missing" in text, text
        assert "not shown" in text, (
            f"a track dropped by a gate looks identical to one that never "
            f"existed unless the count is on screen: {text!r}"
        )
        assert "dismissed" in text and "already in the library" in text, text

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_note_survives_a_zero_missing_count(self, run):
        """Everything delivered/dismissed must NOT read as a complete album."""
        assert "not shown" in run()["onlyExcluded"], run()["onlyExcluded"]

    @pytest.mark.parametrize("run", [_live, _rebuilt], ids=["live", "test_site"])
    def test_the_badge_clears_when_nothing_is_hidden(self, run):
        assert run()["cleared"] == "", repr(run()["cleared"])
