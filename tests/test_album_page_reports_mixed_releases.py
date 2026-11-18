"""The album page must SAY when a folder holds more than one release.

REQUEST

> "The page still hides the problem. It shows the album as if it had only the
>  majority's tracks. I fixed the save; I did not change the display. Worth
>  doing — the page should flag '2 files in this folder belong to a different
>  release'"

WHY IT MATTERS

``album_detail`` scopes its tracklist to ONE release group. Rows from another
release are dropped from the page, so an album whose files disagree renders as
if it were complete. That silence is what made a stale SKZ2020 id read as "the
metadata isn't saving" — the offending file was never on the page being edited.

The fix counts the rows the page had to leave out and states them, with each
other release addressable so its own rows can be edited.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
TEMPLATE = REPO_ROOT / "test_site/templates/Pages/album_detail.html"


def _handler_source() -> str:
    return UI_ROUTES.read_text(encoding="utf-8")


class TestTheHandlerReportsTheRowsItCannotShow:
    def test_it_collects_the_unscoped_rows(self) -> None:
        src = _handler_source()
        assert "_unscoped_tracks" in src, (
            "rows dropped by the release-group scope are still silently lost"
        )

    def test_it_counts_them_for_the_page(self) -> None:
        src = _handler_source()
        assert "unscoped_track_count=len(_unscoped_tracks)" in src

    def test_it_groups_them_by_their_own_release(self) -> None:
        """A count alone cannot say WHICH release the strays belong to."""
        src = _handler_source()
        assert "other_releases=_other_releases" in src
        start = src.index("_other_releases: list[dict[str, Any]] = []")
        block = src[start:start + 1600]
        assert "release_group_mbid" in block
        assert "release_mbid" in block
        assert "count" in block

    def test_a_uniform_folder_collects_nothing(self) -> None:
        """The banner must not appear on an ordinary album."""
        src = _handler_source()
        assert "_unscoped_tracks: list[dict[str, Any]] = []" in src, (
            "the collection must start empty, so an unscoped album reports zero"
        )

    def test_both_scoping_branches_collect(self) -> None:
        """An explicit group scope AND the majority-pick branch."""
        src = _handler_source()
        assert src.count("_unscoped_tracks = [t for t in tracks if t not in scoped]") >= 2


class TestTheTemplateShowsTheNotice:
    def _banner(self) -> str:
        """The notice block, spanning to its MATCHING endif.

        Nested tags mean the first ``{% endif %}`` is the inner one, so the
        block is found by counting depth rather than by searching for a token.
        """
        src = TEMPLATE.read_text(encoding="utf-8")
        start = src.index("{% if other_releases %}")
        depth = 0
        for match in re.finditer(r"{%\s*(if|for|endif|endfor)\b[^%]*%}", src[start:]):
            tag = match.group(1)
            depth += 1 if tag in ("if", "for") else -1
            if depth == 0:
                return src[start:start + match.end()]
        raise AssertionError("the notice block is not closed")

    def test_the_banner_is_conditional(self) -> None:
        assert "{% if other_releases %}" in self._banner()

    def test_it_states_the_count_in_words(self) -> None:
        banner = self._banner()
        assert "unscoped_track_count" in banner
        assert "belong to a different release" in banner

    def test_it_explains_why_they_are_absent(self) -> None:
        """The user must know the page is scoped, not that the files vanished."""
        banner = self._banner()
        # The prose is wrapped across lines, so compare on ONE line.
        assert "scoped to one release" in " ".join(banner.split())

    def test_it_links_to_that_release(self) -> None:
        banner = self._banner()
        assert "other.release_group_mbid" in banner
        assert "/album/" in banner

    def test_it_names_the_stray_tracks(self) -> None:
        assert "other.titles" in self._banner()

    def test_it_warns_editing_here_will_not_retag_them(self) -> None:
        """The exact mistake the notice exists to prevent."""
        banner = " ".join(self._banner().split())
        assert "does not re-tag them" in banner


class TestTheTemplateStaysValid:
    def test_jinja_blocks_are_balanced(self) -> None:
        src = TEMPLATE.read_text(encoding="utf-8")
        for opener, closer in (("{% if", "{% endif %}"), ("{% for", "{% endfor %}")):
            assert len(re.findall(re.escape(opener), src)) == len(
                re.findall(re.escape(closer), src)
            ), f"unbalanced {opener}"

    def test_it_parses_with_jinja(self) -> None:
        from jinja2 import Environment

        env = Environment(autoescape=True)
        env.parse(TEMPLATE.read_text(encoding="utf-8"), filename=str(TEMPLATE))
