# The dashboard's abandoned-artists banner is dismissible, and the queue processor is now watched

**Date:** 2026-10-19 · **Area:** `ui` / `queue`

## Reported

> This is showing on the dashboard from an earlier scan, but there is no way to
> dismiss the message.
>
> `Artists skipped / abandoned — investigate`
> `Visions Of Atlantis  abandoned  exceeded 2700.0s budget`

and, in the same report:

> Also, when the queue processor fails, it shows it with the option to restart
> in both the scanner queue and the config page, but if it's failed, it should
> also show on the dashboard and a restart on failure should happen
> automatically.

Three separate defects, all on the same surface.

## 1. The banner had no way out

`abandonedBanner()` renders from `scan.abandoned_artists` on the `/api/scan-progress`
row, and `updateActiveScans()` rebuilds the whole panel **every 5 seconds** — so
removing the element in devtools brought it straight back. There was no
acknowledgement at all in the code: no storage, no key, no button.

The "from an earlier scan" half is the same story from the other end. The panel
only lists rows where `is_running` is true, and a scan that dies mid-run leaves
that flag set — `reset_stale_scan_states()` deliberately only runs **at boot**,
because it has to be certain no scan is live. A crashed scan therefore keeps
claiming to be running (and keeps showing its notice) until the next restart.

**Fix.** The banner is dismissible, and the acknowledgement is keyed on a
**fingerprint of the content** — `scan_type` plus every artist name and reason,
sorted:

```
full_scan::Visions Of Atlantis=>exceeded 2700.0s budget|abandoned
```

Dismissing means *"I have seen **this**"*, not *"never tell me about this scan
again"*, so a re-run that abandons a different artist (or the same artist for a
different reason) has a different id and is shown. A boolean flag or a key of
just the scan type would silently swallow the next abandonment — which is the
one that actually needs attention. That is asserted directly
(`test_a_different_abandonment_is_not_swallowed`).

Stored in `localStorage` rather than `sessionStorage`: the report was "it's
still there when I come back", and an artist abandoned an hour ago is just as
true in a new tab. The element is also removed on the spot, because five seconds
of a dismissed banner still sitting there reads as a button that did not work.

⚠️ The id goes into `data-alert-id`, escaped by a new `escapeAttr()`. The id is
built from **artist names**, so `Al "Test" Band` would otherwise close the
attribute and inject markup — and the live tree's `escapeHtml()` does *not*
escape quotes (it round-trips through `createTextNode`/`innerHTML`, which escapes
only `&`, `<`, `>`). The probe asserts on the escaped value, not merely on the
attribute existing.

## 2. The dashboard knew nothing about the queue processor

`pages/downloads/queue.html` and `monitor.html` include
`components/search/_queue_status.html`, and the Config page draws its own
scheduler card — both with a **Restart Processor** button. The dashboard, which
is the page an operator leaves open, had no view of the queue at all. A
processor that had stopped could sit there accepting items and processing none
of them, with the only evidence on a page nobody was looking at.

**Fix.** The dashboard polls `/api/queue-processor/status` (the same endpoint,
the same verdict, the same action as the card) and draws an alert **only** when
it says `stopped`:

- nothing at all while the processor is healthy — a permanent green "running"
  banner on every load is noise, not information;
- an alert with **Restart Processor** when it is stopped, which POSTs
  `/api/queue-processor/restart`;
- a line saying what auto-restart is doing, including when it is switched off,
  because otherwise the alert implies help is coming when it is not.

An unreachable endpoint draws nothing: the page is visibly broken already, and
the status endpoint is only one of several it needs.

The container (`#queueProcessorAlert`) is in the markup on both trees even
though it starts empty — the renderer returns early when the host is missing, so
a container-less page would make a stopped processor invisible again with no
error anywhere.

## 3. Nothing restarted it

**New `services/queue/queue_watchdog_service.py`** plus a scheduled job.

There is no processor process for the WebUI to own. The queue is driven by the
standalone `queue_worker` (started by `entrypoint.sh`) **or** the APScheduler
`download_queue_processor` job, and neither PID is visible to the other — so
`kill` is not available. What *is* available is what the Restart button already
does: make the next cycle happen now, by re-arming the in-process job or running
one cycle itself. That re-stamps the heartbeat, which is what "restarted"
honestly means here. A supervisor daemon would be a lie about a process this app
does not control.

The watchdog reads the **same** verdict the card and the dashboard draw
(`status == "stopped"`), so the thing the operator is told and the thing the
watchdog acts on cannot disagree. `running` and `restarting` are left alone — a
cycle in flight is not a failure, and re-kicking it would double up work.

Three guards keep it from becoming a hazard:

| Guard | Why |
|---|---|
| **Cooldown** (default 300s, floored at 30) | a genuinely broken environment — database down, so every cycle raises — would otherwise restart on every tick |
| **`enabled: false`** | the operator's off switch, and it removes the job rather than leaving it running |
| **gated on the queue being enabled** | a deployment that switched the download queue off must not have cycles kicked into it every minute |

It is registered from `_register_default_jobs` (so it inherits the leader gate
and the config re-apply) and runs through the **same `_job_tick_lock`** as every
other tick, so two web workers can never both decide the queue is down and kick
it. The check cadence is the queue worker's own `interval_seconds` — a third
setting whose only correct value is "the same as the other one" is not a
setting.

The last restart (time, reason, count) is recorded in
`<state>/queue_processor_watchdog.json` and reported through
`queue_processor_status()["auto_restart"]`, which is what lets the dashboard say
whether help is coming.

## Config

`queue.auto_restart` — `enabled` (default `true`) and `cooldown_seconds`
(default `300`). Both appear on the Config page (**Queue Processor Auto-Restart**,
mirrored in both trees) and are collected by both `config.js` copies; the
cooldown is floored client-side as well as server-side, so a typed `0` cannot be
saved as "restart in a tight loop".

## Files

| File | Change |
|---|---|
| `services/queue/queue_watchdog_service.py` | **new** — the decision, the cooldown, the state file |
| `services/queue/queue_diagnostics_service.py` | `auto_restart` added to the status payload |
| `services/scheduler/scheduler_service.py` | `queue_processor_watchdog` job + tick lock |
| `static/js/dashboard.js`, `test_site/.../pages/dashboard.js` | dismissible banner, queue alert, delegated click handler |
| `templates/pages/dashboard.html`, `test_site/…/Pages/dashboard.html` | `#queueProcessorAlert` container |
| `templates/pages/config.html`, `test_site/…/Pages/config.html` | auto-restart card |
| `static/js/config.js`, `test_site/…/pages/config.js` | persist `queue.auto_restart` |
| `tests/js/dashboard-alerts-probe.js` | **new** — drives the shipped renderers |
| `tests/test_dashboard_alerts.py` | **new** — 21 tests |
| `tests/test_queue_processor_auto_restart.py` | **new** — 22 tests |

## ⚠️ Two things worth remembering

**`escapeAttr` is written with `split`/`join`, not a character-class regex — on
purpose.** The escaping set contains both quote characters, so the regex literal
would carry them itself, and `tests/js/_extract.js` explicitly does not
understand regex literals: it reads those quotes as string delimiters and throws
*unbalanced braces*. A function that cannot be brace-matched out of the shipped
source cannot be driven by a probe. (The existing walker's limitation is
documented; this is the first time it changed a product line rather than a
test.)

**One probe, both trees.** `tests/js/dashboard-alerts-probe.js` is run against
the live and rebuilt dashboards and the two JSON payloads are **diffed**, so a
fix applied to only one of them fails. The dismissal storage key is pinned to a
single literal for the same reason — two literals would mean one page dismissing
an alert that the other keeps showing.

## Tests

`tests/test_dashboard_alerts.py` (21) — behavioural, via the probe: the banner
appears; it offers a dismiss control; dismissing hides it and it *stays* hidden
across the 5-second re-render; a different abandonment is still shown; the
element is removed immediately; a quoted artist name cannot break out of the
attribute; the queue alert is silent while running and restarting, and present
with a restart control when stopped; the two trees agree on every key.

`tests/test_queue_processor_auto_restart.py` (22) — the config defaults,
clamping and fail-open behaviour; only `stopped` triggers anything; the cooldown
refuses a second restart and then expires; the count only advances on a real
restart; disabled means disabled; the tick swallows its own failures; a corrupt
state file does not stop it; the payload reports the state; and the scheduler
registration (registered, *removed* when switched off, not registered when the
queue itself is off, and guarded by the shared tick lock).

## Verification

Run against a clone of `develop` (`3ed8b16`) with this workspace's changed files
overlaid, and — for the comparison — a pristine clone of the same commit:

| | result |
|---|---|
| `tests/test_dashboard_alerts.py` | 21 passed |
| `tests/test_queue_processor_auto_restart.py` | 22 passed |
| Targeted sweep — the 102 files that reference any changed surface | 2 448 tests, **0 failed, 0 errored** |
| Whole remaining suite, one process per file | failure set **identical** to the pristine baseline |

**Zero regressions.** 174 tests fail on this tree and the *same* 174 fail on
pristine `develop` — the genre-aggregation group, the migration-idempotency
group, the auth-gate group and friends, none of them touching anything here.

⚠️ **Two traps, both of which produced a reassuring and completely empty result
first.** A sweep is only worth anything if it can fail:

- pytest writes `<testsuites>` with **no attributes** and puts
  `tests`/`failures`/`errors` on the inner `<testsuite>`. Reading the root yields
  empty strings, so the report says "0 failures" for every file — until you
  notice it also says **0 tests**.
- `Get-Content` without `-Raw` returns a string **array**, and casting that to
  `[xml]` gives a document with no elements. The same silent zero.

The instrument was therefore validated against two suites with KNOWN pre-existing
failures (`test_download_match_lifecycle` → 10, `test_scan_stall_fixes` → 7),
which it reports correctly. Running the whole suite in ONE process is not an
option here: it dies with `Windows fatal exception: access violation` in the scan
suites, which is why the sweep is chunked and the crashed chunk re-run per file.
