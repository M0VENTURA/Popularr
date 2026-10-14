# Compilation star rating: two absolute guards on the top tier (2026-10-14)

## What was reported
On an 81-track playlist compilation (`Spotify - New Nu Metal`) the
per-track-artist catalogue rating inverted the album's real hierarchy — in
**both** directions:

* **Tiny catalogues were promoted to 5★.** "devil may cry" (Echochamber, 713
  Last.fm listeners, rank #3/171) and "type shit" (Gritmonger, 1.2k listeners,
  #1/9) took 5★.
* **Heavyweights were buried.** Deftones "infinite source" (401.2k listeners,
  #58/187) took 3★; Korn "Reward the Scars" (53.6k, #159/190) and Hollywood
  Undead "1x1" (15.5k, #126/176) took 1★.

## Root cause
The compilation branch rates on the credited artist's **rank percentile**,
which is *scale-free*: it answers "how popular is this song for this artist",
never "is this song popular". So `#1 of 9 songs` is 5★ whatever the audience
is, and a deep discography pushes a genuine hit far down its own artist's
chart. Neither direction can be seen without an **absolute** view.

The 17 `GLOBAL 5★ LOCKED` lines in the same log are unrelated and inert: that
lock is explicitly excluded for compilations (`not is_compilation`) and is
computed against the whole 3,098-track *Various Artists* catalogue.

## The fix — two config-driven guards

Both live under `single_detection.compilation_online_catalogue` and are
rendered on **both** Config pages (the config contract), with `0` disabling
either.

1. **Top-tier gate** — `top_tier_min_score` (default **55**) OR
   `top_tier_min_listeners` (default **250 000**). A compilation's 5★ must
   also clear an absolute bar. The artist catalogue still decides everything
   below the top tier, and either bar alone is enough.
2. **Standout floor** — `standout_floor_percentile` (default **0.10**) /
   `standout_floor_stars` (default **4**). A track in the compilation's own
   top band — ranked by the **cross-artist raw blend**, which is exactly what
   `raw_score` exists for — floors at 4★. Never 5★: the artist catalogue keeps
   ownership of the top tier, so the floor lifts a buried hit without
   flattening the album.

⭐ `_raw_combined` is the only usable ranking source here: for a compilation
every track's `popularity_score` has already been remapped onto its own
artist's catalogue
(`_apply_track_artist_relative_normalization`), so those values are no longer
comparable across artists. The raw blend is, and `post_album_star_ratings`
now passes the compilation's raw distribution down as `compilation_raw_scores`.

## Measured effect (from the reported table)

| Track | Artist | Rank | Before | After |
|---|---|---|---|---|
| type shit | Gritmonger | #1/9 | 5★ | **4★** |
| devil may cry | Echochamber | #3/171 | 5★ | **4★** |
| Do You Feel Alive? | Alchemize | #1/68 | 5★ | **4★** |
| KICK BACK | Private School | #2/192 | 5★ | **4★** |
| infinite source | Deftones | #58/187 | 3★ | **4★** |
| Reward the Scars | Korn | #159/190 | 1★ | **4★** |
| Sally's Song | Amy Lee | #1/164 | 5★ | 5★ (genuine hit, kept) |

## Tests
`tests/test_compilation_tier_guards.py` (27): the gate (each reported tiny
catalogue · either bar clearing it · lower tiers untouched · `0` disables), the
floor (the buried heavyweight · mid-table untouched · never 5★ · a 100-track
album floors only its decile · no/thin distribution means no floor · `0`
disables), the reported album parametrised end to end, and the config plumbing
(defaults present · partial override · **`0` survives the round trip** · both
Config pages render every key · both config JS files save every key · the save
path uses a finite check rather than `|| default`).

**Oracle**: reverting the two source files (tests kept) → **24 failed /
3 passed**.

## Verification
Baseline-identical (all pre-existing): `test_compilation_online_catalogue`
1F/27P, `test_compilation_more_5star` 2F/2P, `test_per_album_star_posting`
8F/40P. Green: `test_nightmare_revisited_scan_findings` 22,
`test_greatest_hits_artist_catalogue` 20, `test_short_track_star_cap` 28,
`test_two_phase_star_reconcile` 20, `test_templates_tag_attribute_integrity`
120, `test_rebuilt_pages_have_no_dead_buttons` 6. `py_compile` + `node --check`
clean on all five edited files.

## Still open (reported in the same analysis, not in this change)
* **#4 the `LB: 100.0` ceiling** — located: `popularity_math.py:725`
  `lb_score = max(lb_log, lb_percentile * 100.0)`. The percentile is
  ALBUM-relative, so any track leading a zero-heavy album's LB distribution
  scores 100 regardless of the absolute count (620 listens → 100, the log-score
  of ~1.8M). Planned fix: cap the album-relative uplift to a bounded number of
  points over the absolute log score, config-driven on the Config page.
* **#5 the delete-then-recreate playlist loop** — needs one confirmation:
  `_navidrome_clients()` builds one client per configured Navidrome user, so
  **two user entries pointing at the same server** would explain it exactly
  (client A dedupes; client B, holding a stale list, recreates). A `base_url`
  dedupe is the fix if that is the config.
* **#6 the `'AI slop'` genre tag** — Last.fm user tags flow into genres; needs
  a blocklist entry rather than a code change.
