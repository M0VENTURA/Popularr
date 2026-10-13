# Lookup MBID: the "Looking up MusicBrainz match…" popup stranded on dismissal (2026-10-13)

## What was reported
"On the mobile view, when selecting lookup mbid, the popup from 'looking up
MusicBrainz match' gets stuck in the view."

## Root cause — a one-way callback
The album page (both trees) shows the busy popup when the lookup starts and
registers a callback with the shared MB picker:

```js
lookupBusy = global.busyPopup.show('Looking up MusicBrainz match…');
global.openGlobalMbSearch(pageArtist(), pageAlbum(), function (selected) {
  if (selected) { ...applyAlbumMatch(selected).finally(endLookupBusy); }
  else { endLookupBusy(); }   // ← written for a dismissal that never arrives
});
```

But `musicbrainz-picker.js` fires `selectionCallback` in **exactly one
place** — `confirmReleaseSelection()`, i.e. only when the user picks a
release AND clicks "Match Release". Every other way to close the modal:

* tapping the backdrop (the **primary** dismiss gesture on mobile),
* pressing Escape,
* the X button / `data-bs-dismiss="modal"`,
* closing it programmatically from anywhere else,

fires **no callback**. So the album page's `else { endLookupBusy(); }`
branch was dead code, and `busyPopup.hide()` was never called → the popup sat
centred on screen (z-index 20000, above the modal) forever. Desktop users
mostly pick a match so they rarely saw it; on mobile the tap-outside dismissal
is the common path, which is why the report came from mobile.

The identical defect exists in the live tree (`static/js/album_detail.js`
`_endAlbumLookupBusy` + the live picker in `static/js/downloads.js`).

## Fix — bind the release to the modal's lifecycle
The popup is the **album page's** payload, so the album page now releases it
whenever the modal closes, regardless of why:

```js
modalEl.addEventListener('hidden.bs.modal', endLookupBusy);
```

`hidden.bs.modal` fires on EVERY close — including after a confirmed match,
where the callback's `.finally(endLookupBusy)` has already released it — and
`busyPopup.hide()` is idempotent + null-safe in both trees, so the
double-release is a harmless no-op. The binder is registered once per page
and retried if the modal is not yet in the DOM.

Applied in **both trees** (`test_site/static/js/pages/album.js` and
`static/js/album_detail.js`). The picker's callback contract is deliberately
left unchanged: firing the callback on dismissal would reach the
download-flow callers (queue monitor, missing-releases, dashboard) whose
callbacks deref `selected.id` and are not null-safe.

## Bonus — a pre-existing false positive in the ghost-guard scan
While extending `tests/test_busy_popup.py`, its
`TestGuardedCallsAreNotGuardingAGhost` scan was found to be RED at pristine
`origin/develop`: `static/js/main.js` has

```js
const NAVBAR_MOBILE = typeof window.matchMedia === "function"
  ? window.matchMedia("(max-width: 991.98px)") : null;
```

and `matchMedia` is a browser global that is never "defined" in app JS, so the
scan flagged it as a silent-no-op ghost. But the guard's **ternary fallback**
(`: null`) is a loud degradation, exactly like `else` — the scan only knew the
`else` keyword. `_guard_has_else` now recognizes a top-level `? … :` fallback
(outside brackets/strings) as loud, with a discriminator test. The
`_KNOWN_DEAD_GUARDS` register entries are untouched (none use ternary guards).

## Tests
`tests/test_busy_popup.py`:
* `test_album_page_releases_the_lookup_popup_on_dismissal` — now asserts the
  `hidden.bs.modal` → release binding (the real mechanism), not just the
  dead `else` branch.
* `test_live_album_page_does_the_same` — same for the live tree.
* `test_a_ternary_fallback_is_not_a_silent_noop` — pins the classification.

41 busy-popup tests + 58 adjacent (dead-button/mobile-navbar/album-art) pass;
`node --check` clean on both edited JS files.