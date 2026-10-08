# A track number changes only when the track name confirms it (2026-10-09)

**Area:** metadata / enrichment (MusicBrainz tracklist comparison)
**Commit:** `fix(musicbrainz): renumber a track only when the track name confirms the pairing`

## Reported

> When matching a release after using lookup mbid, can it also use the track
> name as a confirmation for track when matching the tracks? Sometimes it's
> falsely changing track numbers when the track name is different.

## How it happened

Steps 1 and 3 of `_match_mb_tracks_to_library` pair on the **name** — step 3
from a fuzzy similarity of only `_TRACKLIST_TITLE_FLOOR` (0.55). A name-driven
pairing legitimately sits at a *different position* (the library rip is ordered
differently, or the two titles are merely similar), so `diff_fields` reported
`track_number` alongside `title`.

The number then rode along with the title change unexamined: accepting rewrote
a **correct** position for a pairing the very same review reports as a
different name.

A probe over the shipped matcher found three shapes that did exactly this:

```
#4 -> #7   'Old Title'          vs 'New Title'              (fuzzy 0.67)
#4 -> #6   'Losing My Religon'  vs 'Losing My Religion'     (fuzzy 0.96)
#2 -> #4   'Sunshine'           vs 'Sunshine of Your Love'  (fuzzy 0.59)
```

## The rule

**A track number is only evidence about the SAME track, so the name must
confirm the pairing first.**

The confirmation is `titles_match_for_review` — the review's *own* title rule,
already shared by the Lookup-MBID preview and the Compare button — so "the
names differ" and "don't renumber" can never disagree. There is deliberately no
new second title rule; a second one is exactly what lets two surfaces drift.

It is **self-healing**, not a permanent block: accept the title first and the
next compare, now seeing matching names, proposes the number on its own.

## Three surfaces had to agree

| # | Surface | Why it needed its own gate |
|---|---|---|
| 1 | `musicbrainz_service._match_mb_tracks_to_library` → `diff_fields` | source of the album page's per-row `.mb-update-row` suggestions (which walk `trackComp.diff_fields`) and of `align_album_tracklist` |
| 2 | `metadata_proposal_service._track_proposals` | the scan-time review deliberately does **not** read `diff_fields` — it joins `local` against the comparison entry — so gating only (1) would leave it proposing a renumber the Compare button no longer shows |
| 3 | `alignTracklist()` in **both** trees (`test_site/static/js/pages/album.js`, `static/js/album_detail.js`) | it re-derived "needs a number" from the raw `library_track_number !== mb_track_number`, ignoring (1) completely |

Surface 3 also walked straight past a per-row **Ignore**: `diff_fields` is
already filtered by `tracks.mb_ignored_fields`, so "Ignore" on a track-number
row used to be undone by Align. Reading the server's verdict fixes that too.

Align's zero state was made honest as well — when the numbers genuinely differ
but no name confirms them it now says *"No track number can be confirmed by its
track name — nothing to align."* rather than the previous *"Track numbers
already match the MusicBrainz order."*, which would have been false.

## What deliberately did NOT change

* **Matching itself.** No pairing was made stricter — `matched` is unchanged in
  every case above, so the rename a user may well want is still offered, and
  `extra_tracks` does not grow.
* **`disc_number`.** Same class of argument, not asked for. (Note:
  `align_album_tracklist` writes `disc_number` unconditionally whenever it
  writes anything — worth a separate look.)
* The `0.55` fuzzy floor, `_track_number_pairing_allowed`, the performance- and
  cover-marker title rules, and `mb_ignored_fields` semantics.

## Tests

`tests/test_track_number_change_needs_name_confirmation.py` — **17**:

* `TestAFuzzyPairDoesNotRenumber` — the three probed shapes: `matched` stays
  True, `title` is still offered, `track_number` is not;
* `TestTheConfirmedRenumberStillWorks` — controls: an exact-name pair at a
  different position still renumbers, a performance marker does not block it,
  an already-equal number stays clean, and an ignored field stays ignored;
* `TestTheScanReviewObeysTheSameRule` — `_track_proposals` no longer proposes
  a renumber when the names differ, and still does when they agree;
* `TestAlignReadsTheServerVerdict` — parametrised over **both** trees: the
  `needsNumber` filter is decided by `diff_fields`, the helper reads it, and
  the honest zero-state message is present.

## Verification

* New suite → **17 passed**.
* **Oracle** — reverting all four changed files → **10 failed / 7 passed**: the
  three fuzzy regressions, the scan-review regression, and the six Align
  assertions (both trees × three). The 7 that pass either way are the controls
  and the file-exists check. Restored → 17 passed.
* **Probe** — the same 18-scenario harness that found the defect: **FALSE track
  number changes 3 → 0**, while the legitimate renames (bonus-track shift
  `3→2`, multi-disc `13→1` / `14→2`, exact-name `12→14`) all still propose a
  number.
* **Sweep** — the 52 test files referencing the touched modules, clean
  `origin/develop` vs this change: **baseline 34 failures, changed 34**,
  `Compare-Object` on the sorted `^(FAILED|ERROR) tests/` lines = **identical,
  0 regressions**.
* `node --check` on both JS files → OK. `import app` → **392 routes**.

## Pre-existing (unchanged, both trees)

`tests/test_album_musicbrainz_matching.py::test_best_release_confidence_scales_down_on_count_mismatch`
fails identically before and after — a stale confidence-threshold expectation
already on `origin/develop`.
