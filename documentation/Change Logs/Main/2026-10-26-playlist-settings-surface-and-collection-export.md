# Playlist settings you could not reach, and a collection-wide popularity export

**Date:** 2026-10-26
**Area:** both `config.html` trees · both `config.js` · `routes/misc_routes.py` ·
`services/popularity/popularity_report_service.py` ·
`services/popularity/stages/finalise_stage.py` (verified only)
**Commit:** `fix(config): expose every playlist setting, add a collection CSV export`

## Request

> "We also added a few settings the other day for other playlist creation
> options, but I don't see the option to turn them on or off via the config
> page. Can you also add an option on the config page to export the popularity
> data for all tracks in the collection similar to how it's done on each artist
> page?"

Plus the follow-up on the per-artist cap from the previous message.

## 1. The per-artist cap was ALREADY a hard quota — verified, not changed

The claim worth checking was "`Max Tracks Per Artist` is not a cap on the
playlist, because interleave runs after it". That was true of the version
documented in the *measured defects* note from 2026-10-07, and `da0575d3`
already fixed it. Measured against the shipped code with a 600-track pool
(1 artist × 200, 4 × 100):

| `max_per_artist` | interleave | returned | per artist | max per artist after `[:300]` |
|---|---|---|---|---|
| 0 | on/off | 600 | 200 / 100 × 4 | 200 (no cap, as documented) |
| 3 | on | 15 | 3 each | 3 |
| 25 | on | 125 | 25 each | 25 |

`_ordered_playlist_rows` ranks → **caps** → interleaves, so capping happens
before spreading and each artist keeps its **strongest** tracks. Nothing is
resurrected, because the cap drops rather than defers.

What WAS wrong is the help text on both Config pages: *"Overflow tracks move to
the end rather than being dropped."* The cap drops them. Rewritten to say so,
and pinned by a test.

## 2. Eight playlist families had no switch, and saving DELETED their settings

`save_config` replaces `config.yaml` wholesale — `config_save_json` carries
forward **top-level sections only**. So any `playlists.*` key the page script
does not emit is removed on save and reverts to its code default.

Measured across both trees (ids rendered vs keys emitted vs keys the backend reads):

* **`test_site` rendered 29 playlist inputs and emitted 29 keys; the backend
  reads 48.** The eight optional families — decade, hit singles, hidden gems,
  new releases, covers, acoustic, high energy, mood — had **no toggle at all**,
  and neither their limits nor their name templates. So they could not be
  switched on, and an ordinary save silently reset all of them.
* **The live tree had the eight toggles but not `essential_max_tracks`** — read
  by `_create_essential_playlists` (default 50), never collected anywhere, so a
  hand-edited value was deleted on every save.

Added to `test_site`: the eight toggles, the eight families' limits, and the
eight name fields. Added to **both**: `Essential Playlist Max Tracks`.

**Two rendered defaults also disagreed with the backend.** `get_playlists_config`
defaults `decade_playlists_enabled` and `hit_singles_playlist_enabled` to
`True` while both templates rendered `False`, so the checkbox said OFF for a
feature that was effectively ON. The templates now default `True`, matching the
`config.js` collectors and the getter — a display fix, **no behaviour change**.

## 3. Collection-wide popularity export

`build_popularity_report(artist=None, album=None)` gained a third scope:
`artist=None` means the whole collection, expressed as `1 = 1` so the statement
keeps ONE shape and the `ORDER BY` is provably identical in every scope. The
artist and album reports therefore remain exact subsets of the collection file
at the same relative order.

* `build_collection_popularity_report()` — a thin wrapper, deliberately not a
  second `SELECT`.
* `GET /api/popularity/report` (`misc_api_bp`) — offloaded with
  `asyncio.to_thread`, because this is the only unbounded scope.
* Config page ▸ Analytics ▸ **Export Popularity Data** (both trees) — a download
  button pointing at the route by ENDPOINT (`url_for`), not a hard-coded URL.

## Verification

`tests/test_playlist_quota_and_config_surface.py` (28).

Oracle — source reverted with the tests present: **17 failed / 11 passed**. The
11 that pass unpatched are the point: five of them are the quota tests (the cap
already worked), and the rest are guards that must hold either way (shared CSV
columns, no orphan collectors). Restored → **28 passed**.

Regression sweep, 42 playlist/config/template suites, one process per file:
patched **23** failing ids vs baseline **23**, `Compare-Object` NEW **empty**.

`import app` → **425 routes**, `/api/popularity/report` registered; both config
templates parse; `node --check` clean on both page scripts.

## Held back / noted

* The cap is applied **before** interleaving on purpose: capping afterwards
  would keep whichever tracks the interleave happened to place first rather
  than each artist's strongest. Do not "fix" that ordering.
* `decade_playlists_enabled` / `hit_singles_playlist_enabled` still default to
  ON in the getter. Making them opt-in like the other six is a deliberate
  product decision, not a bug fix — it would stop two playlists being generated
  for anyone who has never saved the Config page.
