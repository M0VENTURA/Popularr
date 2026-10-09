# Scan windows, scheduled scans, and saying what each mode does (2026-10-08)

## Asked

> If I run the finalise scan from the dashboard, will that also run the prior
> scans if they haven't been run for a while such as metadata, popularity,
> singles, essentia?
>
> Is there a config setting that sets how long each scan will run again if not
> forced? If not, can we detail it in a cleaner way using test_site?

## Answers

**1. No.** The dashboard's *Finalise* is dispatched as `singles_only=True` +
`finalise_only=True` (`popularity_pipeline.py`), and those flags switch the
other stages *off*:

| Stage | In a Finalise pass |
|---|---|
| Album skip gates | **bypassed** — every album is visited (`if not force and not album_filter and not _mode_finalise:`) |
| Album type resolution | stored/heuristic only — no MusicBrainz release-group lookup, no write |
| Full enrichment (Last.fm / ListenBrainz / Discogs) | off (`singles_pass` short-circuits `_run_full_enrichment`) |
| Post-singles enrichment (covers, genres, artist metadata) | off (`if _full_pass or _mode_meta:`) |
| Annotation repair, cover detection, recommend stash, missing-track recompute | off (`and not _mode_finalise`) |
| Popularity | only where a score is **missing** (`_pop_due = False`) |
| Singles | only where the verdict is stale/missing |
| Stars + Navidrome sync + genre playlists + file-tag sync | run |
| **Essentia** | **not part of any popularity pass** — `options["run_essentia"]` is set but never read |

So a stale metadata/popularity/singles/Essentia scan is **not** caught up by
Finalise. Nothing on any page said so.

> ⚠️ **SUPERSEDED (2026-10-09)** — "Album skip gates: bypassed" and
> "**Finalise** bypasses them too" below are no longer true. A NON-FORCED
> Finalise now follows the config rules for the scan type it runs (it is
> dispatched as `singles_only`, so the **Singles Scan Window** and the singles
> half of **Skip Unchanged Albums** decide, against the `singles` history it
> writes); Force still bypasses. See
> `2026-10-09-finalise-follows-the-skip-windows.md`.

**2. Yes — two settings, and both were half-broken.**

*Rescan windows* (Config → **Scan Behaviour**): `album_skip_days` (7),
`popularity_skip_days` (7), `singles_skip_days` (7), `metadata_skip_days` (0 =
always), plus the `*_old_album_skip_days` (30) used for albums older than
`old_album_age_months` (48). `0` = always; **Force** bypasses them;
**Finalise** bypassed them too (superseded 2026-10-09 — it now honours them,
and Force still bypasses).

*Scheduled jobs* — `scheduler.jobs.<id>.interval_minutes` in `config.yaml`
(`library_sync` 360 min, `popularity_scan` 1440 min). **Not editable anywhere
in the UI**, and two defects meant even a hand edit did not behave:

1. **`reschedule_jobs_from_config()` passed the ROOT config** to
   `_register_default_jobs`, while `get_scheduler()` passed
   `config["scheduler"]`. So a Config save read `config["jobs"]` — a key that
   does not exist — and **reset every custom interval to its default**, while
   boot honoured the intervals but read the `watcher` toggles from a
   `scheduler.watcher` path that does not exist. Each caller saw half the
   settings.
2. **The Config page's collector emitted no `scheduler:` key at all**, and
   `save_config()` *replaces* `config.yaml` wholesale — so an ordinary save
   **deleted the whole section**.

And `_interval` was `float(x or default)`, which maps `0` back onto the
default, so "0 = never" could never have worked.

## Changes

**Backend (shared — fixes both UI trees):**

* `services/scheduler/scheduler_service.py`
  * `_register_default_jobs(scheduler, cfg, *, root_cfg=None)` — `jobs` from
    `scheduler.jobs`, `watcher` falling back to the **root** `watcher:` section
    (which is what the Config page edits). Both callers now pass the same shape.
  * `reschedule_jobs_from_config()` reads `cfg.get("scheduler")`.
  * `_interval` preserves an explicit `0`; `library_sync` / `popularity_scan`
    treat `interval_minutes <= 0` as *disabled* (same convention as every other
    "0 = never" setting here). Garbage still falls back to the default.
* `helpers/config_helpers.py` — new `read_config_from_disk()` (bypasses cache).
* `routes/ui_routes.py::config_save_json` — carries forward any **top-level
  section the payload is missing**, so an ordinary save can no longer delete
  what the page does not collect. Only *absent* keys are filled in, so an
  explicit payload value always wins. **This is what keeps the LIVE tree safe**
  too, since its collector was deliberately left alone.

**test_site UI:**

* `components/_scan_mode_key.html` (new) — a JS-free `<details>` legend: what
  each mode does, its rescan window, and explicitly that **Finalise runs
  neither metadata nor Essentia and ignores every window**. Included from all
  four selectors (`_scan_selector.html`, `dashboard.html`, `artist_detail.html`,
  `_release_section.html`).
* `Pages/config.html` — new **Scheduled Scans** card (both intervals, 0 =
  never, what it does *not* schedule) and a read-only **Rescan windows at a
  glance** table rendered from the live config, so it cannot disagree with the
  inputs. The old help text listed four modes and omitted Finalise entirely.
* `static/js/pages/config.js` — the collector now emits `scheduler:`,
  preserving every entry it does not edit, and reads both inputs with
  `parseNumber` (never `|| default`, which would turn a 0 back into a default).

## Tests

`tests/test_scan_windows_and_scheduled_scans.py` — **38**:

* the legend: exists, reaches all four selectors, is JS-free, lists every mode,
  and states the three facts about Finalise;
* the config card/inputs/collector: ids, `scheduler.jobs` prefill, emission of
  the section, `parseNumber` not `|| default`;
* the summary table: read-only, every window read from config with its real
  default, Finalise/Essentia rows;
* both scheduler entry points read the same shape (source anchors on the two
  call sites);
* **behaviour**: interval `0` removes the job, positive registers it, garbage
  falls back, a root `watcher` toggle gates the job, an explicit
  `scheduler.watcher` wins, and the store-race suite's existing disabled-config
  shape still behaves;
* **end-to-end**: a `POST /config/save-json` payload *without* `scheduler:`
  leaves `scheduler.jobs.popularity_scan.interval_minutes: 90` intact while the
  payload's own value still wins when it IS present.

**Oracle:** reverting the ten source/UI files → **36 failed / 2 passed** (the 2
are the genuine controls: the store-race shape and the "payload already carries
it" case).

**Sweep:** 45 config/scheduler/scan/route/save files, clean `origin/develop` vs
this change — **base `17 failed / 742 passed`** vs **new `17 failed / 780
passed`**, `Compare-Object` on the sorted `^FAILED` lines = **empty both ways**.
**0 regressions**, +38 new tests.

## Traps hit and recorded

* **A guard's own comment quoting the pattern.** The `0`-preservation test
  failed on `// NOT \`|| 360\`.` — the comment *beside* the collector. Fixed by
  stripping JS comments before matching (the same trap as the `window.X =`
  guards). A source probe must strip comments first, always.
* **`add_job.call_args[0]` is the CALLABLE, not the job id** — `_put` pops
  `func` out of kwargs. Reading the first positional made every job assertion
  look at the wrong thing; the id is `kwargs["id"]`.

## Known and deliberately not changed

* The **live** tree's `static/js/config.js` and `templates/pages/config.html`
  were not touched (test_site-only instruction). The live collector still emits
  no `scheduler:` — but the route-level carry-forward now protects it, so a
  save in either mode is safe.
* `config_save_json` now does one extra small synchronous file read inside an
  async handler, joining the blocking `save_config()` write that was already
  there. `test_async_routes_do_not_block_event_loop` fails identically at base.
* `test_album_scan_budget.py::TestTheMissingTrackSnapshotIsNotStarvedByASkip::
  test_the_endpoint_stays_database_only` fails at base too — it reads only
  `routes/album_routes.py`, which this change does not touch.
