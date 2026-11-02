# Featured/co-credit spellings no longer raise a duplicate-artist alert

**Date:** 2026-10-31
**Area:** artist corrections / duplicate MBIDs
**Files:** `routes/misc_routes.py`,
`tests/test_bonus_trailing_tracks_and_artist_labels.py` (new)

## Report

> Track Artists having a featured artist is causing it to incorrectly alert that
> there are duplicate artists sharing an MBID.

## Cause

`lookup_and_save_artist_mbid` resolves the **primary** credit and writes that
MBID onto the credit-bearing row, so `"Powerwolf"` and
`"Powerwolf feat./with/& Unleash The Archers"` legitimately share one MBID.
`api_get_duplicate_artists` groups by the raw `artist` string.

The existing guard (`strip_guest_credit`) folds only
`feat./ft./featuring` — deliberately, so a genuine co-credit is not offered for
a destructive merge. Credits the MBID writer strips with the permissive
`strip_featured_artist` (`with`, `w/`, `&`, `and`) were therefore still reported.

## Fix

`_collapse_duplicate_artist_rows` gained `permissive=False`:

* `permissive=True` uses `strip_featured_artist` for the **"is this a duplicate
  at all?" decision**;
* the merge LIST keeps the narrow `strip_guest_credit`, so a genuine
  co-credit ("Simon & Garfunkel") is never offered for a merge into one act.

The endpoint computes both: `decision_data` (permissive) gates the alert,
`variations_data` (narrow) is what the merge dialog shows.

## Verification

`tests/test_bonus_trailing_tracks_and_artist_labels.py::TestFeaturedCreditsDoNotRaiseADuplicate`

| Test | Pins |
|---|---|
| `test_a_permissive_fold_covers_and_and_with` | ⭐ `&`/`with` fold for the decision; merge list stays 3 |
| `test_the_endpoint_uses_the_permissive_fold_for_the_decision` | wiring |

Existing `test_corrections_disc_and_featured_artists.py` tests for the narrow
collapse are unchanged.
