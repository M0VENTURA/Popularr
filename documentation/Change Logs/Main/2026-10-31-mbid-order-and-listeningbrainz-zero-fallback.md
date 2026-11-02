# MBID lookup order and zero-ListenBrainz fallback

**Date:** 2026-10-31
**Area:** track stage popularity identity
**Files:** `services/popularity/stages/track_stage.py`,
`tests/test_track_stage_mbid_order.py` (new)

## Order on a full scan

1. The runner builds the album's MusicBrainz recording batch
   (`_build_album_recording_batch`) before any track worker starts.
2. `process_track` calls `_resolve_track_mb_metadata` **before** popularity
   scoring. This applies the batch identity or searches MusicBrainz, and writes
   the resolved MBID into the track payload used by both providers.
3. Last.fm is queried (title/artist/ISRC scoped).
4. The ListenBrainz block resolves a missing MBID via ISRC, the album batch, or
   `get_suggested_mbid`, then calls `get_recording_popularity(recording_mbid)`.
5. `_score_track_popularity` combines the two.

So the metadata MBID lookup is **before** popularity matching on a normal full
scan. A `popularity_only` pass skips step 2, but the LB block still resolves an
MBID (step 4) before the LB request.

## Why Last.fm had listeners and ListenBrainz showed zero

ListenBrainz is **recording-MBID scoped**; Last.fm is title/artist scoped.

When the reserved MBID is missing, wrong, or a **compilation-only recording**
with no LB audience, Last.fm still finds the song by artist/title, while the LB
request returns zero. `get_aggregated_listenbrainz_popularity` deliberately
trusts a known MBID (to avoid summing the same audience twice), and the track
stage previously called `get_recording_popularity` directly with no retry.

This is an identity-source difference, not an ordering bug.

## Root cause example: Placebo — Every You Every Me

The stored MBID `b8d4bbb1-8c14-42c7-a3a6-ef7fe8fb9666` is a **music-video
recording**:

```json
{"title":"Every You Every Me","video":true,
 "first-release-date":"2004",
 "releases":[{"title":"Once More With Feeling: Videos 1996–2004",
              "media":[{"format":"DVD-Video"}]}]}
```

It is not the audio recording and has no ISRC. ListenBrainz has no audience for
a video recording, so the MBID-scoped count is zero while Last.fm (artist/title)
still finds the song. The recording search did not exclude video results, so a
video recording could win a title/artist lookup or an album batch.

### Video-recording guards

* `get_suggested_mbid` skips recordings with `video: true`.
* `_lb_search_recording_ids` skips video results.
* `_recording_to_metadata` carries `video`; `_build_album_recording_batch`
  drops a video identity so the track uses the normal audio search.
* `lookup_recording_metadata` rejects a stored/searched video MBID and
  re-resolves the audio recording.

A row that already stores a video MBID and has genres skips the metadata lookup
in a normal scan, so the LB zero fallback fixes its count immediately; run a
forced metadata scan on that track/album to replace the stored MBID. The video
guards prevent new ones.

## Change

When the MBID-scoped LB count is **zero**, the track stage now retries by
title/artist through `get_aggregated_listenbrainz_popularity` (no primary MBID
and no ISRC, so it performs the MusicBrainz title/artist search). If that finds
a real count, it is used and the source is recorded as `title_fallback`.

- This is skipped when the count is already non-zero, avoiding double counting.
- The stored `recording_mbid` is **not** overwritten; only the popularity count
  is used from the fallback.
- Config: `api_integrations.listenbrainz.zero_mbid_fallback` (default `true`);
  set it to `false` to avoid the extra MusicBrainz search on very large scans.
- A DEBUG event `listenbrainz.identity` records the title, artist,
  `recording_mbid`, LF listeners, LB listens, source (`single_lookup`,
  `title_fallback`, `stored_fresh`, `prefetch`, `album_tracklist`, `error`, …)
  and `has_fresh_lb`.

If LB remains zero after the fallback, ListenBrainz likely has no data for the
recording/performance; the request path and ordering are working.
