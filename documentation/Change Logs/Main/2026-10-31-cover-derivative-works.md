# Cover detection: derivative works and very large works

**Date:** 2026-10-31
**Area:** MusicBrainz work-based cover detection
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_cover_derivative_work.py` (new)

## Reported

* `6d16f5a0-cc2a-4107-9a50-d9764b6abdb4` (Fall Out Boy — "We Didn't Start
  the Fire") scanned as **not a cover**.
* "My Heart Will Go On" still resolved to James Horner / Deja Vu instead of
  Céline Dion.

Neither was a configuration switch.

## 1. Derivative works were not followed

MusicBrainz models Fall Out Boy's 2023 version as a **new work**:

```
work a709fc25…  "We Didn't Start the Fire"  (disambiguation: "Fall Out Boy 2023 version")
  composer: Billy Joel
  additional lyricists: Fall Out Boy members
  based on → work 4d246902…  "We Didn't Start the Fire" (Billy Joel)
```

The recording has only a `performance` relation to the derivative work — no
`cover` attribute. The detector therefore correctly saw no cover relation, and
because Fall Out Boy members are additional writers of that derivative work,
the performer-wrote-work check concluded "not a cover".

Fix: `_work_base_ids` follows `based on` / `version of` work-work relations when
the base has the **same normalised title**. `_detect_via_work_id` then resolves
the earliest recording of the base work (Billy Joel) and returns it with
`source="musicbrainz_work_based_on"`. A different-titled base is not treated as
a cover (samples/interpolations would otherwise rename unrelated tracks).

## 2. The work's first page is not the complete original list

"My Heart Will Go On" is a very large work. The first 100 recordings included
writer/score recordings and covers (James Horner, Deja Vu) but not Céline
Dion's original. The old code only used that first page.

Fix: when the first page has a writer's own recording, the detector also runs
the existing title search (`_find_original_recording`) for the work title and
writer. It accepts a writer-verified performed recording at least as early as
the page result, and explicitly rejects the writer's own score recording. This
finds Céline Dion's 1997 recording over James Horner's 2004 score entry.

The resulting verdict still carries:

```
is_cover: true
cover_detection_source: musicbrainz_work
work: My Heart Will Go On
original_artist: Céline Dion
writers: James Horner, Will Jennings
```

The existing update path strips any stale `(X Cover)` suffix and rebuilds it
from the new `original_artist`, so a row that previously said
"(James Horner Cover)" is corrected when the verdict is re-written.

## Tests

`tests/test_cover_derivative_work.py`:
- follows a same-titled `based on` work;
- ignores a differently titled base;
- resolves the derivative work to Billy Joel;
- resolves a huge work to Céline Dion via the title search.
