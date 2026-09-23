# Queue row actions were all dead: `bindActions` didn't bind `this`

**Date:** 2026-09-23
**Area:** `test_site/static/js/services/item-groups.js`

**Scope: `test_site` only.**

## Reported

> "Manual search on soulseek doesn't seem to work when clicking on it on the
> download queue"

## Root cause — one call shape, ten dead buttons

`services/item-groups.js::bindActions` dispatched its handler as a **bare call**:

```js
el.addEventListener('click', function (event) {
  handler(this, event);          // <- binds nothing
});
```

Inside that callback `this` already **is** the element, so the element was being
passed as an *argument* while `this` was left unbound. The result:

| Handler mode | `this` becomes | `this.dataset` |
|---|---|---|
| strict (what `download-queue.js` has — `'use strict'` at its IIFE top) | `undefined` | throws |
| sloppy | `globalThis` | throws (no `.dataset`) |

Both fail; strictness only changes which `TypeError` you get. Verified:

```
STRICT handler  (what download-queue.js has, via "use strict")
   this === undefined
   this === element? false
   -> this.dataset  === THROWS

SLOPPY handler
   this === globalThis
   this === element? false
   -> this.dataset  === THROWS
```

And every caller was written in the `this.dataset` style:

```js
// download-queue.js:516
'.queue-manual-search': function () {
  manualQueueSearch(this.dataset.query, parseInt(this.dataset.queueId, 10) || null);
},
```

## ⚠️ The blast radius is much larger than the report

`manualQueueSearch` was the visible symptom, but **all ten** selectors in
`pages/download-queue.js`'s handler map were dead, plus the folder actions in
`pages/monitor.js`:

```
.queue-manual-search  .queue-cancel  .queue-retry  .queue-organize  .queue-delete
.group-cancel  .group-retry  .group-organize  .group-delete  .group-organize-modal
```

So Cancel, Retry, Organize, Delete, every group action and the Organize-Group
modal were all inert. A `TypeError` inside a click listener is **invisible** —
there is no toast, no visible error, the button simply does nothing — which is
why this presented as "manual search doesn't seem to work" rather than as an
obvious breakage.

## The fix

```js
handler.call(this, this, event);
```

The element is passed **both** ways, deliberately:

* as the first **argument**, which is the documented `(element, event)` contract;
* as **`this`**, because every existing caller uses `this.dataset`.

`bindActions`'s own JSDoc said handlers receive `(element, event)` — correct, and
yet no caller used the parameter. Fixing only the callers, or only the
signature, would have broken the other style, so the dispatch satisfies both.

## ⚠️ This came in with `105a7ae1` ("reorganiation"), and no test covered it

`git log` on `item-groups.js` shows the `handler(this, event)` line arriving in
`105a7ae1`, already an ancestor of `origin/develop`. Nothing in `tests/` grepped
for `bindActions` at all — which is how ten dead buttons shipped.

## Tests

`tests/test_item_groups_bindactions_this.py` (14) drives
`tests/js/probe-bindactions-this.js`, which **loads the real `bindActions` out of
its IIFE** and the **real handler map out of `download-queue.js`**, then
dispatches a click at each selector and asserts the collaborator was reached.
Nothing under test is reimplemented — a reimplementation would prove nothing
about the shipped code.

⚠️ Writing the probe surfaced its own trap, worth recording: the first version
built handlers via `new Function()`, which is **sloppy mode**, so `this` became
`globalThis` and the failure mode looked different from production. The probe
now asserts on the *effect* (did the collaborator run) rather than on which
`TypeError` appears, so it is insensitive to strictness.

**Oracle:** 14 passed patched → **3 failed** with the one-line fix reverted.

**Regression sweep:** 17 suites / 529 tests, pre-existing failing set
**IDENTICAL** (7) before and after — 0 regressions. Those 7 are pre-existing
(`test_static_js_is_not_jinja[static\js\artist_detail.js]`, slskd artist-gate
cases, and album-art/playlist transport harness issues).

## Not fixed here

* **`handler.call(this, this, event)` is a two-style dispatch.** It is correct
  but slightly unusual; the alternative is to migrate all callers to the
  parameter form and then simplify. That is a larger, mechanical change and was
  not done to keep this fix small and reviewable.
* The `itemGroups` map's **`this.classList`** use in `monitor.js:376` is covered
  by the same fix (`this` is the element again), but only the `.dataset` paths
  are asserted by name.
