"""The Config page's config collector must actually RUN.

REPORTED
--------
> Error: Could not collect config: getValue is not defined

on every save of the Config page (the report came in while entering a Gemini
API key, which is incidental — the failure is unconditional).

ROOT CAUSE — PURE SCOPING, NOT A MISSING FIELD
----------------------------------------------
``collectGenreSelectionConfig()`` is declared at file scope (live) / IIFE scope
(rebuilt) and is called from *inside* ``buildConfigObject()`` as
``selection: collectGenreSelectionConfig()``.

It uses ``getValue`` / ``getChecked`` / ``parseNumber``. Those were declared
INSIDE ``buildConfigObject()``, so their declaration sat two scopes below where
the collector was DEFINED. JavaScript resolves a free variable against the
function's own scope chain, not the caller's — so the call threw
``ReferenceError: getValue is not defined`` and ``saveConfig()`` caught it into
exactly the reported message.

⭐ WHY THE EXISTING TEST MISSED IT
``tests/test_genre_selection_engine.py`` already asserted the source CONTAINS
``selection: collectGenreSelectionConfig()`` — and kept passing while the page
was dead. ``tests/js/_extract.js`` is name-based too: it will happily pull a
NESTED ``getValue`` out of ``buildConfigObject``, so an extraction-based harness
defines the helper and never reproduces the bug.

⇒ The only harness that observes real scope is the WHOLE FILE, so
``tests/js/config-load-probe.js`` loads the shipped script into a vm context
with a DOM stub and CALLS the collector. It is verified as an oracle: run
against pristine ``develop`` it fails with the reported message.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "config-load-probe.js"

LIVE_CONFIG_JS = REPO_ROOT / "static" / "js" / "config.js"
REBUILT_CONFIG_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js"

CONFIG_SCRIPTS = (LIVE_CONFIG_JS, REBUILT_CONFIG_JS)

HELPERS = ("getValue", "getChecked", "parseNumber")


def _node_available() -> bool:
    try:
        return subprocess.run(["node", "--version"], capture_output=True).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run_probe(source: pathlib.Path) -> dict:
    result = subprocess.run(
        ["node", str(PROBE), str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"probe crashed on {source}: rc={result.returncode} stderr={result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def probe_results() -> dict[pathlib.Path, dict]:
    if not _node_available():
        pytest.skip("node is required")
    return {source: _run_probe(source) for source in CONFIG_SCRIPTS}


# ===========================================================================
# 1. The reported failure
# ===========================================================================
class TestTheConfigCollectorRuns:
    @pytest.mark.parametrize("source", CONFIG_SCRIPTS, ids=lambda p: p.parent.name)
    def test_the_script_loads(self, probe_results, source: pathlib.Path):
        result = probe_results[source]
        assert result["loaded"], (
            f"{source}: the shipped script threw at load time: {result.get('load_error')}"
        )

    @pytest.mark.parametrize("source", CONFIG_SCRIPTS, ids=lambda p: p.parent.name)
    def test_building_the_config_does_not_throw(self, probe_results, source: pathlib.Path):
        """The reported symptom, verbatim: 'Could not collect config: …'."""
        result = probe_results[source]
        assert result["collect_ok"], (
            f"{source}: buildConfigObject() threw {result.get('collect_error')!r}. "
            "This is the 'Could not collect config' failure — every Config save is dead."
        )

    @pytest.mark.parametrize("source", CONFIG_SCRIPTS, ids=lambda p: p.parent.name)
    def test_it_is_not_a_reference_error_about_a_field_reader(
        self, probe_results, source: pathlib.Path
    ):
        """CONTROL on the message: that specific ReferenceError must be gone.

        Asserting only ``collect_ok`` would also pass if the probe stopped
        calling the collector at all.
        """
        result = probe_results[source]
        for helper in HELPERS:
            assert f"{helper} is not defined" not in str(result.get("collect_error") or ""), (
                f"{source}: still cannot resolve {helper}() — it is declared in a "
                "narrower scope than the function that uses it"
            )


# ===========================================================================
# 2. The collector really ran
# ===========================================================================
class TestTheGenreSelectionCollectorReallyRan:
    def test_the_selection_block_is_present(self, probe_results):
        """Without this, "it did not throw" could mean "it was never called"."""
        for source, result in probe_results.items():
            assert result["collect_shape"]["has_genres_selection"], (
                f"{source}: config.genres.selection is missing — the collector did not "
                "contribute to the payload"
            )
            assert result["selection_is_object"], f"{source}: selection is not an object"

    def test_the_selection_values_are_the_field_defaults(self, probe_results):
        """Concrete values, so a stub returning {} cannot pass.

        The probe's DOM has no inputs, so every reader must fall back to the
        defaults declared in ``collectGenreSelectionConfig``.
        """
        for source, result in probe_results.items():
            assert result["selection_seems_populated"], (
                f"{source}: selection is not populated with its defaults "
                f"(enabled={result['selection_enabled']!r}, "
                f"max_genres={result['selection_max_genres']!r}, "
                f"family_slot_limit={result['selection_family_slot_limit']!r})"
            )

    def test_the_rest_of_the_payload_still_builds(self, probe_results):
        """The collector shares its scope with the whole payload.

        A fix that hoisted the helpers but broke something else would show up
        here.
        """
        for source, result in probe_results.items():
            shape = result["collect_shape"]
            assert shape["has_queue"], f"{source}: config.queue missing"
            assert shape["has_playlists"], f"{source}: config.playlists missing"


# ===========================================================================
# 3. Structural guard — no second copy for someone to nest again
# ===========================================================================
class TestTheFieldReadersStayInFileScope:
    """The behavioural probe above is the real guard; this pins WHY it works.

    A future edit could re-add a nested ``getValue`` inside
    ``buildConfigObject`` that shadows the file-scope one — the behavioural test
    would still pass (the collector resolves the outer one), but the duplicate
    is exactly the trap that caused this bug. One definition, at the scope the
    collectors live in.
    """

    @pytest.mark.parametrize("source", CONFIG_SCRIPTS, ids=lambda p: p.parent.name)
    def test_there_is_exactly_one_definition_of_each_reader(self, source: pathlib.Path):
        text = source.read_text(encoding="utf-8")
        for helper in HELPERS:
            count = text.count(f"function {helper}(")
            assert count == 1, (
                f"{source.name}: {helper} is declared {count} times; a second copy is "
                "how one caller ends up resolving a name the other cannot see"
            )
