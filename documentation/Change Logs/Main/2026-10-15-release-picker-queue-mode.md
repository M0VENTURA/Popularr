# The "Select Version" flyout could not actually select a version

**Date:** 2026-10-15
**Files:** `routes/musicbrainz_routes.py`, `static/js/main.js`,
`test_site/static/js/main.js`, `tests/test_release_picker_select_mode.py`

Reported:

> When using the search for an album, then selecting queue, it sometimes comes up
> with an option to select a version, but the version can't be selected

## What the screenshot showed

The *Select Version: End the Silence* flyout, listing two editions — a 29-track
tour edition, and a 14-track one with "3 editions" folded into it. Each had a
**Preview Tracks** button. Neither had anything that queued. The instruction above
them read *"Select the exact version you want to queue:"*.

## The dead end

`openReleasePicker` probes the release group first and only auto-queues when the
probe returns **exactly one** release:

```js
if (releases && releases.length === 1) { ... queue it ... }
window.openSlideOver(url, 'Select Version: ' + title)   // everything else
```

Everything else opened the flyout — and the flyout rendered a button **only** for
`mode=select`, which is the **album page's link flow**. Its handler
(`.js-pick-release` → `applyAlbumMbid`) lives on that page, so reusing the mode
here would have rendered a button that did nothing.

A comment in the route is what allowed this:

> Only the second one needs a button — the queue path auto-selects (one release,
> or every edition sharing a track count) and is driven by the caller.

**That was false.** The JSON probe never applied the edition grouping, so a group
whose editions genuinely differ (29 tracks vs 14) was never auto-selected. The
claim described a fallback that did not exist, and it is exactly what justified
leaving the flyout without a control.

## The fix

1. **`mode=queue`** on `GET /api/musicbrainz/release-picker` renders a
   *Queue this version* button (`js-queue-release`) on each card.
   `mode=select` is untouched, and the two are **mutually exclusive** so a card
   never offers both actions.
2. **`openReleasePicker` asks for it** (both trees) — the flyout URL now carries
   `mode=queue`.
3. **A delegated document listener** (both trees) turns the button into a
   `queueSpecificRelease(releaseId, title, artist)` call. That is the same
   function the auto-queue path uses, so the queue POST, the success toast, the
   `onQueued` callback and closing the slide-over all behave identically.

The artist rides on the button as `data-release-artist` because the queue POST
requires one and the slide-over has no other source for it.

Delegation (rather than binding the button) is required because the flyout is
**server-rendered HTML** — the same reason `album_detail.js` delegates
`.js-pick-release`. Binding to `document` rather than the slide-over means it
works whichever page opened the flyout: unified search, the downloads monitor,
upcoming releases, artist or album pages, which do not all load the same scripts.

The button disables while the POST is in flight — the round trip is seconds long
and a second click would queue the release twice — and **re-enables on failure**,
because the slide-over only closes on success and the user has to be able to retry.

## Verification

- `tests/test_release_picker_queue_mode.py` (29): markup, both trees' wiring, the
  stale claim, and **six end-to-end tests that drive the real route** with a
  mocked MusicBrainz client.
- **Oracle: 13 mutations, all 13 caught** — the button not rendered, the mode flag
  not passed to the renderer, the artist dropped, both buttons rendering at once,
  each tree's flyout not requesting queue mode, `mode=queue` moved off the flyout
  URL (the bug's exact shape), the delegated handler gone, the handler queueing
  nothing (both trees), the button never re-enabling, the stale claim returning,
  and the route rendering an empty list.

  Two are worth noting. The "handler gone" mutation was **not caught first time**:
  the assertion was a bare `.js-queue-release` substring, which the handler's own
  explanatory *comment* satisfied. It now asserts a `closest('.js-queue-release')`
  **call**. And the "empty list" mutation is caught only by the end-to-end layer —
  the template tests pass the releases in directly, so nothing below them can see
  the route dropping them.
- `tests/test_release_picker_select_mode.py` updated rather than left stale: its
  docstring repeated the false claim, and its `test_queue_mode_is_untouched`
  asserted the absence of a button as if that were the contract. That is now split
  into two controls that pin what each mode must **not** render.
- Sweep (`album|…|search|picker|queue`) run on BOTH trees: **107 failures at
  baseline, 102 with the change, zero regressions.** The 5-test difference is
  `test_scan_db_first_persistence` flakiness — those pass in isolation on both
  trees, and the change does not touch that code.

### Also found

`test_album_release_picker_contract::test_include_track_counts_browses_group`
fails on a `TypeError` — `get_release_group_releases() got an unexpected keyword
argument 'include_track_counts'`. **Pre-existing** (identical at baseline) and
untouched here, but it means that function's contract and its caller have drifted.
