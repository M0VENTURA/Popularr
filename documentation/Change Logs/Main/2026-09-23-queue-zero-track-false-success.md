# Queueing an owned release reported a false success (`0 tracks`)

**Date:** 2026-09-23
**Area:** `services/queue/queue_processing_service.py`,
`services/downloads/download_pipeline_service.py`,
`routes/musicbrainz_routes.py`, `test_site/static/js/**`

**Scope: `test_site` only.** The live tree's `static/js/main.js` and
`static/js/unified_search.js` keep their existing behaviour and are deliberately
untouched.

## Reported

> "Selecting to add an item to the queue from the universal search doesn't add
> it into the download queue"

The UI announced success — a green pill, or a "Download queued" toast — and the
download queue stayed empty.

## Root cause — the chain could not express "I did nothing"

Two independent defects, either one of which is enough to produce the symptom.

### 1. `add_release_tracks_to_queue` returned `list[int]`, and `[]` means three things

The adder has three legitimate skip paths. All three `return []` / `continue`:

| Path | Line | Condition |
|---|---|---|
| active re-queue guard | `:228` | the release already has rows in an *active* status |
| already in library | `:269` | `find_library_track` hit — the user **owns the track** |
| already in queue | `:285` | a row with the same artist+title exists in a queued/active status |

An empty list cannot distinguish "you already own this album" from "an error
occurred", and nothing counted which path fired. The counts simply did not exist.

Note `_active_statuses` (`:209`) and the duplicate-status list (`:277`) are
**not** the same set — `backed_off` is excluded from the active re-queue guard
but the duplicate check adds `matched`. That asymmetry is pre-existing and was
left alone.

### 2. The endpoint branched on a flag that is hard-coded `True`

`download_pipeline_service.py:1251` returned `"success": True` unconditionally,
after `"queue_items_created": len(queue_ids)`. So `api_musicbrainz_download`
took the happy path for a zero-track request and answered:

```json
{ "success": true, "message": "Download queued for Abyss (0 tracks)",
  "total_tracks": 0, "queued_tracks": 0, "tracking_id": null }
```

with **HTTP 201**. `tracking_id` was `None`, because it fell back to
`result["queue_ids"][0]` on an empty list. The UI was *told* the count was zero
and ignored it.

### 3. Both frontend paths never read the response

```js
// test_site/static/js/ui/search-flyout.js — before
await global.api.postJson('/api/musicbrainz/download', {...});
settle(true);                                   // marks the button "Queued"
if (global.toast) global.toast.queued(...);     // green pill
```

```js
// test_site/static/js/main.js — before
if (!data.success && !data.tracking_id) { ...error... ; return; }
global.toast.queued(releaseTitle);              // fires for 0 tracks too
```

Neither inspected `queued_tracks`.

## Fix

### Backend — report *why* nothing was queued

`add_release_tracks_to_queue_detailed` is the new implementation; the old
`add_release_tracks_to_queue(release_id, ...) -> list[int]` is now a **thin
wrapper** over it, so the one production caller and the four existing tests that
assert on the list return keep working unchanged.

The detailed form counts each skip and maps it to a reason code with a
human-readable message:

| `reason` | Meaning |
|---|---|
| `already_active` | the release already has active downloads |
| `no_tracks` | MusicBrainz returned an empty tracklist |
| `all_in_library` | every track is already in the library |
| `all_present` | some owned, some already queued |
| `already_queued` | every track is already in the queue |
| `all_duplicate` | the release listing repeated the same tracks |
| `nothing_queued` | catch-all |

`start_release_download` passes the reason through as additive keys
(`queued`, `queue_reason`, `queue_message`, `queue_skipped`) alongside the
existing `queue_items_created` / `queue_ids`, so nothing that reads the old keys
is affected.

The endpoint now treats zero tracks as **not success**:

```json
{ "success": false, "queued": false, "reason": "all_in_library",
  "message": "Every track in this release is already in your library.",
  "error": "…same text…", "queued_tracks": 0, "total_tracks": 11 }
```

### ⚠️ Why HTTP 200 and not 4xx

`test_site/static/js/utils/api.js:137` **throws** on any non-2xx:

```js
if (!response.ok) {
  const serverMsg = (data && data.error) || response.statusText;
  throw new Error(`HTTP ${response.status}: ${serverMsg || 'Request failed'}`);
}
```

A 400 would collapse the specific, actionable "already in your library" message
into a generic `HTTP 400` toast. HTTP 200 with `queued: false` lets each caller
show the real explanation.

### Frontend — surface the warning instead of celebrating

All three `test_site` callers now read the response:

- `main.js::queueSpecificRelease` — new `nothingQueued(data)` / `warnNothingQueued(data)`
  helpers; the `onQueued` callback and the picker close only run on real success.
- `main.js::downloadReleaseViaSoulseek` — same guard.
- `search-flyout.js::queueRelease` — a no-op calls `settle(false)` and shows a
  warning; the button is never marked "Queued".
- `pages/download-queue.js::addMbDownloadToSession` — `notifyError` with the
  API's message instead of `notifySuccess("Download queued: …")`.

## ⚠️ The track COUNT outranks `success`

The first draft of this fix had a precedence bug that the probe caught:

```js
// WRONG — `success: true` short-circuits the count check
const succeeded = !(data && (data.queued === false || data.success === false))
                  || queuedCount > 0;
```

That still fires the success toast for the endpoint's *old* shape
(`success: true` + `queued_tracks: 0`), i.e. it reinstates the original bug.
The correct form only treats an **explicit** `queued_tracks` of zero as failure,
while a legacy payload that omits the count entirely still succeeds:

```js
const succeeded = !(
  (data && (data.queued === false || data.success === false))
  || (data && data.queued_tracks !== undefined && queuedCount === 0)
);
```

## Tests

- `tests/test_queue_zero_track_false_success.py` — 20 tests + 2 node-gated probe
  wrappers: one per reason code, the wrapper/detailed agreement, the pipeline
  pass-through, and five endpoint cases (including the 201-on-success path and
  the "older partial result shape" case).
- `tests/js/queue-zero-track-probe.js` — 21 checks. Extracts the **shipped**
  `notifyError` / `markQueued` / `queueRelease` by brace-matching (preserving the
  `async` modifier) and drives them with stubs. Covers the explicit
  `success:true + queued_tracks:0` shape and the bare `queued:false` contract.
- `tests/js/mutate-queue-zero-track.js` — 3 mutations, **all detected**.

**Oracle:** with only the source fixes stashed, `test_queue_zero_track_false_success.py`
goes from 22 passed to **18 failed / 4 passed**.

**Regression sweep:** 26 related suites. Pre-existing failing set **identical**
(29 → 29, `Compare-Object`); the two suites this change owns went 19 failures → 0.

## Known limitation

This was not reproduced against a running instance with a real database — the
strongest available evidence is the zero-track response shape. The fix is
defensive in either direction: a release that genuinely queues tracks is
unaffected (`queued_tracks > 0` still returns 201 + `success: true`).

## Not addressed

The **owned-release Queue button is not disabled** — `search-flyout.js:465`
already computes `owned`, but the button stays active. It now reports "already
in your library" instead of lying. Disabling it outright was left as a product
decision.
