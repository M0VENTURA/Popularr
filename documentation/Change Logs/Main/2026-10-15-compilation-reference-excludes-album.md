# A compilation is no longer part of the reference used to judge it

**Date:** 2026-10-15
**Files:** `services/popularity/stages/finalise_stage.py`,
`services/popularity/scan_stage_runner.py`

Asked
-----
> I previously had that 5 star tracks shouldn't have a limit on compilations,
> is that still the case? I want any song that was a high popularity single to
> be able to score 5 star on a compilation as they may be a greatest hits
> population. But I also don't want that to force any that weren't 5 star to be
> 1 star due to the rest of the album being rated so highly.

Two separate questions. Only one needed changing.

## 1. The slot cap — still absent, now pinned

Both cap passes in `post_album_star_ratings` are gated `and not is_compilation`
(the live-album caps and the era slot caps), and the 4★ percentage cap is
commented out. A compilation may hold as many 5★ tracks as earn them.

`TestNoSlotCapOnACompilation` pins both directions: six forced-5★ tracks all
survive on a compilation, while the same six are capped to 2 on a studio album.
The control is what makes the first assertion mean anything — without it,
"nothing was demoted" could simply be a cap that stopped working everywhere.

## 2. The ratings WERE self-referential — fixed

`_assign_stars` rates a compilation track against the credited artist's
catalogue, and that catalogue came from
`compute_track_artist_scores(artist, album_results)` — **which counts the album
under rating**. A greatest-hits record is nothing but hits, so its own tracks
dragged the artist's catalogue mean up, pushed every track's z down, and
flattened the album.

Measured, an 8-track best-of against a 20-track catalogue of the same artist:

| | 5★ | 4★ | 3★ |
|---|---|---|---|
| album in its own reference | 0 | 1 | 7 |
| album excluded | 6 | 1 | 1 |

A record of hit singles that could not produce a **single 5★**. That is the
first half of the request failing, and its mirror image is the second half: the
same inflated mean is what pushes the weaker tracks down.

There is a second, independent reason: on a best-of, every hit is also on the
album it was lifted from, so **the same recording was counted twice** — the
hits carried double weight in the distribution they are supposed to be
exceptional against. The function's own docstring already intends
``excluding any (album, title) pair already covered … so nothing is
double-counted``; the dedupe cannot see this case, because the two copies are
different (album, title) pairs.

### The change

`exclude_album` keeps the album out of both halves of that decision, because
they are two views of one question:

* `compute_track_artist_scores` — the reference the track is rated against;
* `_load_track_artist_scores` — the distribution behind the per-artist score
  remap that produces the score in the first place.

Both filter at the **album** level, not just by (album, title): a stored title
can drift from the scanned one (a rename, or a feat. credit the scan moved onto
the artist), and those rows still belong to the album under rating.

Also removed: a duplicated `_build_compilation_track_artist_scores` call in
`post_album_star_ratings` — the same computation ran twice and the second result
overwrote the first.

### Why it cannot crush anything

Removing the album's own (high) scores **lowers** the reference mean, so every
remaining track's z rises. The change is upward-only in that respect, which is
exactly the property the request asked for.
`test_a_weak_track_is_not_crushed` pins it: the filler stays at its own rating
while the hits are lifted.

### Scope: compilations only

`compute_track_artist_scores` has exactly one production caller, the compilation
builder. Studio albums are untouched — there the album genuinely IS part of the
artist's catalogue rather than a re-presentation of it.

A library that owns ONLY a best-of is left with a thin catalogue, which is the
honest answer (a best-of cannot evidence the artist's range) and falls through
to the existing online/thin path.

### Not configurable, on purpose

This is a correctness fix, not a preference: "should an album be evidence about
itself" has one right answer. No Config-page entry was added.

## Tests

`tests/test_compilation_reference_excludes_album.py` — **17 tests**: the
reference and remap exclusion (DB-backed, with an unseen-title case for the
album-level filter), the rating consequence, the crush control, the no-cap
question in both directions, and controls proving `exclude_album=None` is the
old behaviour and an unknown album name is a no-op.

⚠️ The most valuable one is `test_post_album_star_ratings_passes_the_album`.
Every other test passes `exclude_album` itself, so on a reverted tree they can
only raise `TypeError` — which proves the parameter exists, not that the rating
path uses it. That test spies on the real call from
`post_album_star_ratings`, so it fails if the album stops being passed.

Oracle (sources reverted, tests kept): **13 failed / 4 passed** — the 4 are the
controls, which pass on both sides by design.

Sweeps, all matching baseline: `test_compilation_online_catalogue` 28 ·
`test_compilation_tier_guards` 27 · `test_greatest_hits_artist_catalogue` 22 ·
`test_nightmare_revisited_scan_findings` 22 · `test_locked_five_star_not_revoked`
17 · `test_two_phase_star_reconcile` 20 · `test_single_release_stars_use_artist_catalogue`
10 · `test_online_artist_catalogue` 12 · `test_release_credit_matching` 29 ·
`test_short_track_star_cap` 28 · `test_album_ratio_standout` 17 ·
`test_lb_cohort_rating` 12 · `test_scan_boundary_unwinds` 11 ·
`test_genre_playlist_exclusions` 57. Pre-existing and unchanged (verified with
the change stashed, ID-identical): `test_compilation_more_5star` 2F/2P,
`test_per_album_star_posting` 8F/40P, `test_prominence_era_benchmark` 3F/7P,
`test_track_sync_and_live_detection` 2F/13P.

Harness note worth keeping: `post_album_star_ratings` reaches
`apply_favourite_rating_floor`, which lives in its own module and therefore uses
its own `db_session` — unreachable from a `finalise_stage` patch. Left real
against the test schema it queries a column that is not there, and a missing
column inside `db_session` is classified as transient, which **disposes the
shared engine** and wipes every table for the rest of the session.
