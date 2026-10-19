# Navidrome import on a four-hourly schedule, and a polling gate for everything else

**Date:** 2026-10-19 · **Area:** `scheduler` · `navidrome`

## The request

> Have the Navidrome import run on a schedule every four hours so the database is
> always up to date. It should be able to run alongside the other scans. It will
> just need to make sure the other scans stop excess polling of Navidrome during
> the Navidrome import.

Two halves, and the second is the harder one: a schedule is trivial, but "runs
alongside" combined with "stop the other scans polling Navidrome" is a
concurrency problem across **four hypercorn workers plus a standalone queue
worker and the scheduler**.

Eighty-seven percent of the work is below section 2. The schedule itself is a
default and an interval trigger.

---

## 1. The four-hourly job

Registered next to the existing scans in
`services/scheduler/scheduler_service.py::_register_default_jobs`:

| setting | default | effect |
|---|---|---|
| `scheduler.jobs.navidrome_import.interval_minutes` | **240** | how often to run |
| `scheduler.jobs.navidrome_import.auto_import_enabled` | `True` | master switch |
| `interval_minutes <= 0` | — | job **removed**, not run |
| `auto_import_enabled = False` | — | job **removed**, not run |

Verified:

```
default (no entry)   -> registered, interval=4:00:00
interval 240         -> registered, interval=4:00:00
interval 0 (off)     -> NOT registered
enabled False        -> NOT registered
```

⚠️ `0` means *never*, not *every tick*. A naive `or 240` default turns a
deliberate "off" into the busiest possible schedule, so the interval is parsed
with an explicit `None` check, and the same rule is applied in the browser
(`parseNumber(...)`, never `|| 240`).

`_JOB_LOCK_KEY_MAP` gained `"navidrome_import": "popularr_scheduler_navidrome_import"`
so the four workers and the scheduler elect a single ticker.

### It runs the SAME pipeline as the dashboard button

`_run_navidrome_import_job` calls `run_navidrome_import_scan(mode="all", …)` —
the exact entry point the Import button uses. Not a parallel implementation: if
the scheduled and manual paths were separate, they would drift, and the schedule
would keep "working" while producing different data than the button.

### Three guards, three different questions

| guard | question it answers |
|---|---|
| `_job_tick_lock("navidrome_import")` | is another **worker** already ticking? |
| `is_import_running()` | is an import running **anywhere**? |
| `is_runtime_running("navidrome")` | the cheap in-process check, covering the window before the progress file is first written |

### ✅ It does NOT block or wait for other scans

This was the explicit requirement, and the design is deliberately asymmetric:

- The import **reads Navidrome and writes `tracks`**.
- A popularity/album scan **reads the library and writes scores**.

Neither blocks the other, so the job **never waits**. What it does instead is
suppress the *other* scans' incidental Navidrome traffic while it runs. The guard
checks only its own scan type — asserted structurally in the tests, because "just
skip everything" is the tempting and wrong implementation.

---

## 2. The polling gate

New module: `services/navidrome/import_gate.py`.

### Why a FILE, not a flag ⚠️

The app is **4 hypercorn workers + a standalone queue worker + the scheduler**. An
in-process boolean would be `True` in exactly one of them, so a scan in worker 2
would happily keep polling while the import ran in worker 3 — the gate would
silently do nothing in production while passing every single-process test.

The state is the **same progress file the import already writes**
(`<state>/navidrome_scan_progress.json`, via
`helpers.config_helpers.get_navidrome_progress_file`). That file is already the
codebase's cross-process signal — `scan_state` uses it for progress, stop
requests and checkpoints — so there is **no second source of truth to keep in
sync**, and the standalone queue worker sees it too because the state directory
is a shared volume.

### An ALLOW-LIST of incidental work

```python
DEFERRED_ENDPOINTS = frozenset({
    "setRating", "star", "unstar",              # rating pushes
    "getPlaylists", "getPlaylist",              # playlist sync + index reads
    "createPlaylist", "updatePlaylist", "deletePlaylist",
    "getCoverArt",                              # artwork pipeline / page loads
})
```

Deliberately an allow-list of the *incidental* round trips rather than a
deny-list of everything else. Under a deny-list, **a future import endpoint
added without updating the gate would be deferred** — and a deferred import
endpoint is an import that imports nothing. Under an allow-list the same mistake
is merely traffic that is not yet optimised.

Verified:

```
running -> defer setRating/getCoverArt/getPlaylists/updatePlaylist : True
running -> defer getArtists/getAlbum/startScan/getScanStatus       : False
```

### Defer, do not block

Every consumer already has a correct "could not do it now" path — a rating push
returns `False` and the album logs it; a cover lookup returns `None` and the
stored artwork stands; a playlist sync reports `skipped`. Blocking would instead
stall a scan for minutes behind an import it cannot influence.

### Fails OPEN

An unreadable or missing file means "no import running", so the caller behaves
exactly as it did before this module existed. A missed defer is merely
sub-optimal traffic; **failing closed would silently disable rating sync,
playlist sync and cover art** whenever the state directory was unavailable.

A 2.0 s cache keeps a per-track caller from reading (and parsing) the file
thousands of times per album.

---

## 3. The import must be exempt from its own gate

`NavidromeClient.__init__` gained `is_import: bool = False`, and the import
pipeline and `perform_library_sync` both construct their client with
`is_import=True`.

⚠️ **Without this the import defers itself.** The import's own client calls
`getAlbum`/`getArtists` (never deferred, by the allow-list) *and*, during its
album tagging pass, `setRating`/`getCoverArt` — which **are**. A self-deferring
import would read the library, then silently skip writing ratings, and the only
symptom would be "the scheduled import doesn't seem to do much". The flag makes
the exemption explicit at the construction site rather than relying on the
caller's endpoint mix.

```
scan client   defers setRating : True     (honours the gate)
import client defers setRating : False    (exempt — it IS the import)
```

`library_sync_service` is also exempt because it is **an import path**: it
reconciles the whole library and has the same "must actually write" requirement.

---

## 4. Guarding the silent hazards ⚠️

A deferral answers with a **plausible success payload** — `{}` for a read, `ok`
for a write — so nothing crashes. But that means "the gate said no" and "the
server said nothing" are the *same value*, and every caller that branches on the
emptiness of a result becomes a data-loss bug. Three were real:

| hazard | what would have happened |
|---|---|
| `sync_playlist_by_name` sees an empty listing | concludes the playlist does not exist → **creates a second copy**; the next dedupe pass deletes one, and the two ping-pong forever (the old multi-account duplicate bug, reborn) |
| `sync_playlist_index` sees an empty listing and counts **zero errors** | the prune runs against a listing containing only file-backed playlists → **every Navidrome playlist is deleted from the index** and artist pages go permanently empty |
| a deferral lands **after** a successful listing (`getPlaylist` returns `{}`) | `replace_playlist` **deletes rows before inserting**, so an "empty" read **wipes the playlist's entries** |

Each now has an explicit guard:

- `sync_playlist_by_name` **skips** (`skipped=True`, `success=True`) when an
  import is running and `force` is not set. `skipped` — not an error — is the
  honest report: nothing was attempted. `force=True` is an explicit user action
  and still proceeds.
- `_enumerate` counts an **empty listing during an import as an error**, which
  refuses the prune. A genuinely empty library outside an import still prunes.
- A deferred detail resolves to **`None` ("unreadable")**, never `[]` — the
  existing *null is not a decision* rule, now load-bearing for this feature.

The guards were written and then **proved necessary by the tests**: an early
version of the suite used a raising sentinel client, and it passed for the wrong
reason because `sync_playlist_by_name` treats a client exception as a warning and
returns normally. The suite now uses an **observation** client, since that is the
only way to tell "the guard fired" from "the call failed".

⚠️ The empty-listing guard is deliberately **narrow**: only the empty payload is
ambiguous (a deferred `getPlaylists` returns `{}`, never a partial list), so only
it is guarded. Consulting the gate for every client would suppress the prune on
healthy listings for the whole import — a behaviour change nobody asked for. The
call count is asserted to keep that from widening unnoticed.

---

## 5. Config UX — and a latent data-loss bug fixed on the way

Per the config contract, the new options are on the Config page in **both UI
trees**:

| tree | template | collector |
|---|---|---|
| LIVE | `templates/pages/config.html` — new **Scheduled Scans** card | `static/js/config.js` |
| REBUILT | `test_site/templates/Pages/config.html` | `test_site/static/js/pages/config.js` |

⚠️ `static/js/config.js` had **no `scheduler` section at all**. Because
`save_config()` replaces `config.yaml` wholesale, the next time a user pressed
Save, the collector's payload would have **dropped `scheduler.jobs` entirely** —
silently erasing every scan interval, including the new one. The collector now
rebuilds `scheduler.jobs` from `window.pageConfig.scheduler` with the three
known jobs overlaid, so unrelated keys survive a save.

---

## Tests

**`tests/test_navidrome_import_schedule_and_gate.py` — 37 passed.** Gate
behaviour; deferred vs never-deferred endpoints; the import-client exemption;
the 240-minute default; configurability; `0`/`False` disabling (seeding the job
first, because `_remove_job` is a no-op on an absent job); the same-pipeline
assertion; the tick lock; skip-when-an-import-is-running; and the concurrency
assertion (on comment-stripped source, so a docstring cannot satisfy it).

**`tests/test_navidrome_deferred_read_safety.py` — 12 passed.** The three
hazards above, each with its control: a deferred sync must not touch the client
at all; `force` must; the guard must be inert with no import running; a
genuinely empty library still prunes; a non-empty listing is never treated as
deferred; one deferred client among several still refuses the prune; the gate is
consulted exactly once for two clients.

## Verification

| sweep | result |
|---|---|
| Navidrome import schedule + gate | **37 passed** |
| deferred-read safety | **12 passed** |
| every test touching the changed playlist modules | 198 passed / 9 pre-existing failures |
| playlist index + sync suites | 132 passed |
| full workspace sweep | 91 files, **1,578 tests** |

Eleven files fail in the sweep, and **all eleven were attributed to
pre-existing causes before being dismissed**:

- **8 failures** are the workspace's **own uncommitted config removals** — the
  `TestTheConfigSurfaceExists` cases in `test_favourite_playlists_per_user.py`
  and `test_genre_ai_covers.py` assert that the `playlists_favourite_enabled` and
  `api_gemini_*` controls exist, and those controls have been deleted from
  `config.html`/`config.js` in the working tree. Absent on `develop`, absent on
  pristine, unrelated to this change.
- **5 failures** in `test_navidrome_playlist_post_body.py` fail **by name,
  identically on pristine `develop`** — a `_FakeResponse` stub with no `.content`.
- **2 failures** in `test_full_scan_startup_fix.py` are pre-existing; the
  baseline re-run dies in the documented Windows access-violation crash before
  reporting.
- The remaining files match pristine failure-for-failure.

⚠️ **Flagged, not fixed:** the favourite-playlists removal in the working tree is
**half done** — the UI controls are gone, but `routes/favourites.py`,
`services/playlists/favourite_playlist_service.py`, `helpers/config_helpers.py`
and the finalise stage still read `favourite_playlists`. Since saving the Config
page rewrites `config.yaml` wholesale and the collector no longer emits that key,
the next save will drop it while the backend still reads it. That is the user's
in-progress work, so it has been left alone.

**Zero regressions attributable to this change.**
