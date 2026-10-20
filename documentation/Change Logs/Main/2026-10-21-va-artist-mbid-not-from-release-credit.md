# Per-track artist_mbid no longer filled from the release-level credit

**Date:** 2026-10-21
**Area:** `services/enrichment/musicbrainz_service.py`
**Commit:** `fix(corrections): do not fill per-track artist_mbid from the release-level credit`

## Report

> "The corrections page is incorrectly matching track artists as having the
> same MBID with other artists when they have the album artist as Various
> Artists."

## Root cause

`_flatten_release` built the per-track `artist_mbid` as:

```python
"artist_mbid": (
    credit_artist_mbid(Recording.get("artist-credit"))
    or credit_artist_mbid(Release.get("artist-credit"))   # ← the bug
),
```

The fallback is the bug. MusicBrainz release tracklists frequently omit the
per-recording artist-credit (it is often empty), so the `or` fell through to
the release-level credit. On a Various Artists compilation that release-level
credit is the "Various Artists" MBID — and it was written to **every** track
of the album.

The duplicate-artist feature then queried:

```sql
SELECT artist, COUNT(*) AS track_count
FROM tracks WHERE musicbrainz_artistid = :mbid
GROUP BY artist
```

with that one "Various Artists" MBID and returned every real performer on the
compilation as a "duplicate artist" sharing the same ID.

## The fix

The per-track `artist_mbid` now comes **only** from the recording-level
credit:

```python
"artist_mbid": credit_artist_mbid(Recording.get("artist-credit")),
```

When the recording-level credit is absent, the per-track `artist_mbid` is
left `NULL` rather than being filled with the album artist's MBID. The
release-level credit is the **album** artist and is never written to the
track. This removes the only source of cross-artist MBID collisions on a
Various Artists compilation.

## What was NOT changed

* The release-level `album_artist_mbid` field is untouched — it still comes
  from the release's own credit and is correct for album-level display.
* The duplicate-artist query itself is untouched — it was always correct; the
  data it was reading was wrong.

## Tests

`tests/test_va_artist_mbid_not_filled_from_release.py` (new, 4 tests):

* the recording-level credit wins when both are present;
* a missing recording credit leaves `artist_mbid` `NULL` (not the VA MBID);
* an empty recording credit leaves it `NULL`;
* no credits at all leaves it `NULL`.

Sweep of 23 single-detection / MB / matching / artist suites diffed against
a pristine `develop` clone — **identical failure sets, 0 new failures.**

## Files

* `services/enrichment/musicbrainz_service.py` — the `or credit_artist_mbid(
  Release.get("artist-credit"))` fallback removed from `_flatten_release`.
* `tests/test_va_artist_mbid_not_filled_from_release.py` — new.
