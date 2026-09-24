"""Config-page contract tests for the short-interlude LB outlier filter.

The interlude filter (``is_interlude_lb_outlier`` in
``services/popularity/popularity_math.py``) reads four keys under
``single_detection``: ``interlude_lb_outlier_enabled``,
``interlude_lb_max_duration_s``, ``interlude_lb_ratio_factor`` and
``interlude_lb_min_count``.  The Config page is the source of truth for all
user-editable settings, so those keys MUST be surfaced as inputs on
``templates/pages/config.html`` (with the same ids ``static/js/config.js``
reads when collecting the save payload) — otherwise a user cannot tune the
filter and the saved config would silently drop the keys.
"""

from __future__ import annotations

import os

import pytest


class TestInterludeLbConfigPageContract:
    """The Config page renders the four interlude-LB filter inputs, and
    ``config.js`` collects them under ``single_detection``."""

    async def test_config_page_renders_interlude_filter_inputs(self, client, monkeypatch):
        # The config page reads CONFIG_PATH; /dev/null (test default) yields
        # an empty config, so the template falls back to the hardcoded
        # defaults (enabled, 180s, 3.0x, 500) — the ids must still render.
        response = await client.get("/config")
        assert response.status_code == 200
        body = await response.get_data(as_text=True)

        # Inputs present (ids match static/js/config.js getValue/getChecked).
        for field_id in (
            "interlude_lb_outlier_enabled",
            "interlude_lb_max_duration_s",
            "interlude_lb_ratio_factor",
            "interlude_lb_min_count",
        ):
            assert field_id in body, f"Config page missing interlude field {field_id}"

        # Defaults baked into the template match popularity_config defaults.
        assert 'id="interlude_lb_max_duration_s" placeholder="180"' in body
        assert 'id="interlude_lb_ratio_factor" placeholder="3.0"' in body
        assert 'id="interlude_lb_min_count" placeholder="500"' in body

    async def test_config_page_prefills_existing_values(self, client, monkeypatch):
        # When config.yaml already carries custom interlude values, the
        # rendered inputs must reflect them (round-trip contract).
        import os

        import yaml

        tmp_config = os.path.join(os.path.dirname(__file__), "_tmp_config_test.yaml")
        with open(tmp_config, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "single_detection": {
                        "interlude_lb_outlier_enabled": False,
                        "interlude_lb_max_duration_s": 120,
                        "interlude_lb_ratio_factor": 5.0,
                        "interlude_lb_min_count": 1000,
                    }
                },
                f,
            )
        try:
            monkeypatch.setenv("CONFIG_PATH", tmp_config)
            from helpers.config_helpers import clear_config_cache

            clear_config_cache()

            response = await client.get("/config")
            assert response.status_code == 200
            body = await response.get_data(as_text=True)

            # The page is a form (not raw YAML textarea) — value= attributes
            # carry the stored numbers back into the inputs.
            assert 'id="interlude_lb_max_duration_s" placeholder="180" value="120"' in body
            assert 'id="interlude_lb_ratio_factor" placeholder="3.0" value="5.0"' in body
            assert 'id="interlude_lb_min_count" placeholder="500" value="1000"' in body
        finally:
            clear_config_cache()
            if os.path.exists(tmp_config):
                os.remove(tmp_config)

    def test_config_js_collects_interlude_keys(self):
        # The save payload must carry the four keys under single_detection
        # (matching the ids the template renders) or saving the page would
        # silently drop them from config.yaml.
        js_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "js", "config.js",
        )
        with open(js_path, encoding="utf-8") as f:
            js = f.read()

        expected = {
            "interlude_lb_outlier_enabled": "getChecked('interlude_lb_outlier_enabled', true)",
            "interlude_lb_max_duration_s": "getValue('interlude_lb_max_duration_s', '180')",
            "interlude_lb_ratio_factor": "getValue('interlude_lb_ratio_factor', '3.0')",
            "interlude_lb_min_count": "getValue('interlude_lb_min_count', '500')",
        }
        for key, fragment in expected.items():
            assert key in js, f"config.js missing interlude key {key}"
            assert fragment in js, f"config.js does not collect {key} via {fragment}"
