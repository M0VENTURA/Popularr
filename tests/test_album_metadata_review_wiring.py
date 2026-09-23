"""Album metadata review — cross-tree wiring contract.

The album page exists TWICE (a live tree and a rebuilt ``test_site`` tree) and
``helpers/test_site_mode`` serves the rebuilt tree FIRST when the cutover is on.
A change applied to one tree only is therefore invisible to whichever tree the
user happens to be on — this session has shipped exactly that mistake before.

These are text-level guards (no browser) asserting that both trees:

* load the review module;
* carry the hidden staging inputs the review posts through;
* define the hook the Lookup MBID flow calls.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _code_only(source: str) -> str:
    """Strip comments so a guard can't be satisfied (or broken) by prose.

    These modules deliberately EXPLAIN the endpoint they avoid calling, and the
    row class they deliberately do not reuse. Asserting on the raw text would
    therefore test the comments instead of the code.
    """
    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", without_block)


# (template, review script path as referenced by the template, real script path)
_TREES = [
    ("templates/pages/album_detail.html", "js/metadata-review.js",
     "static/js/metadata-review.js"),
    ("test_site/templates/Pages/album_detail.html", "js/services/metadata-review.js",
     "test_site/static/js/services/metadata-review.js"),
]


class TestTemplateWiring:
    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_template_loads_the_review_module(self, template, ref, script):
        text = Path(template).read_text(encoding="utf-8")
        assert ref in text, f"{template} does not load {ref}"
        assert Path(script).exists(), f"{script} does not exist"

    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_template_carries_the_staging_inputs(self, template, ref, script):
        """The staged payload travels WITH the form — that is what makes the
        review one atomic save instead of a write on lookup."""
        text = Path(template).read_text(encoding="utf-8")
        assert 'id="staged_track_updates"' in text
        assert 'name="staged_track_updates"' in text
        assert 'id="pending_recommendations"' in text

    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_review_runs_inside_the_album_form(self, template, ref, script):
        """The hidden input must be a child of #albumMetadataForm or the form
        will not post it."""
        text = Path(template).read_text(encoding="utf-8")
        form_start = text.index('id="albumMetadataForm"')
        staged_pos = text.index('id="staged_track_updates"')
        assert staged_pos > form_start, "staging input sits outside the form"


class TestScriptDefinesTheHook:
    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_script_publishes_the_review_global(self, template, ref, script):
        code = _code_only(Path(script).read_text(encoding="utf-8"))
        assert "albumMetadataReview" in code
        assert "applyProposal" in code

    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_script_posts_to_the_propose_endpoint(self, template, ref, script):
        code = _code_only(Path(script).read_text(encoding="utf-8"))
        assert "/api/album/musicbrainz/propose" in code

    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_script_never_calls_a_write_endpoint_on_lookup(self, template, ref, script):
        """The preview must be side-effect free.

        ``apply-mb-field`` writes IMMEDIATELY. If the review module ever called
        it, a lookup would silently persist changes — the exact behaviour this
        feature exists to avoid. The compare flow may still use it; this module
        must not.
        """
        code = _code_only(Path(script).read_text(encoding="utf-8"))
        assert "apply-mb-field" not in code
        assert "ignore-mb-field" not in code

    @pytest.mark.parametrize("template,ref,script", _TREES)
    def test_script_uses_the_staged_row_class_not_the_apply_row(self, template, ref, script):
        """`.mb-update-row` rows are picked up by the compare flow's
        clear/update-all handlers. Staged rows must be distinct."""
        code = _code_only(Path(script).read_text(encoding="utf-8"))
        assert "mb-staged-row" in code
        assert "mb-update-row" not in code


class TestLookupHooksTheReview:
    """The Lookup MBID flow must actually invoke the preview."""

    @pytest.mark.parametrize("js", ["static/js/album_detail.js",
                                    "test_site/static/js/pages/album.js"])
    def test_lookup_flow_calls_apply_proposal(self, js):
        code = _code_only(Path(js).read_text(encoding="utf-8"))
        assert "albumMetadataReview" in code, f"{js} never invokes the review"
        assert "applyProposal" in code


# Artist page — the per-album "waiting for review" summary.
_ARTIST_TREES = [
    ("templates/pages/artist_detail.html", "js/metadata-recommendations.js",
     "static/js/metadata-recommendations.js"),
    ("test_site/templates/Pages/artist_detail.html",
     "js/pages/metadata-recommendations.js",
     "test_site/static/js/pages/metadata-recommendations.js"),
]


class TestArtistPageWiring:
    @pytest.mark.parametrize("template,ref,script", _ARTIST_TREES)
    def test_artist_template_has_a_host_element(self, template, ref, script):
        text = Path(template).read_text(encoding="utf-8")
        assert 'id="artistMetadataRecommendations"' in text, f"missing host in {template}"

    @pytest.mark.parametrize("template,ref,script", _ARTIST_TREES)
    def test_artist_template_loads_the_script(self, template, ref, script):
        text = Path(template).read_text(encoding="utf-8")
        assert ref in text, f"{template} does not load {ref}"
        assert Path(script).exists(), f"{script} does not exist"

    @pytest.mark.parametrize("template,ref,script", _ARTIST_TREES)
    def test_artist_script_is_read_only(self, template, ref, script):
        """Saving/discarding belongs on the album page, against that album's
        actual tracklist. An "apply all" here would apply unreviewed changes."""
        code = _code_only(Path(script).read_text(encoding="utf-8"))
        assert "/api/artist/metadata-recommendations" in code
        assert "/api/album/metadata-recommendations/discard" not in code
        assert "apply-mb-field" not in code
        assert "stageTrackChanges" not in code


class TestEndpointsRegistered:
    @pytest.mark.parametrize("rule", [
        "/api/album/musicbrainz/propose",
        "/api/album/metadata-recommendations",
        "/api/album/metadata-recommendations/discard",
        "/api/artist/metadata-recommendations",
    ])
    def test_route_exists(self, rule):
        import importlib

        app_mod = importlib.import_module("app")
        rules = {str(r.rule) for r in app_mod.app.url_map.iter_rules()}
        assert rule in rules
