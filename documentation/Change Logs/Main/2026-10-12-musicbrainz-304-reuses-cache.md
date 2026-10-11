# MusicBrainz 304 Not Modified reuses the cached payload instead of failing

**Date:** 2026-10-12
**Area:** api / scan
**Commit:** `fix(api): a MusicBrainz 304 reuses the cached payload instead of failing`

## Reported

```
MusicBrainz request failed permanently endpoint='artist/' status_code=304
error="Redirect response '304 Not Modified'…"
```

## Root cause

The ETag conditional-GET layer (audit G7, shipped in `7c25cd83`) sends
`If-None-Match` so an unchanged resource revalidates with a ~300-byte
exchange instead of re-downloading the body. Its 304 handler **existed** —
but it sat *after* `response.raise_for_status()`:

```python
response.raise_for_status()          # httpx raises here for 304
...
if response.status_code == 304:      # ← unreachable
    return _cached_payload
```

httpx **0.28.1** raises `HTTPStatusError` for a 3xx status
(`Redirect response '304 Not Modified'`) *even with `follow_redirects=True`*.
So every revalidation raised before the handler was reached, fell into the
`except httpx.HTTPStatusError` branch, was **not** recognised (304 is
neither 5xx nor 400/404), logged at WARNING as "request failed
permanently", and returned `{}` — throwing away the very cached payload the
layer existed to reuse. The conditional-GET layer was effectively dead: it
cost a network round-trip *and* reported a failure, gaining nothing.

## Fix

Handle 304 **before** `raise_for_status()`:

- **304 with a cached entry** → return the cached payload (a successful
  revalidation, logged at DEBUG).
- **304 with no cached entry** (a stray/proxy 304; we never send
  `If-None-Match` without a cache) → return `{}` cleanly, without raising
  and without the misleading "permanent failure" log.

The genuine failure paths (5xx retry + circuit breaker, 400/404 debug
logging) are untouched and pinned by a control test.

## Tests

`tests/test_musicbrainz_304_reuses_cache.py` (8):

- The reported bug: first call stores the ETag, second (304) returns the
  **same** body — and the `If-None-Match` validator was actually sent.
- A 304 is never logged as a permanent failure (WARNING or otherwise).
- A 304 does not trip the circuit breaker.
- Defensive: a 304 with no cached body returns `{}` without raising.
- Controls: a normal 200 still works; different params don't share a cache
  entry; the genuine 5xx path still retries and opens the circuit.

**Oracle** at `ab1f9d83`: 3 fail (the core 304 tests), 5 pass (controls).
**Sweep**: 7 MB/recording/scan suites → 17 failed / identical set to
baseline; 6 API-client suites → 4 failed / identical set to baseline (both
sets pre-existing) → **0 regressions**.
