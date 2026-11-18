"""The Corrections page's "Find MBID" button.

REPORT

> "I do get this on the corrections page on the Find MBID button
>  MusicBrainz search failed: Server returned HTML instead of JSON (HTTP 405).
>  This usually means an auth/session redirect or a server error page."

THREE DEFECTS BEHIND ONE BUTTON

1. **405 — wrong HTTP method.** The page called
   ``api.getJson('/api/album/musicbrainz?artist=…&album=…')`` while the route
   was ``methods=["POST"]`` and read a JSON body. The request never reached the
   handler; Quart answered with its own HTML error page, so the JS helper's
   "HTML instead of JSON" message fired and blamed auth.

2. **Wrong payload key.** ``lookup_musicbrainz_album`` returns ``{"results":
   [...]}``; the page read ``data.releases``, so even a successful call would
   have looked empty.

3. **Wrong ID KIND.** The search returns release-GROUP ids, but "Apply MBID"
   writes into ``musicbrainz_album_mbid`` and the ``musicbrainz_albumid`` FILE
   tag — both of which must be RELEASE ids. Applying a group id reproduces the
   very album split this page exists to repair. The endpoint now resolves each
   group to its concrete release before answering.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES = REPO_ROOT / "routes" / "album_routes.py"
JS = REPO_ROOT / "test_site/static/js/pages/artist-corrections.js"


def _route_declaration() -> str:
    src = ROUTES.read_text(encoding="utf-8")
    match = re.search(r'@album_bp\.route\("/musicbrainz", methods=\[([^\]]+)\]\)', src)
    assert match, "the lookup route was renamed"
    return match.group(1)


def _strip_comments(source: str) -> str:
    """CODE only — the docstrings below quote the broken call they describe."""
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )


def _js_function(name: str) -> str:
    """One JS function body, comments removed, to its closing brace."""
    src = JS.read_text(encoding="utf-8")
    start = src.index(f"async function {name}")
    depth = 0
    seen = False
    for index in range(start, len(src)):
        if src[index] == "{":
            depth += 1
            seen = True
        elif src[index] == "}":
            depth -= 1
            if seen and depth == 0:
                return _strip_comments(src[start:index + 1])
    raise AssertionError(f"could not find the end of {name}")


def _py_handler() -> str:
    src = ROUTES.read_text(encoding="utf-8")
    start = src.index("async def api_album_musicbrainz_lookup")
    depth = 0
    seen = False
    for index in range(start, len(src)):
        char = src[index]
        if char == ":":
            seen = True
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        # The body ends where the next decorator starts.
        if seen and depth == 0 and src.startswith("\n@", index):
            return src[start:index]
    return src[start:]


class TestTheMethodMatchesTheCaller:
    def test_the_route_accepts_the_method_the_page_uses(self) -> None:
        assert "GET" in _route_declaration(), (
            "Find MBID issues a GET; a POST-only route answers 405 with an "
            "HTML error page"
        )

    def test_the_route_still_accepts_post(self) -> None:
        """The album page sends a JSON body to the same endpoint."""
        assert "POST" in _route_declaration()

    def test_the_page_uses_post_with_a_body(self) -> None:
        body = _js_function("searchAndApplyAlbumMbid")
        assert "postJson('/api/album/musicbrainz'" in body
        assert "getJson(" not in body, (
            "the page still issues the GET that produced the 405"
        )

    def test_the_handler_reads_query_arguments_too(self) -> None:
        """So the GET form works even if a caller uses it."""
        assert "request.args.get" in _py_handler()


class TestThePayloadKeyIsRight:
    def test_the_page_reads_results(self) -> None:
        body = _js_function("searchAndApplyAlbumMbid")
        assert "data.results" in body
        assert "data.releases" not in body, (
            "lookup_musicbrainz_album returns {results: [...]}, so reading "
            "releases always looked empty"
        )

    def test_the_candidate_fields_match_the_payload(self) -> None:
        """The entries name ``mbid`` / ``first_release_date``."""
        body = _js_function("searchAndApplyAlbumMbid")
        assert "release.mbid" in body
        assert "release.first_release_date" in body

    def test_it_applies_the_mbid_field(self) -> None:
        assert "applyAlbumMbidNow(album, selected.mbid)" in _js_function(
            "searchAndApplyAlbumMbid"
        )

    def test_it_never_applies_the_id_field(self) -> None:
        body = _js_function("searchAndApplyAlbumMbid")
        assert "selected.id" not in body


class TestTheIdKindIsARelease:
    def test_the_endpoint_resolves_groups(self) -> None:
        assert "resolve_release_id" in _py_handler(), (
            "the search returns release-GROUP ids, which must never be written "
            "into musicbrainz_album_mbid"
        )

    def test_it_records_the_group_it_came_from(self) -> None:
        assert "release_group_mbid" in _py_handler()

    def test_a_resolution_failure_cannot_500_the_search(self) -> None:
        body = _py_handler()
        assert "except Exception" in body, (
            "a resolution outage must not fail the whole search"
        )
