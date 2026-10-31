# A work's writer is not the original artist

**Date:** 2026-10-31
**Area:** `enrichment` (cover detection)
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_cover_writer_is_not_original_artist.py` (new)

## Reported symptom

> The Metallica song "So What" has been detected as a cover of Animal, when
> it's a cover of Anti-Nowhere League.

## What MusicBrainz actually says

Verified against the live API:

* the Garage Inc. recording is a `performance` of the work **"So What?"** with
  the `cover` attribute — correct;
* the work's writers are **Clive Blake, Nick Culmer and Chris Exall** — there
  is no artist called Animal credited as a writer;
* Nick Culmer has an `is person` relation to the performing alias **"Animal"**
  (member of Anti-Nowhere League) and a `member of band` relation to
  **Anti-Nowhere League**;
* several **Metallica live** recordings of the work are credited
  `Metallica feat. Animal` / `Metallica & vocals by Animal`.

## Root causes

1. **`_original_by_writer` scanned every artist credit.** For a recording
   credited `Metallica feat. Animal`, Metallica is skipped as the performer and
   **Animal** — a writer-linked candidate — was then taken as the original
   artist. Featured guests are not the performing artist; only the first credit
   is, which is what `_detect_via_work_id` and `_earliest_across_pages` already
   assumed.
2. **`_writer_from_cover_work` returned a writer's personal name** as the
   original artist. A work's writer need not be its performer, so the last-resort
   fallback now resolves the writer's **band** first (Anti-Nowhere League) and
   uses the personal name only when no performing identity is linked.

## Verification

`tests/test_cover_writer_is_not_original_artist.py`:

| Test | Pins |
|---|---|
| `test_the_band_is_preferred_over_the_writers_alias` | last resort returns Anti-Nowhere League, never Animal |
| `test_the_writers_name_is_used_when_no_performing_identity_is_linked` | a genuine solo writer still resolves |
| `test_a_featured_credit_is_ignored` | ⭐ a page holding only `Metallica feat. Animal` yields no verdict (never "Animal") |
| `test_the_earliest_original_still_wins_when_present` | the 1981 Anti-Nowhere League recording wins over the 2011 live guest credit |
