# Artist-page scan froze the app: blocking work on the asyncio event loop

**Date:** 2026-09-19
**Area:** `routes/ui_routes.py` / `tests`
**Status:** fixed, ratcheted by `tests/test_async_routes_do_not_block_event_loop.py`

---

## Symptom

Starting a scan from the artist page made the whole app unresponsive — not just
that page, every page served by the same worker.

## Root cause

`routes/ui_routes.py::artist_detail` was an `async` handler whose **entire body
ran inline on the event loop**, and that body performs blocking work:

- **5 `db_session` reads** (synchronous SQLAlchemy/psycopg2 I/O), and
- `get_artist_members_cached()` and `get_artist_genre_sources()`, which reach
  MusicBrainz/Last.fm through the shared client.

The handler awaited exactly once — the final `render_template`. Everything
before it blocked.

### Why a scan triggers it

MusicBrainz is globally rate-limited to ~1 req/s by
`api_clients/musicbrainz_http.py::_strict_throttle`, which enforces the budget by
**sleeping to reserve a future slot**:

```python
allowed_time = max(now, last_request + MUSICBRAINZ_MIN_INTERVAL)
wait_time = allowed_time - now
self.state["musicbrainz_last_request"] = allowed_time   # slot claimed
...
if wait_time > 0:
    time.sleep(wait_time)                                # and now it waits
```

`get_artist_members_cached` calls `search_artists` → `get_artist_members` through
that throttle. A running popularity scan saturates the 1 req/s budget, so the
artist page's lookup queues behind it and blocks for tens of seconds. The
production log shows exactly this: many MusicBrainz calls taking **30–40s**, with
repeated `[MB] call still running Section='release.fetch_metadata' Elapsed_s=31.3`
warnings, alongside `popularity scan — 17%`.

`entrypoint.sh` runs hypercorn with `--worker-class asyncio` and one event loop
per worker, so a blocked loop stalls **every** request in that worker — the app
looks frozen.

## Fix

Split the handler. The context build became a plain synchronous function,
`_build_artist_detail_payload`, called from a worker thread:

```python
@ui_bp.route("/artist/<path:name>")
async def artist_detail(name: str) -> Any:
    payload = await asyncio.to_thread(_build_artist_detail_payload, name)
    return await render_template("pages/artist_detail_v2.html", **payload)
```

Only the render stays on the loop, because `render_template` needs the
app/request context that a worker thread does not have.

Safety check performed before offloading: the handler body uses **no Quart
context-locals** (`request`/`session`/`g`/`flash`/`url_for`/`jsonify`). The only
`session` is the local bound by `with db_session() as session:`. The single
`await` is the render, and there are no nested `async def`s — so the body is
safe to run off-loop.

## The guard is a RATCHET, not a clean assertion

Auditing the repo for the same shape found the identical anti-pattern in **53
other async handlers** (`dashboard`, `album_detail`, `track_detail`,
`api_popularity_run_compat`, the `track_routes` and `misc_routes` API surface, …):

```
TREE: origin/develop
offenders: 54      <- including ui_routes.py::artist_detail
after the fix: 53  <- artist_detail removed
```

Those are pre-existing and mostly harmless in isolation — a small indexed query
on an idle server returns in a millisecond and nobody notices. They only become
dangerous when a blocking call can stall for a long time: rate-limited network
I/O, pool exhaustion, or a big table scan.

Refactoring all 53 is an invasive change with real regression risk and is **not**
what this fix was asked to do. So `tests/test_async_routes_do_not_block_event_loop.py`:

- pins the known offender set in `_KNOWN_OFFENDERS`,
- fails when a **new** async handler blocks the loop,
- fails when an allow-list entry no longer blocks (so the ratchet only tightens),

which stops the class from growing while leaving the backlog to be burned down
deliberately. `ui_routes.py::artist_detail` is deliberately **absent** from the
allow-list.

## Verification

Worktree at `origin/develop` (`f324cb53`), same edits applied to both trees:

| Tree | Result |
|------|--------|
| Baseline (unfixed) | **2 failed** — naming `ui_routes.py::artist_detail (line 616) -> db_session, get_artist_genre_sources, get_artist_members_cached` |
| Fixed | **4 passed** |

Related suites in the fixed tree: `test_scan_continuation_guard.py` +
`test_async_routes_do_not_block_event_loop.py` +
`test_scan_async_signature_contract.py` → **33 passed**.

Independent deterministic audit (`ast`-based, not pytest parsing):
**54 offenders → 53**, with `artist_detail` gone.

## Files changed

- `routes/ui_routes.py` — `import asyncio`; new sync `_build_artist_detail_payload`;
  `artist_detail` now offloads via `asyncio.to_thread` and renders from the dict
- `tests/test_async_routes_do_not_block_event_loop.py` — new ratchet guard
