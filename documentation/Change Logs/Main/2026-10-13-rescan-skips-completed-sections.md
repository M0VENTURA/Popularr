# Re-scans of a recently-scanned artist skip the completed sections

**Date:** 2026-10-13
**Area:** popularity scan / album-skip gate / file-tag sync (+ migration 022)

## The report

> "This is the popularity scan running for a recently scanned artist, can you
> recommend a faster way to skip through the artist skipping sections that
> were already completed?"

The pasted log was a re-scan of **"Various Artists" (182 albums)**. Not one
`Skipping album` line appeared — every album ran the FULL pipeline
(enrich → track phase → post-singles → cover → file-tag sync), and the file-tag
sync alone was **~90-120s per album** even when it wrote 0-6 files. Per-album
wall-clock from the log: ~3 minutes for a 100-track album, half of it tag sync.

## Root causes (three, all verified in code)

### 1. A MusicBrainz recording search that found NOTHING was forgotten.

`musicbrainz_service.get_suggested_mbid` records a miss in an **in-memory**
`_mbid_cache` (negative TTL 7 days). The cache is **per-process** — the queue
worker and the web worker are separate processes — so `candidate_count=0`
tracks (e.g. `Papa Won't Leave You, Henry` at 5.1s, `1996 #011…` at 18.6s in
the log) re-searched **on every scan**.

Worse, the album-skip override `is_album_incomplete` read **no recording_mbid**
as *"a re-run could fill it"* → it FORCED the whole album through the full
pipeline on every pass, neutralising `was_album_scanned` and
`skip_unchanged_albums` simultaneously.

**Fix:** persistence of the search OUTCOME.
* `musicbrainz_service` gains an `on_miss` callback, fired **only** at the two
  moments a miss is confirmed (the `_record_mbid(cache_key, "", 0.0)` branch
  and the negative-cache-hit branch) — **never** on a transient error or a
  disabled service.
* `track_stage` (both the metadata resolution path and the ListenBrainz
  popularity path) passes `on_miss` that stamps
  **`tracks.recording_mbid_checked_at`** onto the persisted payload.
* `is_album_incomplete` treats a **fresh** stamp as terminal
  (`_recording_mbid_checked_recently`, window = the same
  `_MBID_NEGATIVE_TTL_SECONDS` so DB and cache cannot disagree about when a
  miss stops being authoritative) — the track stops blocking its album's skip.
  After the window expires, the track is re-searchable (the completeness gate
  re-applies), exactly like the in-memory cache. Migration **022** adds the
  column.

### 2. A stored 0.0 score was read as "not scored".

The log's `1996 #011 - Quad City Dj's - C'mon N Ride It (the Train)` row scored
**0.0** (LF 0, LB 0 — genuinely unknown). Two gates used `> 0`:
`skip_unchanged_albums` (`all(final_score > 0)`) and `is_album_incomplete`
(`val <= 0` → incomplete). That one row made its album "changed" / "incomplete"
**forever** — the zero score is real, re-running computes the same zero.

**Fix:** "assessed" now means **non-NULL / non-empty**, not `> 0`. A stored 0.0
IS a completed assessment; only NULL means the pass never scored the track.

### 3. File tags were re-read on every scan.

`sync_album_file_tags` opened **every audio file with mutagen** per album per
scan (the ~90-120s), re-filled nothing, and re-recorded its corrections — even
when the scan had changed nothing for the album.

**Fix:** scope the sync to tracks the scan actually changed.
* `process_track` computes a **`tag_changed`** flag against the exact columns
  `_db_tag_candidates` reads (title/artist/album/album_artist/years/track/disc/
  isrc/writer/genres/lyrics/MBIDs) — true only when THIS pass wrote a different
  value.
* The runner collects the flagged ids (plus the title-corrected ids) into
  `changed_track_ids` and passes them to `sync_album_file_tags`.
* The sync skips tracks not in that set **and already reconciled**
  (`tracks.file_tags_synced_at` set); never-synced tracks are always visited.
* Tracks whose file was actually read are stamped `file_tags_synced_at` (one
  UPDATE). A track with NO file is NOT stamped, so it is revisited when the
  file appears.
  Migration **022** adds the column; `db/schema.py` registry + ORM carry both.

## What a future re-scan does now

1. `was_album_scanned` (or `skip_unchanged_albums`) fires and prints
   `Skipping album …` — the zero-score and unresolvable-MBID tracks no longer
   defeat it.
2. `is_album_incomplete` only forces a rerun for genuinely un-fillable gaps.
3. Albums that DO run skip the mutagen re-read for every unchanged track — the
   tag-sync phase drops from minutes to seconds.

## Tests

`tests/test_recent_rescan_skips_completed_sections.py` (19): the `on_miss`
threading (`get_suggested_mbid` + `lookup_recording_metadata` + both
`track_stage` call sites + the except arm never fires it), the
`_recording_mbid_checked_recently` freshness helper, behavioural completeness
(album with one unresolvable track is skippable; the relief is scoped; the skip
gate treats zero as scored), tag-sync scoping (changed-only reads, never-synced
always read, full-set control, stamp covers only files that were read), runner
wiring, and migration 022 registration (migration + registry + ORM + upsert
filters by real columns).

`tests/test_metadata_scan_mbid_first_and_completeness.py` updated: the old
`final_score=0 → incomplete` pin (the bug) becomes `final_score=None →
incomplete` + `0.0 → assessed`; new fresh/stale miss stamp tests.

## Validation

* New tests + updated: `test_recent_rescan_skips_completed_sections` (19),
  `test_metadata_scan_mbid_first_and_completeness`, `test_album_tag_sync_service`,
  `test_known_mbid_skips_recording_search`, `test_migrations_idempotent` —
  **75 passed**.
* **Oracle** (pristine `origin/develop` worktree): **21 failed / 26 passed** —
  the failures are exactly the new discriminators; the 26 passers are controls.
* **Sweep 1** (15 suites, 307 tests): **307 passed / 0 failed**.
* **Sweep 2** (12 suites, 309 tests): **1 failed =
  `test_release_categories::…::test_no_type_falls_back_to_title`** — verified
  **pre-existing at base** (fails with my changes stashed). **0 regressions.**

⚠️ Migration 022 applies on the next rebuild/boot (`alembic upgrade head` +
bootstrap). Existing rows backfill naturally: tracks scanned before the upgrade
have NULL stamps, so the first post-upgrade scan treats them as never-synced /
re-searchable — exactly the safe default.