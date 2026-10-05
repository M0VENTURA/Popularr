# Saving album metadata returns to the tab it was saved from

**Date:** 2026-10-06 · **Area:** ui / album
**Commit:** `fix(ui): a metadata save returns to the Edit Album tab`

## Reported

> Currently saving metadata asks to save twice. It saves the album metadata,
> then switches to the tracklist with another save button. Is this required or
> should one save be sufficient with a modal that pops up explaining what part
> of the save is happening as it goes through each metadata save?

## One save is already sufficient

The album fields **and** the staged per-track review travel in the **same
POST**: `staged_track_updates` is a hidden input inside `albumMetadataForm`,
and the single **Save Metadata** button submits both (there is exactly one
such button per album page — verified in both trees). There is no second save
to perform.

## What actually made it look like two steps

1. **The redirect threw away the user's tab.** The POST comes from the
   *Edit Album* tab, but the handler returned
   `redirect(url_for("ui.album_detail", …))` with no fragment — and
   `tab-tracks` is `class="nav-link active"` by default. So the reload always
   landed on the **tracklist**, right after the user had been editing album
   metadata.
2. **The tracklist is where the other Save affordance lives.** Any
   `form[data-sticky-save]` gets a lazily-created `#stickySaveBar`
   ("You have unsaved metadata changes" + **Discard** / **Save Metadata**), and
   the MusicBrainz lookup calls `markFormDirty('albumMetadataForm')`. Drop the
   user on that tab after a save and the next button they see reads as a
   *second* save.

## The change

* **`routes/ui_routes.py`** — the save's redirect now carries `#tab-details`.
* **both album pages** (`static/js/album_detail.js`,
  `test_site/static/js/pages/album.js`) — select that tab on load. Bootstrap 5
  does **not** restore a tab from the URL by itself, so the fragment alone
  would have done nothing.

With that, the sticky bar (when it genuinely has unsaved changes) is seen
*where the edits are*, instead of appearing to belong to the tracklist.

## Tests

`tests/test_album_save_returns_to_edit_tab.py` — **5**:

* the redirect carries the fragment, located from the save's own
  `return redirect(` (anchored on `redirect_artist = new_artist …` — `ui_routes`
  has 19 redirects, so the first one is the login flow);
* both trees read `location.hash === '#tab-details'` and show the tab. That
  exact expression appears only in code, never in the comments written with
  this change, so the assertions cannot be satisfied by their own docstring;
* CONTROL/premise: `tab-tracks` really is the default active tab in both
  templates.

**Oracle:** stashing the three source files → **3 failed, 2 passed** = exactly
the tests that depend on the change (both controls pass); with it, 5/5.

### The trap this walked into

The first draft read `window.location.hash` unguarded, and **13 tests in
`tests/test_album_page_missing_tracks_list.py` failed**. That suite drives the
real module in Node with a DOM stub that does **not** define `location`, and it
*invokes* the `DOMContentLoaded` listener directly — so the `TypeError` took the
whole handler down before `loadAlbumMissingTracks()` could do anything. SQLite
would never have caught this class of failure either; only the node harness did.

The read is now short-circuited (`window.location && window.location.hash === …`),
which keeps the exact expression the assertions match on while making the
handler safe when `location` is absent. Worth remembering: a UI handler that
assumes browser globals runs inside test harnesses that may not provide them.

## Related, not fixed here

* **Save timeouts** — the save still performs two synchronous MusicBrainz
  calls on the event loop (the route is listed in `_KNOWN_OFFENDERS` in
  `tests/test_async_routes_do_not_block_event_loop.py`). Gating that backfill
  on the release MBID actually changing, plus `asyncio.to_thread`, is the fix;
  it also has to land before a progress modal can show honest phases.
* **`album_routes.py::api_album_metadata`** blocks the loop with `db_session`
  — a pre-existing failure already recorded in the baseline, untouched by this
  change.

## Verification

- targeted (new + album save tests): **51 passed**, then **59 passed** once the
  hash read was guarded (the missing-tracks harness recovered)
- **Oracle:** stashing the three source files → **3 failed, 2 passed**
- affected set (30 files): 653 passed / 2 failed → **0 new vs baseline**
- full suite: **225 failed, 4642 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (the only ID absent from the
  baseline is the known native-flaky `test_sibling_torrents_root_is_searched`)

## Files

- `routes/ui_routes.py`
- `static/js/album_detail.js`
- `test_site/static/js/pages/album.js`
- `tests/test_album_save_returns_to_edit_tab.py` (new)
