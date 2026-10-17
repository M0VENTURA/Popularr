"""The album page's ``#page-data`` block must be VALID JSON, not markup.

Reported
--------
"The lookup mbid is broken — it's not auto filling the artist and album":
``Lookup MBID`` POSTed to ``/api/album/metadata-proposal`` and got

    HTTP 400: artist, album and release_mbid are required

Root cause
----------
Commit ``4bf0e503`` moved the album page's scripts around and spliced a

    <script src="{{ versioned_static('js/ui/scan-modal.js') }}"></script>

line INSIDE the ``<script id="page-data" type="application/json">`` element:

    <script id="page-data" type="application/json">
    <script src=".../scan-modal.js"></script>          <-- NOT JSON
    { "artistName": ..., "albumName": ... }
    </script>

``JSON.parse(document.getElementById('page-data').textContent)`` therefore threw,
``window._pageData`` stayed **undefined**, and every reader of it —
``pageArtist()`` / ``pageAlbum()`` in ``metadata-review.js`` — returned ``''``.
The proposal route rejects a blank artist/album with exactly the reported 400,
and the album/artist fields never auto-filled.

⭐ A JSON ``<script>`` element is a SELF-CONTAINED unit: any markup inside it is
parsed as JSON text (and, because HTML ends a script element at the first
``</script>``, it also truncates the element). Nothing may be nested inside it.

This test RENDERS the element's content and parses it, so it fails on the real
bug rather than pattern-matching the fix.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]

ALBUM_TEMPLATES = [
    REPO_ROOT / "templates" / "pages" / "album_detail.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html",
]

#: The element's inner text. NON-GREEDY on purpose: with the regression present
#: the first ``</script>`` is the NESTED one, so the capture stops at ``<script
#: src=...>`` — which is exactly what the browser hands JSON.parse.
_PAGE_DATA_RE = re.compile(
    r'<script id="page-data"[^>]*>(?P<body>.*?)</script>', re.S
)


def _page_data_body(path: Path) -> str:
    match = _PAGE_DATA_RE.search(path.read_text(encoding="utf-8"))
    assert match, f"no #page-data element in {path}"
    return match.group("body")


def _render(snippet: str) -> str:
    """Render a template fragment the way the server would."""
    env = Environment()
    env.globals["versioned_static"] = lambda p: "/static/" + p
    return env.from_string(snippet).render(
        artist_name="Probe Artist", album_name="Probe Album"
    )


@pytest.mark.parametrize("path", ALBUM_TEMPLATES, ids=lambda p: p.parent.parent.name)
class TestThePageDataBlockIsJson:
    def test_nothing_is_nested_inside_the_json_element(self, path):
        """The direct statement of the rule the regression broke."""
        body = _page_data_body(path)

        assert "<script" not in body.lower(), (
            f"{path.name}: a <script> tag is nested inside the #page-data JSON "
            "element — JSON.parse will throw and window._pageData will be undefined"
        )

    def test_the_rendered_block_parses_as_json(self, path):
        """THE DISCRIMINATOR: render what the browser sees and parse it."""
        rendered = _render(_page_data_body(path))

        data = json.loads(rendered)          # throws on the regression
        assert data["artistName"] == "Probe Artist"
        assert data["albumName"] == "Probe Album"

    def test_the_page_still_loads_the_scan_sheet(self, path):
        """CONTROL — the fix MOVES scan-modal.js, it must not DELETE it. Without
        this, 'remove the line' would pass both tests above."""
        assert "js/ui/scan-modal.js" in path.read_text(encoding="utf-8")


class TestTheConsumersReadTheParsedBlock:
    """Both trees' metadata-review module resolves artist/album from the block,
    so the two facts must line up."""

    @pytest.mark.parametrize(
        "path",
        [
            REPO_ROOT / "static" / "js" / "metadata-review.js",
            REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js",
        ],
        ids=["live", "rebuild"],
    )
    def test_page_artist_and_album_come_from_page_data(self, path):
        src = path.read_text(encoding="utf-8")

        assert "global._pageData ? global._pageData.artistName : ''" in src
        assert "global._pageData ? global._pageData.albumName : ''" in src
