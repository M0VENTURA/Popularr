# Restore the album page's dead buttons (and fix a silent no-op)

**Date:** 2026-09-24
**Area:** `ui` / `album`

## Summary

Five functions were called by the album page's inline handlers but defined
nowhere. A template has no compiler, so the buttons shipped and threw
`ReferenceError` on click. All five are now implemented in **both** trees, and
three of the four new handlers were found to depend on things that did not
exist — each fixed here.

`tests/test_rebuilt_pages_have_no_dead_buttons.py` previously excused these via
its `_OBSOLETE_BUTTONS` register. Implementing them required **removing those
register entries** or the register's own staleness test fails; that coupling is
now pinned by a test so the two suites cannot disagree.

## The five handlers

| Handler | Call sites | Backend |
|---|---|---|
| `autoLinkAllMbids` | Actions item + inline "Link" (both trees) | existing `POST /api/musicbrainz/link-album-mbids` |
| `downloadMissingTracks` | Actions item | existing `POST /api/queue/add` per missing row |
| `renameAlbumFiles` | Actions item (passes artist/album) | existing `POST /api/album/{artist}/{album}/rename-files` |
| `openAlbumArtModal` | Actions item **and the pencil over the album art** | existing `GET /api/album/search-art`, `POST /api/album/set-art`, `POST /api/album/upload-art` |
| `alignTracklist` | inline "Align" button | existing `POST /api/v1/tracks/<id>/apply-mb-field` |

No new backend was needed: every endpoint already existed and was unreachable.

## Design notes

- **`downloadMissingTracks` clicks the existing per-row buttons** rather than
  re-deriving the payload. The payload lives in a **closure** in the rebuilt
  tree (and in `data-*` in the live tree), and a closure cannot be read back out
  of the DOM — so clicking is the one path that cannot drift from the per-row
  behaviour. The selector therefore matches **either** tree:
  `.mb-queue-missing, [onclick*="queueMissingTrack("]`.
- **`alignTracklist` writes immediately.** There is no bulk "align" endpoint,
  and inventing one that rewrites files would bypass the existing per-track
  apply path. It uses the same `apply-mb-field` endpoint the inline Apply button
  uses, one track at a time, so the DB row and the file tags are updated
  identically. ⚠️ NOTE: track numbers on this page are **display-only** cells
  (`.track-number-display-<id>`), not form inputs — writing to the DOM would
  have done nothing.
- **`openAlbumArtModal`** acknowledges that `search-art` returns base64
  `data:` URLs in `images[].url` (it embeds the fetched bytes rather than
  exposing a remote link) and that `set-art` accepts a `data:` URL, so a search
  hit is applied directly with no re-download. Upload must use a raw `fetch`
  with `FormData` — a JSON helper would break the multipart body.

## Bugs found *while* implementing (all fixed here)

1. **`refreshAlbumPage` was a silent no-op.** It was called from **both** trees
   and defined in **neither** — and because every caller guarded with
   `typeof window.refreshAlbumPage === 'function'`, the missing definition
   degraded to *nothing*: album art and auto-linked MBIDs appeared to succeed
   while the page kept showing stale state. Now defined in both trees (see
   below). This was introduced by this change set, not pre-existing.

2. **`downloadMissingTracks` reported a false success on total failure**
   (rebuilt tree). The poll waited for `disabled || dataset.queued` — but
   `buttonState.setBusy` sets `disabled = true` for the whole request, so the
   condition matched on the **first** tick and every row counted as queued. And
   `data-queued` is never set by anything. Settlement is now read from the real
   markers: in-flight = `_popularrBusy`, success = the non-reverting
   `btn-success` class that `setDone` adds.

3. **`window.buttonState` does not exist in the live tree** — `button-state.js`
   is only loaded by the rebuilt tree. The live handlers initially used
   `buttonState.setBusy(...)` (and treated its return value as a restore
   function, when the real API is `withBusy(btn, label, fn)`), which would have
   silently skipped every busy state there. Replaced with a local
   `withSpinner` helper so neither tree depends on the other's bundle.

## New guard: guarded calls must not guard a ghost

`typeof x === 'function'` reads as defensive but is a **silent no-op** when `x`
is defined nowhere. `test_rebuilt_pages_have_no_dead_buttons.py` cannot see this
because it only harvests `onclick=` handlers from templates — a guarded call
from inside a JS module is invisible to it.

`tests/test_busy_popup.py::TestGuardedCallsAreNotGuardingAGhost` closes that
gap. Two correctness details, both proven by mutation:

- A guard with an **`else`** is *intentionally optional coupling* and degrades
  loudly, so it is not reported. Only bare guards (no `else`) are.
- JS **comments must be stripped** before matching. The docstrings quote the
  very patterns being searched for, and a comment containing
  `window.refreshAlbumPage === 'function'` satisfies a naive definition regex
  (`window.refreshAlbumPage\s*=` matches the first `=` of `===`) — so a
  *deleted* definition went undetected until both the comment stripper and a
  `=(?!=)` lookahead were added.

The definition scan also reads **template inline `<script>` blocks**:
`addSelectedTrack` and `openReplacementTrackModal` are defined in
`templates/playlists/importer.html`, so a `*.js`-only scan called them dead.

### Pre-existing ghosts recorded, not fixed

`_KNOWN_DEAD_GUARDS` lists guards that are dead **in the current tree** and were
confirmed **not** to be on a line added by this change set. Each is a missing
*feature*, not a typo, so making the guard resolve means writing that feature —
out of scope here, and wrong to do silently inside an unrelated UI change:

| Global | Effect of it being undefined |
|---|---|
| `loadFolderGroups` | **Both trees.** `monitor.js` publishes only `csvInline*`, so Download-Queue refreshes after a folder match silently keep stale contents. |
| `searchMusicBrainzReleases` | **Both trees.** Plain searches in the global search / flyout silently fall through to the generic path. |
| `loadUpcomingReleases` | Rebuilt tree. The Upcoming Releases panel does not refresh after a queue change. |
| `showSlskdResults` | Rebuilt tree. The "view results" affordance on a queue row does nothing. |

The register keeps the same two-rule discipline as `_OBSOLETE_BUTTONS`: every
entry must **still** be dead (staleness test), and a **new** ghost fails the
build, so it cannot rot into a blanket suppression.

## Tests

- `tests/test_busy_popup.py` — 40 passed (was 29 before this work):
  - the five handlers are implemented in **both** trees;
  - they are **not** in `_OBSOLETE_BUTTONS` (otherwise that register is stale);
  - the general no-dead-handler guard now runs over **both** templates;
  - `TestGuardedCallsAreNotGuardingAGhost` — silent-guard detection,
    comment stripper, inline-script scan, `else`-branch classification,
    probe sensitivity, and register staleness.
- `tests/test_rebuilt_pages_have_no_dead_buttons.py` — the five entries removed
  from `_OBSOLETE_BUTTONS`; the four dead-button prose updated.
- 100 passed across the popup + dead-button + artist-contract + terminal-queue
  suites.

Mutation-verified: nulling **or** deleting the live `refreshAlbumPage`
definition both fail the suite. The first attempt at this guard did **not** catch
the deletion — see the comment-stripper note above.
