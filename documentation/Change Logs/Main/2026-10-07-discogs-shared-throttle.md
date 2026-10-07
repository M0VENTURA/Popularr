# Discogs throttled across processes at 1 req/s

**Date:** 2026-10-07
**Area:** api (Discogs rate limiting)
**Commit:** `fix(api): throttle Discogs across processes at 1 req/s`

## Report

Near-constant read timeouts against the Discogs API:

```
Network I/O attempt failed for https://api.discogs.com/database/search
… ReadTimeout('The read operation timed out')
```

## Root cause

`throttle_discogs()` enforced `_DISCOGS_MIN_INTERVAL = 1.0` through a
**process-local** lock. Popularr runs as several processes (web workers +
queue worker), so each process believed it had waited its second — the
combined rate against Discogs's 60/min quota was several requests per
second, which the API answers by throttling (read timeouts, 429 cooldowns).
MusicBrainz and ListenBrainz already use the disk-backed shared limiter
precisely for this reason (`_reserve_shared_slot` documents the same
multi-process bug).

## Fix

- `services/infrastructure/api_rate_limiter.py` — new
  `DISCOGS_MIN_INTERVAL = 1.0` and `throttle_discogs()` backed by
  `_reserve_shared_slot("discogs", …)` (exclusive lock on the shared state
  file, sleep outside the lock); `discogs_daily_count` resets with the
  other providers in both reset paths.
- `api_clients/discogs_http.py` — `throttle_discogs()` asks the shared
  limiter first and keeps the local lock + 429-cooldown logic as fallback,
  mirroring the MB/LB pattern.

ListenBrainz needed no change: every `_get`/`_post` path (including
`/1/metadata/recording/`) already throttles through the shared limiter at
1.0 s.

## Tests

`tests/test_api_rate_limit_enforcement.py` (+4): the reservation writes the
`discogs_*` keys; a second process cannot claim the same slot;
`discogs_http` asks the shared limiter first; the local lock still works as
fallback when the shared limiter is unavailable.

**Gate:** targeted ✓ · oracle (collection errors on revert — the new
constant disappears) · affected 0 NEW · full suite 0 NEW.
