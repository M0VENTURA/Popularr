"""Regression tests for the queue button's loading / in-flight behaviour.

Reported symptom
----------------
"The queue button doesn't show anything when selected, then after pressing a few
times, 20 seconds later it gets multiple popups."

Root cause
----------
Both search implementations took an early return when ``openReleasePicker``
existed::

    if (openReleasePicker) {
      openReleasePicker(id, title, artist, function () { ...mark... });
      return;                     # <-- no busy state, no disable, no guard
    }

- Nothing touched the button, so the click looked ignored while a MusicBrainz
  release-picker probe (seconds) ran.
- ``_queuedIds[id]`` was only set inside the picker's CALLBACK — which fires
  after the user picks a version — so the ``if (_queuedIds[id]) return`` guard
  never blocked a repeat click. Each click started another probe; because
  ``releasePickerOnQueued`` is a single module slot, they also clobbered each
  other, and every response then surfaced at once.

Proven against the original code: 5 rapid clicks started 5 probes with no
button feedback and no disabling, producing 5 popups together.

What is asserted here
---------------------
``tests/js/queue-button-probe.js`` extracts the SHIPPED functions by
brace-matching and drives them with stub collaborators, emitting one JSON line.
A purely structural test would pass even if the guard were moved after the async
call, so the behaviour is asserted by running that probe.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "queue-button-probe.js"

LIVE_SEARCH = "static/js/unified_search.js"
LIVE_MAIN = "static/js/main.js"
REBUILT_SEARCH = "test_site/static/js/ui/search-flyout.js"
REBUILT_MAIN = "test_site/static/js/main.js"

ALL_SEARCH_FILES = [LIVE_SEARCH, REBUILT_SEARCH]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for the JS probe"
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _node_available() -> bool:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, shell=True
        ).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run_probe(search_rel: str) -> dict:
    """Run tests/js/queue-button-probe.js against a search source and parse it."""
    out = subprocess.run(
        ["node", str(PROBE), str(REPO_ROOT / search_rel)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), shell=True,
    )
    assert out.returncode == 0, f"probe failed:\n{out.stdout}\n{out.stderr}"
    lines = [
        ln for ln in out.stdout.strip().splitlines()
        if ln.strip().startswith("{")
    ]
    assert lines, f"probe produced no JSON:\n{out.stdout}\n{out.stderr}"
    return json.loads(lines[-1])


def _extract_js_fn(source: str, name: str) -> str:
    """Brace-match ``function <name>(...) { ... }`` out of a source file."""
    start = source.index(f"function {name}(")
    i = source.index("{", start)
    depth = 0
    in_str = None
    while i < len(source):
        ch = source[i]
        prev = source[i - 1]
        if in_str:
            if ch == in_str and prev != "\\":
                in_str = None
            i += 1
            continue
        if ch in "\"'`":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


# ---------------------------------------------------------------------------
# Behavioural: the whole point of the fix
# ---------------------------------------------------------------------------

@needs_node
def test_rapid_clicks_start_only_one_picker_probe():
    """Five rapid clicks must produce ONE probe, not five.

    This is the direct reproduction of "after pressing a few times … multiple
    popups".
    """
    result = _run_probe(LIVE_SEARCH)
    assert "error" not in result, result
    assert result["probesStarted"] == 1, (
        f"5 rapid clicks started {result['probesStarted']} release-picker "
        "probes; the in-flight guard must allow exactly one"
    )


@needs_node
def test_button_shows_feedback_while_working():
    """A spinner + disabled state must appear immediately.

    "Doesn't show anything when selected" was the first half of the report.
    """
    result = _run_probe(LIVE_SEARCH)
    assert "error" not in result, result
    assert result["spinnerDuring"] is True, "no spinner while working"
    assert result["disabledDuring"] is True, "button not disabled while working"
    assert result["ariaBusyDuring"] is True, "button not marked aria-busy"


@needs_node
def test_button_not_marked_queued_before_a_version_is_chosen():
    """Picking a version opens a flyout; being mid-lookup is not "Queued"."""
    result = _run_probe(LIVE_SEARCH)
    assert "error" not in result, result
    assert result["queuedDuring"] is False


@needs_node
def test_button_restored_when_flow_settles_without_queueing():
    """Cancelling the version flyout must leave the button usable again.

    Otherwise the in-flight guard would strand the button forever.
    """
    result = _run_probe(LIVE_SEARCH)
    assert "error" not in result, result
    assert result["restoredAfterSettle"] is True, (
        "button not restored after the picker settled without queueing"
    )


# ---------------------------------------------------------------------------
# Structural guards (fast, and catch a guard placed in the wrong order)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Structural guards that do not need node
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ALL_SEARCH_FILES)
def test_search_files_track_in_flight_state(rel: str):
    """The in-flight map must exist and be consulted before the async work."""
    src = _read(rel)
    assert re.search(r"_queuedInFlight", src), f"{rel}: no in-flight state"
    # The guard must appear in queueRelease.
    fn = _extract_js_fn(src, "queueRelease")
    assert "_queuedInFlight[id]" in fn, (
        f"{rel}: queueRelease does not check the in-flight guard"
    )


@pytest.mark.parametrize("rel", ALL_SEARCH_FILES)
def test_guard_is_set_before_the_picker_call(rel: str):
    """Order matters: set the guard BEFORE awaiting, or a re-click races in.

    A guard placed after the async call would be useless, and the behavioural
    test above could still pass on a fast path — so assert the ordering
    explicitly.
    """
    src = _read(rel)
    fn = _extract_js_fn(src, "queueRelease")
    guard_at = fn.index("_queuedInFlight[id] = true")
    picker_at = fn.index("openReleasePicker")
    assert guard_at < picker_at, (
        f"{rel}: the in-flight guard is set AFTER openReleasePicker is called, "
        "so a rapid second click can slip through"
    )


@pytest.mark.parametrize("rel", ALL_SEARCH_FILES)
def test_guard_is_cleared_on_every_settle_path(rel: str):
    """A leaked guard would leave the button permanently unclickable."""
    src = _read(rel)
    fn = _extract_js_fn(src, "queueRelease")
    assert "delete _queuedInFlight[id]" in fn, (
        f"{rel}: queueRelease never clears the in-flight guard, so the button "
        "could stay blocked forever"
    )


@pytest.mark.parametrize("rel", ALL_SEARCH_FILES)
def test_button_gets_busy_feedback(rel: str):
    """Some visible feedback must be applied before the network work."""
    src = _read(rel)
    fn = _extract_js_fn(src, "queueRelease")
    has_feedback = (
        "setQueueBtnBusy" in fn
        or "setBusy" in fn
        or "spinner-border" in fn
    )
    assert has_feedback, f"{rel}: queueRelease gives no loading feedback"


def test_live_main_returns_the_picker_promise():
    """The live picker must return its promise so the caller can await it.

    Without a return value the caller cannot know when the flow settled, which
    is what left the button inert.
    """
    src = _read(LIVE_MAIN)
    fn_start = src.index("window.openReleasePicker = function")
    block = src[fn_start : fn_start + 2500]
    assert re.search(r"return fetch\(url \+ '&format=json'", block), (
        "live openReleasePicker must return the probe promise"
    )


@pytest.mark.parametrize("rel,marker", [
    (LIVE_MAIN, "window.openReleasePicker = function"),
    (REBUILT_MAIN, "async function openReleasePicker"),
])
def test_open_release_picker_is_awaitable(rel: str, marker: str):
    """Both trees must expose an awaitable picker."""
    src = _read(rel)
    assert marker in src, f"{rel}: openReleasePicker not found"
    if "async function" in marker:
        return  # async functions are awaitable by definition
    start = src.index(marker)
    block = src[start : start + 2500]
    assert "return fetch(" in block, f"{rel}: picker does not return its promise"
