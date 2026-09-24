# The artist page's Edit Release modal could not edit a release

**Date:** 2026-09-24
**Area:** `test_site/templates/components/modals/_artist_modals.html`,
`test_site/static/js/pages/artist-releases.js`, `static/js/artist-releases.js`,
`routes/album_routes.py`, `routes/ui_routes.py`

## Reported

> Clicking the edit album modal on the artist page brings up Edit Release with
> Title / Year / MBID. When save is selected, it doesn't seem to update those
> fields on the tracks contained in the album or the album in the database.
> The edit album modal should also contain all the edit album fields that show
> when on the album page.

## Three separate defects

**1. The MBID was never sent at all.**

The save handler hand-picked three values into a `FormData` and never read
`#editReleaseMbid`. The route reads `album_mbid`, which the modal never posted
under any name. So the id was discarded — and because the response was still OK,
the handler reported **"Release updated."** while changing nothing. A silent
no-op that reports success is the worst version of this bug.

**2. The values were never loaded.**

The modal was seeded only from the release row's `data-*` attributes
(`artist/album/mbid/rgid/year`). Every other field rendered **empty**, and
saving an empty field posts a blank over real data — so the fix had to include
loading the album's actual values, not just adding inputs.

**3. The modal exposed a strict subset of the album page's fields.**

Both forms POST to the **same** endpoint (`/album/<artist>/<album>`), so the
artist page could do strictly less than the album page for no reason.

## The fix

**A new `GET /api/album/metadata`** returns the album's current values, using the
**same field names the album page posts** and the **same column precedence** the
album page's `album_data` uses. That is deliberate: a field added to the album
page is then returned automatically, so the two forms cannot disagree about what
the current value is.

**The modal now mirrors the album page** — title, artist, release name, both
years, album type, all four identifiers, and the release-details block
(label, catalog number, barcode, ASIN, release date, media, country, language,
track/disc totals, status, version, disc subtitle, genres, track artist).

**The save handler sweeps the form generically** (`#editReleaseModal [name]`)
rather than hand-picking values. A field added to the markup is now posted
automatically and cannot be forgotten — which is exactly how the MBID came to be
dropped.

⚠️ **Two hazards found while doing this, both worth remembering:**

- **`track_artist` is deliberately NOT prefilled.** It applies to *every* track,
  so prefilling a value (e.g. the majority artist) would write it across the
  album on the next save and silently homogenise a compilation. The album page
  leaves it blank for the same reason; the endpoint omits it entirely.
- **Modal ids are prefixed `editRelease…`.** The album page's inputs use ids like
  `album_recordlabel`, and the album page *includes this same partial* — so
  reusing those ids would create duplicate DOM ids, and `getElementById` returns
  the first match, meaning the modal would silently write into the album page's
  field. Guarded by a test.

## The route now accepts both MBID names

`album_mbid` (album page) and `musicbrainz_release_id` (what
`/api/album/update-ids` uses) are now both read, along with
`album_release_group_mbid` / `musicbrainz_release_group_id`. Accepting both
rather than picking a winner matters because either caller may be the one
posting — and a name mismatch here fails silently, which is the whole bug.

## Tests

`tests/test_artist_edit_release_modal.py` (**21**).

The important ones are **drift guards derived from the other file**, not
hard-coded lists that would rot the same way:

- every non-hidden field on the album page is present in the modal — this test
  found `track_artist` genuinely missing while I was writing it;
- every field the modal posts is one the route actually reads — the exact
  silent-discard class;
- the MBID input is named `album_mbid`; the handler sweeps generically;
- the modal loads real values and still seeds from the row, so a failed fetch
  cannot leave the *previous* album's values on screen;
- no duplicate ids inside the modal, and none shared with the album page;
- the endpoint: real values, later-track and column-priority resolution, a clean
  404 for an unknown album, `track_artist` omitted, and a DB failure returning
  200 (the modal falls back to the row) rather than a 500.

⚠️ `force` is excluded from the drift check on purpose: it is the album page's
**"Force full rescan"** checkbox, a scan control, not album metadata. Putting it
in a metadata modal would let a save trigger a rescan by accident.

Mutation-verified, all four caught: removing the MBID's `name`; renaming a field
so the route cannot read it; pointing the fetch at a non-existent endpoint; and
reverting the generic sweep.

## Also ported

`static/js/artist-releases.js` is served by **both** trees and a drift guard
requires the two copies to be identical, so the same change is in both.

Sweep: 19 artist/album suites, 12 failures, **all pre-existing**, 0 new (oracle
confirmed against the changes stashed).
