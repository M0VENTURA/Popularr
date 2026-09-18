# Queue button: no loading feedback, and stacked popups after repeat clicks

## Symptom

"The queue button doesn't show anything when selected, then after pressing a few
times, 20 seconds later it gets multiple popups."

## Root cause — two bugs in one early return

Both search implementations had this shape:

```js
if (typeof openReleasePicker === 'function') {
  openReleasePicker(id, title, artist, function () { ...mark queued... });
  return;                     // <-- no busy state, no disable, no guard
}
```

**1. No feedback.** The branch returned immediately without touching the button.
The click looked ignored while a MusicBrainz release-picker probe (seconds) and
then the queue POST ran.

**2. No in-flight guard.** `_queuedIds[id]` was set only *inside the picker's
callback*, which fires after the user has chosen a version. Until then the
`if (_queuedIds[id]) return` guard could not block anything, so every click
started another probe. Because `releasePickerOnQueued` is a single module-level
slot, concurrent picks also overwrote one another — so the results arrived
together and only the last button got marked.

**Reproduced against the original code** (`tests/js/queue-button-probe.js`):

| | original | fixed |
|---|---|---|
| probes from 5 rapid clicks | **5** | **1** |
| spinner while working | no | yes |
| disabled while working | no | yes |
| `aria-busy` | not set | set |
| restored after settling without queueing | n/a | yes |

## Fixes

- Added a `_queuedInFlight` map, checked **before** the async call and cleared on
  every settle path, so a repeat click is a no-op for the whole
  lookup + queue window (not just the POST).
- The button now enters a busy state immediately (`spinner-border` + `disabled`
  + `aria-busy`) and a shared `settle()` clears it, so no path can leave it
  stuck.
- **Picking and queueing are now distinct.** Choosing a version opens the
  flyout; that is not "Queued". Only an actual enqueue marks the button, and
  settling without queueing restores it — otherwise cancelling the flyout would
  strand the button on the in-flight guard forever.
- The live `openReleasePicker` now **returns** its probe promise (the rebuilt one
  was already `async`), so the caller can await settlement instead of guessing.

## Why the live tree needed its own helpers

`static/js/` has no `ui/` folder, so the live dashboard never loads
`ui/button-state.js` and has no `global.buttonState`. The live fix therefore sets
the busy state inline (`setQueueBtnBusy` / `restoreQueueBtn` / `markQueueBtnDone`,
including `aria-busy`) so both trees behave identically. The rebuilt tree keeps
using `global.buttonState`.

## Tests

- `tests/js/queue-button-probe.js` — extracts the **shipped** functions by
  brace-matching and drives them with stub collaborators, printing one JSON
  line. A structural test would still pass with the guard moved after the async
  call, so the behaviour is asserted by running this.
- `tests/test_queue_button_feedback.py` — 12 tests: four behavioural (one probe
  from five clicks; busy state shown; not marked queued before a version is
  chosen; restored after settling without queueing) plus structural guards that
  the in-flight state exists, is set **before** the picker call, is cleared
  afterwards, that busy feedback is applied, and that both trees' pickers are
  awaitable.

The probe is deliberately runnable by hand:

```
node tests/js/queue-button-probe.js static/js/unified_search.js
```

## Config

No new config keys.
