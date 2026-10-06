"""Albums are scoped by RELEASE GROUP, not by year.

Reported
--------
> [Battlefield Vietnam … /2004] and [Battlefield Vietnam … /1963] — this is
> what I mean, the same release, but split in years. The same Release MBID is
> assigned but has two different pages to browse to under test_site.

And:

> Ones that do get split should be viewable under the artist page to be able to
> browse to each release properly to edit the information.

Why year was never a sound identity
-----------------------------------
A compilation credits its source *recordings*, so the tracks of ONE release
carry many different years. `2003 - Battlefield Vietnam` holds tracks tagged
1963 … 2004 — and the old rule (`album_key = name + year`) sliced that single
release into one page per year. The screenshots show it exactly: `…/2004`
listed 7 tracks (2, 7, 11, 13–17) and `…/1963` listed 16 (1–9, 11–17), both
from the same folder, both headed "(2004)".

The rule now
------------
* an explicit **UUID** segment → that release group;
* **one** release group on the rows → *no split at all*, even when the URL
  carries a year (old `/2004` links stop slicing, and the dashboard keeps
  building them);
* several release groups sharing a name → the one with the most tracks, with
  the artist page listing **each** of them as its own browsable card;
* **no** release-group data → the old year split, unchanged.

Digits still mean a year, so every existing link keeps working.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
LIVE_SECTION = REPO_ROOT / "templates" / "components" / "_album_category_section.html"
REBUILT_SECTION = REPO_ROOT / "test_site" / "templates" / "components" / "_album_category_section.html"


class TestTheAlbumPageKnowsItsScope:
    def test_a_uuid_segment_is_read_as_a_release_group(self):
        src = UI_ROUTES.read_text(encoding="utf-8")

        assert "_RG_SEGMENT_RE" in src, "the path segment is never parsed as a UUID"
        assert "if _RG_SEGMENT_RE.fullmatch(album_year_seg) else \"\"" in src, (
            "the segment must be classified BEFORE it is treated as a year — "
            "a UUID is not a number and would otherwise select nothing"
        )

    def test_a_single_release_group_never_splits(self):
        """The reported bug, stated as code."""
        src = UI_ROUTES.read_text(encoding="utf-8")

        assert "elif len(release_groups) == 1:" in src, (
            "one release group must produce one page"
        )
        # The single-RG branch must come BEFORE the explicit-year branch, or a
        # year URL (which the dashboard still builds) slices the release again.
        single = src.index("elif len(release_groups) == 1:")
        by_year = src.index("elif explicit_year and album_year_filter is not None:")
        assert single < by_year, (
            "an explicit year URL must not beat a single release group — that "
            "is exactly the /2004 vs /1963 split that was reported"
        )

    def test_several_release_groups_default_to_the_largest(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "elif len(release_groups) > 1:" in src
        assert "_primary = max(counts, key=lambda r: counts[r])" in src, (
            "with several releases sharing a name the page must pick one "
            "deterministically rather than merging them"
        )

    def test_no_release_group_data_falls_back_to_the_year_split(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        branch = src[src.index("else:", src.index("elif len(release_groups) > 1:")):]
        assert "len(all_album_years) > 1" in branch, (
            "an album with no release-group data must keep the year split"
        )


class TestTheArtistPageGroupsByRelease:
    def test_the_group_key_is_built_from_the_release_group(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "release_groups_by_name.setdefault" in src, (
            "the artist page still keys albums by name + year — one release "
            "would keep appearing as several cards"
        )
        assert 'album_key = f"{_name_key}::rg:' in src

    def test_each_card_carries_the_release_group_for_its_link(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert '"release_group_mbid": entry_rg,' in src, (
            "a card without a release group cannot link to a release-scoped "
            "album page, so split releases would not be browsable"
        )

    def test_the_tracks_are_collected_with_the_same_key(self):
        """Not re-derived from name + year — that would mis-fill an RG card."""
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "tracks_by_key.setdefault(album_key, []).append(track)" in src
        assert "tracks_by_key.get(album_key, [])" in src


class TestTheLinksPreferTheReleaseGroup:
    def test_both_trees_scope_their_links(self):
        for path in (LIVE_SECTION, REBUILT_SECTION):
            html = path.read_text(encoding="utf-8")
            assert "{{ album_scope }}" in html, (
                f"{path.name} still links by year only"
            )
            assert "album_rg" in html, (
                f"{path.name} never computes the release-group scope"
            )

    def test_the_old_year_segment_is_gone_from_the_links(self):
        for path in (LIVE_SECTION, REBUILT_SECTION):
            html = path.read_text(encoding="utf-8")
            assert "/{{ album.get('album_year') }}{% endif %}" not in html, (
                f"{path.name} still appends a year segment directly — the "
                "release group must win when it exists"
            )
