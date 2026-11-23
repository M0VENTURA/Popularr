# The missing list outlived the release that produced it

**Date:** 2026-11-24
**Area:** `services/metadata/album_lookup_skip_service.py` ·
`services/metadata/album_service.py` · `tests/test_album_missing_cleared_with_mbid.py` (new)
**Commit:** `fix(album): release the missing tracks when the album's release goes`

## Reported

> "I had an album that was previously linked to an MBID so it filled in the
> missing releases, I removed the MBID and selected 'Do not search album
> release', but the missing releases are still showing"

## Root cause: two halves, only one reversible

The missing badges read `missing_album_tracks`. The row that DELETES a stale
badge is the scan's per-album missing-track refresh
(`get_missing_tracks` → `_persist_missing_tracks`, which removes any persisted
row not in the freshly computed set).

That refresh is **skipped** for a "do not search album release" album:

```python
# scan_stage_runner.py
_skip_missing_refresh = _album_skip_lookup(artist, album)
if not _skip_missing_refresh:
    get_missing_tracks(artist=artist, album=album)
```

and that skip is correct — the refresh would resolve the wrong release and
delete the user's CSV tracklist. The consequence, though, is that the moment the
flag went on nothing could ever remove a row a previous MBID-linked scan had
written. The list was frozen exactly as reported, and clearing the flag only
fixed it as far as the *next scan* — the page kept showing the stale badges in
the meantime.

Removing the MBID was the other half: `update_album_ids` clears
`musicbrainz_album_mbid` and `album_lookup_skip` was never touched, so the two
paths disagreed about what the album now is.

## The fix

Both reversals now release the release-derived rows immediately.

**Discriminator, not a guess.** A release-derived row carries `release_id`
(`musicbrainz_service` sets `mb_release_mbid` on every comparison row, and
`_persist_missing_tracks_from_comparison` copies it). A CSV-imported row is
written with `release_id = NULL` by `import_csv_tracklist`, and a row the user
rejected has `ignored = TRUE`. The delete is therefore:

```sql
DELETE FROM missing_album_tracks
WHERE artist_name/album_name match
  AND COALESCE(ignored, FALSE) = FALSE
  AND release_id IS NOT NULL AND TRIM(release_id) <> ''
```

* `set_album_lookup_skip(..., skip=False)` releases them, so unticking the box
  empties the list at once.
* `update_album_ids(clear_album_mbid=True)` releases them, because removing the
  MBID is what made them stale.
* **Turning the flag ON still deletes nothing** — the CSV import sets it
  deliberately, and its rows must survive. Pinned by a control test.

## Verification

Measured end-to-end against a real schema (CSV row and a rejected row included
throughout):

```
before             : MB Missing 1, MB Missing 2, CSV Row, User Rejected
flag on            : unchanged                      (correct: import sets it)
after MBID clear   : CSV Row, User Rejected          removed=2
after flag cleared : CSV Row, User Rejected          removed=2
```

`tests/test_album_missing_cleared_with_mbid.py` (6 tests). Four of the six fail
with the fix stashed, so they pin the bug rather than describing the code.

Full suite: 404 failed, and **no new failures** against the previous run's set —
the two that disappeared are the documented collection-order flakes
(`test_playlist_index_sync::test_a_playlist_gone_from_the_source_is_pruned`,
`test_playlist_song_id_rejections`), which move in and out of the failing set
with collection order.

## Not changed

The scan's `_skip_missing_refresh` gate stays exactly as it is: an album with no
release must not have its missing list recomputed against one. This change only
makes the *reversal* path release the rows it had already decided were stale.
