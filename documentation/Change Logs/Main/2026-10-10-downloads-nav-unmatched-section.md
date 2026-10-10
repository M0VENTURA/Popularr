# Downloads nav anchor now targets the real unmatched-folders section (2026-10-10)

**REPORT:** "The layout in active queue and downloads has regressed on test_site.
It had an area earlier today that showed the unmatched downloads, that's now
missing. It seems to be reset to how it was set up on the original site."

**URL in the report:** `/downloads/monitor#folderGroupsSection`

## What was actually wrong (three separate facts)

1. **The nav anchor was dead.** Both trees' navbar "Downloads" link read
   `/downloads/monitor#folderGroupsSection` — but the `folderGroupsSection`
   element was **removed from the monitor page on 2026-09-07** (`e649e1df`
   "Remove Download Queue Groups from monitor.html"). The matched/unmatched
   area on the page today is `#unmatchedFoldersSection`. So clicking
   "Downloads" scrolled to a section that no longer exists: the unmatched
   downloads area never came into view, even though it was on the page. Fixed
   in BOTH `templates/base.html` and `test_site/templates/base.html` to point
   at `#unmatchedFoldersSection`.

2. **The unmatched area is data-gated, and that is by design.** The monitor
   template ships `#unmatchedFoldersSection` with `style="display:none"`;
   `static/js/monitor.js`'s `renderUnmatchedFolders()` shows it only when
   `/api/downloads/unmatched-folders` returns non-empty folders (and hides it
   at zero). "The area disappeared" is therefore *also* explained by "there are
   currently no unmatched downloads" — the card correctly folds away when the
   downloads folder has nothing to show.

3. **On test_site, `/downloads/monitor` serves the LIVE page — this is the
   shadow, not a regression.** `test_site/templates/Pages/downloads/monitor.html`
   is a stray artist-page snapshot and is in `_SHADOWED_TEMPLATES`
   (`helpers/test_site_mode.py`), so the rebuilt tree's request for that
   template falls through to the live `templates/pages/downloads/monitor.html`.
   That is exactly why the page "looks like the original site" — it IS the
   original site's page, served deliberately because the rebuilt copy is a
   known-bad artifact that cannot be deleted from the vfs. The live page has
   the unmatched area; once unmatched downloads exist, it renders.

## Change

- `templates/base.html` + `test_site/templates/base.html`: the Downloads nav
  link now targets `#unmatchedFoldersSection` (the real section id).

## Tests

`tests/test_downloads_nav_unmatched_section.py` (4):

- live navbar links Downloads at `#unmatchedFoldersSection`, never the dead
  `#folderGroupsSection`, and every `/downloads/monitor#...` target resolves to
  an element that actually exists on the live monitor page;
- the rebuilt navbar carries the same fixed anchor;
- the live monitor template provides `#unmatchedFoldersSection`;
- the live `monitor.js` still contains both the hide-when-empty and
  show-when-data branches (the visibility contract is pinned).

Regression sweep: `test_test_site_shadowing`, `test_artist_page_contract`,
`test_rebuilt_pages_have_no_dead_buttons`, `test_template_macro_imports_resolve`,
`test_manual_search_button_works_in_test_site`, `test_item_groups_bindactions_this`,
`test_artist_upcoming_releases` — all pass.