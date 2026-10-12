# The slskd server-state gate reads the real payload (2026-10-12)

## Report

Queue log, one line per queue item at every cycle:

```
WARNING services.downloads.slskd_service  Soulseek server not connected — search not attempted query='Olek Popka - New York City Boy'
INFO     popularr.queue                  [QUEUED] Olek Popka - New York City Boy → slskd disconnected, search skipped
```

The user: **"It's not disconnected"** — correct: slskd was up and connected.

## Root cause — the gate only recognised the modern boolean shape

The server-state gate added by `d08415d5` read ONLY explicit booleans from
the application state:

```python
server = state.get("server", {}) if isinstance(state, dict) else {}
return bool(server.get("isConnected") or server.get("isLoggedIn"))
```

But real slskd — including the version this deployment runs — reports the
Soulseek connection as a **C# enum-flags STRING** in `server.state`:
`"Connected, LoggedIn"`, `"Connected"`, `"Disconnected"`. That is exactly
what the ORIGINAL client parsed (`old_system/api_clients/slskd.py::is_connected`,
which supported both the boolean and the flag string, and probed
`GET /server` with a `GET /application` fallback). With the boolean keys
absent, the new check was **permanently False** → every search was skipped as
"disconnected" since the hardening shipped, while the connection was fine.

The only thing that kept it green in tests: the hardening suite stubbed
`svc.is_connected = lambda: True/False` directly and never exercised the
HTTP-level parse with a realistic payload.

## Changes (`api_clients/slskd_http.py`)

* **`get_server_state()`** — resolves the server sub-state across versions:
  `GET /application/state` → `{..., "server": {...}}` first (current shape),
  with the ORIGINAL client's `GET /server` (server object directly) and
  `GET /application` (server nested) fallbacks, and a top-level-shape
  recognition for versions that carry the fields at the application-state
  root.
* **`is_connected()`** — parses BOTH shapes, mirroring the original client's
  preference order: explicit `isConnected` boolean authoritative when
  present, then `isLoggedIn`, then the flag string
  (`"connected"`/`"loggedin"` in the comma-separated flag set). Empty /
  unreachable (`{}`) still means NOT connected — that keeps the gate's
  original purpose (a genuinely down slskd must not record `no_results` +
  24h backoff).

Service-level `SlskdService.is_connected` and the queue's
`slskd_disconnected` handling are unchanged (the failure was purely in how
the HTTP layer decided "connected").

## Tests — `tests/test_slskd_server_state_gate.py` (15)

* HTTP layer with a canned-JSON fake session: flag-string `"Connected,
  LoggedIn"` → connected (THE regression), single `"Connected"` → connected,
  modern `isConnected`/`isLoggedIn` booleans → connected (controls), 
  `"Disconnected"` → false, explicit `isConnected: false` authoritative,
  `application/state` empty → falls back to `GET /server`, `GET /server`
  404 → falls through to `GET /application`, all endpoints dead → false,
  top-level state shape → connected.
* Service layer: `search_and_filter` with the REAL client gate proceeds on
  the flag-string payload, proceeds on the boolean payload, returns `None`
  (search NOT started) for `"Disconnected"` and for an unreachable slskd.

**Oracle** (revert to the boolean-only parse): 7 failed / 8 passed — exactly
the flag-string + endpoint-fallback shapes; the boolean/disconnected/dead
controls stayed green.

**Sweep**: all `test_*slskd*` + `test_queue*` suites (26 files, 448 tests) —
7 failures, **identical set at pristine `b493fb20`** (backslash-sanitisation
×4 Windows path-sep, wrong-artist gate ×2, `test_schedule_search_retry_
keeps_backing_off_never_failed`) → 0 regressions. `import app` OK (399
routes).

## Note for the user

No config change needed and no rescan required — the fix is at the HTTP
layer. After rebuilding, searches will resume: the gate will read
`server.state = "Connected, LoggedIn"` as connected. If the queue still shows
`slskd disconnected` after a rebuild, the next thing to check is that `slskd`
in Config points at the right `web_url`/API key (a 401 there also reads as
"not connected" — and would show as failed state probes in the logs).