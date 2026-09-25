# Manual Soulseek Search button was dead on /downloads/monitor in test_site

**Date:** 2026-09-25
**Area:** `test_site/templates/components/modals/_soulseek_manual_search.html`
**Reported:**

> The button still doesn't work on the test_site.

## Root cause

The rebuilt partial deliberately carries **no inline `onclick`** and relied on
`services/slskd.js` to bind the button on `DOMContentLoaded`. That is correct on
the pages that load `slskd.js` — but the **monitor PAGE is shadowed to the live
tree** (`helpers/test_site_mode.py::_SHADOWED_TEMPLATES`, because the rebuilt copy
was a stray artist-page snapshot). So in test_site mode the monitor page was
rendered from `templates/pages/downloads/monitor.html`, which loads
`static/js/downloads.js` and **not** `services/slskd.js`.

Measured before the fix:

| page | mode | inline onclick | loads slskd.js |
|---|---|---|---|
| `/downloads/monitor` | live | yes | — |
| `/downloads/monitor` | **test_site** | **no** | **no** |
| `/downloads` | test_site | no | yes (works) |

Neither binding path existed on the monitor page, so the click did nothing — and
did so **silently**, because the page still returned 200.

### Why this took three attempts to find

**Every probe I ran rendered with the DEFAULT config, which inspects the LIVE
tree — where the button works.** Verifying this needs
`features.use_test_site: true` in the config the app reads. Any future check of
this area must enable the cutover first, or it tests the wrong tree.

There was also an existing guard, `TestManualSearchModalIsWired`, whose docstring
says *"The click was dead, so nothing downstream was ever exercised."* It checked
only that the **queue** page loads `slskd.js` — never the monitor page. So the
one page that breaks was the one page not covered.

## The change

The partial is now **self-contained**: the button calls a NAMED
`runSoulseekManualSearchFromModal`, defined in an inline `<script>` in the same
partial, which resolves the implementation **at click time**:

* `services/slskd.js` → `global.runSoulseekManualSearch` (queue page)
* `static/js/downloads.js` → the same name (monitor page)

With neither present the user gets an explanation instead of a button that
appears dead. A guard prevents a second definition if the partial is included
twice.

⚠️ **Named, not an anonymous IIFE.** My first attempt used
`onclick="(function(b){…})(this)"`, which the dead-button scanner
(`tests/test_rebuilt_pages_have_no_dead_buttons.py`) reads as a call to
`function(...)`, cannot resolve, and reports as a **false** dead button. The
scanner was right to complain and the fix was to satisfy its model rather than
weaken it — a named function is clearer markup anyway.

### Double-binding, checked rather than assumed

On the queue page BOTH the inline handler and `slskd.js`'s own
`addEventListener('click')` fire. Measured with a real click: **exactly one**
search POST, because `runManualSearch` runs through
`buttonState.withBusy`, whose `BUSY_FLAG` guard refuses the duplicate. So the
fallback does not double-queue a search.

## Verification

**NEW `tests/test_manual_search_button_works_in_test_site.py` (12)** plus **3 new
guards** in `tests/test_item_groups_bindactions_this.py`. The behavioural tests
drive the **real shipped handler** in Node and fire a real click — inline handler
*and* every registered listener — for all three page shapes:

| scenario | expected |
|---|---|
| queue page (`slskd.js` + `button-state.js`) | exactly 1 search POST |
| monitor page (`downloads.js` only) — the broken case | exactly 1 search POST |
| neither script | 0 POSTs + a message, never silence |

Plus pins on the premise, so the fallback can be removed if it stops being
needed: the monitor page **is** in `_SHADOWED_TEMPLATES`, the live monitor page
**does not** load `slskd.js`, and **does** load `downloads.js`.

**Mutation-verified 5/5 CAUGHT**: dropping the inline `onclick`, making the
handler a no-op, removing the alert fallback, renaming the handler, and
loading `slskd.js` on the live monitor page (breaking the premise itself).

**Oracle** (worktree at `a389add8`): sweep over
`slskd/soulseek/modal/button/item_groups/layout/static/template/contract`.
**Zero new failures** — the count returns to the baseline's 14.

## Deployment note

The partial change lives in `test_site/`, so it affects **test_site mode** only;
the live tree's partial is untouched and still works (every live page that
includes it loads `downloads.js`). No restart is needed beyond the usual image
rebuild, since templates are read per request.
