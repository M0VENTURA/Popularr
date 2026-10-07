# MusicBrainz 301 redirects are followed, not permanent failures

**Date:** 2026-10-07
**Area:** api (MusicBrainz HTTP)
**Commit:** `fix(api): follow MusicBrainz 301 redirects instead of failing them`

## Report

```
MusicBrainz request failed permanently endpoint='recording/ec14fb08…'
status_code=301 error="Redirect response '301 Moved Permanently'…"
```

## Root cause

MusicBrainz merges duplicate recordings and 301-redirects the old MBID to
its new home. `httpx` does **not** follow redirects unless asked
(`follow_redirects=False` is the default), and `MusicBrainzHttpClient.get()`
treats any status outside (400/404 → debug, 5xx → retry/circuit) as a
permanent failure — so every lookup of a merged recording returned `{}`
even though the data was one redirect away.

## Fix

- `api_clients/http_utils.py` — `create_retry_client()` now sets
  `follow_redirects=True` by default, so the shared `session` and
  `timeout_safe_session` (used by most clients) follow redirects. The retry
  transport only forcelists 429/5xx, so a 301 is passed to httpx's redirect
  handling exactly once.
- `api_clients/musicbrainz_http.py` — after a successful request whose final
  path differs from the requested one, log
  `MusicBrainz endpoint redirected (merged MBID)` with the resolved path, so
  an obsolete stored MBID is visible instead of silent. A client constructed
  *without* following still degrades to `{}` (logged), never raises.

## Tests

`tests/test_musicbrainz_follow_redirects.py` (3): shared sessions follow by
default; an end-to-end 301 → 200 via `MockTransport` resolves the merged
recording **and** emits the redirect log line (spying on the module logger —
`structlog.testing.capture_logs` is defeated by suite-wide structlog
config); an opt-out client degrades to `{}` without crashing.

**Gate:** targeted 3/3 · oracle (both behavioural tests fail on revert) ·
affected set 0 NEW · full suite 224 failed / 4929 passed → 0 NEW after the
capture fix (only the known flaky `test_sibling_torrents_root_is_searched`).
