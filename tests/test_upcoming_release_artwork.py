"""Upcoming-release artwork: one rule, implemented identically in both trees.

An upcoming release is BY DEFINITION an album you do not own, so the app's own art
endpoint answers 404 for it.  Every row therefore fell back to a placeholder — the
MusicBrainz-linked ones most visibly, since the match chip promised a confirmed
identity and no cover followed.  The Cover Art Archive can be asked *directly* by
release group, so a matched release now gets real artwork.

This module pins the RULE rather than the markup it produces:

    1. owned        -> the app's own ``/api/album/.../art`` (Navidrome-first)
    2. matched      -> ``coverartarchive.org/release-group/<id>/front-<size>``
    3. otherwise    -> ``''``, and therefore NO request at all

and it pins the rule in BOTH trees at once.  ``test_site/`` and the live
``static/`` tree are separate implementations by design, so the rule exists
twice; nothing but a parity check stops the second copy from drifting.  Each
probe drives the SHIPPED function — pattern-matching the generated markup would
pass on a wrong URL as long as the shape looked plausible.

The live tree's rule lives in ``static/js/upcoming_releases.js``, the service the
dashboard, the dedicated page and the downloads monitor all load, and the
dashboard delegates to it rather than keeping a second copy.

⚠️ A CANDIDATE match is not a match.  ``mbid_match_status == 'candidate'`` means
the scorer found a plausible release group and is waiting for a human to confirm
it.  Artwork is the most believable thing on a card, so drawing that release's
cover would present a guess as this album's identity.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tests" / "js"

REBUILT_SERVICE = REPO_ROOT / "test_site" / "static" / "js" / "services" / "upcoming-releases.js"
#: The live rule lives in the SHARED service, not in dashboard.js — the dashboard,
#: the dedicated page and the downloads monitor all render this API payload, so
#: one copy is what keeps them consistent.
LIVE_SERVICE = REPO_ROOT / "static" / "js" / "upcoming_releases.js"
#: The live dashboard DELEGATES to the service. Asserted here so the rule cannot
#: quietly grow a second implementation on the page that shows it most often.
LIVE_DASHBOARD = REPO_ROOT / "static" / "js" / "dashboard.js"

REBUILT_PROBE = PROBE_DIR / "upcoming-art-probe.js"
LIVE_PROBE = PROBE_DIR / "upcoming-art-live-probe.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is required to drive the shipped JS helpers"
)

#: Keys the two probes must agree on EXACTLY — the URLs themselves.
_URL_KEYS = (
    "owned",
    "matched",
    "candidate_only",
    "neither",
    "hostile",
    "owned_size",
    "matched_size",
)

#: Keys describing the ``<img>`` rather than the URL.  The two trees build the
#: element with different class names on purpose, so only the behaviour is
#: compared here — laziness, self-removal, escaping, and '' when there is
#: nothing to ask for.
_THUMB_KEYS = (
    "thumb_none",
    "thumb_has_img",
    "thumb_removes_on_error",
    "thumb_is_lazy",
    "thumb_escapes",
)

#: The one host a direct cover request may go to.  A bare "release-group" check
#: would pass on a typo'd hostname, so the whole origin is spelled out.
CAA_ORIGIN = "https://coverartarchive.org/release-group/"


def _run_probe(probe: Path, target: Path) -> dict:
    result = subprocess.run(
        [NODE, str(probe), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"probe crashed: {result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in payload, (
        f"{target.name} no longer exposes the artwork helpers: {payload}"
    )
    return payload


@pytest.fixture(scope="module")
def rebuilt() -> dict:
    return _run_probe(REBUILT_PROBE, REBUILT_SERVICE)


@pytest.fixture(scope="module")
def live() -> dict:
    return _run_probe(LIVE_PROBE, LIVE_SERVICE)


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


class TestTheArtworkRule:

    def test_an_owned_album_uses_the_apps_own_endpoint(self, rebuilt: dict, live: dict) -> None:
        """Ownership wins over a match even when both are present.

        The local endpoint proxies Navidrome, so it is the picture the rest of
        the app already shows for that album — same-origin and already cached.
        """
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            import urllib.parse

            url = payload["owned"]
            assert url.startswith("/api/album/"), f"{name}: not the local endpoint: {url}"
            decoded = urllib.parse.unquote(url)
            assert decoded == "/api/album/a-ha/Take On Me/art?s=250", (
                f"{name}: artist/album are not encoded into the path as expected: {decoded}"
            )

    def test_a_matched_release_uses_the_cover_art_archive(self, rebuilt: dict, live: dict) -> None:
        """⭐ The case this change exists for: unowned but matched to a release group."""
        expected = CAA_ORIGIN + "rg-2/front-250"
        assert rebuilt["matched"] == expected
        assert live["matched"] == expected

    def test_the_size_reaches_both_branches(self, rebuilt: dict, live: dict) -> None:
        """A hardcoded size in one branch would silently serve the wrong resolution."""
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            assert payload["owned_size"].endswith("/art?s=500"), name
            assert payload["matched_size"] == CAA_ORIGIN + "rg-3/front-500", name

    def test_a_candidate_match_is_not_artwork(self, rebuilt: dict, live: dict) -> None:
        """A guess must not be drawn as this album's cover."""
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            assert payload["candidate_only"] == "", (
                f"{name}: the candidate release group was used for artwork"
            )

    def test_nothing_to_ask_for_makes_no_request(self, rebuilt: dict, live: dict) -> None:
        """Unmatched AND unowned: the caller draws its placeholder for free.

        Returning a URL here is not a cosmetic bug — it is one guaranteed 404
        per row, fifty of them on a full list, to render what could be rendered
        without a request.
        """
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            assert payload["neither"] == "", f"{name}: requested art with no source to ask"

    def test_an_mbid_cannot_break_out_of_the_attribute(self, rebuilt: dict, live: dict) -> None:
        """The URL is echoed into ``src="…"``, so an mbid must be encoded.

        The value is data from an external API; a quote in it would end the
        attribute and let the rest be parsed as markup.
        """
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            url = payload["hostile"]
            assert '"' not in url and "<" not in url and ">" not in url, (
                f"{name}: raw attribute-breaking characters survived: {url}"
            )
            # The space is encoded too, so the URL cannot be truncated there.
            assert "%22" in url, f"{name}: quote was not percent-encoded: {url}"


class TestTheThumbnail:

    def test_no_element_when_there_is_nothing_to_ask_for(self, rebuilt: dict, live: dict) -> None:
        """The absence of an <img> IS the absence of a request."""
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            assert payload["thumb_none"] == "", (
                f"{name}: rendered an <img> with no source to ask"
            )

    def test_it_is_lazy_and_removes_itself_on_error(self, rebuilt: dict, live: dict) -> None:
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            assert payload["thumb_has_img"] is True, name
            # Fifty covers must not all be fetched during the first paint.
            assert payload["thumb_is_lazy"] is True, (
                f"{name}: without loading=lazy a full list blocks on fifty images"
            )
            # A release group the archive has no art for answers 404; a
            # broken-image glyph is worse than a cell with only the name.
            assert payload["thumb_removes_on_error"] is True, (
                f"{name}: a missing cover must not leave a broken-image icon"
            )

    def test_the_escaped_url_stays_inside_the_src_attribute(
        self, rebuilt: dict, live: dict
    ) -> None:
        for name, payload in (("rebuilt", rebuilt), ("live", live)):
            assert payload["thumb_escapes"] is True, (
                f"{name}: the src attribute does not close cleanly — the URL is not escaped"
            )


# ---------------------------------------------------------------------------
# The two copies must not drift
# ---------------------------------------------------------------------------


class TestBothTreesAgree:
    """``test_site/`` and ``static/`` implement the rule separately."""

    def test_the_url_rule_is_identical(self, rebuilt: dict, live: dict) -> None:
        differing = {k: (rebuilt[k], live[k]) for k in _URL_KEYS if rebuilt[k] != live[k]}
        assert not differing, (
            "the two trees disagree on where artwork comes from: "
            + ", ".join(f"{k}: rebuilt={a!r} live={b!r}" for k, (a, b) in differing.items())
        )

    def test_the_element_behaviour_is_identical(self, rebuilt: dict, live: dict) -> None:
        differing = {k: (rebuilt[k], live[k]) for k in _THUMB_KEYS if rebuilt[k] != live[k]}
        assert not differing, (
            "the two trees build a different <img>: "
            + ", ".join(f"{k}: rebuilt={a!r} live={b!r}" for k, (a, b) in differing.items())
        )


# ---------------------------------------------------------------------------
# The rule must actually be reached
# ---------------------------------------------------------------------------

#: Whole-line ``//`` and block comments.  ⚠️ Load-bearing: the docblocks in both
#: files name the helpers and quote URLs, so an assertion about what the CODE
#: does is satisfied by prose unless the prose is removed first.
_JS_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.MULTILINE)
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_js_comments(source: str) -> str:
    return _JS_LINE_COMMENT.sub("", _JS_BLOCK_COMMENT.sub("", source))


def _function_body(source: str, name: str, indent: str) -> str:
    """Slice a function out of a bundle, up to the next sibling declaration.

    Boundary-by-indentation rather than brace counting: template literals in
    these files nest ``${…}`` inside HTML, and a brace count that is off by one
    silently returns a fragment of the wrong size.
    """
    start = source.index(f"function {name}(")
    end = source.find("\n" + indent + "function ", start + 1)
    if end == -1:
        end = source.find("\n" + indent + "async function ", start + 1)
    assert end != -1, f"could not find the end of {name}()"
    return source[start:end]


@pytest.fixture(scope="module")
def rebuilt_js() -> str:
    return _strip_js_comments(REBUILT_SERVICE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live_js() -> str:
    return _strip_js_comments(LIVE_SERVICE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live_dashboard_js() -> str:
    return _strip_js_comments(LIVE_DASHBOARD.read_text(encoding="utf-8"))


class TestTheRebuiltTreeUsesTheRule:

    def test_the_carousel_asks_the_helper(self, rebuilt_js: str) -> None:
        body = _function_body(rebuilt_js, "renderCarousel", "  ")
        assert "releaseArtUrl(" in body, (
            "the carousel must not build its own art URL — the priority rule "
            "lives in one place"
        )

    def test_the_table_asks_the_helper(self, rebuilt_js: str) -> None:
        """``renderTable`` only assembles sections; the CELLS come from here."""
        body = _function_body(rebuilt_js, "renderReleaseRow", "  ")
        assert "artThumb(" in body, "the table row lost its thumbnail"
        assert 'data-label="Album"' in body, (
            "the thumbnail must live in the Album cell — a new <td> would need a "
            "matching <th>, a data-label and every colspan kept in step"
        )

    def test_the_archive_url_is_built_in_exactly_one_place(self, rebuilt_js: str) -> None:
        """A ratchet, not a style note.

        A second literal somewhere else is how the two trees drifted apart in
        the first place; one construction site is what the parity test relies on.
        """
        assert rebuilt_js.count("coverartarchive.org") == 1, (
            f"the archive host is named {rebuilt_js.count('coverartarchive.org')} times — "
            "artwork URLs must all come from caaReleaseGroupUrl()"
        )


class TestTheLiveTreeUsesTheRule:

    def test_the_table_row_uses_the_helper(self, live_js: str) -> None:
        body = _function_body(live_js, "renderReleaseRow", "  ")
        assert "upcomingArtThumb(" in body, "the table row lost its thumbnail"
        assert 'data-label="Album"' in body, (
            "the thumbnail must live in the Album cell — a new <td> would need a "
            "matching <th>, a data-label and every colspan kept in step"
        )

    def test_the_thumbnail_precedes_the_album_name(self, live_js: str) -> None:
        """⚠️ Placement, not a call count.

        Counting calls to the helper passes while the thumbnail is built and
        then never interpolated — the oracle caught exactly that in the other
        tree, where deleting the row's use of the variable left the count at two.
        """
        body = _function_body(live_js, "renderReleaseRow", "  ")
        album_cell = body[body.index('data-label="Album"') :]
        assert album_cell.index("upcomingArtThumb(") < album_cell.index(
            "escapeHtml(release.album_name"
        ), "the thumbnail does not precede the album name in its cell"

    def test_the_archive_url_is_built_in_exactly_one_place(self, live_js: str) -> None:
        assert live_js.count("coverartarchive.org") == 1, (
            f"the archive host is named {live_js.count('coverartarchive.org')} times — "
            "artwork URLs must all come from upcomingArtUrl()"
        )


class TestTheThumbnailIsSizedWhereverItRenders:
    """⚠️ The rule must live in a stylesheet EVERY page loads.

    The thumbnail is emitted by a service that renders rows on three pages — the
    dashboard, the dedicated upcoming page and the downloads monitor — but
    ``css/dashboard.css`` is linked by the dashboard ALONE. A class declared
    there styles exactly one of the three, and the other two get no sizing at
    all: a full-size 250px cover dropped into a table cell. That is not a
    cosmetic detail, it is the difference between a thumbnail and a layout
    wreck, and it is invisible on the page a developer is most likely to open.

    So the assertion is about WHERE the rule is declared, not what it says.
    """

    @staticmethod
    def _reads(base: Path, sheet: Path) -> tuple[str, str]:
        return (
            base.read_text(encoding="utf-8"),
            sheet.read_text(encoding="utf-8"),
        )

    def test_the_live_thumbnail_is_declared_globally(self) -> None:
        base, popularr = self._reads(
            REPO_ROOT / "templates" / "base.html",
            REPO_ROOT / "static" / "css" / "popularr.css",
        )
        assert "css/popularr.css" in base, (
            "base.html no longer loads the global stylesheet — every assertion "
            "below rests on that"
        )
        assert ".upcoming-thumb {" in popularr, (
            ".upcoming-thumb is not declared in the globally-loaded stylesheet, so "
            "the dedicated upcoming page and the downloads monitor render an "
            "unsized full-size cover"
        )

    def test_the_rebuilt_thumbnail_is_declared_globally(self) -> None:
        base, popularr = self._reads(
            REPO_ROOT / "test_site" / "templates" / "base.html",
            REPO_ROOT / "test_site" / "static" / "css" / "popularr.css",
        )
        assert "css/popularr.css" in base, (
            "base.html no longer loads the global stylesheet — every assertion "
            "below rests on that"
        )
        assert ".ur-thumb {" in popularr, (
            ".ur-thumb is not declared in the globally-loaded stylesheet, so the "
            "dedicated upcoming page and the downloads monitor render an unsized "
            "full-size cover"
        )

    def test_the_rule_is_not_only_in_the_dashboards_own_stylesheet(self) -> None:
        """CONTROL: dashboard.css must not be the only place either rule lives.

        Stated as its own check because this is the exact shape of the original
        bug — the class WAS declared, just somewhere only one of the three pages
        could see it.
        """
        for sheet, cls in (
            (REPO_ROOT / "static" / "css" / "dashboard.css", ".upcoming-thumb"),
            (REPO_ROOT / "test_site" / "static" / "css" / "dashboard.css", ".ur-thumb"),
        ):
            global_sheet = sheet.parent / "popularr.css"
            if f"{cls} {{" in sheet.read_text(encoding="utf-8"):
                assert f"{cls} {{" in global_sheet.read_text(encoding="utf-8"), (
                    f"{cls} is declared in {sheet.name}, which only the dashboard "
                    "loads, and not in popularr.css"
                )


class TestTheLiveDashboardDelegates:
    """The dashboard renders this API payload too, and must not re-decide.

    A second copy of the priority order is a second place for it to drift, and
    the dashboard is the page where artwork is seen most often.
    """

    def test_it_asks_the_shared_service(self, live_dashboard_js: str) -> None:
        body = _function_body(live_dashboard_js, "_upcomingArtThumb", "")
        assert "UpcomingReleasesService" in body, (
            "the dashboard must delegate to the shared artwork rule"
        )
        assert "upcomingArtThumb" in body

    def test_it_does_not_reimplement_the_rule(self, live_dashboard_js: str) -> None:
        """CONTROL: neither half of the rule may be rebuilt here."""
        body = _function_body(live_dashboard_js, "_upcomingArtThumb", "")
        assert "coverartarchive.org" not in live_dashboard_js, (
            "the dashboard names the archive host itself — that is the drift this "
            "delegation exists to prevent"
        )
        assert "album_in_collection" not in body and "release_group_mbid" not in body, (
            "the ownership/match decision is being made in the dashboard again"
        )
        assert "/art?s=" not in body, (
            "an inline art URL in the dashboard bypasses the shared rule"
        )

    def test_a_missing_service_means_no_art_rather_than_a_crash(
        self, live_dashboard_js: str
    ) -> None:
        """⚠️ The fallback must be '' and not a URL.

        An unguarded call would throw inside the row builder and take the whole
        table with it. Falling back to a URL instead would defeat the rule: an
        unowned, unmatched release has nothing to ask for, so any URL there is a
        guaranteed failed request.
        """
        body = _function_body(live_dashboard_js, "_upcomingArtThumb", "")
        guard_line = "if (!svc || typeof svc.upcomingArtThumb"
        guard = body.index(guard_line)
        # The GUARD STATEMENT itself — sliced to its own line, so a `return ""`
        # from anywhere else in the function cannot satisfy it.
        guard_stmt = body[guard : body.index("\n", guard)]
        assert 'return ""' in guard_stmt, (
            f"the guard must fall back to no art, got: {guard_stmt.strip()!r}"
        )
        call = body.index("return svc.upcomingArtThumb(", guard)
        assert guard < call, "the guard must precede the call, not follow it"

    def test_the_row_has_a_thumbnail_in_both_its_shapes(self, live_dashboard_js: str) -> None:
        """The desktop ``<tr>`` and the mobile card are separate markup strings."""
        body = _function_body(live_dashboard_js, "_buildUpcomingReleaseRow", "")
        assert "html:" in body and "card:" in body, "the two markup shapes moved or were merged"
        row_markup = body[body.index("html:") : body.index("card:")]
        card_markup = body[body.index("card:") :]
        assert "${rowThumb}" in row_markup, "the desktop table row lost its thumbnail"
        assert "${cardThumb}" in card_markup, "the mobile card lost its thumbnail"
