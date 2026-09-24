# Soulseek manual search: free-slot filtering was never implemented

**Date:** 2026-09-25
**Area:** `routes/download_search_routes.py`, `static/js/downloads.js`
**Reported:**

> Under the active queue, when pressing the search button next to it, a Soulseek
> search modal appears. The search button doesn't do anything. This is supposed
> to wire in a manual search for soulseek, show results and allow the user to
> select a result that starts downloading. **The results are filtered showing
> only items that have a free download slot.**

---

## The free-slot filter was never implemented — at either layer

| Layer | State |
|---|---|
| `services/downloads/slskd_service.py::get_search_results` | **reads** `hasFreeUploadSlot` off the slskd response into `SearchResponse.has_free_upload_slot` |
| `routes/download_search_routes.py::slskd_search_results` | **DISCARDED it** — the result dict had no slot field |
| `downloads.js::renderSoulseekManualSearchResults` | filters `row.freeUploadSlots > 0`; a MISSING value defaults to 1, so it kept **everything** |
| `downloads.js::pollSlskdSearchResults` (search tab) | same no-op filter, while the footer printed *"filtered zero slots"* |

So the user was offered peers that **cannot accept another upload** — the exact
thing the filter exists to prevent — and told they had been filtered. The data
was read correctly at the top and thrown away one function later.

### The fix

The route now sends the flag under **both** spellings: `freeUploadSlots` (what
the clients already read) and `has_free_upload_slot` (matching the service and
dataclass so one fact has one name). It derives the value from the response
object rather than a constant, and every file of one peer shares that peer's
slot state (the slot belongs to the peer, not the file).

The manual modal now also states **how many** were offered
(`Found N available result(s) (of M returned)`), because silently dropping
results is the confusion the report describes.

---

## Two dead buttons found while investigating

An inline `onclick` resolves on `window`. When nothing defines the name, the
click throws `ReferenceError` and the button is **inert** — invisible in the UI,
because a template has no compiler and the page still returns 200.

* **`downloadSlskdFile`** — the "Download" button on every search-tab result.
  It *was* defined — in `static/js/artist_detail.js`, **which the downloads pages
  never load**. This is the trap that makes a repo-wide grep look reassuring: a
  definition *somewhere* is not a definition *here*. Now defined in
  `downloads.js` where its button renders.

  ⚠️ The `artist_detail.js` copy posts to `/api/slskd/download-single`, a route
  that **does not exist** in this codebase — so that copy could only ever
  produce a 404. The new implementation posts the single shape to
  `/api/slskd/download`, which accepts both single and batch payloads.

* **`showSlskdResults`** — the "Select" button on an awaiting-selection
  MusicBrainz download. Defined only in the **rebuilt** tree's
  `download-queue.js`, so dead in live mode (trees are served exclusively).

  ⚠️ **Deliberately left unimplemented.** There is **no endpoint** serving the
  stored candidate files for such a download: `GET /api/musicbrainz/download/<id>`
  is DELETE-only (405), and the only results route needs a *live* slskd search id
  which an awaiting-selection row does not carry. Inventing a fetch would mean
  guessing at a data source and shipping a button that *looks* wired but cannot
  work — the false-success class this file has already been burned by. It now
  fails **loudly** (console error naming the gap + a message pointing at the
  queue-row search icon) instead of silently doing nothing.

---

## A guard for the whole dead-button class

**NEW `tests/test_no_undefined_onclick_handlers.py`** extracts every
`onclick="fn("` a renderer emits and checks it against the definitions in every
script of the **same tree**.

Two things it must get right, both of which produced false results while writing
it:

1. **Strip comments first.** The renderer's own header explains the bug by
   *quoting* the offending `onclick` — and one such quote sat inside dead code,
   making the scanner report a handler no live markup references.
2. **Check the whole tree, not a hand-picked file list.** A hand-picked list
   reported a handler as missing that the tree *did* define.

It also survives an operational subtlety: the rebuilt renderer legitimately
reports **zero** inline handlers (it binds with `addEventListener` + `data-*`),
so "found nothing" is asserted differently per tree rather than being conflated
into one weak check that can never fail.

---

## Verification

**NEW `tests/test_soulseek_free_slot_filter.py` (12)** and
`tests/test_no_undefined_onclick_handlers.py` (4).

⚠️ **The first version of the free-slot tests was worthless — mutation testing
proved it.** All four mutations SURVIVED:

| Mutation | Why it survived |
|---|---|
| route renames the key to `freeUploadSlots_removed` | the test asserted `"freeUploadSlots" in source` — a **substring** match on a renamed key |
| route drops the alias | same |
| client filter becomes `return true;` | the test only asserted the field was **mentioned** in the function |
| `downloadSlskdFile` definition renamed | the definition regex counted `window.downloadSlskdFile = downloadSlskdFile;` as a definition — a **re-export** is not a definition |

Each was then fixed at the root:

* the **route is CALLED** through a real Quart app and its JSON inspected, so a
  missing/renamed/hard-coded value fails;
* the **client filter is RUN** in Node against real rows, so only a genuine
  outcome change is detectable;
* the definition regex now requires the right-hand side to actually **create** a
  function, so an alias pointing at nothing no longer counts;
* the function-body extraction **brace-matches** instead of slicing to
  end-of-file — the slice had swallowed LATER renderers containing the identical
  `slots > 0` text, which is precisely how the `return true` mutation survived.

Re-run: **4/4 mutations CAUGHT.**

**Oracle** (worktree at `a0cf2636`): sweep over
`slskd/soulseek/download/queue/handler/button/static/layout` on both trees.
One new failure — `test_sibling_torrents_root_is_searched`. Verified
**pre-existing and order-dependent**: it fails 3/3 in isolation at BASELINE, and
my diff does not touch `download_completion_service`. Not caused by this change.

---

## Still unexplained, and stated plainly

I could **not** reproduce "the search button doesn't do anything" on the manual
modal itself. Driven directly in Node against the verbatim endpoint payload,
`runSoulseekManualSearch()` POSTs, polls, and renders rows with working Select
buttons; the handler is a genuine top-level global (no IIFE, no CSP, no syntax
error, no duplicate id, no modal nesting — both queue pages verified well-formed
by walking `div` depth through the served HTML). The `aria-hidden` warning the
reporter supplied is generic Bootstrap modal-hide noise and does not by itself
identify a cause.

The console message the user supplied contained **no exception**, which by
itself rules out the dead-handler explanations in their tree. The remaining
candidate is the slot-busy path: `runSoulseekManualSearch` sets
`waitingForSlot = true` and returns, so its `finally` deliberately does **not**
re-enable the button; it stays disabled until `_pollForSlotFree` sees the slot
free. If that poll stops, the button is disabled indefinitely. That needs
in-browser confirmation (does the button show a spinner?) and is NOT claimed as
fixed here.
