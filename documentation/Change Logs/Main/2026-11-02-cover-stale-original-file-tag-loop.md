# A corrected cover original now reaches the file tags

**Date:** 2026-11-02
**Area:** metadata / enrichment (cover detection)
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_cover_stale_original_and_file_tags.py` (new)

## Report

The deep pass resolved the right original —

```
[COVER_DETECT] original lookup: writers=Michael Jackson | … | result=Michael Jackson (1987-08-25)
[COVER_DETECT] writing tags for 1 cover(s)…
[COVER_DETECT] …: 1 cover(s) (new=0) | checked=1, settled-in-window=0,
    already-confirmed=1, titles-repaired=1
```

— but a stale **"Santana"** attribution kept being written back, and the album
re-ran every two minutes.

## Root cause

Two halves, both required:

1. **The file kept the OLD verdict.** `_update_file_metadata` wrote only
   `title`. The DB row was corrected to
   `original_cover_artist = Michael Jackson`, while the audio file still
   carried `ORIGINAL_COVER_ARTIST = Santana`. The Navidrome import reads that
   tag back (`metadata_reader.read_release_tag_values` — Subsonic never echoes
   these fields), so Santana returned to the DB on the next scan and the repair
   rebuilt a "(Santana Cover)" title.
2. **The rewrite woke the scanner.** Each repair changed the file's bytes and
   mtime → Navidrome rescanned → Popularr scanned → repair again. That is the
   every-two-minutes cycle.

A stored cover verdict with a *different* original was already written over
(the `_record` fall-through), but the file's `ORIGINAL_COVER_ARTIST` was never
part of that write, so the correction could not stick.

## Fix

`CoverDetector._update_file_metadata` now writes the **whole verdict** —
`title` (with its suffix), `is_cover = "1"` and `original_cover_artist` — so
the file and the DB can never disagree.

`_suffix_repair_update` accepts the original THIS pass just re-confirmed and
prefers it over the stored value, so a repair can never rebuild a suffix from
an older attribution. When the stored original differs from the resolved one,
the pass logs the correction and writes the new value to the DB and the file.

Once the file matches the DB, the repair produces no update, the file is not
touched, and the rescan loop stops.

## Deliberately unchanged

Positive verdicts are still **re-verified on every pass**. That is a tested
contract (`tests/test_cover_flag_cleared_on_rescan.py`): a false flag written
by an older detection must be correctable. The start line's "0 already
settled" therefore still describes negative-only freshness; the loop was the
file churn, not the re-check.

## Tests

`tests/test_cover_stale_original_and_file_tags.py`:

| Test | Pins |
|---|---|
| `test_a_new_original_overwrites_the_stored_one` | Santana → Michael Jackson is persisted |
| `test_the_corrected_verdict_reaches_the_audio_file` | `ORIGINAL_COVER_ARTIST` / `is_cover` / title are written together |
| `test_the_suffix_repair_carries_the_original_artist` | a re-confirmed verdict writes the full payload |
| `test_a_matching_suffix_produces_no_update` | a stable verdict rewrites neither DB nor file |
| `test_resolved_original_wins` / `test_stored_original_is_the_fallback` | the repair's source of truth |
