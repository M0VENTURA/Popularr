"""The "Downloads" nav link must target the section that actually exists.

REPORT (2026-10-10): on test_site, the Downloads page "reset to how it was set
up on the original site" and the unmatched-downloads area could not be reached.

Diagnosis:

* Both trees' navbar pointed "Downloads" at
  `/downloads/monitor#folderGroupsSection` — but the `folderGroupsSection`
  element was REMOVED from the monitor page in `e649e1df` (2026-09-07,
  "Remove Download Queue Groups from monitor.html"). The current monitor
  template renders the matched/unmatched area as `#unmatchedFoldersSection` (a
  card hidden by default and revealed by `renderUnmatchedFolders()` only when
  `/api/downloads/unmatched-folders` returns folders). So the anchor was dead:
  clicking Downloads landed on the page top with the unmatched area invisible
  in the URL's own scroll target.

* On test_site, `/downloads/monitor` is SHADOWED (the test_site copy is a
  stray artist-page snapshot) and falls through to the LIVE monitor page —
  which is why the layout looks like "the original site". That is the
  shadowing design documented in `helpers/test_site_mode.py`, not a
  regression; the live page is the correct page and shows the unmatched area
  when unmatched downloads exist.

These tests pin the fix: the nav anchor must name a section that the served
monitor page actually provides, in BOTH trees, so the unmatched-downloads area
is reachable from the navbar again.
"""

from __future__ import annotations


def _nav_targets(source: str) -> list[str]:
    """All "/downloads/monitor#..." hrefs in a base.html."""
    import re

    return re.findall(r'/downloads/monitor#([A-Za-z0-9_-]+)', source)


def _element_ids(template_source: str) -> set[str]:
    import re

    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', template_source))


class TestDownloadsNavPointsAtARealSection:
    def test_live_nav_targets_the_unmatched_section(self):
        from pathlib import Path

        base = Path(__file__).resolve().parents[1] / "templates" / "base.html"
        monitor = Path(__file__).resolve().parents[1] / "templates" / "pages" / "downloads" / "monitor.html"

        targets = _nav_targets(base.read_text(encoding="utf-8"))
        ids = _element_ids(monitor.read_text(encoding="utf-8"))

        assert "unmatchedFoldersSection" in targets, (
            "the navbar must point Downloads at the section that shows "
            "matched/unmatched folders"
        )
        assert "folderGroupsSection" not in targets, (
            "folderGroupsSection was removed from the monitor page in "
            "e649e1df; linking to it scrolls nowhere"
        )
        for target in targets:
            assert target in ids, (
                f"nav anchor #{target} does not exist on the monitor page — "
                "the link scrolls to nothing"
            )

    def test_test_site_nav_targets_the_unmatched_section(self):
        """The rebuilt tree must carry the same (already-fixed) anchor."""
        from pathlib import Path

        base = Path(__file__).resolve().parents[1] / "test_site" / "templates" / "base.html"

        targets = _nav_targets(base.read_text(encoding="utf-8"))
        assert "unmatchedFoldersSection" in targets, (
            "the rebuilt navbar must use the same anchor as the live one"
        )
        assert "folderGroupsSection" not in targets

    def test_the_monitor_page_provides_the_unmatched_section(self):
        """The served (live) monitor page has the card the nav links to.

        On test_site the rebuilt monitor copy is shadowed and the LIVE page is
        what actually renders, so the anchor must resolve against the LIVE
        template (this is the page the user reaches).
        """
        from pathlib import Path

        monitor = Path(__file__).resolve().parents[1] / "templates" / "pages" / "downloads" / "monitor.html"
        ids = _element_ids(monitor.read_text(encoding="utf-8"))
        assert "unmatchedFoldersSection" in ids


class TestUnmatchedAreaIsStillDataDriven:
    def test_the_live_monitor_js_hides_the_section_when_empty(self):
        """Guard the visibility contract so a future change cannot silently
        remove the area either way."""
        from pathlib import Path

        js = (
            Path(__file__).resolve().parents[1] / "static" / "js" / "monitor.js"
        ).read_text(encoding="utf-8")
        # Both the hide-when-empty and the show-when-data branches must exist,
        # and the section id the JS targets must match the template.
        assert "folders.length === 0" in js
        assert "unmatchedFoldersSection" in js
        assert "section.style.display = 'block'" in js