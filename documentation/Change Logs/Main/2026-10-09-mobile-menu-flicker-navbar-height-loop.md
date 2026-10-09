# The mobile menu no longer fights the navbar-height token (flicker)

**Date:** 2026-10-09 - **Area:** `ui` - mobile navbar
**Reported:**

> When pressing the hamburger menu in mobile view, the bottom section under
> discover keeps flickering

## Root cause - a feedback loop, not a layout mistake

Two things reference each other:

1. `static/js/main.js::syncNavbarHeight()` sets `--navbar-height` from the
   **navbar's own measured height** (`nav.offsetHeight`), and a
   `ResizeObserver` on the navbar re-runs it whenever that changes;
2. **`.navbar-collapse.show` is clamped by that same token** -
   `max-height: calc(100dvh - var(--navbar-height) - 0.5rem)`.

So while the menu is OPEN, the navbar being measured *includes the menu*:

    menu grows -> navbar taller -> token grows -> max-height shrinks
    -> menu clamps -> navbar height changes -> ResizeObserver fires -> ...

The token settles on **two** values and sweeps between them forever, and every
consumer of the token moves on each sweep - `main`'s `padding-top`, the
dashboard's `min-height`, the sticky footer. On a 720px-tall phone the sweep is
**540px <-> 282px**, i.e. the menu only ever gets 172px of the 430px it needs,
which is why the visible symptom is at the **bottom** of the menu.

`tests/js/mobile-navbar-flicker-probe.js` reproduces it against the real
function - the pre-fix file reports `stable: false, sequence: [540, 282]`.

## The fix

`syncNavbarHeight()` now measures the **collapsed bar**:

* the open collapse is **subtracted** from the measured height
  (`navbarCollapseIsOpen()` -> `height -= collapse.offsetHeight`), covering both
  Bootstrap's `show` and the mid-animation `collapsing` state;
* a `max-width: 991.98px` guard keeps that subtraction to the mobile layout -
  at `lg` and up the toggler is hidden and the collapse sits **inline in the
  row**, so it *is* part of the bar's real height and subtracting it would
  under-measure.

The token therefore means "how tall the collapsed bar is" - exactly what
`main` has to clear. The open menu **overlays** the page instead of moving it,
and because the clamp measures from the collapsed bar the menu now gets the
whole remaining viewport (430px where it had 172px).

A comment beside `.navbar-collapse.show` in both `popularr.css` files records
the invariant, so the rule and the script are not "simplified" back into a loop.

## Tests

`tests/test_mobile_navbar_no_flicker.py` - **18**:

* **behaviour** (both trees, via the node probe): pressing the hamburger does
  not make the token oscillate; the token equals the collapsed bar; the menu
  gets the rest of the viewport; and it holds at 560 / 720 / 900px;
* **structure**: the open collapse is excluded, both animation phases are
  covered, the `991.98px` guard is present, and - as a control - the
  `ResizeObserver` and the measurement itself are **still there**, so the fix
  cannot be "solved" by never measuring;
* both stylesheets still clamp the menu from the token.

`tests/js/mobile-navbar-flicker-probe.js` extracts the real function, drives the
ResizeObserver/CSS loop in a stub DOM whose layout obeys the same rule, and
prints one JSON line (`stable`, `unique_tokens`, `applied_token`, `menu_height`).

**Oracle** - the four source files reverted (both `main.js` and both
`popularr.css`): **15 failed, 3 passed**; restored: **18 passed**. The 3 that
pass pre-fix are the contract-preservation controls.

**Sweep** - the 7 test files touching `main.js` / navbar / `popularr.css`:
**0 failures on both sides**.

`node --check` clean on both `main.js` files and the probe.
## Follow-up — the WIRING is now pinned per tree (the report was the rebuilt UI)

The flicker was reported on the **rebuilt (`test_site`) UI**. That tree serves
its own copies of everything, so the fix is only effective if **both** roots
carry it and the template renders the DOM it keys on. A third test class pins
that, per template:

* `<nav class="… navbar … fixed-top …">` must exist — the script's selector is
  `nav.navbar.fixed-top`, and with no match nothing ever sets the token;
* `#navbarNav` must exist, and must be **nested inside that nav** — the guard
  returns early when the collapse is not a descendant of the measured element
  (`nav.contains(collapse)`), which would silently restore the loop;
* the toggler must still target `#navbarNav`;
* the template must load `versioned_static('js/main.js')`, and **both** static
  roots must contain the guarded implementation — `versioned_static` resolves
  the rebuilt tree first and falls back to live, so the same tag serves either
  copy and an unfixed copy in *either* root would reintroduce the flicker.

**Oracle for the wiring** — renaming `id="navbarNav"` in
`test_site/templates/base.html` (rebuilt tree only) fails exactly the two
rebuilt-template wiring tests and leaves the live ones passing: **2 failed, 22
passed**; restored: **24 passed**. That proves the check is per-tree, not a
whole-file string match.

**Related suites** — `test_mobile_navbar_no_flicker.py` +
`test_navbar_system_menu_and_search_shortcut.py` +
`test_layout_css_js_template_integrity.py`: **304 passed, 1 skipped** (the skip
is pre-existing).
## Files

- `static/js/main.js`
- `test_site/static/js/main.js`
- `static/css/popularr.css`
- `test_site/static/css/popularr.css`
- `tests/test_mobile_navbar_no_flicker.py` (new)
- `tests/js/mobile-navbar-flicker-probe.js` (new)
