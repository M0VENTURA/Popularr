# Great Covers — rank by popularity, cap while walking down

**Date:** 2026-11-24
**Area:** `services/popularity/stages/finalise_stage.py` ·
`helpers/config_helpers.py` · Config page (both trees)
**Commit:** `fix(covers): rank by popularity like genre Top Tracks`

Apply with an album/artist scan, or **Config → Rebuild Genre and Playlists**.

---

## What was reported

* "the playlist's order has essentially no relationship to listener counts or
  final scores (correlation about 0 for both), so it looks shuffled"
* "only 50 of the 139 tracks are among the 139 most popular metal covers"
* "115 of the tracks are rated 5 stars, so tracks were picked mainly for being
  the top-rated song on their album"
* "Wrong version: Faith (edited) is used … prefer the unedited album version"
* false covers from an album-level "Cover" tag, plus a remix

All verified against `tests/Popularr - collection popularity report (15).csv`.

## 1. The order was shuffled, literally

The covers path called `_weekly_shuffle` with **no `tier_key`**, so it fell
through to a whole-list shuffle. Whatever the sort key computed, the pushed order
was randomised once per ISO week — a correlation of ~0 is the expected result.

It now passes `tier_key=_genre_shuffle_band`, so rotation happens *within* a
reach band, exactly as the genre Top Tracks builder does.

## 2. Selection ranked on the wrong number

The old key was **stars first, then `final_score`**. `final_score` is
album-relative — a remap inside one release — so it says nothing about whether a
cover is famous. In a list spanning many artists that is why 115 of 139 entries
were 5★: they were the top-rated song on their own album.

Now: **stars ≥ 4, rank by reach**, the rule the genre playlists already use.

⭐ Personal Jesus (Marilyn Manson) has **489k listeners but only 3★**. Under
stars-first it could never appear. Under reach it leads.

## 3. The cap now walks down the ranking

`_apply_artist_cap` runs on the **ranked** list, before the `max_tracks` slice —
the order the genre builder uses. This matters more for covers than anywhere
else: Metallica alone has 26 of the 139 most popular metal covers, Apocalyptica
15. A cap of 3 keeps each act's **biggest** covers.

Reproduced on the report: cap 3 → 203 tracks pass, minimum reach in the first 150
is ~9k; cap 5 → ~21k; no cap → ~37k.

## 4. Version preference: the unedited album copy wins

`Faith (edited)` and `Faith` are one track to a listener and share a listener
count, so nothing in the score could separate them — `(edited)` won on title
order. `_version_preference_key` now decides it explicitly:

1. never `(edited)` / `(explicit)` / `(clean)`;
2. never a compilation (the album version is the original);
3. a measured row beats an unscanned duplicate reporting 0 listeners.

⚠️ Opt-in via `prefer_canonical=True`, because `_sync_single_curated_playlist`
is shared by **eight** playlists (acoustic, high-energy, …). Only the two covers
builders set it.

## 5. False covers

`This Is the New Shit`, `No Class` and `Take a Look Around (Timbaland remix)`
were admitted by the flag alone.

**Root cause:** MusicBrainz tags a *cover album* with the genre `cover`, and the
album blend writes it to the track. It is a fact about the **release**, not the
recording. The detector itself is per-recording and does not do this — I checked
`cover_detector_impl` step 5, which explicitly excludes album text.

`_cover_rows_are_credible` keeps a row only when it has **per-track** evidence:
`original_cover_artist` recorded, or the title carrying the detector's
`(X Cover)` suffix. Both are written per recording. A row justified only by an
album-level genre is dropped, and so is a remix (a remix of your own song is not
a cover).

⚠️ **It fails open.** With no per-track evidence anywhere — an older library, or
a detector that never ran — the guard does nothing rather than emptying the
playlist. `_CURATED_ROWS_SQL` now selects `original_cover_artist` to make the
distinction possible at all.

## 6. Star floor: 3 → 4

`covers_min_stars` (default 4, `0` disables) replaces the hard-coded 3. At 3 a
2★ cover with **39 listeners** reached the pool because its broadly-relative
score happened to be the highest on its own release.

⚠️ `0` really disables it — written as `max(0, int(...) or 0)`, not `or 4`, which
would have silently turned 0 into the default.

## 7. Config surface

`covers_min_stars` is emitted by all three collectors and rendered by both Config
pages. The repo's own guard caught the omission before I committed:
`save_config` replaces the whole `playlists` section, so an uncollected key would
be **deleted on every save**.

## Tests

`tests/test_covers_playlist_ranking.py` — 15 tests driving the REAL builder;
8 fail at the previous commit.

## Not changed

* Disturbed is in the excluded-artists list, so `The Sound of Silence` and
  `Land of Confusion` stay out. That is correct.
* `Come Sail Away` (Eric Cartman) is not tagged as a cover but still reaches the
  pool through `is_cover`; its genres are `Alternative Metal; Comedy`. The
  credibility guard does not remove it, because "comedy" alone is not evidence
  either way. **It needs a per-track verdict correction, not a code rule** — and
  a Comedy-affiliation track slipping into a metal-covers list is worth a
  separate look at whether genre should gate this playlist at all.
