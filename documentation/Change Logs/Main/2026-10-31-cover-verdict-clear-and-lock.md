# Scan Flags: clear a wrong cover verdict and lock it

**Date:** 2026-10-31
**Area:** track page / cover detection
**Files:** `templates/pages/track_detail.html`,
`test_site/templates/Pages/track_detail.html`,
`test_site/static/js/pages/track.js`,
`routes/track_routes.py`,
`services/enrichment/cover_detection_service.py`,
`tests/test_cover_work_original_artist_and_mbid_validation.py`

## Request

> On the track page, Under scan flags, there is an option for "is Cover Song"
> with detect or lock. This should also have an option to clear the cover song,
> then lock it to stop overwrite for incorrectly matched covers.

The card only showed a read-only Yes/No badge, a Detect button and a lock
checkbox. The hidden `is_cover` input always posted the current value, so a
wrong verdict could not be cleared — only re-detected.

## Change

A new **Clear & Lock** button next to Detect calls
`POST /api/track/<track_id>/cover-verdict` with `{is_cover: false, lock: true}`.

`clear_track_cover_verdict` then:

* sets `is_cover = 0`, `original_cover_artist = NULL`;
* sets `is_cover_reason = "Manually cleared and locked"`;
* sets `cover_manual_override = 1` (the detector's existing lock: the batch
  update has `AND cover_manual_override IS NOT TRUE`, and the clear step skips
  locked rows);
* strips the scanner's trailing "(X Cover)" title suffix;
* clears the file's `IS_COVER` / `ORIGINAL_COVER_ARTIST` tags and rewrites the
  clean title, so a later Navidrome import cannot restore the flag.

A normal form save still posts `is_cover`/`cover_manual_override`, so the
cleared+locked state survives editing the track.

## Verification

`tests/test_cover_work_original_artist_and_mbid_validation.py::TestClearCoverAndLock`

| Test | Pins |
|---|---|
| `test_the_verdict_suffix_and_file_tags_are_cleared` | ⭐ DB flags, title suffix, file tags |
| `test_an_unknown_track_returns_none` | 404 path |
