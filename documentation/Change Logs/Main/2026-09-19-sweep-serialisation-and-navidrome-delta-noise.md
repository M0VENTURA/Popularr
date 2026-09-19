# Missing-releases sweep serialisation, Navidrome delta noise, and three dead test modules

**Date:** 2026-09-19
**Area:** `services/metadata` / `services/scanning` / `tests`
**Status:** implemented, guarded by `tests/test_scan_continuation_guard.py` and `tests/test_navidrome_delta_scan.py`

---

## What was asked

Three production log lines were reported:

```
WARNING services.metadata.artist_scan_service  Cannot determine whether a popularity
  scan is active
  reason='no known accessor found; the missing-releases sweep cannot serialise
  itself against the popularity scan'
  probed=['services.scanning.scan_state.is_popularity_scan_running',
          'services.scanning.scan_state.is_scan_running',
          'services.popularity.scan_state.is_scan_running',
          'services.scheduler.scheduler_service.is_popularity_scan_active']

INFO  services.metadata.artist_scan_service  Starting missing releases scan
  total_artists=1131

WARNING api_clients.navidrome  Navidrome API returned a failed status
  endpoint='getAlbumList2' code=0 message="type 'recentlyAdded' not implemented"
```

Two independent defects, plus a third found while validating them.

---

## Defect 1 — the sweep could not see a running popularity scan

`services/metadata/artist_scan_service.py::_popularity_scan_active()` probed four
`module.attribute` candidates for "is a popularity scan running?":

| Probed | Exists? |
|--------|---------|
| `services.scanning.scan_state.is_popularity_scan_running` | **No** |
| `services.scanning.scan_state.is_scan_running` | **No** |
| `services.popularity.scan_state.is_scan_running` | **No** |
| `services.scheduler.scheduler_service.is_popularity_scan_active` | **No** — `scheduler_service` imports that name **lazily inside a function body**, so it is never a module attribute |

Not one of them has ever existed. The real accessor is:

```
services/scanning/pipelines/popularity_pipeline.py::is_popularity_scan_active()
```

and it is already used by `routes/scan_routes/api.py`,
`routes/scan_routes/artist_album.py`, `routes/scan_routes/popularity.py` and
`services/scheduler/scheduler_service.py`.

### Impact

The probe never resolved, so the sweep **always** concluded "no popularity scan
is running" — including while one genuinely was — and ran concurrently with it.
The two walks share one 1 req/s MusicBrainz budget; the surrounding code comments
record that running together previously drove MusicBrainz to 503 within seconds.
The `_wait_while(_popularity_scan_active, …)` guard at the top of the artist loop
was therefore dead code.

### Fix

The probe list is now a module constant, `_POPULARITY_SCAN_PROBES`, whose first
and only entry is the canonical accessor, recorded separately as
`_CANONICAL_POPULARITY_ACCESSOR`. All four permanently-dead candidates were
removed — a dead candidate is worse than no candidate, because it makes the probe
look like it is doing something. The import/`getattr` probing is retained, so if
the accessor ever moves the code still degrades to a single warning instead of
raising. The warning now also reports `canonical=` so the expected path is
visible in the log.

---

## Defect 2 — `recentlyAdded` is not a Navidrome `getAlbumList2` type

`services/scanning/navidrome_service.py` requested both `newest` **and**
`recentlyAdded`:

```python
_DELTA_LIST_TYPES = ("newest", "recentlyAdded")
```

Navidrome does not implement `recentlyAdded` for `getAlbumList2`; it answers with
a Subsonic envelope carrying `status="failed"`, which `api_clients/navidrome.py`
logs as a WARNING (only code 70, "not found", is treated as routine). So every
delta scan logged a warning for a list type that could never return data.

Two further points made the request pointless:

- `_album_sort_ts` read `created` / `updated` / `recentlyAdded`. Navidrome album
  objects carry `created` and `updated`; `recentlyAdded` is never a field, so the
  final fallback was unreachable.
- `newest` is already ordered by `created` descending, and the per-album
  `created`/`updated` filter against `since_epoch` does the actual recency work.

### Fix

`_DELTA_LIST_TYPES = ("newest",)`, the unreachable `recentlyAdded` fallback in
`_album_sort_ts` was dropped, and the docstrings were corrected to describe what
the code does. No behaviour is lost: the artist-level delta is carried by
`getIndexes` + the per-artist `songCount` diff, which is the mechanism the
existing delta-scan tests already pin.

---

## Defect 3 — three test modules could not be collected at all

Discovered while validating the guards for defects 1 and 2: my new
`tests/test_navidrome_delta_scan.py` guards would never have run, because that
module **fails to import on `origin/develop`**.

`services/scanning/navidrome_import.py` renamed

```
artist_album_name_diff(artist_name, artist_id, *, client=None)
```

to

```
compute_artist_album_diff(artist_name, nav_albums)
```

— a new name *and* a new signature (the client is now the caller's problem; the
function takes a pre-fetched album list). Three test modules still imported and
called the old name:

| Module | Failure |
|--------|---------|
| `tests/test_navidrome_delta_scan.py` | `ImportError` → whole module uncollectable |
| `tests/test_navidrome_import_removals.py` | `ImportError` → whole module uncollectable |
| `tests/test_album_release_title_naming.py` | `ImportError` → whole module uncollectable |

These were the three "pre-existing collection errors" recorded in earlier
sessions. They were not environmental: they are a rename that missed its tests,
and they were hiding **four** latent failures (see below).

### Fix

All three modules now import and call `compute_artist_album_diff`, passing the
already-fetched album list.

Repairing the imports immediately exposed four tests that had **never executed**:

1. `test_navidrome_import_removals.py` — the three `scan_artist_to_db` diff-mode
   tests seed fixed ids (`g1`, `k1`, …). The test engine is a single
   `StaticPool` in-memory SQLite **shared by the whole suite**, so rows from one
   test outlive it and the seed collides on `tracks.id`. More importantly these
   tests assert on the *complete* id set (`_db_track_ids()`), which is only
   meaningful with isolation. Added an autouse `_isolated_tracks` fixture that
   empties `tracks` around each test.

2. `test_album_release_title_naming.py::test_genuinely_removed_album_still_removed`
   — monkeypatched `db.engine.db_session`, but `navidrome_import` did
   `from db.engine import db_session` at import time, binding the name into its
   own namespace. The patch therefore never took effect and the test's fake
   session was ignored. Now patches the name **as bound in `navidrome_import`**.

---

## Verification

Worktree at `origin/develop` (`589cc262`), same edits applied to both the
workspace and the worktree:

| Run | Result |
|-----|--------|
| Baseline, the four affected modules | **3 collection errors, 13 tests collected** |
| After the fixes | **48 passed** |
| Baseline, `tests/test_navidrome_delta_scan.py` | `ImportError: cannot import name 'artist_album_name_diff'` |

Guards added:

- `tests/test_scan_continuation_guard.py::TestMissingReleasesSweepProbe` — 8 tests:
  the canonical accessor is probed first; **every** probed attribute resolves to a
  callable (so a dead candidate cannot be reintroduced); the probe is `False` when
  idle and `True` for a running `popularity_scan` row, a running `full_scan` row,
  and the in-process runtime registry; no warning when the accessor resolves; and
  exactly **one** warning when no accessor resolves.
- `tests/test_navidrome_delta_scan.py` — 3 tests: `recentlyAdded` is absent from
  `_DELTA_LIST_TYPES` while `newest` is present; `_album_sort_ts` ignores the
  never-present `recentlyAdded` field; and `fetch_changed_albums` requests only
  `["newest"]` from a recording client.

## Files changed

- `services/metadata/artist_scan_service.py`
- `services/scanning/navidrome_service.py`
- `tests/test_scan_continuation_guard.py`
- `tests/test_navidrome_delta_scan.py`
- `tests/test_navidrome_import_removals.py`
- `tests/test_album_release_title_naming.py`
