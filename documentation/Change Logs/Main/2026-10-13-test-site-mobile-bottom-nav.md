# test_site: mobile bottom navigation (2026-10-13)

## What

A phone-first tab bar pinned to the bottom of the viewport, exposing only the
destinations people reach for constantly: **Home · Artists · Downloads ·
Playlists · Search**. Everything else stays in the hamburger menu.

## Why it is a flex footer item, not `position: fixed`

The bar is a **flex child of `<body>`**, exactly like `#globalPlayerBar` and
`#scanStatusBar`:

```
<main>                     flex: 1, min-height: 0, overflow-y: auto
#globalPlayerBar           flex-shrink: 0
#scanStatusBar             flex-shrink: 0
.bottom-nav                flex-shrink: 0   ← new
```

`main` is the only scroll container and it **shrinks** to make room, so the bar
can never cover page content. `position: fixed` would detach it from the flex
column, break the `--footer-stack-height` maths, and reproduce the exact bug the
inline `position: fixed` on the player bar used to cause (content hidden behind
a footer).

## Footer-stack maths

A new token keeps every existing consumer correct:

```css
--bottom-nav-height: 0px;                                   /* desktop */
@media (max-width: 991.98px) { --bottom-nav-height: 3.5rem; }

--footer-stack-height: calc(var(--status-bar-height) + var(--bottom-nav-height) + env(safe-area-inset-bottom, 0px));
body.player-visible   { --footer-stack-height: calc(status + player + bottom-nav + safe-area); }
```

So `0` on desktop means the footer stack is **unchanged** there, and the
`.sticky-save-bar` (which sits at `bottom: var(--footer-stack-height)`) stays
above the nav on a phone instead of behind it.

`env(safe-area-inset-bottom)` is included because the bar is the bottom-most
element and would otherwise sit under the iOS home indicator. It only reports a
non-zero inset because `base.html` sets `viewport-fit=cover` (added in the
previous pass) — the two changes are coupled.

## Details

- **Hidden at lg and up** via its own media query (not a Bootstrap utility), so
  the `display: none` → `display: flex` switch and the `--bottom-nav-height`
  collapse stay together in one place.
- **44px minimum touch target** on every item; the label is not the only
  tappable part.
- **Active state is decided SERVER-SIDE** from `request.path` — no JS, so the
  bar is correct on first paint instead of flashing the wrong tab. It sets
  `aria-current="page"` as well as a colour, and the colour is paired with a
  top indicator bar because colour alone is weak on a dark theme and vanishes in
  forced-colours mode.
- **"Search" is a `<button>`, not a link** — it calls `openUnifiedSearch()`
  (the same global the navbar's search button uses), so it must not navigate.
- Destinations map to their sections: `/artists` **and** `/artist/<name>` →
  Artists; `/playlists` **and** `/playlist/<id>` → Playlists; `/downloads/*` →
  Downloads.

## ⭐ A bug the render probe caught

`/` **302s to `/dashboard`**, so the real dashboard path is `/dashboard` — and
the first version only matched `_nav_path == '/'`, leaving Home **inactive on
the page it represents**. The condition now accepts both. This was found by
actually rendering the template and printing the active item per path, not by
reading the condition — the source looked correct.

`request.path` had **never been used in any template in this tree**, so its
availability in the Jinja context was verified by rendering
(`TestRequestIsAvailableInTemplates`) rather than assumed.

## Tests

`tests/test_test_site_mobile_and_pwa.py` 53 → **63**:

- `TestMobileBottomNav` (static) — present in base.html, a flex footer item and
  NOT fixed, the five destinations, Search being a button, hidden at lg with
  `display: flex` only below, the height token 0/3.5rem, the footer stack
  including it in BOTH the base and player-visible values, safe-area padding,
  44px targets, and the active treatment not relying on colour alone.
- `TestRequestIsAvailableInTemplates` — renders `{{ request.path }}`.
- `TestBottomNavActiveState` — renders the real `test_site/templates/base.html`
  (loader swapped for the render, since the test app serves the LIVE tree by
  default) and asserts the active tab for nine paths, that all five render in
  order, that `aria-current` agrees with the highlight, and that no more than one
  tab is ever active.

Sweep of 10 layout/navbar/dashboard/template suites: the only failure is the
long-documented pre-existing
`test_static_js_is_not_jinja[static/js/artist_detail.js]` (LIVE tree, untouched)
→ **0 regressions**.

## Notes

- test_site only, per the standing instruction. Live is the DEFAULT mode, so the
  bar appears only when `features.use_test_site` is on.
- `test_shared_module_copies_do_not_drift.py` does not exist in this tree.
