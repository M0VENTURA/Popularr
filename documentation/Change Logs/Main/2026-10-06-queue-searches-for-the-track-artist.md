# The download queue searches for the track artist, not the album artist

**Date:** 2026-10-06
**Reported:**

> When adding to the queue, it's adding the album artist, not the track artist
> so it's searching for Various Artists rather than the actual artist of the
> track.

## Where the bug actually was

Two independent halves were both sending the **album's** artist, so a
compilation track reached Soulseek as `Various Artists` — an artist no shared
file has — and the query fell back to title-only, which is exactly the query
this repo already documented as binding a *wrong-artist* hit
(`tests/test_compilation_track_artist.py`).

### Server: the key that was read but never written

`services/metadata/album_missing_service.py::persist_missing_from_comparison`
already encoded the right rule:

```python
"track_artist": entry.get("mb_artist") or artist,
```

…but `_match_mb_tracks_to_library` **never set `mb_artist`** on a comparison
entry. The key was always missing, the `or artist` fallback always won, and
`artist` is the album's artist. The intended design was correct; the field it
depended on simply did not exist.

The comparison entry now carries both sides:

* `mb_artist` — the MusicBrainz **recording** credit (on a compilation, the
  individual band);
* `library_artist` — the file's own credit, for matched rows (blank when there
  is no library row).

### Client: three buttons hardcoded `pageArtist()`

Even a correctly-stored `track_artist` was thrown away at the click:

| Where | Was | Now |
|---|---|---|
| live missing-row button (`album_detail.js`) | `data-artist="${pageArtist}"` | `trackComp.track_artist \|\| trackComp.mb_artist \|\| pageArtist` |
| rebuilt missing-row payload (`test_site/.../album.js`) | `artist: pageArtist()` | `trackComp.track_artist \|\| trackComp.mb_artist \|\| pageArtist()` |
| redownload button, both trees (`metadata-review.js`) | `pageArtist()` | `check.artist \|\| pageArtist()` |

`data-album-artist` / `album_artist` deliberately still carry the page artist —
that field *means* the album artist, and the queue row's `album_artist` column
is supposed to hold it.

The redownload button needed a server value, so `_duration_checks` now emits
`artist` (library credit, MusicBrainz recording credit as fallback, `""` when
neither exists — never an invented album name).

### Knock-on: a placeholder must not seed fallback queries

`_build_fallback_search_queries` used `album_artist` whenever it differed from
`artist`, which on a compilation always produced `Various Artists - <title>`.
A placeholder album artist is now ignored there, for the same reason
`build_search_query` already ignored it for the primary query: a title-only or
placeholder-bound query that *wins* attaches someone else's recording.

### Rows already in the database

`missing_album_tracks.track_artist` written before this fix holds the album
artist for affected albums. Nothing invented a migration for it: the value is
recomputed, not read-only state — `scan_stage_runner` refreshes that table per
album and a Compare rewrites it (`persist_missing_from_comparison`), so the
next scan or Compare of the album replaces the placeholder with the recording's
own credit. Repairing it in the READ path would have meant joining
`musicbrainz_release_tracks` on `recording_mbid`, a column with **no index**,
on the endpoint the artist page calls once per owned album — a data fix that
costs a seq scan on every page view, for rows a normal scan already corrects.

## Tests

`tests/test_queue_track_artist.py` — **13**:

* the comparison entry carries `mb_artist` / `library_artist` (matched,
  unmatched, and a recording with no credit);
* a persisted missing row on a `Various Artists` album stores **Skulker**, not
  the album artist — the reported bug, reproduced;
* CONTROL: a recording with no credit still falls back to the album name
  (blank would be worse);
* `_duration_checks` reports the library artist, falls back to the recording
  credit, and stays blank rather than inventing an album name;
* all three buttons, both trees, prefer the track artist;
* the placeholder album artist never reaches a fallback query, while a real
  one still earns its query.

**Oracle:** with the seven source files stashed → **11 failed / 2 passed**, the
2 passes being exactly the two controls; the reproduction reads
`AssertionError: assert 'Various Artists' == 'Skulker'`. Restored, 13/13.

## Verification

- new suite: **13 passed**
- affected set (71 files): **1332 passed / 52 failed**, against a baseline
  subset of 52 for the same files → **1 new, 1 fixed, 0 real new**. The new id
  (`test_download_completion_not_found_loop.py::TestFreshItemReFetch::
  test_refresh_returns_none_when_no_longer_downloading`) passes standalone and
  runs green at file level; the fixed one is
  `test_peer_failure_handling.py::TestReconcileFailedPeerBlock…`.
- full suite: **224 failed / 4705 passed / 2 skipped** in 689 s against a
  baseline of 303 failures → **79 fixed, 0 new**.

## Files

- `services/enrichment/musicbrainz_service.py`
- `services/metadata/metadata_proposal_service.py`
- `services/downloads/download_pipeline_service.py`
- `static/js/album_detail.js`
- `static/js/metadata-review.js`
- `test_site/static/js/pages/album.js`
- `test_site/static/js/services/metadata-review.js`
- `tests/test_queue_track_artist.py` (new)
