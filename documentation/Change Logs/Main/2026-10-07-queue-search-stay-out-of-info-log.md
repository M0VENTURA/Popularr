# Queue/search lines stay out of the info log (2026-10-07)

## Reported

> download logs are being added to the info log, these should only be in
> queue or search logs

(the pasted lines are `… INFO popularr.search [AUTOMATIC] …` search events)

## Investigation

**The current build routes these correctly — verified empirically, not just
by reading config.** A live probe (`setup_logging` → emit → inspect every
file) produced:

| emission | landed in |
|---|---|
| `log_search` (`popularr.search`) | **search.log only** |
| `log_queue` (`popularr.queue`) | **queue.log only** |
| `log_unified` (`popularr.unified`) | **unified_scan.log only** |
| an ordinary propagating module logger | info/debug/unified (control) |

info.log received **zero** `popularr.search` lines.

The paste's format also identifies its origin: current renderers emit
`2026-10-07 12:31:12 [INFO] [popularr.search] …` (bracketed level and name),
while the paste shows `2026-10-05 17:16:29 INFO popularr.search …` (bare).
The bare shape matches only the `_plain_renderer` that existed between
**2026-08-06 and 2026-08-23** (removed by `341bd172`); `propagate: False` for
`popularr.search` has existed since **2026-08-06** (`c182defc`). Conclusion:
the pasted lines come from a **stale build or an old log volume**, not from
any recent version of this code.

## Fix (defence in depth + a pinned guarantee)

Even though the config is right, nothing *enforced* it at emit time, so a
future third-party logging reconfiguration (or a half-updated container)
could silently flip propagation back on and duplicate every queue/search
line into info/debug. Now:

- `log_search`, `log_queue` and `log_unified` re-assert
  `propagate = False` immediately before emitting — one line each, so the
  separation holds no matter what ran before;
- `tests/test_log_routing.py` (6) pins the whole guarantee:
  - the dictConfig contract (each dedicated logger → its own handler,
    `propagate: False`, never info/debug; the root still carries info_file
    as a control);
  - a behavioural pass that emits all three lines plus a control into a
    temp `LOG_PATH` and asserts each file holds exactly its own lines —
    the search line is absent from info.log, unified and queue.log — with
    the control proving info.log writing works (no vacuous negatives) and
    the bracketed renderer pinned (documenting that the reported bare
    format is not produced by this build);
  - the emit-time guard: flipping `propagate` back on is corrected before
    the record is emitted (root spy sees nothing).

## Oracle

Stashed `helpers/logging_config.py` → **1 failed / 5 passed** — the guard
test fails (the flip is no longer corrected); the contract and behavioural
tests pass because the routing was already correct — they are regression
pins. Popped → markers present → stashes back to 3.

## Verification

- Affected set (21 logging-related files): **0 new** vs the baseline subset.
- Full suite → baseline reconciliation (`_logroute_full.txt`).

## If it reappears on a live install

The lines can only reach info.log from a process whose `popularr.search`
logger was never configured by this config. On the host:

```
grep -c 'popularr.search' /config/info.log      # current build → 0
grep -m1 'INFO popularr' /config/info.log        # bare format = old build's output
docker image inspect <container> --format '{{.Created}}'
```

Rotating/truncating `info.log` (or the `/config` volume's old files)
clears the historical lines.
