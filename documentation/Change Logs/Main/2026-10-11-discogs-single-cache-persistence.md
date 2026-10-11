# Discogs single resolved on demand now persists to the release cache — and two stale confidence tests

Date: 2026-10-11
Branch: develop

## What was flagged

Two pre-existing follow-ups deliberately left open when `e4c7e277`
("a Discogs single past the 15-master cap is detected again") shipped:

1. `_fetch_discogs_releases` still defaulted a **format-less** release to
   `"album"` when writing the `artist_release_cache`, so a single that the new
   on-demand format resolution confirmed at scan time was **not persisted** for
   the next scan's fast path.
2. Two unrelated, pre-existing confidence-calc test failures:
   `test_unverified_match_never_confirms` and
   `test_full_path_promo_returns_medium_confidence`.

## ① The on-demand resolution now survives to the next scan

The Discogs **artist-releases endpoint does not carry the single/EP TYPE
token** — that lives on the release detail. The prefetch therefore saw every
beyond-the-cap / non-master single with no format at all and **guessed
`"album"`**. Three rules then trusted that guess:

- the guess re-applied itself on **every 7-day re-prefetch** (the upsert's
  `ON CONFLICT … release_type = EXCLUDED.release_type`), so even a corrected
  row would be clobbered back;
- the singles fast path (`get_artist_single_titles`,
  `release_type IN ('single','ep')`) never saw the title, so **every scan
  re-paid the Discogs release-detail call** the on-demand resolution makes.

Fixed in `services/popularity/release_cache_service.py` +
`services/enrichment/discogs_service.py`:

* **Format-less = UNKNOWN, never a guessed `"album"`.** Both writers
  (`_fetch_discogs_releases`, `upsert_artist_release_rows`) store
  `release_type = ''`. `_scan_releases` already resolves these rows on
  demand, so detection behaviour is unchanged — only the lie is gone.
* ⚠️ **A third, hidden writer of the guess:** `_upsert_releases`' params line
  was `row.get("rtype") or row.get("release_type", "album")` — `''` is
  **falsy**, so the legitimate UNKNOWN value fell through to the `"album"`
  default one layer below and silently defeated the whole fix. Now resolves
  `rtype`/`release_type` with an explicit `None` check and writes `''` as-is.
  (Caught by the test, not by reading.)
* **The upsert never lets UNKNOWN overwrite a known classification:** the
  `ON CONFLICT` clause now preserves the existing `release_type`/`is_promo`
  pair when the incoming classification is `''`. A FACT (real format tokens)
  still always wins over an old value.
* **New `persist_resolved_discogs_single(artist, release_id, is_promo)`** —
  the scan-time write-back. **UPDATE-only by contract**: a global-search
  match whose release the artist's prefetch never cached must not invent
  cache rows; the prefetch owns the row set, this only corrects a
  classification. Returns True only when a row actually changed (the WHERE
  clause skips no-op writes).
* **`get_single_status` calls it** for every confirmed single, keyed on the
  artist whose release list produced the match (the inverted-artist retry
  persists under the inverted artist). **EP-lead promotions are deliberately
  NOT persisted** — an EP is capped at medium confidence
  (`DISCOGS_EP_LEAD_CONFIDENCE_CAP = 0.74`) and must not enter the fast path
  as an exact 0.85 cached hit.

End-to-end, pinned by `TestNextScanFastPathSeesTheSingle`: format-less row →
scan-time on-demand resolution → row corrected to `'single'` →
`get_artist_single_titles` sees it (next scan's fast path, zero API calls) →
the next 7-day re-prefetch (still format-less) **does not clobber it**.

## ② The two confidence-calc tests

* **`test_unverified_match_never_confirms` — the TEST was stale, not the
  code.** It was written 2026-08-12; on **2026-09-05** (`0876b864`) the
  unverified medium band was introduced *deliberately*
  (`DISCOGS_UNVERIFIED_WEIGHT = 0.60`, `DISCOGS_MIN_UNVERIFIED_CONFIDENCE =
  0.50`, documented "Deliberately below DISCOGS_FULL_CONFIDENCE so these can
  never trigger the early exit"). Reverting the code to satisfy the old
  assertion would also **kill the revived global-search fallback** from
  `e4c7e277`, which only ever runs unverified. Updated to the two-tier
  design: exact unverified match confirms in the MEDIUM band (0.60 < 0.85,
  never definitive); unverified fuzzy (0.45) confirms nothing. Added
  `test_unverified_fuzzy_match_does_not_confirm` as the lower-bound control.
* **`test_full_path_promo_returns_medium_confidence` — a REAL bug.**
  `_detect_discogs` built `metadata = {"is_promo": is_promo}` then
  `metadata.update(calc["metadata"])` — and `calculate_discogs_confidence`
  was called **without** `is_promo`, so its metadata ALWAYS carried
  `is_promo=False` and **clobbered the service's real flag**. Consequence:
  `determine_final_status`'s `if discogs_promo and high == 0: return
  'medium'` could never fire for a full-path promo match — a promo-only
  single was counted as a commercial single and could resolve HIGH. Now the
  real flag wins (`metadata = dict(calc…); metadata["is_promo"] = bool(is_promo)`).
  Confidence stays 0.85 here by design — the medium cap is downstream.

## Tests

* New `tests/test_discogs_single_cache_persistence.py` (16): unknown
  classification from both writers; unknown-never-overwrites-known (and
  facts still win); `persist_resolved_discogs_single` (UPDATE-only, no-op
  skip, promo carried, other sources/releases untouched, blank inputs);
  `get_single_status` wiring (confirmed single persisted, promo flag carried,
  **EP-lead never persisted**, no match persists nothing); end-to-end
  fast-path + re-prefetch survival.
* `tests/test_discogs_album_false_positives.py`: stale assertion updated
  (+1 control test).
* **Oracle** (new suite at baseline `fce50f59`): **14 failed / 2 passed**
  (the 2 are either-way controls). Reverted → identical failures.
* **Sweep** 53 discogs/single/release-cache suites: base `101 failed / 744
  passed` vs new `99 failed / 747 passed`; only-in-CHANGED **empty** (the 2
  only-in-BASE are exactly the two fixed tests) → **0 regressions**.

## Known pre-existing (NOT touched, confirmed identical at baseline)

`tests/test_release_category_persistence.py` fails 6 tests at `fce50f59`
*and* after this change: five are stale label-vs-key assertions (the
category helpers now return canonical KEYS like `live_album` while the tests
still expect the old labels `Live Album`), and
`test_refresh_skips_singles_outside_current_year` is a separate
missing-releases logic failure. Worth its own pass.
