# Scoring page: the rule order is now stated, ratings explain themselves, and the config is exportable

**Date:** 2026-11-23
**Area:** `services/popularity/stages/finalise_stage.py` ·
`services/popularity/star_resync_service.py` · `routes/ui_routes.py` ·
`helpers/config_export.py` · `test_site/templates/Pages/config.html` ·
`templates/pages/config.html` · `test_site/static/js/pages/config.js` ·
`static/js/config.js`
**Commits:** `feat(scoring): document the star-rule order and explain each rating`,
`feat(config): add a redacted configuration export`

## Reported / observed

The Popularity & Scoring page explained ~ten separate ways a track can reach or
lose 5★, each on its own, and never said **which one wins**. It also carried
several help strings that no longer matched the value next to them, and buried
drift: nothing told you a setting had been moved off its default.

## First: most of the reported "conflicts" are configuration drift

Before changing a single number, each claim was checked against the code
defaults. **12 of 21 reported values are not defaults at all** — they are what
this installation has drifted to:

| Setting | Reported | Code default |
| --- | --- | --- |
| Medium-Bump Top % | 0.10 | **0.20** (`artist_medium_bump_percentile`) |
| Minor era max 5★ | 3 | **2** (`_DEFAULT_ERA_RULES`) |
| Peak era max 5★ | 3 | **4** |
| High↔Medium boundary | 0.8 | **1.0** (`zscore_high_threshold`) |
| Force 4★ Top % | 0 | **0.10** |
| Album ratio min_passed | 2 | **3** |
| Album curve enabled | on | **off** |
| Album curve 5★ %ile | 0.80 | **0.85** |
| 3★ / 2★ album-z | -0.3 / -0.8 | **-0.5 / -1.2** |
| Organic Floor Score | 50 | **45.0** |
| Per-Track Scan Timeout | 120 | **30** |

So "Medium-Bump equals Artist Top %" is **not** a code bug: the default is
already 0.20, twice the top band, and the bump fires. Changing the shipped
default would have "fixed" a config that was simply edited. The gap was that the
page never *said* a value had drifted — which is now fixed by the export below.

Two more reported items are not defects:
* **Force 4★ Top % = 0** is a deliberate disable (`0` disables), and the
  shipped default is 0.10.
* **"untitled" in the interlude regex** does not exist. The default regex is
  `(^intro$|\bintro$|interlude|\[intro\]|\(intro\))`; `untitled` appears
  nowhere in it, and `grep` finds it only in test fixtures. The real defect is
  the one that *is* there: `\bintro$` also matches `"Golden Ratio (Interlude)"`
  and `\bintro$`-style titles, which is covered in "still open" below.

## What changed

### 1. The rule order is on the page

A collapsed block at the top of Star Ratings lists the order the code actually
applies, with line references into `_assign_stars`:

1. a user rating returns 5★ before anything else and is never capped;
2. live albums are rated on artist-z and return there; compilations take their
   own path;
3. the global 5★ catalogue lock (skipped in a popularity-only scan, which is why
   those scans cap regular albums at 4★);
4. the forced artist percentile (top 3% → 5★, top 10% → 4★), which
   short-circuits every later rule;
5. the **base tier** — Album Curve if enabled, else the z-bands;
6. 5★ promotion: standout + organic reach + clear of the album-shape gate;
7. the dedicated-single floor of 4★;
8. then the ordered caps, which can only demote: live-album slots → era slots →
   minor-era penalty → short-track cap.

It also states what is **protected from every cap**: a user rating, a
`_lock_honoured` catalogue-top track, a forced-percentile 5★, and (on a
penalised minor-era album) a genuine standout.

`tests/test_star_rule_order.py` (12 tests) pins that sequence against the source,
so the page cannot document an order the code no longer uses.

### 2. Every rating explains itself

`star_rating_reason(track)` reads the verdict fields the rules already write
(`_force_floor`, `_album_curve_stars`, `_live_reason`, `_ratio_5star_reason`,
`_era_5star`, `_lock_honoured`, `_dedicated_single_floor`) and produces one line:

```
3★ · album curve put it at 4★; album-shape gate: floor_failed
5★ · forced 5★ (artist top % by raw listeners); album_z=0.20, artist_z=0.40
5★ · user rating wins (never capped)
```

It never re-derives a rating, so it cannot disagree with the score it describes.
The resync preview now shows it under each changed track.

### 3. The Preview shows the library's shape

`star_histogram` (before/after counts per tier) and `five_star_albums` (which
albums carry the top end, most first) are in the resync payload, rendered as a
before/after table with a delta column. This is the fastest read on "did that
change make the library too harsh or too generous" — the question the brief
opens with. Counted over **every** rated track, not just the changed ones, so it
describes the library rather than the diff.

### 4. Help text that tracks its value

- **"Top 15% of the album"** was a literal next to a configurable percentile. At
  this install's 0.80 the page claimed 15% while the code used 20%. It is now
  computed from the value (and rounds `15.000000000000002` before `ceil`, which
  the first attempt printed as 16%).
- **The 1★ card** hard-coded `-1.2` for the 2★ band; it now reads the 2★ input.
- **The 5★ catalogue cap** says "top 15%" computed from its own value.

Verified at four configurations; the text follows the value every time.

### 5. A shareable configuration export

New card at the **top** of System & Users, plus `GET /config/export`
(`?format=yaml` default, `?format=json` for tooling).

An export exists to be shared, so every credential and account name is replaced
with `<redacted>` — while the marker still distinguishes "configured" from "not
configured", which is what a support thread needs. It covers every position a
config holds one: top-level (`nav_pass`, `pg_password`, `lastfm_api_key`),
nested (`api_integrations.*.api_key` / `token`), and inside lists
(`navidrome_users[].user` / `.password`).

It also carries `changed_from_default`, the audit the page otherwise makes you do
by hand across 400+ fields — the same audit that answers the drift question at
the top of this entry.

Non-secrets survive: `music_folder`, `num_users`, `base_url`, and a playlist
literally named `"Secret Garden"` are all preserved. Matched on the KEY, never
the value, precisely so that name is not blanked.

`tests/test_config_export.py` (10 tests) covers all 15 credential positions,
the configured-vs-missing distinction, and the over-eager-redaction failure.
The first implementation **missed `nav_pass`** — `"pass"` is a fragment of
`"password"`, not a word of its own — which that test caught.

## Verification

- 45 new tests pass; the config/playlist/genre suites are unchanged at
  **416 passed**.
- `changed_from_default` correctly flags exactly the three drifted values from a
  real-shaped config (`zscore_high_threshold`, `artist_medium_bump_percentile`,
  `track_timeout_seconds`) and nothing else; `0.10` vs `0.1` is not drift.
- The export was exercised end-to-end: both formats return 200, correct
  `Content-Type` and `Content-Disposition`, and zero leaked values.
- Both trees render identically: curve text "Top 20% of the album" for 0.80,
  cap "top 15%" for 0.85, 1★ band `-0.8` — each following the live value.

### One failure investigated and cleared

The full suite runs **404 failed / 9818 passed** against a baseline of 403. The
single extra is
`test_playlist_song_id_rejections.py::test_repeated_scan_does_not_rewrite_for_a_known_rejected_id`:
my new test files shift pytest's alphabetical collection order, and that test
needs a `playlist_song_rejections` table which an earlier test's `db_session`
call can dispose. Confirmed **pre-existing**: it fails identically in a clean
`git worktree` at HEAD with none of these changes applied.

## Deliberately not done

Retuning numbers. The brief's own instruction was to fix the conflicts and make
the order explicit first, and the export now shows which values are off default,
so a retune can be based on that rather than on the table above.

### Still open, worth doing next

- **The interlude regex.** `\bintro$` matches `"Golden Ratio (Interlude)"` — a
  real song on a real release — because `interlude` is matched as a substring
  with no duration guard. The fix is word boundaries plus "only when the track
  is short", which needs the duration at match time and therefore a signature
  change in `album_classification_service`.
- **Album Curve catalogue floor.** With the curve on, a strong album still gets
  a forced bottom tier (`curve_2star_percentile` 0.12 means the bottom 12% is
  1★). A floor based on the artist's whole catalogue would stop a classic album
  inventing weak tracks.
- **Sandbox fidelity.** It already says it "excludes artist-z marking and the
  era 5★ caps" — accurate, but it now matters more, since the curve and the caps
  decide much of the top end.
- **Section order** (weights last, visibility first) and moving Genre
  Re-evaluation off this page, which duplicates Genre → Playlist Resync.
