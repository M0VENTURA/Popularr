# Manual MusicBrainz picker: studio-first, informative, length-aware

**Date:** 2026-10-31
**Area:** track page → MusicBrainz Track Lookup
**Files:** `routes/track_routes.py`,
`test_site/static/js/pages/track.js`,
`test_site/templates/Pages/track_detail.html`,
`templates/pages/track_detail.html`,
`tests/test_mb_lookup_ranking.py` (new)

## Reported

The manual lookup showed many identical 100% "Every You Every Me" entries with
only the MBID, including the music-video recording
`b8d4bbb1-8c14-42c7-a3a6-ef7fe8fb9666`. There was no way to tell a studio
recording from a live/video/alternate one or a different-length version.

## Backend

`POST /api/track/musicbrainz` now accepts the track's `duration` (seconds) and
returns much richer candidates:

* `album`, `year`, release list with `primary_type`, `secondary_types`,
  `formats`, `country`, `status`;
* `is_studio`, `is_live`, `video`, `disambiguation`, `release_type`;
* `length`, `length_seconds`, `length_delta_seconds`, `length_match`
  (5-second tolerance);
* `title_core_match`, `unrequested_variants`, `missing_requested_variants`,
  `requested_is_live`.

Music-video recordings are **excluded from search results** by default (a
`include_video` flag can opt in); the count is returned as `omitted_video`.
A direct MBID lookup still shows the requested recording, with a Video badge.

### Ordering

Candidates are sorted by:

1. audio before video;
2. exact core-title match without unrequested version markers;
3. requested version intent present (live/remix request);
4. live/studio matching the request;
5. length match, then closest length delta;
6. studio release;
7. confidence.

A plain requested title therefore prefers a studio recording with no extra
words; a requested `(Live)`/`(Remix)` title prefers that variant. Among
same-title studio recordings, the matching length wins.

## Frontend

Both track pages now send the track duration and render:

* a `Best match` badge on the first result;
* `Studio` / `Live` / `Video` and release-type badges;
* release title and year, disambiguation, and formatted length;
* `Length match` or the length delta;
* a note when video results were hidden.

Nothing is auto-selected; the user still presses **Select**. The first row is
the recommended default.

## Tests

`tests/test_mb_lookup_ranking.py`: studio/live/video formatting, extra-title and
requested-live intent, studio-before-live/video ordering, and length-based
tie-breaking.
