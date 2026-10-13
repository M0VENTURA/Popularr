# Navbar search: segmented oversized field → one clean pill (2026-10-13)

## What was reported
"The search still doesn't look clean its just bigger."

The `408cff14` promotion made the navbar search field large (input-group-sm →
input-group-lg + a pill wrapper) but kept the old **segmented input-group
structure**, so it read as a bigger version of the same messy control.

## What the markup/CSS actually produced (verified from the screenshot)
- **TWO magnifying glasses** — a leading `input-group-text` icon AND a
  trailing `btn` search button whose click did exactly what the input's
  `onfocus`/Enter already do (open/submit the unified search).
- **Internal border dividers** — every segment (`input-group-text`, `btn`)
  carried `bg-dark border-secondary`, so the pill was drawn as four joined
  boxes with visible joins instead of ONE outline.
- **Clipped placeholder** — the four segments left the input ~half its
  width, and "Search library or MusicBrainz…" truncated to "Search li".

## The clean contract (test_site only — the live tree keeps its compact
## `input-group-sm` field and was deliberately untouched)
1. **One magnifying glass.** The redundant right-hand search button is
   removed; the pill is icon + input + Ctrl+K badge.
2. **One pill outline.** `.nav-search-input-group` owns the single
   `border: 1px solid var(--border-color)` + `background: var(--secondary-bg)`
   and `overflow: hidden` clips every child into the pill radius. The segment
   elements are `border: 0; background: transparent;` — no internal dividers.
3. **No double focus ring.** The input's own Bootstrap `box-shadow` is
   neutralized inside the group (`&:focus { box-shadow: none; }`) so the
   group's `:focus-within` green glow is the only focus treatment.
4. **A placeholder that cannot clip.** `placeholder="Search…"` — concise and
   standard for a command-palette field; the full context stays in the
   `aria-label="Search library or MusicBrainz"` and the wrapper `title`.

The Ctrl+K badge keeps `d-none d-lg-flex` (hidden where the navbar is tight),
the Enter path (`submitUnifiedSearch`) is unchanged, and `input-group-lg` +
the pill are preserved — this is a *cleanliness* fix, not a prominence rollback.

## Guards
`TestTheSearchReadsAsOnePill` (5 tests in
`tests/test_navbar_system_menu_and_search_shortcut.py`): exactly ONE
`bi bi-search` in the search group; no `btn btn-outline-secondary` search
button; the input tag no longer carries the long placeholder; the group CSS
carries its own border + background + `overflow: hidden`; the input's focus
box-shadow is neutralized. Plus the pre-existing `d-none d-lg-flex` hint
assertion updated to the new (borderless) class string — intent unchanged.

146 navbar/search-contract tests + 327 layout/CSS/dead-button tests pass;
`get_errors` clean on all three files.