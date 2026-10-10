# Cover detection now answers from the work the metadata scan already logged (2026-10-10)

**REPORT:** "The cover detection at the end of an album seems very slow. Can it be sped up?
Doesn't the work details get grabbed during the metadata scan so then that could be used to
check for covers for songs rather to then use what's found in the local db to initiate the
cover detection for only those tracks, skipping ones that don't have it logged?"

**CHOSEN SCOPE (user):** work-first fast path — persist the work MBID, then use one
`browse_work_recordings` call per track to decide the cover question directly, and skip the
expensive ISRC / writer-search fan-out for any track whose work is logged and conclusive.

## Why the album-end cover pass was slow

`CoverDetector.detect_covers_for_album` ran its full MusicBrainz fan-out per track, per scan,
against the shared 1 req/s throttle:

| Step | Cost |
|------|------|
| ISRC lookup | 1 req/track |
| Recording-relation fetch (`get_recording`, several `inc` sets) | 2–3 req/track |
| **Writer detection** (`_find_original_recording`) | **~1 title search + up to 8 recording fetches PER WRITER** |
| Work fallback (only flagged tracks) | — |

Step 4 was the dominant cost (~9 MB requests per writer per track), and the seed recording was
even fetched twice (`_detect_via_recording_relation` then `_resolve_cover_chain`).

## The blocker found (and fixed): the work detail was fetched and DROPPED

`_recording_to_metadata` resolves the recording's `work_mbid`, and `track_stage._resolve_track_mb_metadata`
wrote it as `payload["work_mbid"]` — but `tracks` has **no `work_mbid` column** (the column is
`musicbrainz_workid`). `popularity_repository._execute_save` filters payload keys with
`if k in columns`, so the freshly-fetched work MBID was silently discarded. The cover pass never
saw a work id (its Step-6 fast path reads `track["work_mbid"]`, a key DB rows never carry) and
re-derived everything over the network. The shallow per-track check (`detect_cover_song`) had the
same hole.

## What changed

1. **Persist the work MBID** (`services/popularity/stages/track_stage.py`): the metadata scan now
   writes `musicbrainz_workid` (the real column) **and** keeps `work_mbid` (the in-memory key used
   by `cover_data` and the per-track result). The per-track scan result publishes `recording_mbid`
   and `work_mbid`.
2. **Publish it to the cover pass** (`services/popularity/scan_stage_runner.py`): the runner
   overlays `options["resolved_track_mbids"]` (id → `{recording_mbid, work_mbid}`) onto the raw
   rows before `_run_album_cover_detection` — the same mechanism as `resolved_track_artists`, so
   THIS pass's fresh resolution is used, not last scan's. Both spellings are set (`work_mbid` and
   `musicbrainz_workid`).
3. **Work-first detection** (`services/enrichment/cover_detector_impl.py`): new Step 1 runs before
   the ISRC step. For every track with a work id (`work_mbid` **or** the `musicbrainz_workid`
   column — normalised at the top of `detect_covers_for_album`), `_detect_via_work_id` answers the
   cover question with **ONE** `browse_work_recordings(work_id, inc="artist-credits+releases")`
   call (cached per detector instance):
   - a recording of the work credited to an artist other than the track's performer → cover
     (earliest year wins, `medium` confidence);
   - every credited work recording is the performer's own → conclusive negative, the track is
     skipped from the ISRC / recording-relation / writer fan-out;
   - no usable work data → `None`, the track falls through to the exact old pipeline (no detection
     is lost).
4. **Skip what was never logged** ("the ones that don't have it logged" half): new
   `_track_has_logged_identity` gate makes Steps 3 and 6 skip a track that carries NO MusicBrainz
   identity (`mbid` / `recording_mbid` / `musicbrainz_trackid` / work ids / album release id). For
   such a track, resolving the recording would be a title search the metadata pass already tried
   and failed — the duplicate network work this change exists to remove. ISRC (Step 2), writer
   (Step 4) and title-hint (Step 5) detection still run for every track.
5. **`_resolve_recording_mbid` accepts every identity column** the scan and the Navidrome import
   write (`recording_mbid`, `musicbrainz_trackid`), not just `mbid` — a stored, already-resolved
   recording never pays for a search.
6. **`detect_cover_song` (the shallow per-track check)** reads `musicbrainz_workid` too, so its
   work-relation fast path fires from stored data.
7. **`_resolve_cover_chain` reuses the seed recording** that `_detect_via_recording_relation`
   already fetched — one fewer rate-limited request per track.
8. `detect_covers_for_artist` (`services/enrichment/cover_detection_service.py`) selects
   `musicbrainz_workid` too (appended last, so the positional index contract holds).

Skipped/answered tracks keep the existing clearing semantics: a work-conclusive-negative that
still carries a stored verdict is cleared by `_clear_unconfirmed_verdicts` exactly like a deep
pass that found nothing (the work relations are the deep evidence); a work-found cover goes
through the normal `_record` funnel (self-credit guard, "(X Cover)" rename, Cover genre, file
tags).

## Safety properties

- Tracks without a work id are untouched as far as detection is concerned — the whole deep
  pipeline still runs for them (only the no-identity search duplication is removed).
- The 90-day `cover_last_checked` window still applies; answered tracks are stamped as assessed.
- `musicbrainz_workid` is already a column (no migration), and the file-tag fan-out picks it up
  automatically via the existing tag sync (column → `MUSICBRAINZ_WORKID`).

## Tests

`tests/test_cover_work_first_detection.py` (12 — work-first) and
`tests/test_cover_reuses_metadata_scan_ids.py` (14 — reuse/skip/gate/seed):

- persistence: `_resolve_track_mb_metadata` writes BOTH `musicbrainz_workid` and the in-memory key;
- work-first positive: one browse ⇒ cover, and **zero** ISRC / search / `get_recording` calls
  (call-log assert);
- work-first negative: conclusive negative skipped from the fan-out, and a stored flag the work
  disproves is still cleared;
- inconclusive: empty work data falls through to the ISRC step;
- column alias: `musicbrainz_workid` read as `work_mbid`; shared-work caching (2 tracks, 1 browse);
- runner: `resolved_track_mbids` overlay (and rows without one are left alone);
- skip gate: no logged identity ⇒ Steps 3/6 never searched; resolved tracks still deep-checked;
- `_resolve_recording_mbid` returns stored `recording_mbid` / `musicbrainz_trackid` with **zero**
  searches; fallback search still exists for genuinely unresolved tracks;
- seed reuse: `_detect_via_recording_relation("rec-1", …)` fetches `rec-1` exactly once;
- Step 6 fast path fires from the stored `musicbrainz_workid` without re-resolving the recording.