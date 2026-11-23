"""`dataset` keys are camelCase — a hyphenated key throws and kills the listener.

This is not a style rule, it is a runtime failure with a very wide blast radius.
``snapshotConfigLoadedValues()`` runs from the DOMContentLoaded listener BEFORE
the Preview buttons are bound, so assigning ``el.dataset['data-config-loaded']``
threw a SyntaxError there and left all three Preview buttons unbound — the
symptom users reported as "the preview changes no longer work" on the Playlists
and Star Ratings tabs.

Only a real browser surfaces it: every static check (parsed JSON, balanced tags,
ids present, script fetched, CSS defined) passed while the page was dead. These
tests pin the property name so it cannot come back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVED = ROOT / "test_site" / "static" / "js" / "pages" / "config.js"
LIVE = ROOT / "static" / "js" / "config.js"


@pytest.mark.parametrize("path", [SERVED, LIVE], ids=["served", "live"])
def test_no_hyphenated_dataset_keys(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "dataset['data-" not in source, (
        "dataset keys are camelCase; a hyphenated key throws a SyntaxError "
        "from the DOMContentLoaded listener and kills every handler after it"
    )
    assert 'dataset["data-' not in source


@pytest.mark.parametrize("path", [SERVED, LIVE], ids=["served", "live"])
def test_the_config_attributes_are_camel_cased(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "const CFG_DEFAULT_ATTR = 'configDefault'" in source, (
        "data-config-default must be addressed as configDefault"
    )
    assert "const CFG_LOADED_ATTR = 'configLoaded'" in source, (
        "data-config-loaded must be addressed as configLoaded"
    )


@pytest.mark.parametrize("path", [SERVED, LIVE], ids=["served", "live"])
def test_dirty_tracking_runs_before_the_preview_bindings(path: Path) -> None:
    """Order matters: this throw is only fatal because of where it sits.

    The binding is the LAST reference to each button id (the earlier ones are
    inside the handler bodies), so compare against that.
    """
    source = path.read_text(encoding="utf-8")
    init = source.index("initConfigPagePolish();")
    for button in ("star_resync_preview_btn", "genre_resync_preview_btn",
                   "genre_playlist_resync_preview_btn"):
        binding = source.rindex(f"document.getElementById('{button}')")
        assert init < binding, (
            f"{button} is bound after the init that used to throw, so an "
            "exception here silently disables the Preview button"
        )
