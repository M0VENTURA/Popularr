# Universal search dropped every local album that was not a studio album

**Date:** 2026-09-21 · **Area:** ui / search

## Reported

"Unified Search: **All 21 · Library 2 · External 19** … `0 artists · 1 album ·
1 track` — the album is still not showing in universal search results even though
it shows 2 results in library. There is a various artists album called Little
Nicky."

## Cause

`buildBuckets()` in the search module read **only `local.albums`**:

```js
(local.albums || []).forEach(function (al) {
  var bucket = buckets[al.type] ? al.type : 'albums';
```

But `/api/search` files each local album under the bucket its **type** maps to —
`albums`, `compilations`, `live_albums`, `eps`, `singles` — so an album that is a
compilation / live album / EP / single is in `local.compilations` (etc.), never in
`local.albums`.

The result counts and the "N album" figure in the meta, however, sum **all five**
buckets. That is the exact reported mismatch: a Various Artists compilation
("Little Nicky") was counted (Library 2 = album + track, meta "1 album · 1 track")
and then silently dropped at render time, leaving only the Tracks section. Studio
albums were unaffected, which is why this went unnoticed.

## Fix

`buildBuckets()` now walks every bucket the payload can carry, once, via a shared
list — so the counted set and the rendered set are the same keys by construction:

```js
var LOCAL_RELEASE_BUCKETS = ['albums', 'compilations', 'live_albums', 'eps', 'singles'];

LOCAL_RELEASE_BUCKETS.forEach(function (key) {
  (local[key] || []).forEach(function (al) {
    var bucket = buckets[al.type] ? al.type : (buckets[key] ? key : 'albums');
```

The fallback now prefers the bucket the item was *filed in* rather than 'albums',
so an unexpected `type` can never mis-file a compilation as a studio album.

Applied to **both** trees (`static/js/unified_search.js` and
`test_site/static/js/ui/search-flyout.js`) — the live tree is the default, so a
`test_site`-only fix would not be seen.

## Verification

A node harness extracts the **real** `buildBuckets()` from the deployed file via
regex and runs it against a payload whose album is in `compilations` (the reported
case), then again with the fix applied to the same source text:

| Source | `compilations` bucket | Rendered items |
|---|---|---|
| Deployed (`origin/develop`) | **0** | **0** — the album is dropped |
| With this fix | **1** | **1** — the album renders |

The harness is extraction-based on purpose: it tests the shipped function rather
than a re-typed copy, and the only difference between the two runs is this change.
