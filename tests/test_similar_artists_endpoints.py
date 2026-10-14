"""The similar-artists blocks call endpoints that actually exist.

Reported (browser console, scanner.ungoogle.au)
----------------------------------------------
    GET /api/search/unified?q=Samurai%20Pizza%20Cats      404 (Not Found)
    GET /api/artist/BABYMETAL%20%26%20Electric%20Callboy/image  404 (Not Found)

Neither URL is a route:

* ``/api/search/unified`` matches NOTHING — the real search endpoint is
  ``POST /api/search`` (the one ``unified_search.js`` calls).
* ``/api/artist/image`` is a QUERY-PARAM route (``?name=``), so the
  ``/api/artist/<name>/image`` form never matched it either.

The cost was not just the 404s: the collection split re-derived an answer the
payload already carried. ``/api/artist/<artist>/similar`` annotates EVERY entry
with ``in_collection`` (``artist_metadata_service._annotate_similar_artist``,
a punctuation/"The"-tolerant key — MORE accurate than the exact lowercase
compare the client ran), so the per-candidate search was redundant as well as
broken: every failed request pushed its artist into "missing", and every card
fell back to the placeholder image.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every file that carried the two broken URLs — BOTH trees, because the live
#: templates had the same defect (that is what the reported console shows).
FILES = {
    "test_site album.js": REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js",
    "test_site track.js": REPO_ROOT / "test_site" / "static" / "js" / "pages" / "track.js",
    "live album page": REPO_ROOT / "templates" / "pages" / "album_detail.html",
    "live track page": REPO_ROOT / "templates" / "pages" / "track_detail.html",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _live_code(path: Path) -> str:
    """The file with comments removed.

    The explanatory comments deliberately QUOTE the broken URLs (they are how
    the next reader learns why the URL is what it is), so a naive search would
    flag the documentation as the bug.
    """
    text = _source(path)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


# ===========================================================================
# 1. The endpoints the pages need are really registered
# ===========================================================================


class TestTheEndpointsExist:
    def _rules(self, app):
        return [
            (rule.rule, set(rule.methods or set()))
            for rule in app.url_map.iter_rules()
        ]

    def test_the_artist_image_route_is_a_query_param_route(self, app):
        rules = self._rules(app)

        assert any(
            path == "/api/artist/image" and "GET" in methods
            for path, methods in rules
        ), (
            "the similar-artist cards build their src from /api/artist/image — "
            "if that rule is renamed the cards 404 again"
        )

    def test_there_is_no_path_segment_image_route(self, app):
        """CONTROL — documents WHY the old URL could not work: no rule accepts
        ``/api/artist/<name>/image``, so the fix is the URL, not a new route."""
        rules = self._rules(app)

        assert not any(
            path.startswith("/api/artist/<") and path.endswith("/image")
            for path, _methods in rules
        )

    def test_the_search_route_is_a_post(self, app):
        rules = self._rules(app)

        assert any(
            path == "/api/search" and "POST" in methods
            for path, methods in rules
        ), "the collection fallback posts to /api/search"

    def test_there_is_no_unified_search_route(self, app):
        """CONTROL — the reported 404 was a URL that no rule serves.

        Checked as an EXACT path: ``/api/unified-log`` is a real (unrelated)
        log endpoint, so a substring match on "unified" would be wrong.
        """
        rules = self._rules(app)

        assert not any(
            path == "/api/search/unified" for path, _methods in rules
        ), "/api/search/unified does not exist and must not be invented"
        assert any(
            path == "/api/search" for path, _methods in rules
        ), "the replacement route must exist"

    def test_the_similar_route_serves_a_path_segment_artist(self, app):
        """CONTROL — this is why the OTHER path-segment URL in these files is
        fine: the similar route really does take ``<path:artist>``."""
        rules = self._rules(app)

        assert any(
            path == "/api/artist/<path:artist>/similar" and "GET" in methods
            for path, methods in rules
        )


# ===========================================================================
# 2. The reported URLs are gone from live code
# ===========================================================================


class TestTheReportedURLsAreGone:
    @pytest.mark.parametrize("label", sorted(FILES))
    def test_no_unified_search_call_remains(self, label):
        code = _live_code(FILES[label])

        assert "/api/search/unified" not in code, (
            f"{label} still calls a URL no route serves — every similar-artist "
            "candidate 404s and the grid reads 'missing'"
        )

    @pytest.mark.parametrize("label", sorted(FILES))
    def test_no_path_segment_artist_image_remains(self, label):
        code = _live_code(FILES[label])

        pattern = re.compile(r"/api/artist/\$\{encodeURIComponent\([^)]*\)\}/image")
        assert not pattern.search(code), (
            f"{label} still builds /api/artist/<name>/image, which matches no "
            "route — every card falls back to the placeholder"
        )


# ===========================================================================
# 3. The replacement URLs
# ===========================================================================


class TestTheReplacementURLs:
    @pytest.mark.parametrize("label", sorted(FILES))
    def test_the_image_is_requested_by_query_param(self, label):
        code = _live_code(FILES[label])

        assert "/api/artist/image?name=${encodeURIComponent(" in code, (
            f"{label} must request the query-param form of the image route"
        )

    @pytest.mark.parametrize("label", sorted(FILES))
    def test_the_fallback_posts_to_the_real_search_route(self, label):
        code = _live_code(FILES[label])

        assert "/api/search'" in code or '/api/search"' in code, (
            f"{label} must call the real search endpoint"
        )
        # The route only answers POST — a GET would 405. The call may go
        # through the shared helper (``postJson``, which sets the method) or a
        # raw fetch that states it.
        assert "method: 'POST'" in code or "postJson('/api/search'" in code, (
            "the search request must be a POST"
        )
        assert "query:" in code, "the route reads the term from the ``query`` field"


# ===========================================================================
# 4. The server's own answer is used before any lookup
# ===========================================================================


class TestTheServerFlagIsUsedFirst:
    @pytest.mark.parametrize("label", sorted(FILES))
    def test_the_in_collection_flag_is_read(self, label):
        code = _live_code(FILES[label])

        assert "in_collection === true" in code and "in_collection === false" in code, (
            f"{label} must classify from the server's per-entry flag, which is "
            "the answer the payload already carries"
        )

    @pytest.mark.parametrize("label", sorted(FILES))
    def test_the_flag_is_consulted_before_the_search_fallback(self, label):
        code = _live_code(FILES[label])

        flag = code.index("in_collection === true")
        fallback = code.index("query:")

        assert flag < fallback, (
            f"{label} must read the flag FIRST — running the lookup first is "
            "what produced one failed request per artist"
        )

    def test_every_similar_artist_entry_is_annotated(self):
        """The premise of the fix: the flag really is on every entry, so the
        fallback is only for a payload that lacks it."""
        from services.metadata.artist_metadata_service import _annotate_similar_artist

        annotated = _annotate_similar_artist(
            ["Ghostkid", {"name": "Electric Callboy"}], {"Electric Callboy"}
        )

        assert all(entry.get("in_collection") is not None for entry in annotated)
        assert annotated[0]["in_collection"] is False
        assert annotated[1]["in_collection"] is True

    def test_the_annotation_tolerates_name_variants(self):
        """The server's key is punctuation/"The"-tolerant, which is a second
        reason to prefer it over the client's exact compare."""
        from services.metadata.artist_metadata_service import _annotate_similar_artist

        annotated = _annotate_similar_artist(["Beatles, The"], {"The Beatles"})

        assert annotated[0]["in_collection"] is True
