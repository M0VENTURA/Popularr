# The cover's original ARTIST is the work's earliest performance, not its composer

**Date:** 2026-10-31
**Area:** cover detection
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_cover_work_original_artist_and_mbid_validation.py` (new),
`tests/test_cover_reuses_metadata_scan_ids.py`

## Reports

> The track Swerve by Papa Roach has been incorrectly matched as a cover of a
> song by Dub Sex.

> The track "My heart will go on" is being picked up as a cover "James Horner"
> cover, not "Celine Dion".

## "Swerve" → Dub Sex

The row carried a **wrong** stored recording MBID:
`3bcdd7e1-0d28-440e-a6fb-fd1b13be4d3e` is Rise Against's "Historia calamitatum",
not Papa Roach's "Swerve". `_resolve_recording_mbid` trusted a stored id
unconditionally, so step 3 followed the wrong work and step 4's writer/title
heuristics matched the *title* "Swerve" to Dub Sex's 1988 song.

Two fixes:

* `_recording_matches_track` validates a stored recording MBID against the
  track's title and first artist credit. A successful fetch that disagrees is
  ignored and the id is resolved again; a fetch FAILURE still trusts the stored
  id.
* With no cover annotation, the recording's **WORK** relationship is now used
  (`_detect_via_work_id`). A recording with **no work relationship at all** is a
  conclusive negative — the writer/title heuristics cannot turn a same-titled
  song into a cover. That negative now settles the track in step 3
  (`elif core is not None: seen_track_ids.add(tid)`).

The wrong MBID itself is corrected by the next metadata scan; cover detection
no longer acts on it.

## "My Heart Will Go On" → James Horner

The work's writers are James Horner and Will Jennings, and the composer's score
recording is earlier than Céline Dion's single. "Earliest recording" alone
therefore picked the composer.

`_detect_via_work_id` now keeps **two buckets**: recordings credited to a
work writer, and recordings by any other performer. The earliest PERFORMER wins;
the writer bucket is used only when no performed recording exists. The writers
stay in the `writer` column — they are not the original artist.

The last-resort `_writer_from_cover_work` path does the same before falling back
to a writer's band (or, with no performing identity, the writer's name).

Work-based verdicts now carry `source: "musicbrainz_work"` and the work title;
`_build_update` records them in `is_cover_reason` (e.g. `… (source:
musicbrainz_work, work: My Heart Will Go On)`), so the operator can see WHY the
artist was chosen.

## Guards

* If the performer's own recording is on the work but **undated**, a dated
  recording by someone else is not enough to call it a cover.
* The work-writer lookup is cached per pass (the work-first step, the writer
  check and the original selection each asked for it).

## Verification

`tests/test_cover_work_original_artist_and_mbid_validation.py`:

| Test | Pins |
|---|---|
| `test_a_mismatched_stored_id_is_re_resolved` | Swerve's wrong Rise Against MBID |
| `test_a_matching_stored_id_is_returned_without_a_search` | no extra calls |
| `test_a_failed_recording_fetch_does_not_discard_the_id` | failure ≠ wrong |
| `test_a_recording_without_a_work_relationship_is_a_negative` | ⭐ no work → not a cover |
| `test_the_earliest_performance_beats_the_writers_score` | Céline, not Horner |
| `test_the_last_resort_writer_path_uses_the_performance` | same, last-resort path |

`tests/test_cover_reuses_metadata_scan_ids.py` now expects `{}` (not `None`) for a
seed with no work.
