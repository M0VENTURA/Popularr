# A progress popup for queueing and MusicBrainz lookups

**Date:** 2026-09-24
**Area:** new `static/js/busy-popup.js` + `test_site/static/js/ui/busy-popup.js`,
`static/js/album_detail.js`, `static/js/unified_search.js`,
`test_site/static/js/pages/album.js`,
`test_site/static/js/services/musicbrainz-queue.js`, both `base.html`

## Reported

> "When selecting add to queue for any areas that go into the download queue,
> can we have a loading splash show to see that something is happening? Same
> with when you go to the album page, lookup mbid, I want a splash to show that
> it's looking up a match that will disappear once it finished"

Scope confirmed with the user: a **compact centred popup** (not a full-screen
dim), covering **add-to-queue everywhere**, the **album page's Lookup MBID** and
**Auto-Link MBIDs**, the **artist page's add-whole-album-to-queue**, and
**everything that hits MusicBrainz on Save**.

## The problem the existing spinner does not solve

`ui/button-state.js` already puts a spinner on the clicked button, and the
queue buttons already used it. But:

1. **A button spinner is easy to miss.** Queueing a release is a release-picker
   probe plus a POST — seconds — and on a dense tracklist the button is small
   and may be off-screen.
2. **Some actions have no button.** The album page's MBID lookup runs from a
   dropdown item.
3. **The window is often not one awaitable call.** The lookup hands off to a
   modal and only finishes when the user picks a match, so the popup has to be
   opened and closed in *different* places rather than wrapped.

## Design

`busy-popup.js` is deliberately **not** an overlay. The user must still be able
to read the page and click the modal underneath, so it is:

* **pointer-transparent** (`pointer-events: none`) so it cannot swallow a click
  meant for the page, at `z-index: 20000` (Bootstrap modals are 1055);
* **a reference counter, not a boolean** — overlapping actions (lookup opens,
  save starts) cannot have one close the other's popup;
* **auto-released on `pagehide`/`beforeunload`** — the one exit path no
  `.finally` can cover;
* **announced** via `role="status"` + `aria-live="polite"`.

API: `show(label)` → handle, `hide(handle)`, `update(handle, label)`,
`showAndRun(label, fn)` (releases in `finally`), `releaseAll()`. `hide()` is
idempotent and tolerates a falsy handle, so `hide(maybeHandle)` at the end of a
branch needs no guard.

The module ships in **both trees** (the live tree is flat, the rebuilt tree is
foldered) and the bodies are pinned byte-identical by
`test_artist_page_contract.test_shared_module_copies_do_not_drift`.

## Wiring

| Action | Where | How |
|---|---|---|
| Album lookup | `pages/album.js`, `album_detail.js` | opened before the picker, relabelled to "Resolving release details…", released in `.finally` on the match and on dismissal |
| Auto-Link MBIDs | `pages/album.js`, `album_detail.js` | `showAndRun` around the POST |
| Album/track add-to-queue | `album_detail.js` | popup alongside the existing button spinner, released in `.finally` |
| Whole-release queue | `services/musicbrainz-queue.js` | `showAndRun` around the batch POST + queue refresh |
| Search-flyout queue | `unified_search.js` | popup around the final POST, released in `.finally` |

## ⚠️ "Auto-Link MBIDs" was a DEAD BUTTON in both trees

Implementing it exposed the reason nothing loaded: the handler was missing
entirely. Both trees' `album_detail.html` called `autoLinkAllMbids()` from
**four** places (the Actions dropdown item and the inline "Link" button, in each
tree) and **nothing defined it** — every click threw
`ReferenceError: autoLinkAllMbids is not defined`. A template has no compiler, so
the broken button shipped.

It was already listed in `_OBSOLETE_BUTTONS` in
`tests/test_rebuilt_pages_have_no_dead_buttons.py` with the note *"Not defined
anywhere"*, which is why the guard suite stayed green — the register documents
known-dead handlers rather than failing. That entry has been **removed**, which
the file's own `test_the_register_has_no_stale_entries` requires.

The endpoint it should have called already existed and was reachable:
`POST /api/musicbrainz/link-album-mbids` matches the local unlinked tracklist
against an MB release's recordings and writes `musicbrainz_trackid` +
`recording_mbid`. Implemented in both trees, wired to that route, with the popup.

**A general guard now exists.** `tests/test_busy_popup.py::
TestAlbumTemplateHandlersAreDefined` harvests every bare `fn()` from the album
template's inline handlers and requires each to resolve in the JS that page
loads — importing the existing `_OBSOLETE_BUTTONS` register rather than
duplicating it. It immediately found **four more** undetected dead handlers on
that page, all confirmed by grep to be called in the template and defined
nowhere:

| Handler | Buttons it breaks |
|---|---|
| `alignTracklist` | the "Align" button |
| `downloadMissingTracks` | "Download Missing Tracks" (dropdown) |
| `openAlbumArtModal` | "Change Album Art" (dropdown **and** the pencil on the art) |
| `renameAlbumFiles` | "Rename Files" (dropdown) |

All four were **already** in `_OBSOLETE_BUTTONS` and are left registered
deliberately — each needs its own endpoint/flow decision (album-art editing was
never ported; file renaming is handled by the organize flow). The point of the
new guard is that a *fifth* one cannot appear silently.

## Tests

`tests/test_busy_popup.py` (29):

* **Behaviour** — 18 tests driving the real module in Node against a small,
  purpose-built DOM stub: hidden until shown, label set, counter semantics
  (releasing one of two claims keeps it up), idempotent `hide`, falsy-handle
  tolerance, `update` relabelling without changing the count, `showAndRun`
  releasing on success **and on throw**, `TypeError` on a missing function,
  `releaseAll`, the `pagehide` reset, single element reuse, single style
  injection, `pointer-events: none`, `z-index` above Bootstrap modals, and the
  ARIA attributes.
* **Copies** — both exist, bodies identical, both `base.html`s load it.
* **Wiring** — each page calls it, the album lookup releases it on dismissal
  **and** in a `.finally`, and the handler/endpoint guards above.

Node is used rather than a browser because the module touches a small,
well-defined slice of DOM; asserting on a stub is more honest about what is
being tested than a headless browser would be. The suite skips if `node` is
absent.

`tests/test_artist_page_contract.py` — the drift-guard canary was hard-coded to
`global.artistReleases`, which is specific to one module; it is now a
per-module parameter (busy-popup's canary is `global.busyPopup`). Without this,
every newly-shared module would fail the guard for the wrong reason.

## Verification

* All 29 popup tests + the two guard suites pass (62 in that run).
* Every edited JS file passes `node --check`.
* Oracle sweep: full suite with and without the change in isolated worktrees,
  failing sets compared.
