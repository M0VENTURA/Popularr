# 2026-10-12 — VA compilation with a mismatched release no longer receives positional identities

## Report

An 80-track "Various Artists - Alternative 90s" scan was "a bit messy, it's a
non-MBID-backed album". The album stage correctly REJECTED the MusicBrainz
album match (`match_score=-1.2`, `release_group_mbid=None`), yet the scan
still:

* position-matched local#8 "Song 2" (Blur)    → release "What You Do to Me" (Teenage Fanclub)
* position-matched local#10 "Where Is My Mind?" (Pixies) → release "Happy" (Ned's Atomic Dustbin)
* position-matched local#18 "Buddy Holly" (Weezer) → release "She Know's She's Wrong" (Jellyfish)

The wrong recording MBID pulled the WRONG song's ListenBrainz count into the
scan ("Song 2" scored Teenage Fanclub's 82,928 listens; "Where Is My Mind?"
scored 21,409), wrote "Song 2" as Teenage Fanclub's recording and even
overwrote the artist (`Artist corrected ... old='Blur' new='Teenage Fanclub'`).

## Root cause

The local folder carried a stored release MBID **for the 46-track single-disc
edition** of a compilation whose local rip has 80 tracks. `_resolve_release_mbid`
trusts that stored id (or a title search with `>= 0.8` similarity — a same-NAMED
different edition passes) and never validates the tracklist. The album stage's
rejection is a *type lookup* (`_lookup_musicbrainz_album_type`), a separate call
the tracklist prefetch never consults.

`get_listenbrainz_album_tracklist_with_release` then indexed the wrong release
and its POSITION alias mapped local#N → release#N for every row whose title
isn't on the release — "N" being a coincidence of numbering on a DIFFERENT
edition. The position+duration guard is not enough: the wrong songs happened
to be within 5s.

## Fix

### 1. Source gate (`popularity_sources`)
The position alias is now emitted only when one of TWO paths admits it:

* **Title corroboration** (`_position_title_corroborates`): the release's
  title at that position is the SAME song with a marker/feat difference
  (feat. credits stripped, then version/cover annotations ignored) — the
  dArtagnan "Farewell (feat. Patty Gurdy)" ↔ "Farewell [Unplugged Version]"
  case the alias exists for. A cover marker on either side NEVER corroborates
  (a cover is a different recording).
* **Exact same-album count** (`_position_alias_allowed`): the release holds
  EXACTLY as many tracks as the local album — the translated-title case
  (Stray Kids: release lists "삐처리", the library stores "BLEEP"; title
  cannot corroborate, but a same-count release IS the same album, and
  disc+position+length is the designed answer).

Both FAIL (Alternative 90s: 80 vs 46) ⇒ no alias; the track falls through to
the per-track search (with the LOCAL artist — so "Song 2" is searched under
Blur, not Teenage Fanclub).

### 2. The runner stops trusting poisoned cache rows (`scan_stage_runner`)
The identity is persisted in `track_popularity_cache` (source=`album_tracklist`
+ `recording_mbid`), so a row poisoned BEFORE the fix had to be refused:

* **Migration 020** + schema/registry: the cache now also stores
  `release_track_title` — WHICH release title the identity was resolved from.
  (`upsert` preserves it with COALESCE; `_row_to_entry` carries it.)
* **Skip-guard corroboration** (`_cached_identity_corroborates`, new): a cached
  album-tracklist identity is skipped (trusted without re-running the pass)
  ONLY when its stored release title still corroborates the local title.
  Legacy rows (no release title) and poisoned rows re-run the cheap pass once,
  which either re-persists a valid identity or drops the stale one.
* **Per-row drop**: when the fresh pass has NO identity for a row that carries
  a stale album-tracklist identity, the recording MBID is dropped from the
  in-memory pool (so the batch and per-track search re-resolve) and `source`
  is cleared so the next scan re-runs the pass. Logged
  `[scan_runner] Album-tracklist identity dropped — release no longer
  corroborates title='...'`.

## Tests

`tests/test_va_compilation_release_mismatch_gate.py` (new, 18):
* the Alternative-90s fixture (count 8 vs 5 local): rogue rows get NO identity,
  no position alias points at a wrong recording, title-keyed rows still match;
* version-marker alias still corroborates (count-mismatched release, so ONLY
  corroboration can admit it);
* `_position_title_corroborates` ×5 and `_position_alias_allowed` ×4 (incl.
  the translated-title fallback and its documented count-path tradeoff);
* runner `_cached_identity_corroborates` ×3;
* `release_track_title` cache round-trip ×2 (write → read → `_row_to_entry`,
  and a listener-only later write cannot clobber the identity evidence).

Also (pre-existing harness fixes, both verified green):
* `test_lb_album_tracklist_translated_titles.py` — the tests faked
  `MusicBrainzHttpClient` via `monkeypatch` on the module attribute, which
  never reaches `get_shared_mb_client()`'s `from ... import` binding; patching
  `musicbrainz_service.get_shared_mb_client` (imported per-call) makes the fake
  deterministic, and a `_reset_shared_mb_client` fixture clears the process-wide
  singleton so the FIRST test's fake cannot leak. Without this, 2 of the file's
  4 tests failed ONLY when an earlier file primed a real cached client.
* `test_scan_db_first_persistence.py` — its cache fixture table gained
  `release_track_title` (the new column).

## Oracle / sweeps

* Gate oracle: reverting just the `_position_alias_allowed` call rejects
  "Where Is My Mind?"/"Song 2"/"Buddy Holly" identities (the mis-mixin case),
  and the count path is proven by the translated-title suite still passing.
* Combined suites (9 files incl. gate): **1 failed = the pre-existing
  `test_the_identity_is_marked_unambiguous`** (KeyError `title_is_unique`,
  verified failing at the pre-change commit) — vs **3 at baseline** (2 of the
  pre-existing translated-suite pollution failures are now FIXED).
* Batch B (cache/scan-db): 83 passed — identical to baseline.
* Batch C (scan-runner): 12 failed — failing sets **byte-identical** to the
  pre-change commit → 0 regressions.
* Guard suites: migrations/bool-SQL failing sets identical; syntax + undefined-
  name suites match baseline.
* Migration 020 driven for real on SQLite: 019 → head adds the column,
  second upgrade no-op, downgrade removes, re-upgrade re-adds.
* `import app` → 399 routes.

## Notes

* Scope discipline: `release_group_mbid is None` (the album TYPE rejection) is
  deliberately NOT the gate — a release MBID can be legitimately stored without
  a release-group id. The gates are per-POSITION (title corroboration) and
  per-ALBUM (exact count), because those are what distinguish "same song,
  different spelling" from "different song, coincidental number".
* Residual hazard (documented in the gate): the exact-count path cannot tell a
  translated title from a coincidentally-numbered different song on a
  same-count release. Title corroboration is the strong path; the count path
  is the fallback that keeps translated titles working.
* **Data cleanup for the affected albums**: the popularity-cache rows poisoned
  before this fix are re-resolved automatically — run a Forced scan of
  "Alternative 90s" (and any other affected album): the guard re-runs the
  album-tracklist pass, the new gate emits no wrong identities, and the stale
  rows are dropped. Tracks whose artist/title were overwritten by the older
  scan still need the user's Navidrome fix (the artist gate shipped in
  `e9d38ab3` only stops NEW writes); after correcting, a metadata pass rescans
  the right recordings.