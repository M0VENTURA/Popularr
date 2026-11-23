# Re-split after a merge: the fill wrote each row's OWN album id to its file

**Date:** 2026-11-23
**Area:** `services/metadata/album_tag_sync_service.py`
(`_dominant_album_id`, `_resolve_mb_release`, the album file-tag pass) ·
`tests/test_album_file_id_convergence.py` (new)
**Commit:** `fix(metadata): write ONE release id per album, not each row's own`

## Reported

> "Something is still updating Navidrome files and splitting them after they are
> correctly merged"

The tag dump supplied with the report contained the answer. Two files of ONE
album — same album name, same album artist, same release-group id:

```
/music/Korn/2002 - Untouchables/01 - Korn - Here to Stay.mp3
    albumartist:              Korn
    album:                    Untouchables
    musicbrainz_albumid:      3c8d8b38-ac3b-480c-87f8-5048a6198ff5
    musicbrainz_releasegroupid: 4041334a-fdca-3a73-878e-8513b464a88d

/music/Korn/2002 - Untouchables/15. Korn - Here to Stay (T Ray's mix).mp3
    albumartist:              Korn
    album:                    Untouchables
    musicbrainz_albumid:      2606ffe5-3d2d-4549-8fa3-14e3ed6b1cfd   ← different
    musicbrainz_releasegroupid: 4041334a-fdca-3a73-878e-8513b464a88d
```

Navidrome's default album PID is
`musicbrainz_albumid|albumartistid,album,albumversion,releasedate` — it prefers
the release id. Two different release ids on two files of one folder IS a split
album, however correct the database is. (The two ids are two pressings of the
same release group: the Japan `EICP 80` and the Canadian `EK 61488`.)

## Root cause: an album-level fact read per row

The end-of-album file-tag fill builds each file's payload with
`_db_tag_candidates(track, …)`, which reads `musicbrainz_albumid` **from that
row**, and that field is in `_FILE_IDENTITY_TAGS` — written with *correction*
semantics, not fill-if-missing:

```python
elif k in _FILE_IDENTITY_TAGS:
    if _norm(file_val) != _norm(v):
        fill[k] = v        # always rewritten when it differs
```

So one row still holding a stale release id stamped that id back onto its own
file, and the next Navidrome scan split the album the user had just merged. The
merge in `album_service.update_album_ids` fans the correct id out to every row
*and* file, but anything that leaves one row behind (a scan that re-reads a
release, a download, a manual correction) is then re-applied to disk by this
pass.

There was a second, quieter half: `_resolve_mb_release` took the release id from
the FIRST row that had one, so the tracklist/perfect-match check could be run
against a release that only some of the album's files came from.

## The fix

One album, one release id.

* `_dominant_album_id(tracks)` picks the representative id — majority wins,
  because a merge leaves the corrected id on the rows the save wrote; an even
  split falls back to the first non-empty id in scan order so the result is at
  least stable. A single-id album is untouched, and an album with no ids stays
  empty.
* `_resolve_mb_release` now uses it, so the perfect-match check names one release.
* The album pass stamps that id onto every row **and converges the rows in the
  database**, so the DB and the files it just wrote agree. Leaving them
  disagreeing is how the next pass re-introduces the split.

Measured on the reported shape (13 merged rows + 1 stray):

```
without the fix : 2 distinct album ids written  -> SPLIT
with the fix    : 1 distinct album id written   -> consistent
```

and for every shape tested — 13+1, 1+13, 2/2 split, single id, no ids — every
file receives exactly one id.

## Verification

`tests/test_album_file_id_convergence.py` pins the selection (majority, order
stability, all five shapes) and the invariant that matters: the reported album
must not produce two distinct values on disk, under either value of the
`perfect` gate, and a single-id album must not be rewritten at all.

Full suite unchanged at **405 failed / 9986 passed vs the 403 baseline**, with an
identical failure set to the previous run — the two extras remain the documented
collection-order flake and `test_work_level_lb_aggregation`, which passes in
isolation and also fails at `fabec976`.

## Not addressed

Nothing here changes which id is *correct* when two pressings genuinely exist;
it makes Popularr stop writing a mixed set. If a release-group's tracks are
split across two pressings on purpose, the merge should be done with the album
picker, which writes one id to every row — this pass will now keep it.
