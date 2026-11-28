"""The poller can change its pace at runtime.

WHY THIS EXISTS
---------------
``poller.create()`` captured ``interval`` in a closure, so a caller that wanted
"poll fast while work is running, slowly when idle" had to stop and rebuild the
poller — losing the in-flight guard, the attempt count and the active-handle
registration. That is why the dashboard polled four endpoints every 5 seconds
forever: the slow path was not expressible.

The dashboard's numbers, for scale: four endpoints × 12 ticks/minute × 60 =
~2,900 requests an hour on a page someone leaves open, to discover nothing has
changed.

These tests drive the SHIPPED ``poller.create`` in Node (it is a plain IIFE over
``window``), because a source assertion cannot tell "the interval is read at
schedule time" from "the interval is read once at construction" — the bug this
replaces.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
POLLER_JS = REPO_ROOT / "test_site/static/js/utils/poller.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to drive the poller"
)


def _run(probe: str) -> dict:
    result = subprocess.run(
        ["node", "-e", probe],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


_HARNESS = """
const fs = require('fs');
global.window = global;
global.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
global.addEventListener = () => {};
eval(fs.readFileSync('test_site/static/js/utils/poller.js', 'utf8'));
"""


# ---------------------------------------------------------------------------
# The API exists
# ---------------------------------------------------------------------------


def test_the_handle_exposes_a_setter_and_a_reader() -> None:
    js = POLLER_JS.read_text(encoding="utf-8")
    assert "setInterval(ms)" in js, "the poller has no way to change its pace"
    assert "get interval()" in js, "the current pace cannot be read back"


def test_the_interval_is_not_captured_at_construction() -> None:
    """The exact defect: `const interval = opts.interval` frozen in a closure."""
    js = POLLER_JS.read_text(encoding="utf-8")
    body = js[js.index("function create(opts = {})"):]
    body = body[: body.index("function schedule()")]
    assert "let interval = opts.interval" in body, (
        "the interval is still a const — setInterval() could never take effect"
    )


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@needs_node
def test_setting_the_interval_updates_the_reader() -> None:
    out = _run(_HARNESS + """
const p = window.poller.create({ interval: 5000, onTick: () => {} });
const before = p.interval;
p.setInterval(60000);
console.log(JSON.stringify({ before, after: p.interval }));
""")
    assert out["before"] == 5000
    assert out["after"] == 60000


@needs_node
def test_a_bad_value_is_ignored_rather_than_breaking_the_poller() -> None:
    """A NaN/zero interval would make setTimeout fire immediately in a loop."""
    out = _run(_HARNESS + """
const p = window.poller.create({ interval: 5000, onTick: () => {} });
p.setInterval(0);
p.setInterval(-1);
p.setInterval(NaN);
p.setInterval('nonsense');
console.log(JSON.stringify({ interval: p.interval }));
""")
    assert out["interval"] == 5000, "an invalid interval was accepted"


@needs_node
def test_setting_the_same_value_is_a_no_op() -> None:
    out = _run(_HARNESS + """
const p = window.poller.create({ interval: 5000, onTick: () => {} });
p.setInterval(5000);
console.log(JSON.stringify({ interval: p.interval }));
""")
    assert out["interval"] == 5000


@needs_node
def test_ticks_use_the_new_interval_after_a_change() -> None:
    """THE BEHAVIOURAL PROOF: the delay actually applied must follow the setter.

    Recorded from ``setTimeout`` calls rather than by waiting in real time —
    a 60-second pace would make the test 60 seconds long.
    """
    out = _run(_HARNESS + """
const delays = [];
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms) => { delays.push(ms); return 1; };
global.clearTimeout = () => {};

const p = window.poller.create({ interval: 5000, onTick: () => {}, immediate: false });
p.start();            // schedules at 5000
p.setInterval(60000); // re-arms at 60000
console.log(JSON.stringify({ delays }));
""")
    assert out["delays"][0] == 5000, "the initial schedule used the wrong delay"
    assert 60000 in out["delays"], (
        "changing the interval did not re-arm the timer — the queued tick still "
        "uses the old pace"
    )


# ---------------------------------------------------------------------------
# The dashboard actually uses it
# ---------------------------------------------------------------------------


class TestTheDashboardPollsByIdle:
    def test_there_is_an_idle_constant(self) -> None:
        js = (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(
            encoding="utf-8"
        )
        assert "STATUS_POLL_IDLE_MS" in js

    def test_the_idle_pace_is_far_slower_than_the_busy_one(self) -> None:
        js = (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(
            encoding="utf-8"
        )
        busy = int(js.split("const STATUS_POLL_MS =")[1].split(";")[0].strip())
        idle = int(js.split("const STATUS_POLL_IDLE_MS =")[1].split(";")[0].strip())
        assert idle >= busy * 6, (
            f"idle {idle}ms is not meaningfully slower than busy {busy}ms — the "
            "point is to stop burning requests on a page nobody is watching"
        )

    def test_the_pace_follows_whether_a_scan_is_running(self) -> None:
        js = (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(
            encoding="utf-8"
        )
        assert "setInterval(busy ? STATUS_POLL_MS : STATUS_POLL_IDLE_MS)" in js, (
            "the dashboard no longer adapts its poll pace to the scan state"
        )

    def test_it_starts_idle_and_is_raised_by_the_first_reply(self) -> None:
        """Starting fast would spend the first minute at 5s on an idle library."""
        js = (REPO_ROOT / "test_site/static/js/pages/dashboard.js").read_text(
            encoding="utf-8"
        )
        block = js[js.index("statusPoller = global.poller.create"):]
        block = block[: block.index("statusPoller.start()")]
        assert "interval: STATUS_POLL_IDLE_MS" in block
