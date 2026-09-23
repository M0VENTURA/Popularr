# Navbar System menu + Ctrl+K search shortcut

**Date:** 2026-09-23
**Area:** `test_site/templates/base.html`, `test_site/static/js/ui/search-flyout.js`,
`test_site/static/css/popularr.css`

Scope: **`test_site/` only** (the rebuilt UI tree), as requested. The live tree
is unchanged, so these appear when `features.use_test_site` is enabled.

## Why

An evaluation of the rebuilt UI reported that the navbar packs ten top-level
items plus the search box, custom bookmark dropdowns, three icon-only system
links and a user menu, causing wrapping and awkward spacing on
1024px–1280px viewports.

Two claims from that report were checked against the code and **not** acted on:

* **A central z-index scale already exists** (`popularr.css`, `--z-dropdown`
  … `--z-search-backdrop`). The report's proposed duplicate set would have
  *regressed* it: it suggests `--z-player-bar: 1030`, but the existing `1029` is
  deliberate — the player bar is a flex footer, not an overlay, so it must sit
  below the navbar. Nothing in that section was changed.
* The badge-replacement snippets in the report used `max-w-md`, which does not
  exist in this codebase (Bootstrap 5 has no such utility), so the class would
  have silently applied no constraint. No template was changed for that item.

## 1. System dropdown

The offending elements were three `d-none d-lg-block` links carrying only a
`title` and an icon:

```html
<li class="nav-item d-none d-lg-block"><a class="nav-link px-2" … title="Logs"><i class="bi bi-file-text"></i></a></li>
```

Between the `lg` and `xl` breakpoints every *primary* label is hidden by
`d-lg-none d-xl-inline`, so those icons sat beside unlabelled page links with
nothing to distinguish a destination from a utility. "Tag Corrections" was a
fourth utility, buried in the Artists dropdown.

All four now live in one **System** dropdown (gear-wide-connected icon, label
shown at `xl` and up). Removed: three top-level items and three of the
`d-lg-none` duplicates in the user menu.

⚠️ **`Tag Corrections` is deliberately listed in BOTH the Artists menu and the
System menu.** It is genuinely both a tagging tool and a utility, and the
Artists menu is where returning users look for it; removing it there to satisfy
a "one home per page" rule would have made it harder to find, not easier.

The user menu keeps account-scoped actions only (Logout), with `dropdown-menu-end`
so it still aligns to the viewport edge.

**Result: top-level navbar items 10 → 8** (Dashboard, Artists, Downloads,
Playlists, Discover, Missing, System, User), and three icons → one.

## 2. Ctrl+K / ⌘K search shortcut

* The search box gains a `<kbd>` hint rendered as a **sibling of the input
  inside the `input-group`**, not an absolutely-positioned overlay — an overlay
  would sit on top of the text the user is typing. It is `d-none d-lg-flex`,
  because below `lg` the search row wraps to its own line and the badge would
  crowd the input it labels.
* `aria-keyshortcuts="Control+K Meta+K"` advertises the binding to assistive
  tech.
* The handler is bound on `document`, not on the input: the point of a shortcut
  is to work when focus is anywhere, and one that only fires after clicking into
  the box saves nothing.
* The modifier check is `(e.ctrlKey || e.metaKey)` with the other **excluded**,
  so Ctrl+Cmd+K does not also match. The key is compared case-insensitively,
  because `key` is `'k'` bare and `'K'` when Shift is part of the combination.
* The combination is **ignored inside text fields** (`input`/`textarea`/`select`
  /`contenteditable`) so the browser's own binding is not hijacked while typing.
  The guard is asserted to precede the `openUnifiedSearch()` call — a guard
  placed after it would guard nothing.
* The `<kbd>` label is rewritten to `⌘` on Apple hardware via `userAgentData`
  (falling back to `navigator.platform`/`userAgent`). Cosmetic only, and
  wrapped in `try`/`catch` so an unavailable API cannot break search.

## Tests

`tests/test_navbar_system_menu_and_search_shortcut.py` (20) — source-contract
assertions (there is no browser harness). The System-menu checks assert the
destinations are reachable **from inside the dropdown**, not merely present
somewhere in the file, because the old markup also contained all four URLs. It
also re-asserts the pre-existing banner-Enter contract (`test_unified_search_enter_submits`
guards it too, and passes).

**Oracle:** changes stashed → 13 failed / 7 passed; restored → 20 passed. The 7
that pass unpatched are the deliberate contract-preservation assertions.

**Regression sweep** (21 suites, 641 tests): failing set IDENTICAL before and
after — 0 regressions. The 15 pre-existing failures are Navidrome/network
harness issues (`createPlaylist`/`updatePlaylist` transport, album art,
genre fallback) plus the known `static/js/artist_detail.js` Jinja case.

`node --check` passes on the modified JS; `import app` → 392 routes.

## Not done

The report's other recommendations (shared `page-header`, badge hygiene,
`data-mobile-cards` for `corrections.html`/`metadata_compare.html`,
`logs.html` scroll-to-bottom) were confirmed as real but left out of this
change. Two carry specific hazards documented for whoever picks them up:

* the `corrections.html` conflicts table is **JS-filled** (`conflicts-tbody`,
  written by `corrections.js`), so adding `data-mobile-cards` without
  `data-label` on every generated `<td>` **removes** the headers below 768px —
  strictly worse than not opting in, and guarded by
  `tests/test_layout_css_js_template_integrity.py`;
* an `.empty-state` class already exists in `analytics.css` (page-scoped), so a
  shared component must not reuse that name.
