# Release picker: merge editions that share a track count, auto-queue when they all do (2026-10-09)

## Reported

> When selecting a release to add to soulseek, it pops up and asks for the
> specific release to download. For all releases that have the same amount of
> tracks, can they be merged into a single release to pick, or if all releases
> have the same amount of tracks, then auto select the best release so the pop
> up doesn't come up.

Both halves were asked for and both are implemented.

## What the picker did before

`openReleasePicker` (`test_site/static/js/main.js`) probes
`GET /api/musicbrainz/release-picker?format=json`:

```js
if (releases && releases.length === 1) {          // exactly ONE → queue it
  return queueSpecificRelease(releases[0].id, …);
}
openSlideOver(url, 'Select Version: …');          // otherwise → the flyout
```

So a release group with **12 pressings that all carry the same 12 tracks**
opened a flyout of 12 rows differing only by country, date and format — a
choice the download does not act on.

## 1. Merge — `_picker_group_editions`

New helper in `routes/musicbrainz_routes.py`, applied to the **rendered
flyout only** (the JSON payload stays raw — see below).

* Releases with an equal `track_count` collapse to **one row**, carrying
  `edition_count` so the card can say `3 editions`.
* The **representative is the first member**, which the endpoint has already
  sorted *Official first, then by track count* — so the surviving row is the
  best of the group, not an arbitrary one.
* A release whose count is **0** is **never merged**: two releases we cannot
  count are not known to be the same, and folding them would hide a real choice
  behind a false equivalence. That is worse than the noise it removes.
* Copies (`dict(release)`) — the same list feeds the JSON response, so
  mutating it would corrupt the client's view.

## 2. Auto-select — `openReleasePicker`

When **every** candidate carries the same track count, the flyout has nothing
to distinguish that the user can act on, so the best one is queued directly —
extending the existing single-release shortcut:

```js
const counts = new Set(releases.map((r) => Number(r.track_count) || 0));
if (counts.size === 1 && !counts.has(0)) {
  return queueSpecificRelease(releases[0].id, …, note);
}
```

* `!counts.has(0)` — an all-zero group is **unidentified**, not identical.
* `releases[0]` — the endpoint's ranking (Official, then most tracks).
* The skip is **announced**: `queueSpecificRelease` gained an optional `note`
  printed **after** `toast.queued`, so the reason only appears once the queue
  actually succeeded. The server-rendered `onclick` still passes three
  arguments, so it is a no-op there.

The `openSlideOver` fallback is untouched and still reachable whenever the
counts differ.

## Why the JSON is deliberately NOT grouped

The client needs the **raw** list to answer "do *all* of them share one
count?" — grouping the payload too would present every group as a single
release and silently destroy that question (and the candidate list behind it).
Only the HTML flyout is folded. Pinned by
`test_the_json_payload_is_not_grouped`.

## Tests

`tests/test_release_picker_merges_identical_editions.py` — **17**:

* **Merge**: equal counts collapse; representative is the ranked best;
  different counts stay separate; **unknown counts never merge**; the input
  isn't mutated; empty group is harmless; the renderer actually calls it; the
  folded card announces its edition count.
* **Auto-select**: the block exists; fires only when every count matches and
  never on 0; queues `releases[0]`; **CONTROL** the flyout still opens when
  counts differ *and* the auto-select path returns before it; **CONTROL** the
  one-release path is untouched; the note lands after a confirmed queue; the
  extra argument stays optional.
* **Agreement**: both sides key on `track_count`; the JSON payload is not
  grouped.

## Verification

* `node --check` clean on `test_site/static/js/main.js`.
* New suite → **17 passed**.
* **Oracle** — reverting both source files → **15 failed / 2 passed** (the two
  that pass either way are the one-release control and the JSON-shape
  control). Restored → 17.
* **Sweep** — 58 musicbrainz/release/picker/queue/download files, clean
  `origin/develop` vs this change: **base `62 failed / 841 passed`** vs
  **new `47 failed / 856 passed`**, `Compare-Object` on the sorted `^FAILED`
  lines = **empty for "only in CHANGED"**. The 15 that only fail at BASE are
  this change's own tests (the oracle) → **0 regressions**; the other 47 are
  identical pre-existing failures (including the documented
  `test_sibling_torrents_root_is_searched` hash-order flake).

## Not changed

* The **live** tree's `static/js/main.js` and `templates/…/_release_picker.html`
  were left alone (test_site-only instruction). The *merge* half lives in
  `routes/musicbrainz_routes.py`, which is shared, so both UIs get it; the
  *auto-select* half is test_site-only until you ask for the live port.
* Nothing about what gets **downloaded** changed — the same ranking picks the
  release it would have picked by hand.
