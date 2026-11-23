# Album page: the missing banner, the bulk edit that never refreshed, per-track re-match

**Date:** 2026-11-24
**Area:** `test_site/static/js/pages/album.js` ·
`test_site/templates/Pages/album_detail.html` · `routes/track_routes.py`
**Commit:** `fix(album): show the busy banner on both match paths, and make bulk edits refresh`

Three reports on the album page.

---

## 1. "Choose from release group" showed nothing happening

`Lookup MBID` acquired a busy popup for its apply phase; `Choose from release
group` did not. Both end in `applyAlbumMbid`, which runs **two sequential server
round-trips** (tracklist findings, then the metadata preview), so the
release-group route looked like it had done nothing at all.

The popup is now acquired *inside* `applyAlbumMbid` rather than at each call site,
so every caller — the release picker, an inline handler, anything added later —
inherits it. Released in `.finally`, the same contract the lookup path already
had (a thrown preview must not strand a popup on screen).

## 2. "Edit Tracks" saved but the page never refreshed

Two faults stacked, and both had to go:

**The server did the file fan-out inline.** After the DB update it looped every
track doing a read-modify-write of the audio file. The album page's mutating
calls go through `utils/api.js`, whose default timeout is **30 s**
(`DEFAULT_TIMEOUT_MS`) — so a large selection aborted the request. The response
never arrived, the code threw past the `location.reload()` that sat after the
success branch, and the page kept showing pre-edit values. It had in fact been
saved, which is exactly the reported "doesn't seem to be saving correctly".

The loop now runs on a daemon thread (`_start_bulk_file_sync`), with one Navidrome
rescan for the whole batch, so the request is a single UPDATE. Same remedy the
star re-rate needed.

**The reload was conditional.** It sat at the end of the success branch, so any
throw skipped it. The client now also allows 60 s for this call and always
reloads once the save is accepted.

## 3. Re-match a SELECTION onto another release

> "It would also be good if I could select multiple tracks on an album and select
> lookup MBID and use those to match another release, but only match those tracks
> to another release for when tracks are accidentally merged to an incorrect
> release."

New **"Lookup MBID for Selected"** in the bulk-action toolbar. `Lookup MBID` is
album-wide: it binds the release to every row and rewrites the album-level fields.
That is right when the whole album is wrong and destructive when two tracks from
another pressing were merged into it. The new action writes the release IDENTITY
onto the ticks alone.

⚠️ **The picker returns a release GROUP, not a release.** `applyAlbumMatch` knows
this and resolves a concrete edition through `/api/album/musicbrainz/best-release`;
writing `selected.id` straight into `musicbrainz_albumid` would bind the tracks to
something that is not a release at all. The new path makes the same call, and
refuses with a pointer to "Choose from release group" when nothing resolves.

`POST /api/track/bulk-update`'s allow-list gained
`musicbrainz_albumid`, `musicbrainz_album_mbid` and `musicbrainz_releasegroupid`
so the existing endpoint (which already fans out to files and Navidrome) serves
this rather than a second endpoint existing for one button.

---

## Verification

`tests/test_album_page_banner_bulk_and_rerelease.py` (14 tests); **9 of the
first 13 fail with the fixes stashed**, so they pin the bugs rather than
describing the code.

`album.js` evaluated under V8 with a minimal DOM — it parses and runs to
completion, which is the only check that catches a wiring mistake (the class of
failure behind the dead Preview buttons).

`test_an_unsupported_field_is_refused` used `musicbrainz_albumid` as its example
of an unsupported field. It is deliberately supported now, so the test uses a
field nothing writes and a new test pins the identity columns as accepted.

Full suite compared against the baseline failure set by diff.
