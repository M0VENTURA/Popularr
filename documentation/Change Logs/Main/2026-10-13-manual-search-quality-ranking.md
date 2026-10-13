# Manual Soulseek search now ranks like the automated search

**Date:** 2026-10-13
**Area:** slskd / downloads
**Commit:** (pending — see final push)

## The report

> Going to slskd search with the search button next to the active queue: the
> automated search went "search, filter, rank (relevance → free slot →
> bitrate), pick the 320 peer with open slots". The manual search returned a
> long raw list — the 320 kbps peer with a free upload slot could sit anywhere
> in 50 rows while the automated search picked it first.

## What changed

### `routes/download_search_routes.py` — `slskd_search_results`

The manual poll response is now built the same way the automated pipeline
judges candidates:

1. **Config-driven quality minimum.** Read `downloads.quality`
   (`get_search_quality_config()`: `min_bitrate`, `min_sample_rate`,
   `min_length_seconds`) — the SAME source the automated path reads, so the
   two views cannot drift apart on what "good enough" means.
2. **One quality rule.** Each file goes through
   `SearchFile.matches_quality()` (unknown bitrate/sample rate is UNKNOWN, not
   a failure), plus a length gate that only fires when the length is known.
   Extracted into a defensive helper `_file_meets_quality()` because the first
   cut called `matches_quality` directly and a file lacking the method emptied
   the ENTIRE result list (the route's `except` swallowed it) — an unknown
   file is KEPT, never fatal, matching the codebase's
   `SearchResponse.__post_init__` "one bad file must not drop the whole
   response" convention.
3. **The shared ranker.** The list is ordered with the same
   `rank_search_results()` the automated path uses — relevance to the query →
   free upload slot → bitrate → sample rate → queue length → upload speed.
   The ranker was promoted from a `SlskdService` staticmethod to a module-level
   function (`services/downloads/slskd_service.py`) because the route tests
   substitute the whole class with a lightweight fake; the staticmethod now
   delegates to it so class-based callers are unaffected.
4. **Qualifying first, nothing hidden.** Files meeting the minimum are listed
   FIRST; the rest are kept below, ranked. A manual search is the tool you
   reach for when the automated one found nothing — hiding the only 128 kbps
   copy of a rare track would defeat the purpose. The `quality` payload
   reports `{min_bitrate, min_sample_rate, min_length_seconds, qualifying,
   total}` and `qualityFallback` is true when no file met the minimum, so the
   UI says so instead of silently showing a low-bitrate list.

### Frontend

* `test_site/static/js/services/slskd.js` — captures `session.quality` and
  `session.qualityFallback`; new `qualityNote(session)` footer helper (" — 12
  of 50 meet your quality minimum (192 kbps / 44 kHz), listed first" / " —
  no file met your quality minimum (...); showing all, best first"); Slots
  column in the results table (`?` for unknown, never `0`).
* `static/js/downloads.js` — Slots column added to BOTH live renderers (search
  tab `renderSkldResults` and the manual modal
  `renderSoulseekManualSearchResults`).

## Tests added — `tests/test_manual_search_quality_ranking.py` (24)

* Quality minimum comes from config, not a hardcoded 192/44100.
* Ranking reuses the shared ranker; tuple order (relevance → slots →
  bitrate) is pinned.
* Qualifying files are partitioned first; below-quality files are KEPT.
* Response reports thresholds, counts, and the fallback flag.
* Renderers: Slots column present; a missing slot count renders `?`.
* `TestOneOddFileCannotEmptyTheList`: a file whose quality cannot be
  determined is kept; the length gate ignores unknown lengths; the helper
  defers to `matches_quality` with the CONFIGURED thresholds.

## Regression notes

While wiring this in, running the EXISTING `test_soulseek_free_slot_filter.py`
found the fatal catch described above — fixed via the defensive helper and
covered by tests. A separate sweep across `test_slskd_service_hardening`,
`test_slskd_candidate_selection`, `test_soulseek_free_slot_filter`,
`test_manual_slskd_search_contract`, `test_manual_search_button_works_in_test_site`,
`test_queue_search_early_termination` is green.

> ⚠️ `test_slskd_wrong_artist_rejection.py` has 2 failures
> (`test_unknown_artist_skips_gate`, `test_generic_various_artist_skips_gate`)
> that are PRE-EXISTING on `develop` — verified by stashing this change and
> reproducing them on the clean tree. Introduced by `bcb61f58` ("VA
> compilation rows no longer hard-reject the same song"). Not part of this
> change; tracked separately.