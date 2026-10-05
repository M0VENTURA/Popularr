"""The track page's album links were built from the TRACK artist.

REPORTED: on a compilation — or any album whose track credit differs from the
release credit — the album link and the album art on the TRACK page go nowhere.

The album route resolves its artist segment with::

    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)

i.e. the **album artist**, falling back to the track artist only when the album
artist is EMPTY. The track page built those URLs from ``track.artist`` (after
``split_artist_collabs(...)[0]``), so:

    album_artist = "Various Artists", artist = "Midnight Oil"
        -> /album/Midnight%20Oil/<album>        -> no rows -> dead page
    album_artist = "Weezer", artist = "Rivers Cuomo"   (featured credit)
        -> /album/Rivers%20Cuomo/<album>         -> dead page

Every OTHER album link in the app is built from the album's own
``artist_name``; only this page disagreed with the route.

Two rules are pinned here:

1. the URL artist is ``album_artist or artist``;
2. it is **not split** — the route matches the stored string WHOLE, so
   ``artist_parts[0]`` would break an album whose stored credit itself
   contains a feat. credit.

The tests harvest the expressions from the SHIPPED templates (both trees), so
reverting either file reverts the assertion with it.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from jinja2 import DictLoader, Environment

from helpers.template_filters import encode_path_segment

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Both trees must agree — this page exists in each.
TRACK_TEMPLATES = (
    REPO_ROOT / "templates" / "pages" / "track_detail.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "track_detail.html",
)

#: The ``{% set %}`` statements the album URLs are built from, in source order.
#: Harvested WITH the URLs — an expression rendered without them would read an
#: undefined variable and silently produce an empty artist segment.
_SET_RE = re.compile(r"\{%\s*set\s+(album_link_artist|hero_art_url)\s*=\s*(.+?)\s*%\}")
#: Every album-keyed attribute (``/album/…`` page link, ``/api/album/…`` art).
_ATTR_RE = re.compile(r'(?:href|src)="(/(?:api/)?album/[^"]+)"')

#: A compilation track: the credit that must NOT appear in the album URL.
COMPILATION_TRACK = {
    "artist": "Midnight Oil",
    "album_artist": "Various Artists",
    "album": "Now That's What I Call Music!",
    "title": "Beds Are Burning",
}


def _album_urls(source: str) -> list[tuple[str, str]]:
    """Every album-keyed URL the shipped file builds, as renderable snippets.

    The ``{% set %}`` statements are prepended so a snippet behaves exactly as
    the page does — otherwise ``album_link_artist`` is undefined in the harness
    and the artist segment renders EMPTY, which would fail for the wrong reason.
    """
    sets = _SET_RE.findall(source)
    prelude = "".join(f"{{% set {name} = {expr} %}}" for name, expr in sets)

    found: list[tuple[str, str]] = []
    if any(name == "hero_art_url" for name, _expr in sets):
        found.append(("hero_art_url", prelude + "{{ hero_art_url }}"))
    for index, attr in enumerate(_ATTR_RE.findall(source)):
        found.append((f"attribute#{index}", prelude + attr))
    return found


def _render(expr: str, track: dict[str, str]) -> str:
    """Render one harvested expression exactly as the page would."""
    env = Environment(loader=DictLoader({"snippet": expr}), autoescape=False)
    env.filters["path_segment"] = encode_path_segment
    return env.get_template("snippet").render(track=track)


def _as_the_route_sees_it(rendered: str) -> str:
    """Undo BOTH encodings, so the value equals what the SQL compares.

    ``encode_path_segment`` quotes TWICE on purpose (hypercorn fully decodes
    ``%2F`` before routing, so one pass would split an ``AC/DC`` segment), and
    the route then calls ``unquote()`` once. Two decodes total.
    """
    return unquote(unquote(rendered))


class TestTheAlbumUrlUsesTheAlbumArtist:
    def test_both_templates_build_album_urls(self):
        """The harvest must not silently match nothing."""
        for path in TRACK_TEMPLATES:
            urls = _album_urls(path.read_text(encoding="utf-8"))
            assert len(urls) >= 4, (
                f"{path.name}: expected the hero-art set plus 3 album links, "
                f"found {len(urls)}"
            )

    def test_a_compilation_track_lands_on_the_album_artist(self):
        for path in TRACK_TEMPLATES:
            source = path.read_text(encoding="utf-8")
            for name, expr in _album_urls(source):
                rendered = _as_the_route_sees_it(_render(expr, COMPILATION_TRACK))
                assert rendered.startswith(
                    ("/album/Various Artists/", "/api/album/Various Artists/")
                ), f"{path.name} {name} -> {rendered!r}"

    def test_the_track_artist_is_never_used_when_an_album_artist_exists(self):
        for path in TRACK_TEMPLATES:
            for name, expr in _album_urls(path.read_text(encoding="utf-8")):
                rendered = _as_the_route_sees_it(_render(expr, COMPILATION_TRACK))
                assert "Midnight Oil" not in rendered, (
                    f"{path.name} {name} still keys the album off the track "
                    f"artist -> {rendered!r}"
                )

    def test_an_empty_album_artist_falls_back_to_the_track_artist(self):
        """The route's own fallback — a track with no album_artist must work."""
        track = {**COMPILATION_TRACK, "album_artist": ""}
        for path in TRACK_TEMPLATES:
            for name, expr in _album_urls(path.read_text(encoding="utf-8")):
                rendered = _as_the_route_sees_it(_render(expr, track))
                assert rendered.startswith(
                    ("/album/Midnight Oil/", "/api/album/Midnight Oil/")
                ), f"{path.name} {name} -> {rendered!r}"

    def test_the_stored_credit_is_never_split(self):
        """The route matches the stored string WHOLE, so [0] would 404.

        ``split_artist_collabs`` was applied to the artist before this fix; it
        cuts at ``feat.``/``w/``, and an album stored as "Artist feat. Other"
        is keyed by that full string.
        """
        track = {
            **COMPILATION_TRACK,
            "album_artist": "Feuerschwanz feat. Subway to Sally",
        }
        for path in TRACK_TEMPLATES:
            for name, expr in _album_urls(path.read_text(encoding="utf-8")):
                rendered = _as_the_route_sees_it(_render(expr, track))
                assert "Feuerschwanz feat. Subway to Sally" in rendered, (
                    f"{path.name} {name} truncated the stored credit -> "
                    f"{rendered!r}"
                )
