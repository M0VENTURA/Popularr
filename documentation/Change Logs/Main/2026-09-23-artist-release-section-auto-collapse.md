# Artist page: collapse release sections that have nothing in the library

**Date:** 2026-09-23
**Area:** `ui`

## What was asked

> "On an artist page, if a release type has 0 showing in Library, I want that
> section minimized so on the page you can easily scroll past. It should still
> show the total albums in missing above the section and allow it to be expanded
> manually."

## What changed

A release category with **nothing owned** now renders collapsed. The decision is
based on the **library count**, not the total — a section of purely-missing
releases is exactly the case to fold away, so keying off `items|length` would
have left it open.

The information is not hidden, only folded:

* the header keeps the `N missing` badge (and the `All / Library / Missing`
  counts), so the total is visible above the collapsed body;
* the toggle still uses Bootstrap's collapse, so it expands manually;
* a short hint — *"none in your library — click to show N"* — accompanies it,
  because a rotated chevron on its own reads as a dead header rather than a
  section that opens.

`data-auto-collapsed` records the initial state so the page script can tell an
auto-collapsed section apart from one the user opened.

## Expand All / Collapse All had to be fixed too

`expandAll(expand)` only revealed **tracklists inside already-open sections** — it
never touched the section bodies. So "Expand All" would have been unable to
reveal a section that auto-collapsed, which is precisely the case a user would
click it for.

* **Expand All** now opens every section body as well.
* **Collapse All** returns each section to **the state the server rendered**
  (`data-auto-collapsed`), rather than shutting everything — otherwise a section
  the server deliberately left open would be closed by a button labelled
  "Collapse All" in a way the page's own layout contradicts.

`aria-expanded` is set directly rather than driven off the collapse event:
collapsing a section whose body is *already* shut fires no event, which would
leave the chevron and the hint stale.

## Files

| File | Change |
|---|---|
| `templates/components/_release_section.html` | conditional `collapse` / `show`, `data-auto-collapsed`, hint |
| `test_site/templates/components/_release_section.html` | same (rebuilt tree) |
| `static/js/artist-releases.js` | `setSectionExpanded`, `restoreInitialSectionState` |
| `test_site/static/js/pages/artist-releases.js` | same (rebuilt tree) |
| `static/css/artist.css` | `.release-collapsed-hint` styling |
| `test_site/static/css/artist.css` | same (rebuilt tree) |

Both trees were updated: `helpers/test_site_mode` serves the rebuilt tree first,
so a single-tree edit is invisible to whichever tree the user is on.

## Tests

**NEW `tests/test_release_section_auto_collapse.py`** (35 tests).

Behaviour is verified by **rendering the shipped macro through the real app's
Jinja environment**, not by grepping the template:

| Input | Asserted |
|---|---|
| 0 library items, 3 missing | body renders `collapse` (shut), `aria-expanded="false"`, `3 missing` badge present, hint present, toggle target wired |
| 1 library item | body renders `collapse show`, `aria-expanded="true"`, no hint |
| all owned | no warning badge in the header |
| no items | section not rendered at all |

**Oracle:** with the change stashed, **23 failed / 12 passed**; restored,
**35 passed**.

**Regression sweep** (5 artist-page / template / CSS suites, 458 tests): failing
set **identical** before and after — `Compare-Object` diff **EMPTY**, i.e.
**0 regressions**. All five failures are pre-existing:

* `test_artist_tracklist_loading.py` (4)
* `test_static_js_is_not_jinja.py::...[static\js\artist_detail.js]` (1)

Both Jinja templates still parse under the real loader, and `node --check` passes
on both release scripts.
