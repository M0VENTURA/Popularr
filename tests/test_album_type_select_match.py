"""Album match must not turn "Album (Soundtrack)" back into "Album".

Reported:

> When doing an album match if an album has a secondary type, it changes it
> back to album even though it shows it's changing to album+soundtrack.
> The drop down is album (Soundtrack).

Root cause (client side, proven by driving the real module in Node):

``fillField()`` picked the matching ``<select>`` option with ONE predicate that
allowed containment in either direction:

    v === wanted || wanted.includes(v) || v.includes(wanted)

``wanted`` is the proposed value, ``album+soundtrack``.  **``"album"`` is a
substring of ``"album+soundtrack"``**, and the bare ``Album`` option renders
BEFORE every ``Album (…)`` option — so ``.find()`` returned ``Album`` and the
form submitted ``album_type=album``.  The orange bar meanwhile still showed the
proposed ``album+soundtrack``, which is exactly the reported contradiction:
"it shows it's changing to album+soundtrack" yet the saved type is ``album``.

The same class of bug bit the category path: ``setAlbumTypeIfPresent()`` could
downgrade an already-composite selection to a bare primary when the cached
search row carries a lossy ``category`` of ``Album``.

Both are pinned here against the REAL option order parsed from the templates,
so reordering the options cannot silently reintroduce the short-circuit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

LIVE_REVIEW_JS = REPO_ROOT / "static" / "js" / "metadata-review.js"
REBUILT_REVIEW_JS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js"
REBUILT_ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"

LIVE_TEMPLATE = REPO_ROOT / "templates" / "pages" / "album_detail.html"
REBUILT_TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise the JS module"
)


def _album_type_options(template: Path) -> list[str]:
    """The ``<option value=…>`` list of ``#album_type``, in render order."""
    html = template.read_text(encoding="utf-8")
    match = re.search(
        r'<select[^>]*id="album_type"[^>]*>(.*?)</select>', html, re.S
    )
    assert match, f"no #album_type select in {template}"
    return re.findall(r'<option\s+value="([^"]*)"', match.group(1))


def _run(program: str) -> dict:
    node = shutil.which("node")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.cjs"
        script.write_text(program, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")]
    if not lines:
        raise AssertionError(f"node produced no RESULT:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(lines[-1][len("RESULT "):])


# ---------------------------------------------------------------------------
# Shared prelude: a real <select> plus the globals both modules expect.
# ---------------------------------------------------------------------------

def _prelude(options: list[str], current: str) -> str:
    return textwrap.dedent(
        f"""
        const OPTION_VALUES = {json.dumps(options)};
        const els = {{
          album_type: {{
            tagName: 'SELECT',
            options: OPTION_VALUES.map((v) => ({{ value: v }})),
            value: {json.dumps(current)},
            style: {{}},
          }},
        }};
        const seen = [];
        const win = {{}};
        win.toast = {{
          success: (m) => seen.push(['success', String(m)]),
          error: (m) => seen.push(['error', String(m)]),
          warning: (m) => seen.push(['warning', String(m)]),
          info: (m) => seen.push(['info', String(m)]),
        }};
        win.alert = (m) => seen.push(['alert', String(m)]);
        win.document = {{
          getElementById: (id) => els[id] || null,
          querySelector: () => null,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => ({{ style: {{}}, classList: {{ add() {{}} }}, dataset: {{}} }}),
        }};
        win.location = {{ reload: () => {{}}, href: '' }};
        win.ui = {{ confirm: async () => true }};
        win.api = {{ postJson: async () => ({{}}), getJson: async () => ({{}}) }};
        win.setTimeout = () => 0;
        win.busyPopup = null;
        globalThis.window = win;
        globalThis.document = win.document;
        globalThis.location = win.location;
        globalThis.alert = win.alert;
        globalThis.setTimeout = win.setTimeout;
        """
    )


def _fill_field_program(js_path: Path, options: list[str], wanted: str, current: str) -> str:
    source = js_path.read_text(encoding="utf-8").rstrip()
    assert source.endswith("})(window);"), f"{js_path} does not end with the IIFE close"
    module = source[: -len("})(window);")]
    probe = textwrap.dedent(
        f"""
        let ok = false;
        let error = null;
        try {{
          ok = globalThis.window.albumMetadataReview.fillField('album_type', {json.dumps(wanted)});
        }} catch (e) {{
          error = String((e && e.stack) || e);
        }}
        console.log('RESULT ' + JSON.stringify({{
          ok,
          value: globalThis.document.getElementById('album_type').value,
          error,
        }}));
        """
    )
    return _prelude(options, current) + module + "})(window);\n" + probe


def _set_album_type_program(options: list[str], category: str, current: str) -> str:
    """Probe ``setAlbumTypeIfPresent`` from INSIDE album.js's IIFE (not exported)."""
    source = REBUILT_ALBUM_JS.read_text(encoding="utf-8").rstrip()
    assert source.endswith("})(window);"), "album.js does not end with the IIFE close"
    module = source[: -len("})(window);")]
    probe = textwrap.dedent(
        f"""
        if (typeof setAlbumTypeIfPresent !== 'function') {{
          globalThis.__RES = {{ error: 'setAlbumTypeIfPresent unreachable' }};
        }} else {{
          const ok = setAlbumTypeIfPresent({json.dumps(category)});
          globalThis.__RES = {{
            ok,
            value: globalThis.document.getElementById('album_type').value,
          }};
        }}
        """
    )
    printer = textwrap.dedent(
        """
        console.log('RESULT ' + JSON.stringify(globalThis.__RES));
        """
    )
    return (
        _prelude(options, current)
        + module
        + probe
        + "})(window);\n"
        + printer
    )


# ---------------------------------------------------------------------------
# 1. The option list the templates really render
# ---------------------------------------------------------------------------

class TestTheRenderedOptions:

    def test_both_trees_render_the_same_option_order(self):
        live = _album_type_options(LIVE_TEMPLATE)
        rebuilt = _album_type_options(REBUILT_TEMPLATE)
        assert live == rebuilt, (
            "the two trees' Album Type selects must stay in step — the option "
            "ORDER is what the substring match short-circuits on"
        )

    def test_the_composite_option_exists_and_follows_the_bare_one(self):
        options = _album_type_options(REBUILT_TEMPLATE)
        assert "album+soundtrack" in options, (
            "the reported dropdown option must exist"
        )
        # The bare "Album" rendering BEFORE "Album (Soundtrack)" is precisely
        # why a containment `.find()` returned it.
        assert options.index("album") < options.index("album+soundtrack")


# ---------------------------------------------------------------------------
# 2. fillField must land on the composite (the reported defect)
# ---------------------------------------------------------------------------

class TestFillFieldSelectsTheProposedOption:

    @pytest.mark.parametrize(
        "js_path",
        [LIVE_REVIEW_JS, REBUILT_REVIEW_JS],
        ids=["live", "test_site"],
    )
    @pytest.mark.parametrize(
        "wanted,expected",
        [
            ("album+soundtrack", "album+soundtrack"),  # the reported bug
            ("album+compilation", "album+compilation"),
            ("album+live", "album+live"),
            ("album+remix", "album+remix"),
            ("album", "album"),
            ("ep", "ep"),
            ("single", "single"),
        ],
        ids=[
            "soundtrack", "compilation", "live", "remix",
            "bare-album", "ep", "single",
        ],
    )
    def test_proposed_value_lands_on_its_own_option(
        self, js_path: Path, wanted: str, expected: str
    ):
        options = _album_type_options(REBUILT_TEMPLATE)
        result = _run(_fill_field_program(js_path, options, wanted, current="album"))
        assert result.get("error") is None, result.get("error")
        assert result["value"] == expected, (
            f"fillField({wanted!r}) selected {result['value']!r}: the proposal "
            f"would save {expected!r} but the form would submit "
            f"{result['value']!r}, so the album match reverts a secondary type"
        )
        assert result["ok"] is True

    @pytest.mark.parametrize(
        "js_path",
        [LIVE_REVIEW_JS, REBUILT_REVIEW_JS],
        ids=["live", "test_site"],
    )
    def test_a_bare_category_still_reaches_the_composite(self, js_path: Path):
        """The containment fallback must remain (only after the exact miss)."""
        options = _album_type_options(REBUILT_TEMPLATE)
        result = _run(_fill_field_program(js_path, options, "soundtrack", current="album"))
        assert result.get("error") is None, result.get("error")
        assert result["value"] == "album+soundtrack", (
            "a bare MusicBrainz category must still map onto the composite option"
        )

    @pytest.mark.parametrize(
        "js_path",
        [LIVE_REVIEW_JS, REBUILT_REVIEW_JS],
        ids=["live", "test_site"],
    )
    def test_unknown_value_changes_nothing(self, js_path: Path):
        options = _album_type_options(REBUILT_TEMPLATE)
        result = _run(
            _fill_field_program(js_path, options, "broadcast", current="album+soundtrack")
        )
        assert result.get("error") is None, result.get("error")
        assert result["ok"] is False
        assert result["value"] == "album+soundtrack", (
            "an option that does not exist must never change the selection"
        )

    @pytest.mark.parametrize(
        "js_path",
        [LIVE_REVIEW_JS, REBUILT_REVIEW_JS],
        ids=["live", "test_site"],
    )
    def test_empty_proposal_leaves_the_select_alone(self, js_path: Path):
        """``''.includes(v)`` is true for EVERY option — must not select one."""
        options = _album_type_options(REBUILT_TEMPLATE)
        result = _run(
            _fill_field_program(js_path, options, "", current="album+soundtrack")
        )
        assert result.get("error") is None, result.get("error")
        assert result["ok"] is False
        assert result["value"] == "album+soundtrack"


# ---------------------------------------------------------------------------
# 3. The category path must not downgrade an existing composite
# ---------------------------------------------------------------------------

class TestSetAlbumTypeKeepsTheSecondary:

    @pytest.mark.parametrize(
        "current,category,expected",
        [
            # The reported downgrade: a lossy "Album" category must not strip
            # the secondary the select already carries.
            ("album+soundtrack", "Album", "album+soundtrack"),
            ("album+live", "album", "album+live"),
            ("album+compilation", "Album", "album+compilation"),
            # A matching secondary category is applied (and is a no-op here).
            ("album+soundtrack", "Soundtrack", "album+soundtrack"),
            # An upgrade still happens.
            ("album", "Soundtrack", "album+soundtrack"),
            ("album", "Live", "album+live"),
        ],
        ids=[
            "no-downgrade-soundtrack",
            "no-downgrade-live",
            "no-downgrade-compilation",
            "secondary-stays",
            "upgrade-to-soundtrack",
            "upgrade-to-live",
        ],
    )
    def test_category_application(self, current: str, category: str, expected: str):
        options = _album_type_options(REBUILT_TEMPLATE)
        result = _run(
            _set_album_type_program(options, category, current=current)
        )
        assert result.get("error") is None, result.get("error")
        assert result["value"] == expected, (
            f"setAlbumTypeIfPresent({category!r}) on a {current!r} selection "
            f"produced {result['value']!r}"
        )
