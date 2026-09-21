# Short-track star cap — nothing under 70s rates above 2★

## Symptom

A 50-second track was rated 4★/5★.  Skits, interludes and hidden-track jokes
are counted as full "listens" by Last.fm and ListenBrainz, so on a popular
album a very short track's raw play counts can out-rank the album's real songs
and earn a rating its length cannot support.  The user asked for tracks shorter
than 70 seconds to be capped at 2★.

## Root cause

There was **no duration-based star rule at all**.  Three existing duration
knobs do different jobs and none of them caps a rating:

| Knob | Default | What it actually does |
|------|---------|----------------------|
| `statistics.exclude_from_median_below_seconds` | 60s | Drops short tracks from the album/artist median & MAD used for z-scores |
| `single_detection.interlude_lb_*` | 180s | Rejects a short interlude whose ListenBrainz count is an outlier |
| `scan_hooks._duration_below_floor` | 30s | Drops short tracks from the stats baseline |

Short tracks could therefore still win the star award from any of the normal
paths — era album-top-N, the 5★ single award, the 4★ single floor, or a plain
high album z-score — and once awarded, nothing demoted them.

## Fix

New `statistics.short_track_star_cap` block (`enabled`, `max_duration_seconds`,
`max_stars`; defaults **enabled / 70s / 2★**) read by the new
`helpers.config_helpers.get_short_track_star_cap_config()`, and a final clamp
in `services/popularity/stages/finalise_stage.py`.

### Ordering is the load-bearing detail

The clamp runs as the **last** rating pass — step "2.9", immediately before the
ratings are persisted — because the passes above it rewrite stars:

- `_apply_live_album_slot_caps` demotes 5★ → **4★** (and 4★ → 3★).
- the era slot cap demotes 5★ → **4★**.

A clamp applied before those would be silently undone.  It also cannot live
inside `_assign_stars`, which returns 5★ early for a user override and has its
own live-album branch that bypasses any wrapper.

### Duration had to be fetched

`track_stage`'s per-track result dict never carried `duration`, so the cap was
blind.  The existing batch query that loads stored stars and file paths

```sql
SELECT id, COALESCE(stars, star_rating, 0) AS stars, file_path FROM tracks
WHERE CAST(id AS TEXT) IN :ids
```

now also selects `duration` into a `_stored_durations` map (rows can still
override it directly).  A test asserts the SELECT contains `duration` so the
cap can never quietly regress to "sees nothing, caps nothing".

### Protections

Per the user's decision, the cap applies to **algorithmic ratings only**:

- a manual override (`single_confidence == "user"`),
- a global 5★ lock (catalogue-top pre-pass),
- a hearted track

are never lowered.  Hearts are protected twice over — `apply_favourite_rating_floor`
runs after persistence and re-raises hearted tracks to the configured floor, so a
heart wins even when this clamp could not see the flag.

The cap applies to **all album types** — studio, live and compilation — since a
50-second live interlude is just as wrong at 4★.

### Defensive details

- A demoted track has `_era_5star` cleared and `_force_floor` clamped, so a
  later slot-cap pass cannot treat the stale 5★ award as protected.
- `0` seconds, or a `max_stars` of 5+, disables the clamp outright.
- Milliseconds are normalised to seconds, but the division only fires **above
  3600s** — a 600s threshold would misread a legitimate 700s (11-minute) epic
  as 0.7s and wrongly cap it.

## Configuration

Surfaced on the Config page in **both** UI trees (the test-site tree is served
first when the cutover is active):

- `templates/pages/config.html` + `static/js/config.js`
- `test_site/templates/Pages/config.html` + `test_site/static/js/pages/config.js`

`statistics.short_track_star_cap` sits inside the existing **Statistics** card,
next to the Interlude / Skit Duration Floor.

## Files

- `helpers/config_helpers.py` — `_DEFAULT_SHORT_TRACK_STAR_CAP`,
  `get_short_track_star_cap_config()`.
- `services/popularity/stages/finalise_stage.py` — `_apply_short_track_star_cap`,
  `_short_track_cap_is_protected`, `_track_duration_seconds`; `duration` added to
  the batch rating/path load; clamp wired as step 2.9.
- `templates/pages/config.html`, `test_site/templates/Pages/config.html` — the
  Short Track Star Cap inputs.
- `static/js/config.js`, `test_site/static/js/pages/config.js` — collect the block.
- `tests/test_short_track_star_cap.py` — 28 tests: config helper defaults and
  partial overrides, the clamp, threshold semantics (`< 70s`, not `<=`),
  ms normalisation, the exemptions, ordering (duration really is SELECTed, and a
  50s 5★ track is persisted at 2★), and the Config-page contract in both trees.
