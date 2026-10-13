# Two-phase star rating: durable per album, finalised at the artist boundary (2026-10-14)

## What was reported
> I've tried updating the details for Various Artists a few times and it fails
> mid way through. Would it be worth running through each album in full
> including the finalize pass before moving to the next? … An artist scan could
> do an extra pass at the end at the artist level.

## The design problem
Star ratings were assigned ONLY at the artist boundary
(`scan_stage_runner._flush_artist_star_ratings`) because two inputs need the
artist's COMPLETE album set:

1. the **global 5★ catalogue lock** (`_compute_global_5star_locked_titles` —
   top 20% of the artist's whole result set), and
2. the **popularity-marking cutoffs** (`_artist_top_marked_cutoffs`).

Deferring everything there meant an interrupted artist scan (crash, budget
stop, the PostgreSQL restart) left every completed album **scored but unrated** —
the only heal was a full re-run of the artist, because the resume checkpoint
and the pending-star buffer are per ARTIST (and "Various Artists" is one artist
row with 183 albums).

## The fix: two phases, tracked by `tracks.stars_reconciled`

**Phase 1 — per album, immediately on completion.**
`_post_album_stars(..., reconciled=False, provisional=True)` runs inside the
album loop right after the album's results are appended to the artist buffer.
It persists the stars, file tags and Navidrome sync at once — the durability
the report asks for — and stamps `stars_reconciled = FALSE` atomically with the
star UPDATE (so a crash can never leave a star value whose provenance is
unknown). Only a compact `[RATINGS] Provisional ratings for '…'` line is
logged: the full SCAN RESULTS table is computed with the artist context
available so far, and printing it twice (or printing a partial ranking as
final) would be dishonest.

**Phase 2 — the existing artist-boundary flush.**
Unchanged in structure; it now passes `reconciled=True`, so the complete-
catalogue rating stamps `stars_reconciled = TRUE`. Its existing
skip-unchanged-ratings gate makes it a write-nothing no-op for albums whose
stars did not move, and it prints the full SCAN RESULTS table exactly once per
album as before.

**The heal for interrupted artists.**
New `_album_needs_star_reconcile(tracks)` un-skips an album whose stars are
still provisional, so the next scan re-runs it (cached scores, no network) and
the artist boundary finalises it. An album with NO stars remains the older
`_album_scored_but_never_rated` heal's job; a missing/NULL flag counts as
reconciled (rows predating the column, or a SELECT that omits it, must never
force an endless re-run).

Schema: `stars_reconciled BOOLEAN DEFAULT TRUE` (migration `023`, registry,
ORM). **Default TRUE is deliberate** — rows written before this change were
already finalised by the old deferred flush, so an upgrade must not re-scan
every artist.

## Test repairs (the suite guarding this exact function was mis-harnessed)
`tests/test_per_album_star_posting.py` failed 15 tests at pristine
`origin/develop` for harness reasons; fixed three defects in it:

1. **Wrong patch target** (10 sites): it patched `db.engine.db_session`, but
   `finalise_stage` binds `db_session` at MODULE IMPORT — the fake never
   reached it, so every capture assertion read 0. Now patches
   `finalise_stage.db_session`.
2. **Missing `begin_nested()`** on the fake session: the star UPDATE runs
   inside a SAVEPOINT, so the fake raised per-track (caught) and the UPDATE
   was never recorded.
3. **Two stale expectations**: a `SELECT id, stars, file_path` needle that
   predates the `COALESCE(stars, star_rating, 0)` column merge; and the
   resilience test still expecting the failing track to be *skipped* — current
   contract is a fallback to the stored rating (minimum 1★). Also
   `compute_artist_scores(scanned_titles=…)` → the key-based `scanned_keys`
   (`(album, essential-title)`) the album-level merge already uses.

## Verification
- New `tests/test_two_phase_star_reconcile.py` (20): column in all three
  layers; the heal (provisional re-run / reconciled left alone / missing =
  reconciled / unrated = other heal / non-identity flag check / skip-gate
  wiring / older heal still wired); phase wiring (phase 1 right after the
  pending-append and non-fatal, phase 2 writes TRUE and prints the table); the
  atomic flag (False/True/None), durability (stars persisted, Navidrome synced
  even in the provisional branch), and log behaviour (table suppressed in phase
  1, printed in phase 2).
- **Oracle**: source reverted → **19 failed / 1 passed** (the passer is the
  documented "older heal still wired" control) → restored **20 passed**.
- **Sweep** (one process per file): rescan-skip 20 ✓, mbid-completeness 28 ✓,
  compilation-cover 12 ✓, short-track-cap 28 ✓, album-ratio 17 ✓, playlist-
  ordering 67 ✓, artist-metadata 12 ✓, album-scan-budget 48 ✓,
  migrations-idempotent 13 ✓ (chain drives 023 up/down/up). Failures only in
  the DOCUMENTED pre-existing sets: prominence-era 3, full-scan-as-artist 1,
  scan-stall 7, startup-fix 2 — and `per_album_star_posting` **15 → 8** (all 8
  remaining are `_assign_stars` band/ratio expectations from the era/ratio rule
  churn, a documented baseline subset). **0 regressions.**
