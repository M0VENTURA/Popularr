# Album Star Curve + Star Rating Resync — 2026-10-27

## Why

Two separate problems, one root cause.

### 1. Star ratings were ranking on a number that inverts

A rating is supposed to describe a track's place **on its album**, and
`_album_z_band_star` plus `_compute_artist_z` already implemented exactly that
curve. The defect was the **input**: on a regular album both `popularity` and
`final_score` carry the per-artist re-map, which is not monotonic in audience.

Measured on the collection report (69,466 rows), inversions against Last.fm
listeners across ~1,790 albums with 8+ tracks:

| metric banded | inversions |
|---|---|
| `popularity` / `final_score` (re-mapped) | **13,390** |
| `lastfm_score` (absolute log) | 400 |
| album-z banded on the re-map | **6,455** |
| album-z banded on the absolute log | **0** |

Concrete, AC/DC *Back in Black*:

| listeners | popularity |
|---|---|
| 2,203,191 | 84.4 |
| 931,173 | **31.1** |
| 388,607 | **35.2** |
| 134,444 | **53.8** |

A 134k track outscored a 931k track. Consequence in stored stars: 59.2% of
albums (1,071 of 1,809) had at least one inversion — *Parachutes*' 956k title
track was 2★ while a 26k track was 4★; the Black Album's 1.12M *Wherever I May
Roam* was 1★ while a 134k track was 4★.

### 2. The bottom tier was unreachable

The absolute ladder's lowest band is `score < 35`, which no track with any
listeners can fall into. Measured: d'Artagnan *Felsenfest* held 8 of 20 tracks
at 2★ and **none** at 1★; *Herzblut*, *In jener Nacht*, *Seit an Seit* and
Ad Infinitum's *Abyss* all had zero 1★ tracks.

## What changed

### `_album_curve_star` (`services/popularity/stages/finalise_stage.py`)

Assigns the tier from the track's **percentile inside its album**, so the shape
is guaranteed:

- `0.80 / 0.55 / 0.30 / 0.12` → 5 / 4 / 3 / 2 stars, below 0.12 → 1 star.
- **Artist-catalogue cap** — each tier must also clear a percentile of the
  artist's *whole* catalogue (`0.85 / 0.60 / 0.30 / 0.10`). The cap settles on
  the tier the catalogue standing supports and can only **demote**; it never
  promotes a track the curve did not already place there.
- **Absolute floor** — optional `five_star_min_listeners`.

### `_curve_metric` — the input fix

Ranks on `min(100, log10(listeners+1) * LOG_SCALE_MULTIPLIER)` instead of
`popularity_score`. This is what removes the 6,455 inversions; the curve is only
meaningful on a monotonic input.

### Scope

The curve replaces the base tier **only** for regular albums:
`not is_compilation and not is_live and not popularity_only`, with at least
`min_album_tracks` (default 3) scored tracks. Compilations, live albums, singles
and EPs keep their existing paths. It supplies a *base* only — every downstream
cap and floor still applies (era slots, live caps, short-track clamp, favourite
floor, user locks, forced percentiles).

### `services/popularity/star_resync_service.py`

Re-rates the library from **stored** popularity data — no Last.fm,
ListenBrainz, Discogs or MusicBrainz calls.

It deliberately does **not** reimplement rating: it groups stored rows into
albums and calls the same `post_album_star_ratings` entry point a scan uses, so
every downstream rule still applies. `POST /api/ratings/resync` exposes it with
`dry_run` for preview.

The artist catalogue pool is built from **every loaded album** of that artist,
not just the album being rated — the cap and the artist-eligibility gate read
the whole catalogue.

## Defaults

⚠️ **The album curve ships DISABLED.** Enabling it replaces the base tier for
every regular album, which would silently re-rate the whole library on the next
scan. Switch it on from the Config page, then run **Preview Rating Changes**.

The resync ships **enabled**, honouring the configured file-tag and Navidrome
toggles.

## Config (§4.1 — Config page first)

New blocks in `single_detection`:

- `album_star_curve` — `enabled`, four curve percentiles, four catalogue caps,
  `five_star_min_listeners`, `min_album_tracks`
- `star_resync` — `enabled`, `write_file_tags`, `sync_navidrome`,
  `albums_per_batch`

All surfaced on both `templates/pages/config.html` and
`test_site/templates/Pages/config.html`, with readers in `helpers/config_helpers.py`
(`get_album_star_curve_config`, `get_star_resync_config`) and the blocks sent by
all three config JS bundles.

## Verification

- 34 new tests in `tests/test_album_star_curve_and_resync.py`.
- Failure-set diff against pristine `develop` on the scoring sweep
  (48 vs 47 failures): **0 new, 0 gone**. The single apparent addition,
  `test_full_scan_startup_fix.py::…test_live_scan_still_rejected`, fails
  identically on pristine `develop` in isolation (`no such table: scan_states`).

## Known limitations

- Enabling the curve re-rates every regular album. Slim EPs with 3–4 tracks are
  forced into 5 distinct tiers by construction, which is aggressive for short
  releases; raise `min_album_tracks` if that is unwanted.
- The cap makes artist-catalogue standing dominant at the top. With the default
  cuts, a track that is #1 on a quiet album but 18th-percentile for its artist
  lands on 2★. All cuts are configurable.
- Measured effect with the floor at 0: 2,556 five-star tracks vs 4,367 stored,
  median reach rising from 44,560 to 97,304 listeners. 386 five-stars still sit
  under 10,000 listeners, because each is genuinely top-decile for a small
  artist. Set `five_star_min_listeners` to bound that.
