# Tracklist compare: same-core-title rows no longer cross-pair

**Date:** 2026-10-07
**Area:** metadata / enrichment (MusicBrainz tracklist comparison)
**Commit:** `fix(metadata): break tracklist name-ties by title and position`

## Report

Album review for Sirenia — *Nine Destinies and a Downfall* proposed
swapping two rows that were already correct:

```
#2  My Mind's Eye (radio edit)   Track #: 10 → 2   (title + recording ID also proposed)
#10 My Mind's Eye                Track #: 2 → 10   (title + recording ID also proposed)
```

"2 and 10 were incorrectly trying to swap."

## Root cause

The release carries BOTH `My Mind's Eye` (track 2) and
`My Mind's Eye (radio edit)` (track 10). Step 1 of both matchers pairs an
MB track by **normalized title**, and `normalize_title_for_lookup` strips
bracketed markers — so the plain and radio-edit titles compare **equal**.
The tie was settled by row/folder order, so MB#2 claimed the radio-edit
row and MB#10 claimed the plain row. The comparison then truthfully
reported the resulting mismatches as a proposed swap of track numbers,
titles and recording MBIDs — and accepting it would have written the
crossed metadata into the files.

## Fix

`services/enrichment/musicbrainz_service.py` — both matchers
(`_match_mb_tracks_to_library` and `match_mb_tracks_to_files` pass 1)
now break name-step ties in a fixed order instead of row order:

1. **verbatim title** (bracket-preserving
   `normalize_title_for_mbid_match`) — the radio edit matches its own MB
   track, the plain version its own;
2. **the row already at this disc/track position** — still gated by
   `_track_number_pairing_allowed` (name/length must not contradict);
3. row order — only for genuinely indistinguishable rows.

Name still beats number (unchanged design: a mis-numbered file must not
be stolen from the track it is named after); the tie-break only decides
between rows whose names are equal.

## Tests

`tests/test_track_matching_prefers_name_and_length.py` (+4,
`TestSameCoreTitleRowsAreNotCrossPaired`): identity pairing with the
radio-edit row listed first; no swap proposed in either row order
(`diff_fields == []`); the folder matcher pairs each file with its own
track; duplicate verbatim titles fall back to the track number.

**Gate:** targeted 200/200 across the 6 matcher test files · oracle (all
4 new tests fail on revert) · full suite 223 failed / 4934 passed →
0 NEW (only the known flaky `test_sibling_torrents_root_is_searched`),
81 fixed vs baseline.
