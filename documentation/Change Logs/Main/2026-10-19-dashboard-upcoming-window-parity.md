# The dashboard hid upcoming releases the dedicated page showed

**Date:** 2026-10-19 · **Area:** `ui` · `api`

## Reported

> "The upcoming releases on the dashboard is only showing singles from
> MusicBrainz. But looking at the MusicBrainz Daily Collection under
> /downloads/discover/upcoming, there are a lot more listed that don't seem to be
> getting displayed in the dashboard."
>
> "…the new releases from Musicbrainz that aren't one that's matched to a
> wikipedia import release is only showing up until the current date, nothing in
> the future."

## Root cause — the dashboard narrowed the configured window to ±7 days

`static/js/dashboard.js` passed a hardcoded `window: 7`:

```js
const data = await UpcomingReleasesService.fetchReleases({
  filter: dashboardTableFilter === "collection" ? "collection" : undefined,
  include_queue: true,
  window: 7,        // ← ±7 days
});
```

⚠️ `upcoming_releases_routes.py` applies `window` as a **SYMMETRIC** clause:

```python
params["win7_start"] = (_today - timedelta(days=window_days)).isoformat()
params["win7_end"]   = (_today + timedelta(days=window_days)).isoformat()
```

So `window: 7` did not mean "the next week" — it ANDed `[-7, +7]` onto the
configured window, and since the configured window is `[-28, +14]`, the
**effective** span became `[-7, +7]`.

| surface | sends | effective span |
|---|---|---|
| Dashboard (live tree) | `window: 7` | **15 days** — `[-7, +7]` |
| Dedicated page (`/downloads/discover/upcoming`) | nothing | **43 days** — `[-28, +14]` |
| Dashboard (rebuilt tree) | nothing | **43 days** — `[-28, +14]` |

Two views of one table, disagreeing about what "upcoming" means.

### Why it looked like "only singles", and "nothing in the future"

Albums are announced weeks to months ahead; singles are dated days ahead. Inside a
±7-day span the only MusicBrainz release groups that survive are the near-term
ones — **singles** — and if an artist has nothing dated in the next seven days
there is nothing future-dated at all. The releases were in the database the whole
time (the fetcher collects **42 days back / 120 days forward**), and the dedicated
page was showing them; the dashboard's own window was cutting them off.

### Why configuring could not fix it

⚠️ The Config page's **Show Last / Show Next (days)**
(`upcoming_releases_lookback_days` / `_lookahead_days`) feed
`get_release_window()` — which the `window: 7` clause then **overrode**. So
widening "Show Next" changed the dedicated page and left the dashboard untouched,
making the feature look broken rather than merely narrow.

### The intent was already written down

`get_release_window()`'s docstring states it plainly:

> "Its `loadUpcomingReleasesTable()` sends no `window` parameter, so the separate
> 'tight' clause in `upcoming_releases_routes` (`window_days > 0`) never applies
> — this function alone governs what the dashboard shows."

And the 2026-09-24 changelog that fixed a previous window bug is built on the same
premise ("the table's `loadUpcomingReleasesTable()` sends no `window`
parameter"). The `window: 7` call contradicted both, and the rebuilt dashboard
(`test_site/static/js/pages/dashboard.js`) has **always** sent none — so the two
trees had silently diverged.

---

## 1. The dashboard no longer passes `window`

Removed, so `get_release_window()` governs the dashboard exactly as it governs the
dedicated page. This makes "Show Last / Show Next (days)" work on **both**
surfaces — including on the dashboard, which it could not before — and re-aligns
the live tree with the rebuilt one.

The API's `window` capability is deliberately **kept** (`if window_days > 0:`) for
callers that genuinely want a tight window; the dashboard simply stops using it to
override the configured one.

## 2. `total` now counts the same query it returns

⚠️ A second defect, visible in the same footer. `total` was snapshotted **before**
the tight clause was appended:

```python
total_where_sql = where_sql          # ← too early
count_params = dict(params)
if window_days > 0:
    where_sql += tight_clause        # rows get it; total does not
```

So a `window`-passing caller received rows for ±N days but a `total` covering the
full configured window. The dashboard's footer reads *"View All N Releases"* from
that number, which is why it advertised far more releases than the table listed —
the other half of "there are a lot more listed that don't seem to be getting
displayed". A client paginating on `total` would also page into rows the filter
excludes.

The snapshot is now taken **once, after every clause**, so the count and the page
describe the same query. The library filter is included too (it previously had to
patch itself into `total_where_sql` separately, which is exactly the kind of
two-place bookkeeping that let this drift).

---

## Tests

`tests/test_upcoming_window_parity.py` (new, **8 tests**) — pinned to the
structural contract, since neither surface has a browser test:

- the dashboard sends **no** `window`, and still sends `include_queue` /`filter`;
- **both** UI trees agree on whether they pass `window` (parity is the point, so
  the two dashboards cannot drift apart again);
- the dedicated page never passed `window` — it is the reference;
- the live tree's `window: 7` is proven absent, with a comment recording why;

plus the `total` defects:

- `total` is snapshotted **after** the `window` clause and **after** the library
  filter, and before the `COUNT(*)`;
- there is **exactly one** snapshot (a second, earlier one is how the mismatch was
  reintroduced once already);
- the count and the page are built from the same clause.

**Oracle-verified:** with `window: 7` and the premature snapshot reinstated,
**3 tests fail**; all 8 pass with the fix.

## Verification

| sweep | result |
|---|---|
| new parity suite | **8 passed** (3 fail with the bug reinstated) |
| all 9 upcoming suites + playlist visibility | **139 passed** |

`routes/upcoming_releases_routes.py` compiles clean. The API's `window`
parameter, the fetcher's 42/120-day collection window, and
`upcoming_releases_lookahead_days` defaults are all **unchanged** — this was a
display-surface defect, not a collection one.

## Note on the remaining limit

The dashboard and the dedicated page now agree, but both still show only what
`upcoming_releases_lookahead_days` allows — **14 days forward by default**. The
fetcher stores up to **120 days** ahead, so far-future albums remain hidden until
that setting is widened on the Config page (see the 2026-10-11 note: *"a 2027
release only shows after widening that window in Config"*). That is intended
behaviour and now genuinely reachable from the Config page on every surface.
