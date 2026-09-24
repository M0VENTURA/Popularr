# Upcoming Releases showed July — the display window was 2 months, not 4 weeks

**Date:** 2026-09-24
**Area:** `services/upcoming_releases/wikipedia_scraper_service.py`,
`templates/pages/config.html`, `test_site/templates/Pages/config.html`,
`static/js/config.js`, `test_site/static/js/pages/config.js`

## Reported

> "Upcoming released on the dashboard is meant to be based around the current
> date showing the last 4 weeks and the upcoming two weeks. But it's still
> showing July"

## Root cause

`get_release_window()` defaulted to a **2-month lookback / 6-month lookahead**:

```python
lookback = max(0, int(get_feature("upcoming_releases_lookback_months", 2) or 2))
lookahead = max(0, int(get_feature("upcoming_releases_lookahead_months", 6) or 6))
...
return now - timedelta(days=30 * lookback), now + timedelta(days=30 * lookahead)
```

On the 2026-09-24 report date that opened the window at **2026-07-26** — the
dashboard was, literally, still showing July. The stated intent is 4 weeks back /
2 weeks forward, i.e. `[-28, +14]` days.

⚠️ **This function is the only thing filtering the dashboard table.** The table's
`loadUpcomingReleasesTable()` sends no `window` parameter, so the separate
"tight" clause in `upcoming_releases_routes.py` (gated on `window_days > 0`)
never applies. Changing it here changes what the dashboard shows.

Two further defects made the window **uncorrectable from the UI**:

- the tuning keys were `upcoming_releases_lookback_months` /
  `upcoming_releases_lookahead_months`, which appeared on **neither** Config page;
- they were read by **neither** `config.js` collector.

So a user could not fix the window no matter what they did in the app — the only
route was hand-editing `config.yaml`.

## The change

### 1. Defaults are days, and they are the intent — 28 / 14

New `_window_days(feature_key, legacy_months_key, default_days)` resolves one
side of the window:

1. `features.upcoming_releases_lookback_days` / `_lookahead_days` (the new keys);
2. falling back to the legacy `_months` keys (`30 * months`), so an existing
   `config.yaml` that sets them is still honoured;
3. else the default (**28** / **14**).

⚠️ The lookup is **not** truthiness-tested. `0` is a legitimate value
("show nothing future-dated") and `value or default` would silently replace it —
a trap this codebase has hit with `or` before. There is a test for it.

### 2. The window is now reachable from the Config page

Two inputs on **both** Config pages, next to the existing "Stale Purge (days)":

| Label | id | Default | Help text |
|---|---|---|---|
| Show Last (days) | `upcoming_releases_lookback_days` | 28 | *28 days = the last 4 weeks.* |
| Show Next (days) | `upcoming_releases_lookahead_days` | 14 | *14 days = the next 2 weeks.* |

Both are `min="0" max="365"`, so they fall under the existing
`validateNumericBounds()` check (it scans `#configForm input[type="number"]`,
and both trees use `<form id="configForm">`).

The collectors use the existing **`parseNumber(id, defaultValue)`** helper, *not*
`parseInt(...) || 28`: `parseInt('')` is `NaN` and the `||` idiom would turn a
legitimate `0` back into the default, reintroducing by the front door the exact
bug fixed on the backend.

⚠️ The keys are deliberately **absent** from `_DEFAULT_FEATURE_FLAGS`.
`get_features_config()` merges that registry over the file config, so a key
present there is never absent — `get_feature(key, None)` would always return the
registry value and the legacy-months fallback would become dead code. An existing
install with `upcoming_releases_lookback_months: 3` would silently snap back to
the default. This is pinned by a test so the symmetry is intentional, and it
matches the sibling `upcoming_releases_purge_days`, which is also absent.

### 3. The purge can no longer delete inside the window being displayed

`purge_stale_upcoming_releases()` deletes rows older than
`upcoming_releases_purge_days` (default 30). Purge window and display window are
**independent settings**, so `upcoming_releases_purge_days: 7` alongside a 90-day
lookback would delete rows the dashboard is still trying to show — the table
would empty from the left as the scheduler ran. The purge is now **floored at the
display lookback**, so it can only ever raise a value that falls inside the
window, never lower one.

⚠️ While writing this I hit the **stdlib-logging trap**: the floor's log call used
structlog kwargs style in a module that uses `logging.getLogger(__name__)`.
`logger.info("msg", key=val)` raises `TypeError`, and the surrounding
`except Exception: pass` swallowed it — silently disabling the floor. Caught and
written `%`-style. A source-level assertion that "the floor exists" would not have
found this, which is why the floor's tests are **behavioural** (fake session,
assert the `cutoff` parameter actually used).

## Falls out of the same bug: a dead test

`tests/test_upcoming_releases_sources.py` seeded its fixture at fixed
**+30 / +60 / +90 days**, calibrated to the old 6-month window. With the window
narrowed, three source-filter tests failed for a reason that had nothing to do
with source filtering. The offsets are now **derived from the live window**, so
the tests stay meaningful for any configured window size.

Repairing that surfaced a genuinely dead test:
`tests/test_interlude_lb_config_page.py::test_config_js_collects_interlude_keys`
had been failing on `NameError: name 'os' is not defined` — it was verifying a
real contract (the interlude keys must be collected into the save payload) and
verifying nothing. One-line fix: add `import os`. The contract it guards holds.

## Tests

- `tests/test_upcoming_release_window.py` (**36**) — frozen `datetime` at
  2026-09-24 covering: the window is four weeks back and exactly two weeks
  forward; `0` is honoured; legacy months keys still work and lose to the days
  keys; a broken / non-numeric / negative config cannot invert the window; the
  Config page exposes both inputs on both trees with matching defaults and clear
  help text; the registry-omission decision above; and the behavioural purge
  floor (short purge is raised and the raised value reaches the `DELETE`; a long
  purge is left alone; a zero-day purge still cannot delete inside the window).
- Red baseline: **29 failed / 3 passed** without the fix.
- Mutation: removing the purge floor block fails exactly
  `test_a_short_purge_is_raised_to_the_display_lookback`.
- Full-suite comparison: no new failures.

## Verified window boundaries

Today 2026-09-24 → old window start **2026-07-26** ("still showing July");
new window **2026-08-27 → 2026-10-08**.
