# Enter now starts the universal search (banner input)

**Date:** 2026-09-21 · **Area:** ui / search

## Reported

"Typing an entry into the universal search on the banner and pressing enter
doesn't search. I need to select Library, All or External before a search is
initiated. I want enter to start the search too."

## Cause

The flyout has two inputs, and only one of them could submit:

| Input | Enter behaviour |
|---|---|
| `#unifiedSearchInput` (inside the flyout) | ✅ `keydown` → `runSearch()` |
| `#navSearchInput` (the banner box) | ❌ inline `onkeydown` → `syncNavSearchQuery()` — copied the text across and stopped |

The banner box is the one that matters, because `openUnifiedSearch()` **focuses
it back** (`if (navInput && navInput !== document.activeElement) navInput.focus()`),
so the flyout's own input — whose handler works — rarely holds focus. And the
inline markup could never do better: `runSearch` is declared *inside* the search
module's closure (`static/js/unified_search.js` / `test_site/static/js/ui/search-flyout.js`)
and is not exported, so an `onkeydown` attribute had no way to reach it.

Clicking Library / All / External worked because that click handler is
`selectScope(...) + runSearch()` — which is exactly the reported
"must select a scope first" behaviour: the tab click was the only path to a
search.

## Fix

Both trees, one shared shape:

* Each search module now exposes **`submitUnifiedSearch()`** (on `window` /
  `global`). It takes the query from *the box the user is actually typing in*
  (`document.activeElement`, falling back to the banner), copies it into the
  flyout input only when a **different** box was active — so an edit made inside
  the flyout can never be clobbered — then calls `runSearch()` and collapses the
  advanced-filter panel.
* It deliberately calls `runSearch()` **directly** rather than routing through
  `openUnifiedSearch()`, whose `if (prefill === _lastQuery && _scope === _lastScope) return;`
  guard would swallow a re-submit of the same text. Pressing Enter is an explicit
  instruction, so it always searches.
* The module binds `keydown` on `#navSearchInput` **and**
  `#dashboardTopSearchInput` (guarded — neither tree's markup currently renders
  the dashboard box), calling `preventDefault()` so a surrounding form cannot
  submit the page.
* The misleading inline `onkeydown` was **removed** from both `base.html` files,
  because the handler now lives where `runSearch` does. `oninput="syncNavSearchQuery()"`
  stays: the two boxes keep mirroring each other as the user types.

Scope: the current scope is untouched — Enter uses whatever scope is selected
(default "All"), which is what "I want enter to start the search too" asks for.
The existing scope-tab clicks still search.

## Verification

`tests/test_unified_search_enter_submits.py` (new, 12 tests) asserts, for **both**
trees (the live tree is the default, so a `test_site`-only fix would not be seen):

* the banner markup carries no Enter handler that only calls
  `syncNavSearchQuery()` (the defect itself), while `oninput="syncNavSearchQuery()"`
  is still present;
* each search module defines and exports `submitUnifiedSearch`, its submit path
  calls `runSearch()`, and the Enter handler reaches `submitUnifiedSearch`.

| Tree | Result |
|---|---|
| Before (this commit's source reverted, test kept) | **10 failed / 2 passed** — each naming the defect |
| After | **12 passed** |

ⓘ One assertion was deliberately **deleted** during development: "Enter is bound
near `#navSearchInput`" passed on the *unpatched* tree (the flyout input's own
listener sits within the search window of that id), so it proved nothing. The
guard asserts the binding through to the submit call instead.

Also verified: `node --check` passes on both modules, and the template/JS contract
suites (`test_static_js_is_not_jinja`, `test_layout_css_js_template_integrity`,
`test_no_python_syntax_errors`, `test_artist_page_contract`) give **364 passed, 1
skipped, 1 failed** — that one failure is the pre-existing
`static/js/artist_detail.js` "Jinja served as JS" case this change does not touch.
