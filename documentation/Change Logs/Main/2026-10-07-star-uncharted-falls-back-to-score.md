# Star rating: uncharted titles fall back to score instead of a fake 1★

**Date:** 2026-10-07
**Area:** popularity / rating (compilation online-catalogue branch)
**Commit:** `fix(popularity): uncharted title is unknown, not a forced 1 star`

## Report

A Resident Evil / Marilyn Manson compilation scan log showed star ratings
that contradicted the tracks' own scores:

- Soundtrack cues scoring **73–82 were rated 1★** (Reunion 73.2, Fight Song
  74.7, Cleansing 74.2, Seizure 78.1, RE Main Title 81.7 …).
- Impossible chart positions in `[TRACK_RESULT]`: `online_rank=#192/191`,
  `#198/197`, `#170/169` — rank greater than the catalogue size.
- A `[dialogue]` credit (Umbrella Corp, Red Queen) was rated from an online
  chart (`#25/191`) that belongs to no real credited artist.

## Root causes

1. `get_online_artist_track_rank()` documented the contract
   *"`rank`/`total` are `0`/`0` … callers must treat that as unknown, never
   as unpopular, so a lookup miss cannot demote a track"* — but a title that
   was **not in the artist's top-tracks list returned a synthetic
   `rank = len + 1` sentinel** with `percentile = 1.0`. The rating layer
   treats any `rank > 0` as a real charted position, so percentile 1.0 → the
   bottom band → **1★ regardless of the track's own score**.
2. Nothing stopped placeholder credits (`[dialogue]`, `Various Artists`,
   `Unknown Artist`, …) from reaching `artist.getTopTracks` at all — Last.fm
   either returns nothing useful or something that does not belong to the
   credited entity, and the (bogus) rank was still used for rating.
3. The impossible `#192/191` display was the same sentinel leaking into the
   per-track log line.

## Fix

- `services/popularity/popularity_sources.py`
  — an uncharted title now returns `rank: 0` (documented contract), keeping
  `total` and `source = lastfm_artist_top_tracks_beyond` for diagnostics.
  `rank == 0` hits `_online_catalogue_stars`'s existing "unknown" gate.
- `services/popularity/stages/finalise_stage.py`
  — new `_is_online_rankable_artist()` guard at the top of
  `_online_catalogue_stars`: bracket credits (`[…]`) and the VA/unknown
  placeholder names return `(0, {"source": "pseudo_artist"})` **before any
  request is made**.
- Consequence (caller unchanged): unknown → stars 0 → the compilation thin-
  catalogue branch falls through to the **absolute score/listener
  thresholds**, so the Manson cues now rate 3★ (≥70 score band) instead of
  1★, and `track_artist_online_catalogue` / the impossible `#x/y` log lines
  can only appear for a genuinely charted rank.

Deliberate non-change: a title that **is** charted at the bottom of the list
still rates 1★ — that is a real-world position, not a lookup miss.

## Tests

- `tests/test_online_artist_catalogue.py`
  – `test_an_uncharted_title_is_unknown_never_a_fake_rank` (was asserting
  the `rank = len + 1` sentinel).
- `tests/test_compilation_online_catalogue.py`
  – `test_a_title_beyond_the_catalogue_is_unknown_not_one_star` (was
  asserting 1★),
  – parametrized `test_pseudo_artist_returns_unknown_without_a_request`
  (`[dialogue]`, `[instrumental]`, Various Artists, … → 0★ + no request),
  – new `TestAnUnknownCatalogueFallsBackToScoreThresholds` end-to-end class
  driving `_assign_stars` with catalogue-level stubs so the REAL not-found
  sentinel runs (score 73.2 uncharted → 3★ absolute fallback;
  `[dialogue]` credit → no chart fetch at all).

## Gate

- Targeted: 39 passed (1 known baseline failure,
  `test_healthy_local_catalogue_skips_the_online_lookup`, pinned by the
  `has_usable_catalogue = False` hardcode of `8005dac6`).
- Oracle: sources reverted → 8 of the new/updated tests fail (9 incl. the
  known baseline), stashes back to 3.
- Affected set (14 files): 32 failed — all 32 in baseline, 0 NEW.
- Full suite: **223 failed / 4918 passed / 2 skipped** — 81 fixed vs the
  303-id baseline, 1 NEW = the known flaky
  `test_sibling_torrents_root_is_searched` (full run #3; runs #1–2 hit the
  known `Windows fatal exception: access violation`, investigated: different
  positions each time — flaky, retried per protocol).
