# Download queue: a frozen transfer looked alive, and an outage looked like a miss

**Date:** 2026-11-23
**Area:** `migrations/versions/036_download_queue_progress_marker.py` (new) ·
`db/repositories/queue.py` · `db/schema.py` · `db/models.py` ·
`services/downloads/slskd_reaper_service.py` ·
`services/downloads/download_completion_service.py` ·
`services/queue/queue_cleanup_service.py` ·
`services/downloads/cleanup_engine_service.py` · `helpers/config_helpers.py` ·
`helpers/normalization_service.py` ·
`services/downloads/download_pipeline_service.py` ·
`services/queue/outage_miss_service.py` (new)
**Commit:** `fix(queue): time out a frozen download, and never record an outage as a miss`

Four days of log: 122 downloads started, 66 imported. The report identified seven
problem areas; this covers the five it prioritised.

---

## 1. Downloads that never finish

### The clock was the bug, not the timeout

Four timeout layers already existed, and **all four measured a clock that a
no-op poll refreshes**:

| Layer | What it measured |
| --- | --- |
| `slskd_reaper_service.reap_stalled_transfers` | `progress` vs `startedAt` |
| `_reconcile_transfer_state` | `download_queue.updated_at` |
| `cleanup_stuck_items` | `updated_at`, with a **6-hour** window |
| `_STALE_DOWNLOADING_MINUTES` | `updated_at`, 60 min |

A transfer that trickles for 59 minutes and then dies is not "60 minutes old", and
the `progress <= 0` arm never fires on a non-zero progress. Meanwhile
`_sync_download_progress` writes `updated_at` on **every** pass, so a
live-but-frozen transfer looked alive forever. That is the reported "each track
was sent to the same file from peer TheCookFeelsBraveNearby four times over about
7 hours. No failure or timeout was logged between attempts."

**New columns** (`036_download_queue_progress_marker`): `slskd_bytes_seen` and
`slskd_progress_at`, written **only when the observed byte count changes**
(`db.repositories.queue.record_download_progress`, which deliberately does not
stamp `updated_at`). An unchanged poll now leaves the marker to age.

`reap_stalled_transfers` gains `_no_progress_stalled`: no new bytes for
`slskd.timeouts.no_progress_timeout_minutes` (default **20**, `0` disables) ⇒
cancel the transfer, `_record_peer_stall` (blocks the peer+file, then the peer on
a second stall) → `record_rejected_candidate` → `mark_failed`. The next search
then picks a different candidate, which is exactly what the report asked for.

In-memory state was rejected: the nine stuck items outlived many restarts, and an
in-memory clock would have given every frozen transfer a fresh window each time.

### The nine rows stuck for three days

`reset_abandoned_items` only ever reset `searching`/`processing`, so nothing in
the app touched `downloading` at startup — every restart reported `downloading=9`
verbatim. It now also resets a `downloading` row when it has no `found_filename`
(the status is written before the enqueue is confirmed) or a `found_filename`
that is **not on disk** (downloaded, then deleted). A row whose file IS on disk is
left alone so the completion pass can still import it.

`_STUCK_DOWNLOAD_SECONDS` 6 h → **1 h**, and the sweep now compares
`slskd_progress_at` as well as `updated_at`.

`cleanup_engine_service.delete_mismatched_download` deleted the file and touched
nothing else, so any caller that used it instead of the completion service's own
helper stranded the row forever — the "three Pitch Shifter tracks, deleted on 20
Nov but never reset" shape. It now returns the row to the retry cycle.

---

## 2. Pitch Shifter rejected as an artist mismatch

`normalize_string` collapses whitespace but never removes it, so
`"pitch shifter"` ≠ `"pitchshifter"` — and the peer's folder was literally
`Pitchshifter/Pitch Shifter`.

New `artist_name_key` = `normalize_artist` with all whitespace removed, used by
**search/matching comparisons only**. `normalize_artist` itself is unchanged,
because it is used for storage and for dozens of identity comparisons.

Both scoring layers adopt it — and they previously disagreed: the pipeline scorer
used a local `_normalise` with no diacritic folding at all, the metadata matcher
used `normalize_artist`. `Motörhead` vs `Motorhead` now agrees across both.

## 3. Combined credits

New `split_artist_credit` / `primary_artist_credit`, built on the existing
`FEAT_SUFFIX_RE` and guest-credit regexes rather than a fourth copy.

* **The query** uses the primary credit: `BTOB, iKON, SF9, THE BOYZ, Stray Kids
  & ATEEZ` searches as `BTOB`, which is the file the report saw rejected.
* **The artist gate** accepts a match on any credited name, so a file named after
  any one of them is evidence.
* The six `Pitchshifter feat. …` variants collapse to one query.

⚠️ The first cut of the gate compared the **parsed** artist against the filename,
which is always true and re-opened the wrong-artist grab
`test_one_of_two_artist_words_is_not_evidence` exists to prevent ("A Pretty Song -
Heaven Knows.flac" for "The Pretty Reckless"). Caught by that test; the gate now
only ever looks for the REQUESTED artist's keys.

## 4. Edition text in the query

`build_search_query` now searches the **bare** title; the annotated form is added
last in the fallback list, so the edition only disambiguates.

`-Japanese ver.-` was a genuine gap: `_EDITION_ANNOTATION_RE` requires brackets,
so **no** edition rule could see a dash-delimited marker, and the stripper's
keyword set did not contain the language words at all ("ver" is not a substring of
"version", so no folding rule could derive them). Fixed in three places: a
dash→bracket conversion in `strip_album_edition_marker`, the language words in
`_EDITION_ANNOTATION_KEYWORDS`, and in `_ALBUM_EDITION_STRIP_RE`.

`(acoustic Alt. version)` is deliberately NOT stripped — an acoustic take is a
different RECORDING, not a pressing, and the scorer has its own variant logic for
it.

## 5. A release queued as a track

`Turmion Kätilöt – Resodance` and `Ice Nine Kills – A Very Vicious Christmas` had
`title == album`, so every result was `title_mismatch`. When the title is the
album and the row carries no duration, the query becomes `Artist - Album` — the
honest reading of such a row — and a WARNING names it.

## 6. Year mismatch → penalty

`_year_mismatch_rejects` returned `0.0`, discarding 670 candidates whose artist
and title both matched strongly (Völund Smed, Æther Realm, Truth Corroded) because
the peer held another pressing of the same album.

`YEAR_MISMATCH_PENALTY = 4.0`, applied to the final score. **4, not 8**: measured,
an exact match with a 1991-vs-2019 difference scores 50.0 against a `min_score` of
45.0, so an 8-point penalty left it at 42 and silently re-implemented the
rejection. At 4.0 it stays selectable (46.0) while a correctly-dated copy still
wins the tie.

## 7. Outages recorded as misses

A zero-result search now bumps a **process-wide** streak
(`_EMPTY_STREAK_LIMIT`, default 5, `SLSKD_EMPTY_STREAK_LIMIT`). Once tripped, a
no-result search is SKIPPED via the existing `search_disconnected` path — no
retry attempt consumed, no weekly miss date — instead of
`_schedule_search_retry(... "persistent miss, retrying Thursday 27 Nov")`. One
result resets the streak.

New `services/queue/outage_miss_service.py` repairs rows already stamped: it finds
runs of ≥ 5 consecutive zero-result/disconnected searches (from
`slskd_search_logs`) and clears the retry date only on rows whose stamp falls
**inside** one of those windows. Deliberately a manual action with `dry_run=True`
default — a reset that also caught genuine misses would simply be re-stamped, and
an automatic hook would refire the backlog on every upgrade.

---

## Verification

New tests: `tests/test_queue_stuck_and_outage.py` (13) and
`tests/test_queue_search_quality.py` (21). Three existing tests pinned the old
behaviour and were updated with the reason recorded in each:
`test_year_mismatch_rejects`, `test_scorer_rejects_wrong_year_candidate`,
`test_year_mismatch_is_still_rejected`, and the `year_mismatch` gate name in
`test_failures_still_name_the_gates`.

Full suite: **405 failed / 405 baseline — an identical failure set, verified by
diff.** No new failures, none fixed by accident.

## Deferred

Logging volume (`check_completed_downloads` timings = 3,404 of 12,406 lines),
maintenance scheduling and the 180 s watchdog window. One ordering change was
made because §1c depends on it: `cleanup_stuck_items` now runs **before**
`cleanup_stale_downloads`, so a slow filesystem walk can no longer starve the
cheap DB-only sweep that clears stuck rows.
