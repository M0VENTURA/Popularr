# Re-scan skip gate: the landed fix shipped with three load-bearing defects (2026-10-13)

## What the user reported
A re-scan of "Various Artists" (182 albums) ran the FULL pipeline for every
album — zero "Skipping album" lines, ~90–120s per album in the file-tag sync
alone, and MusicBrainz re-searching tracks it cannot resolve.

## The two verified root causes (from the reported log)
1. **`is_album_incomplete` treated "searched and found nothing" like "not yet
   searched".** A track whose recording-MBID search returned
   `candidate_count=0` can NEVER get an MBID, yet the completeness override
   forced the whole album through every scan. One unresolvable track on a
   100-track compilation ⇒ the album can never skip.
2. **`skip_unchanged_albums` died on a zero score.** `all(final_score > 0)`
   treated a stored `0.0` (LF 0, LB 0 — genuinely unknown) as "not scored"
   forever.

Plus: `sync_album_file_tags` re-read every audio file with mutagen on every
scan and re-recorded pending conflicts with no "already exists" short-circuit.

## Landed on develop (`ae5920e8`) — then corrected (`ba911f49`)
`ae5920e8` shipped the full design (durable `recording_mbid_checked_at` stamp
fired on a CONFIRMED miss via a new `on_miss` callback threaded through
`get_suggested_mbid`/`lookup_recording_metadata`; `file_tags_synced_at` +
`changed_track_ids` scoping for the tag sync; migration 022) **but with three
load-bearing bugs** — fixed in `ba911f49`:

1. **`_scored(t)` used `str(t.get("final_score") or "")` — `0.0 or ""` is
   `""`**, so a stored zero read as "not scored" and the reported loop NEVER
   ended. Hoisted to module-level `_track_has_assessed_score` (behaviourally
   tested: `0.0`/`0`/`"0.0"` are assessments; only NULL/empty is "never
   scored"). ⭐ A falsy-but-real value must not be routed through `or ""`.
2. **The ListenBrainz MBID-resolution block was de-indented** — after an ISRC
   already resolved the recording, `_mb_entry` was undefined ⇒ `NameError` ⇒
   the `except` nulled `recording_mbid`, discarding the resolution. ⭐ A
   `continue`-free re-indent that moves a block out of its `if` scope changes
   which branch the variable is visible to.
3. **`_album_changed_track_ids = set(_corrected_title_ids)` sat BEFORE
   `_corrected_title_ids = set()`** in the source ⇒ `UnboundLocalError` on the
   FIRST album of EVERY scan. Now collected in the single results loop where
   both sets are first known. ⭐ Variable-initialization order is a runtime
   crash that no linter with the variable's first use inside a nested scope
   can catch by eye.

Also corrected the ORIGINAL test's false-positive needle: it asserted
`'t.get("final_score") is not None' in block`, which the BUGGY line satisfied
too — replaced with behavioural helper tests.

Migration `022_tracks_recording_mbid_checked_and_file_tags_synced` (both
TIMESTAMP columns) verified portable and idempotent on SQLite (upgrade →
upgrade → downgrade → upgrade).

## Config checks for the user (option A of the original brief)
None of this replaces the skip windows: confirm on Config → Scan Behaviour that
`album_skip_days` / `album_old_album_skip_days` are not `0` ("never skip") —
the 30-day old-album window applies to compilations.