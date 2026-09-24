# Cover verdicts were deleted before detection ran — now cleared only after it

**Date:** 2026-09-24
**Area:** `services/popularity/stages/track_stage.py`,
`services/enrichment/cover_detector_impl.py`,
`services/enrichment/cover_verdict_repair_service.py`, `db/bootstrap.py`

## Reported

```
[TRACK] false cover flag cleared track_id='…' title='Ruby, Don't Take Your Love to Town' detector_verdict='no_match'
[TRACK] false cover flag cleared track_id='…' title='Mahna, Mahna'                       detector_verdict='no_match'
[TRACK] false cover flag cleared track_id='…' title='Multiply the Heartaches'            detector_verdict='no_match'
```

All three are genuine covers. The diagnosis in the report is correct in every
part, and I confirmed each claim in the code rather than accepting it:

1. **`detect_cover_song` is a shallow, fast check.** Its only real evidence path
   needs `track_data["work_mbid"]`, which is not populated this early in a scan,
   so mid-scan it routinely falls through to `(False, "no_match")`.
2. **The deep pass runs at the END of the album scan**
   (`CoverDetectorImpl.detect_covers_for_album`, "Starting cover detection") —
   MusicBrainz work relations, ISRC, recording relations, writer coverage.
3. **`no_match` was read as "disproven"** and the branch set `is_cover = False`
   and purged "Cover" from both genre columns before that pass ever ran.

⚠️ **A detail the report did not mention, and it makes it worse: clearing both
artifacts defeated the check meant to protect a confirmed cover.**
`CoverDetectorImpl._is_already_confirmed_cover` requires a truthy `is_cover`
**AND** a "cover" genre. The branch removed exactly the two things that guard
looks for, so a genuine cover could never be recognised as already-confirmed.

## The fix: clear at the end, where a negative means something

The report proposed deleting the `elif` block outright. I kept that half but
**the deletion alone is not sufficient** — nothing else in the pipeline can
clear a cover verdict (the deep detector is set-only:
`UPDATE tracks SET is_cover = 1, …`, and `_build_update` hard-codes
`"is_cover": True`). Deleting the branch with no replacement would have made the
earlier "false cover" bug permanently unfixable.

So the clearing moved rather than vanished:

* **`track_stage`** now strips the wording and **nothing else**. The title
  normalisation was always the legitimate half and still works.
* **`CoverDetectorImpl._clear_unconfirmed_verdicts`** runs **after** the full
  detection pipeline. There, "nothing confirmed it" is a *meaningful* negative,
  because every technique has actually been tried against real data.

What is deliberately left alone:

| Verdict | Why |
|---|---|
| `cover_manual_override` | A user-locked verdict outranks every heuristic. |
| Tracks with a real `original_cover_artist` | The stored verdict is internally consistent; `_is_already_confirmed_cover` trusts it. |
| `cover_last_checked` within 90 days | Never examined this run, so silence is not evidence. Without this the daily scan would clear every unflagged track on its second pass. |
| Confirmed detections | Obviously. |

## Also fixed: self-referential verdicts

`"Track X (Track X Cover)"` is a contradiction — a recording is never a cover of
its own performer. This was written when a heuristic compared the original it
found against the **album-level placeholder**: on a compilation the album artist
is "Various Artists", matching no real credit.

`_stored_verdict_is_self_referential` now detects this and stops the stored
verdict suppressing detection, so the track is genuinely assessed.

⚠️ It uses `names_match`, and I **verified** rather than assumed what that
normalises: it handles case and whitespace, but is **NOT** accent- or
punctuation-insensitive (`"P.O.D."` vs `"POD"` and `"Ünloco"` vs `"Unloco"` both
return `False`). My first version of the test asserted the opposite and failed —
the right outcome for a test encoding an assumption about a shared helper. The
guard is therefore conservative: a miss leaves the verdict alone, whereas a
looser match would clear a genuine cover.

## The repair, for rows already damaged

Fixing the code does not heal a library — the verdict is stored, and nothing
re-derives it. `services/enrichment/cover_verdict_repair_service.py` re-flags
them at boot (both the immediate and the deferred path, in a daemon thread,
matching the other boot repairs).

It keys off the reason string the removed branch wrote —
`"cover attribution removed from title"` — which is the only exact fingerprint,
because the flag itself is useless for this (`is_cover = 0` is the normal state
of almost every track).

⚠️ **It also clears `cover_last_checked`, and that is load-bearing.** The deep
pass skips any track checked within `COVER_RECHECK_DAYS` (90), so re-flagging
while leaving a recent timestamp would put the row in front of the detector and
have it skipped as fresh — the repair would appear to do nothing for three
months.

It only re-flags; it does not decide the cover question. The deep detector does
that with real evidence, and clears the row again if nothing confirms it. A
second run finds nothing, because the repair overwrites the fingerprint it
searches for.

## Tests

- `tests/test_cover_verdict_cleared_only_after_deep_detection.py` (**19**) —
  the stage must not clear; the deep pass must; neither may touch a manual
  override; a fresh track is not cleared; the self-referential guard; and an
  **ordering assertion** that the clear call comes after every detection
  technique in the source, so a refactor cannot reorder it.
- `tests/test_cover_verdict_repair.py` (**13**) — the fingerprint is the reason
  not the flag; overrides excluded; `cover_last_checked` reset; idempotent; a
  failure never raises; wired into both boot paths in a daemon thread.
- `tests/test_cover_clear_all_artifacts.py` and
  `tests/test_scan_identity_and_year_tags.py` — **inverted**, with the reason
  recorded in each docstring. Both encoded the old semantics; the title-strip
  assertions are kept, and the flag/genre ones now assert the stage leaves them
  alone.

If this looks like flip-flopping between two reports: it is, and both were
legitimate. The distinguishing question is *when* the decision is made — the
earlier fix cleared on a shallow check's silence, this one clears on the deep
pass's evidence. Only the latter can tell the two cases apart.

Mutation-verified: reinstating the stage's clearing fails 4 tests; making the
deep pass never clear fails 1; keying the repair off `is_cover = 0` instead of
the reason fails 1. Targeted sweep: 47 failures, **all pre-existing**, 0 new.
