# "2 delivered not shown" tracks are visible again

**Date:** 2026-10-06 · **Area:** album / missing tracks
**Commit:** `fix(album): a delivered track is still editable`

## Reported

> Items that have the wrong year are missing with this notice
>
> ```
> ##### Tracks (11)
> 11 already in the library, 2 delivered not shown
> ```
>
> As they aren't showing it makes them hard to edit with the correct
> information.

That notice is the `excluded` breakdown added earlier: **11** tracks are in the
library (so `in_library` keeps them off the missing list — correct), and **2**
were being hidden by a *second* gate — their download-queue row carried a
delivered status.

## Root cause

`_DELIVERED_QUEUE_STATUSES` (`imported`, `completed`, `matched`,
`in_collection`) treated **delivered ⇒ it's in your library**. That holds when
the import landed and the library match succeeded — but when the row is
`imported` and the track never made it *into this album*, the track is:

* not a library row → the album page can't show it for editing,
* not a missing row → the missing list won't show it either.

Invisible, and therefore uneditable. Exactly the reported situation.

## The change

**`in_library` is now the only visibility gate.** A queue status is an
*annotation*, not a reason to hide:

```python
queue_status = queued_titles.get(norm) or queued_positions.get(...)
# no `continue` — the entry is built and carries queue_status
entry["queue_status"] = queue_status
```

* `_DELIVERED_QUEUE_STATUSES` — **deleted** (nothing consults it).
* `excluded["queued"]` — **removed** from the breakdown, so the notice now
  reads `11 already in the library, 2 dismissed` (or nothing) instead of a
  phantom "delivered" category.
* `persist_missing_from_comparison` no longer consults the queue at all — its
  skip and the docstring that justified it are gone. (It only iterates
  `comparison_rows` where `matched` is false, so an in-library track was never
  persisted by it anyway.)
* The **UI label** `queued: 'delivered'` is removed from both trees; a row
  with a status already renders it as a badge and **disables the download
  button** (`queue_status` → `_queueStatusLabel`).

## Why this doesn't reintroduce double downloads

`tests/test_clear_imported_records.py` pinned that an `imported` row was what
kept "Download Missing Tracks" from offering a track again. What actually
prevents the second download is the **row** — the badge and the disabled
button on a track that carries `queue_status` — not the list's silence. The
class now documents that history and pins the rule that replaced it, and its
second test was retitled and rewritten because it modelled the *old* rule as
if it were current (its own `is_covered()` model asserted a suppression that
no longer exists).

## Tests

* `tests/test_album_lookup_findings.py`
  * `test_a_delivered_queue_row_stays_hidden` → **`..._is_listed`**: both tracks
    listed, `queue_status == "imported"` preserved, `"queued" not in excluded`;
  * `test_an_undelivered_queue_row_is_still_listed` and
    `test_excluded_counts_name_the_gate` assert the key is gone rather than
    being zero — the gate itself is what's under test.
* `tests/test_clear_imported_records.py` — the two tests above.

**Oracle:** stashing `album_missing_service.py` → **3 failed, 27 passed** = the
three tests that depend on this change.

## Verification

- targeted: **50 passed** (clear-imported + lookup-findings)
- **Oracle:** stashing `album_missing_service.py` → **3 failed, 27 passed** =
  the three tests that depend on this change
- affected set (35 files): 756 passed / 56 failed → **0 new vs baseline**
- full suite: **224 failed, 4666 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 new**

## Next

The **year split** (two pages for one release, `/2004` and `/1963`) is the
reason those tracks had the wrong year in the first place — grouping moves to
`musicbrainz_releasegroupid` next.

## Files

- `services/metadata/album_missing_service.py`
- `static/js/album_detail.js`
- `test_site/static/js/pages/album.js`
- `tests/test_album_lookup_findings.py`
- `tests/test_clear_imported_records.py`
