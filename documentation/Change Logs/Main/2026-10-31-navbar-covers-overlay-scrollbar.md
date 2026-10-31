# The fixed navbar covered the scrollbar on overlay-scrollbar systems

**Date:** 2026-10-31
**Area:** `ui` (test_site shell)
**Files:** `test_site/static/js/main.js`,
`tests/test_test_site_mobile_and_pwa.py`

## Reported symptom

> The side scroll bar gets covered by the banner.

The fixed navbar spans the viewport and paints over the top of `main`'s
scrollbar, so the bar could not be seen or grabbed in the first ~navbar-height
pixels.

## Why the existing fix missed it

`syncScrollbarWidth()` measured the bar as `main.offsetWidth - main.clientWidth`
and CSS pulled the navbar's right edge in by that token. That works for
**classic** scrollbars, which consume layout width.

**Overlay scrollbars consume no layout width** — `offsetWidth - clientWidth` is
`0` on macOS, in Chrome's overlay mode, and on touch devices. The token stayed
`0px`, so the navbar remained full-width and the browser painted the overlay bar
over `main`'s right edge, underneath the navbar.

A second gap: the measurement ran only on init/resize/load. When async content
made the page scrollable after `DOMContentLoaded`, a classic scrollbar appeared
without a re-measure.

## Fix

* When the measured width is `0` but `main` is scrollable
  (`scrollHeight > clientHeight`) and the device has a fine pointer, reserve an
  explicit `OVERLAY_SCROLLBAR_WIDTH` (15px) strip. The navbar no longer paints
  over the bar. Touch devices are excluded: their scrollbar is transient and a
  permanent gap would only narrow the header.
* Observe `main` with a `ResizeObserver` and re-measure. A classic scrollbar
  appearing shrinks the content box, so this catches the late-scrollbar case.

## Verification

`tests/test_test_site_mobile_and_pwa.py`:

| Test | Pins |
|---|---|
| `test_overlay_scrollbars_reserve_an_explicit_strip` | the 0-width + scrollable fallback, fine-pointer scoped |
| `test_the_scroll_container_is_observed` | `main` is observed, so a late scrollbar re-measures |
