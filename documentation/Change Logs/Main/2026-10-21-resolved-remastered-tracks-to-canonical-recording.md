# Remastered tracks resolve to the canonical studio recording

**Date:** 2026-10-21
**Area:** `helpers/normalization_service.py` ·
`services/popularity/popularity_matching.py` ·
`services/popularity/popularity_sources.py` ·
`services/enrichment/single_detection_service.py` ·
`services/popularity/stages/track_stage.py`
**Commit:** `fix(popularity): remastered tracks resolve to the canonical studio recording`

## Report

> "A remastered track should always resolve to the canonical original
> studio recording for popularity, single detection, and ListenBrainz
> playcounts, rather than being isolated to the remastered edition's
> specific MusicBrainz recording ID."

## Root cause — a remaster is the SAME performance, but MusicBrainz minted a NEW recording for it

MusicBrainz creates a brand-new Recording MBID whenever an engineer
remasters an album: the audio fingerprint (AcoustID) and the mastering
credits changed, so the model records a new recording. But a remaster is
a mastering pass on the exact same studio performance — the listener
pressing "Higher (remastered 2024)" is listening to the flagship studio
song "Higher", and the audience is the SAME audience.

That split broke three decisions:

### 1. ListenBrainz scoring locked onto the remaster's near-empty count

ListenBrainz counts plays per Recording MBID. The 1999 original of "Higher"
carries hundreds of thousands of lifetime plays; the 2024 remaster's fresh
Recording MBID carries a few hundred (only the listeners who tagged their
files with the new Deluxe MBID). When the preloaded LB tracklist resolved
the release's Recording MBIDs, the remastered track's popularity score was
computed from the tiny remaster count — pairing, e.g., 965,353 Last.fm
listeners with 371 ListenBrainz plays for the SAME song.

### 2. Single detection missed the match

MusicBrainz and Discogs file singles under the plain title ("Higher",
"With Arms Wide Open"). The library copy carries the mastering pass in
its own title ("Higher (remastered 2024)"). Comparing those two titles
literally missed the single entirely — the reported `is_single = False`.

### 3. Variant compatibility treated the remaster as a different performance

`_HARD_VARIANT_TOKENS` listed `remaster`/`remastered` alongside
`live`/`acoustic`/`remix`/`demo`, so a remastered title was judged
incompatible with its identically titled studio original and kept its own
empty playcount. Only live/acoustic/remix/demo describe a DIFFERENT
performance — a remaster does not.

## The fix — one rule, four boundaries

A remaster is the same performance. The rule is applied at every boundary
where a remaster must collapse to its canonical recording:

| Boundary | What changed |
| --- | --- |
| `helpers/normalization_service.py` | `strip_remaster_qualifier(title)` — strips the REMASTER half only, keeping the genuine variant half ("Higher (radio edit / remastered 2024)" → "Higher (radio edit)"); `has_remaster_marker(title)` — is this title a remaster? |
| `popularity_matching.py` | `_HARD_VARIANT_TOKENS` drops `remaster`/`remastered`. A remastered title is now COMPATIBLE with its studio original. The docstring no longer claims to mirror `queue_matching_config.TITLE_VARIANT_TOKENS` (that set governs filename matching, a different question). |
| `single_detection_service.py` | The single-match key and the live single-detection lookup title strip the remaster qualifier, so "Higher (remastered 2024)" matches the plain-title single entry on MusicBrainz/Discogs. |
| `popularity_sources.py` | The preloaded LB tracklist detects a remastered release — title says "remaster" OR the release year is more than two years newer than the release group's original — and resolves the group's ORIGINAL release's recordings, which carry the real playcount. One extra MusicBrainz call corrects every track of the album. The per-track MBID suggestion lookup also strips the remaster qualifier. |

## What was NOT changed, and why

* A remastered FILE is still a different file (different audio bytes, different tags). `queue_matching_config.TITLE_VARIANT_TOKENS` is untouched — the download queue IS the right place to treat a remaster as a separate file.
* A remastered track's own title is NOT rewritten in the database. The strip is applied at lookup/scoring/detection time only, so the file tags stay honest.
* Live / acoustic / remix / demo markers describe a DIFFERENT performance and remain hard variants. The three that were already there stay.

## Tests

`tests/test_remaster_resolves_to_canonical_recording.py` (new, 21 tests):

* the shared helpers — every remaster form is recognised, the compound form
  keeps its other half, non-remaster titles are untouched;
* variant compatibility — a remastered title is compatible with its studio
  original, while live/acoustic/remix/demo remain incompatible;
* single-detection match key — a remastered title matches the plain-title
  single entry;
* the remaster-edition detector — title and year-gap triggers, with the
  correct boundary (two years is not a remaster, three is);
* the original-release resolver — exact-year match wins, earliest wins
  otherwise, no-releases/no-group are handled.

**Sweeps:** genre / single-detection / matching / title / LB / MB suites,
one process per file, failing node ids diffed against a pristine `develop`
clone — the failure sets are byte-identical across all sweeps. **0 new
failures.** `import app` → 425 routes.

## Files

* `helpers/normalization_service.py` — `strip_remaster_qualifier`,
  `has_remaster_marker`, `_strip_remaster_from_qualifier`, and the regexes
  they run on.
* `services/popularity/popularity_matching.py` — `_HARD_VARIANT_TOKENS`
  (remaster removed), updated docstring and examples.
* `services/popularity/popularity_sources.py` — `_release_is_remaster_edition`,
  `_original_release_of_group`, `_release_year`, and the canonical-recording
  wiring in the preloaded tracklist function.
* `services/enrichment/single_detection_service.py` — `single_match_key`,
  the match key applied to the MB/Discogs cached-single lookups, and the
  remaster-stripped lookup title at the entry point.
* `services/popularity/stages/track_stage.py` — the per-track MBID
  suggestion lookup strips the remaster qualifier.
* `tests/test_remaster_resolves_to_canonical_recording.py` — new.
