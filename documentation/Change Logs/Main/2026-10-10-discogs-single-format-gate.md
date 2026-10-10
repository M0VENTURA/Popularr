# A Discogs single went undetected — the format gate, not the "Pt." wording

**Date:** 2026-10-10
**Area:** `services/enrichment/discogs_service.py` · single detection
**Reported:** *"Leaving Song Part II by AFI isn't being detected as a single by
Discogs. It is a single, but it's worded in Discogs as "The Leaving Song Pt.
II". Due to the PT. rather than Part I think it's being missed."* Plus an
architecture question: would it be better to pre-pull all Discogs releases and
match locally rather than checking online per track?

## The stated cause was wrong — and that matters

The report blamed the "Pt." vs "Part" wording. It does not. The shipped
`_discogs_title_similarity` scores "Leaving Song Part II" vs "The Leaving Song
Pt. II" at **0.947**, comfortably above the 0.75 gate — and the same 0.947 when
the Discogs title carries the `AFI – ` prefix. The abbreviation was never the
problem. (A probe against the real shipped function established this before any
code was changed; a test now pins it so the theory is not re-litigated.)

## Root cause: the `format` gate ran before the title match

`_scan_releases` required a single/EP **type** token before it ever computed
title similarity:

```python
tokens = release_format_tokens(rel.get("format"))
if not tokens:
    continue                      # <- skipped before the title was compared
```

But the Discogs `/artists/{id}/releases` endpoint does **not** carry that token.
It reports the *physical* medium ("CD", "Vinyl") or nothing at all. The single/EP
type lives in the release **detail**'s `formats[].descriptions`, which
`resolve_master_formats` recovers — but only for the first
`_MAX_MASTER_FORMAT_RESOLUTIONS` (**15**) masters.

So for a catalogue-heavy artist like AFI, a genuine single past that cap arrived
format-less and was skipped before its near-exact title match was ever computed.
This is why the miss is intermittent: it depends on where in the discography the
single sits.

## A second, independent defect: the search fallback was dead

The safety net — a global `/database/search` fallback — filtered its results by
`_release_artist_matches(result["artist"], artist)`. Discogs search results often
**omit** the `artist` field entirely, carrying the credit only in the title
("AFI – The Leaving Song Pt. II"). `_release_artist_matches("")` returns False,
so every such result was dropped and the fallback could never fire. Either
defect alone was enough to miss the track.

## Fix

* **Title gate first.** `_scan_releases` now gates on the local title
  similarity, then resolves the format *on demand* for a title-matching release
  that has no single/EP token. New `_resolve_release_format()` fetches the
  release detail and writes `format`/`track_count` back onto the release dict —
  which lives in `_artist_releases_cache`, so the lookup is paid **once per
  artist, not once per track**. This is strictly better than raising the
  15-master cap: it only spends a request on releases that already match, and it
  also covers non-master release rows that `resolve_master_formats` skips
  entirely.
* **Fallback accepts a title-embedded credit.** New `_search_result_matches_artist()`
  matches the leading credit segment of the title when the `artist` field is
  absent, so the global-search fallback is reachable again.

## Verification

* `tests/test_discogs_single_format_gate.py` (16): the reported case across
  every release shape (format absent, physical-only, master, already-resolved),
  on-demand caching, and the guard set (album format still rejected, a fetched
  Album detail not promoted, a wrong title rejected *without* a network call, a
  non-Main role still rejected, a different artist in the title still excluded).
  Oracle: source reverted → 10 failed / 6 passed (the 6 are controls that must
  hold either way); source fixed → 16 passed.
* Regression sweep, 20 single-detection/Discogs suites: **0 new regressions**;
  10 baseline-only failures (the new tests) all fixed by the change.
* Two failures remain in the swept set and are **pre-existing** (identical at
  baseline, neither touched by this change):
  `test_unverified_match_never_confirms` (asserts an unverified match never
  confirms, but the code deliberately allows it at medium confidence —
  `DISCOGS_MIN_UNVERIFIED_CONFIDENCE`) and
  `test_full_path_promo_returns_medium_confidence` (`metadata.update(calc
  ["metadata"])` lets the confidence calc's default `is_promo=False` clobber the
  real flag). Both are flagged, not fixed here.
* Six stale `_scan_releases(title, "", catalogue, ...)` calls in
  `test_discogs_album_false_positives.py` were repaired (an `album` argument was
  removed from the signature earlier but the tests still passed it, so they
  errored at the call and exercised nothing). They guard the *over*-detection
  risk — deep album cuts being flagged as singles — so they now genuinely
  validate that this change does not widen detection.

## The architecture question: pre-pull already exists

The suggestion to "pre-pull all releases from Discogs and match locally" is
**already implemented**, and is undermined by the same format-blindness:

* `prefetch_artist_releases()` runs during the artist scan and writes to
  `artist_release_cache` (DB, 7-day TTL). `DiscogsService._get_artist_releases`
  reads that cache *first*, and only falls back to the paginated API.
* `_detect_discogs` has a `cached_single_titles` fast path that matches a track
  against known single titles with **zero network**.
* MusicBrainz is already handled the same way — the missing-releases pull
  populates `missing_releases` with `category='single'`.

The Discogs Singles & EPs filter linked in the report is a **web-UI-only**
parameter (`superFilter`/`subFilter`); it is not on the public API. The API
equivalent is the search `format` filter. The reason the pre-pull did not help
here is that `_fetch_discogs_releases` uses the same 15-master format
resolution and defaults format-less releases to `"album"` — so the cache itself
could hold the single misclassified. The on-demand resolution in
`_scan_releases` closes that gap for the in-memory path.

**Not fixed here (follow-up candidates):** `_fetch_discogs_releases` still
defaults a format-less release to `"album"` when writing the cache, so a single
resolved *only* by the new on-demand path is not persisted as a single for the
next scan's fast path. Raising the cache-side resolution — or classifying
format-less rows as unknown rather than album — would close that.
