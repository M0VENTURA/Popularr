# Songs on single releases no longer hard-cap at 3 stars

**Date:** 2026-10-12
**Area:** popularity / stars
**Commit:** `fix(popularity): a single release is rated against the artist catalogue, not its 1-2 track 'album'`

## Reported

> Songs on single releases seem to be scoring low.

The Nct Dream popularity report makes it concrete: `Fireflies` (a 2019
single, sitting at 49.2) was 3★; `JOY` (31k listeners, 4th in the artist's
catalogue) was held to 4★; `JOY (Inst.)` (94 listeners) got **3★ while a
52k-listener album track got 1★** — the small-album stars were not tracking
popularity at all.

## Root cause

A single release is a **1-2 track "album"** (title track + instrumental).
Three mechanisms keyed on album-pool size then conspired to cap it:

1. `_apply_album_relative_normalization` skips albums with fewer than 3
   tracks — the raw score is never album-relative remapped.
2. `_album_z_band_star` returns `min(3, max_stars)` when the album pool has
   fewer than 3 valid scores — **a hard 3★ ceiling for every single
   release, however popular the song**.
3. `_compute_album_z` uses `calculate_robust_zscore(min_count=3)`, so a 1-2
   track album's z is `0.0` by construction and `is_standout` (the 5★ path)
   can never fire — and the base band judges the single against its own
   instrumental, not against the artist.

Only tracks near the top of the artist's listen-percentile force path (the
`_force_stars` percentiles) escaped — which is why `JOY` (rank 4) scraped
4★ while everything else on singles sat at 3★ or below.

## Fix

A 1-2 track album is not a distribution — the compilation branch already
documents the same reasoning ("there is no separate album pool that means
anything") and rates against the credited artist's catalogue. The single
release now does the same:

- **`_assign_stars`**: when the album pool has fewer than 3 valid scores,
  both z-scores are computed against the ARTIST's catalogue
  (`album_z = artist_z`), exactly like the compilation branch. `is_standout`
  and the star bands now read a meaningful distribution.
- **`_album_z_band_star`**: the `len(valid) < 3 → min(3)` early return is
  replaced with artist-catalogue bands (5+ valid artist scores required,
  else the old 3★ cap is kept — an honest fallback when the artist's
  catalogue is also too small).

The era caps still gate the 5★ (catalogue-top cutoff / top-N / 5★ slots), so
a mid-catalogue single cannot inflate past its true position — the
`catalog_cutoff` (artist's top-20% score) must be cleared for 5★
eligibility. An instrumental's low catalogue z keeps it low.

## Effect on the reported data

Re-rating the Nct Dream singles against the 26-track artist catalogue:
`Fireflies` (score 49.2, mid-catalogue) stays 3★ — the data genuinely says
it is mid; `JOY` (top-quartile artist z) can now reach a proper 4★ (and 5★
if it clears the catalogue-top gate); `JOY (Inst.)` (artist-z markedly
negative) drops below 3★ as it should. The complaint's resolution is that a
**mid** single rightly stays mid, whereas before **every** single was
flattened toward 3★ regardless of rank; a genuinely popular single can now
rise.

## Tests

`tests/test_single_release_stars_use_artist_catalogue.py` (10):

- A popular single reaches 4★ in the base band (was: hard 3★ cap).
- A mid single stays 3★; a weak single drops ≤2★ (no inflation).
- No artist catalogue / too-small artist catalogue → old 3★ cap kept.
- `max_stars` ceiling (live album) still respected.
- CONTROL: a 4+ track album still rates on its OWN pool.
- `_assign_stars` uses the artist pool for the top-of-function z on small
  albums; a normal album keeps album-relative z; an instrumental single
  stays low.

**Oracle** at `542db57f`: 3 of the 10 fail (the behavior changes), 7 pass
(controls). **Sweep** (10 star/finalise/compilation/live/type suites):
baseline 11 failed / new 8 failed — the 3 only-in-BASE failures are exactly
the new oracle tests; nothing in only-in-NEW → **0 regressions**.