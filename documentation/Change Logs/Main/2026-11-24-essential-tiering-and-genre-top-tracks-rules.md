# Essential Collection tiering, and the genre Top Tracks rules

**Date:** 2026-11-24
**Area:** `services/popularity/stages/finalise_stage.py`
**Commit:** `feat(playlists): star-tiered Essential order, reach-ranked genre Top Tracks`

Two playlist rule changes. Apply either with **Config → Rebuild Genre and
Playlists**.

---

## 1. Essential Collection — 5★ first, then 4★, each by popularity

Reverts the ordering half of the previous change (selection is untouched).

```
winners = sorted(key=( -stars, -reach, title, id ))
```

The selection still uses the six-step spec: primary/featured credit, version
filter, 4★ floor, and the 80th-percentile reach gate computed from primary
tracks only. Only the ORDER changed.

**Why the tier leads.** Ordering by reach alone put an enormous 4★ track above
the artist's biggest 5★ hit. For an Essential Collection the star tier is part
of what the list says — "their best" — so it comes first, and reach orders
inside each tier.

⚠️ **This exposed a bug in the percentile helper, now fixed.** It used
nearest-rank, which lands the threshold ON a data point; with a `>=` comparison
that kept the boundary point plus everything above, so a "50th percentile" floor
retained n/2 + 1 rows and, with ties, more. A "80th percentile" floor kept 3 in
10 rather than 2. `_essential_percentile` now uses **linear interpolation**, which
is the standard definition and makes the floor cut the intended fraction. This
affected the Essential gate too, so collections may change membership slightly.

## 2. Genre Top Tracks — six rule adjustments

| Rule | Implementation |
| --- | --- |
| 1. Pool, drop live/remix/demo, merge duplicates keeping the original album copy | unchanged (`_track_is_playlist_eligible`, `_group_duplicate_tracks`) |
| 2. 4★+ AND reach ≥ **global** 50th percentile of the genre | new `_genre_reach_thresholds`, built once per genre |
| 3. Reach = Last.fm listeners, else ListenBrainz listens | `_essential_reach` (shared with Essential) |
| 4. Rank by reach, artist cap applied while walking down | inline sort + `_apply_artist_cap` on the ranked list |
| 5. Covers held to the same threshold | no exemption path exists — covers are ordinary rows |
| 6. Length capped, order preserved | `max_tracks` slice, then band-scoped rotation |

**Rule 2 is the substantive change.** The old order key led with
`artist_catalogue_percentile` — an ARTIST-relative question. In a genre list that
is wrong twice over: a minor act's best song outranked a major act's, and the
300-track cap filled with acts nobody has heard. The new key is reach, the only
cross-artist number available (the stored `popularity_score` / `final_score` are
album-relative).

⚠️ **`playlists.playlist_order_mode` no longer affects genre Top Tracks.** It
selects between album-relative scores, which is meaningless for a cross-artist
chart. It still governs the curated builders.

### Rule 6 — rotation now bands on popularity

`_weekly_shuffle` used to reorder the WHOLE playlist after the cap. That undid
rule 4: the order was "most popular first" when sliced and no longer was once the
week rolled over. Genre playlists now pass `tier_key=_genre_shuffle_band`, which
bands a track by its reach rounded to one significant figure — so 9.1M and 9.4M
may swap, 9M and 900k may not. A listener could not tell the first two apart.

A fixed block size (`shuffle N at a time`) would NOT have been correct: it lets
track 1 trade places with track 10 purely because they landed in the same block.

Callers that pass no `tier_key` keep the old whole-list behaviour.

## Tests

* `tests/test_genre_top_tracks_ranking.py` — 17 tests driving the REAL builder
  through a stubbed Navidrome. 11 of them fail at the previous commit.
* `tests/test_essential_collection_selection_rules.py` — updated for the tiered
  order and the corrected percentile.

**A note on writing these tests.** The 50th-percentile floor is always in play,
so a small fixture silently tests the floor rather than the rule it names. Each
test therefore pads with tracks deliberately BELOW the ones under test, which
pulls the median down and leaves the survivors alone — and the failures during
development were all fixture arithmetic, not the code.
