# Genre aggregation: classify before scoring, then make every slot earn its place

**Date:** 2026-10-15
**Files:** `services/enrichment/genre_vocabulary.py` (new),
`services/enrichment/genre_selection.py` (new),
`services/enrichment/genre_vocabulary.json` (new, the shipped default),
`services/enrichment/genre_aggregation_service.py`,
`services/popularity/stages/track_stage.py`, `routes/maintenance_routes.py`,
`helpers/config_helpers.py`, `routes/ui_routes.py`, both Config pages + their JS

Reported (the report's own summary):

> Your current "top three genres" approach is sensible, but the selection stage is
> happening too early. The main issue is not the limit of three. It is that broad
> parents, precise subgenres, moods, geography, formats, and malformed source tags
> are all competing for the same three positions.

## What was actually wrong

Nothing asked **what KIND of value** a genre string was before ranking it. So
`dark` (a mood), `German` (geography), `live` (a performance attribute) and
`Billboard Hot 100` (a chart artefact) all competed for the same three slots as
`melodic death metal`. Four other faults compounded it:

| fault | consequence |
|---|---|
| track / album / artist evidence pooled under one key | an artist's broad `rock` carried the same authority as a track's `melodic death metal` |
| hierarchy condensation was 7 hardcoded pairs | `rock` beside `melodic death metal` kept a slot |
| no per-position thresholds | a weak artist-only fallback **filled** a blank slot instead of leaving it empty |
| `\|` was not a separator, and `/` always was | a pipe-delimited list was kept whole and **every genre in it was lost**; `death/doom` split into `death` + `doom`, so `death_doom` could never match and a bare `doom metal` was written instead |

## The pipeline

The order is the fix, so it is the order of the code:

```
parse → normalise → classify → canonicalise → score → condense → select
```

**Classify first.** A canonical vocabulary of **199 genres, 20 moods, 60 countries,
21 regions and 17 attributes** gives every value a stable identity and a kind. Four
spellings of one genre (`Alt Rock`, `Alt.Rock`, `alternative-rock`,
`Alternative Rock`) now become four pieces of evidence for one genre instead of
four competing entries — and moods, geography, attributes and content types are
**recognised and set aside**, not silently dropped: the result reports what moved
and why, so a mis-classification is visible rather than invisible.

**Genre is matched before geography and mood, deliberately.** `ambient`,
`dark ambient`, `dream pop` and `chillout` are all plausible mood words; the mood
map runs last so a real genre can never be reclassified as a mood and deleted.
Likewise `K-pop`, `Americana`, `Afrobeat`, `Neue Deutsche Härte` and
`Anatolian rock` stay genres — geographic-sounding names that ARE musical
classifications.

**Entity levels.** `source@entity` keys weight evidence by how specific it is —
track `1.00`, album `0.70`, artist `0.35`. A bare source key is treated as
`track`, which is both the previous behaviour and usually the truth.

**Condensation with the report's two exceptions.** A selected genre is dropped when
a more specific selected genre covers it — with `keep_together` (pairs that
describe *different aspects*, e.g. alternative metal + alternative rock) and
`conditional_keep` (collapse only when the narrower genre has no track/album
evidence). `metal` sits **under** `rock` so the report's headline rule
(`Rock + Death Metal → Death Metal`) holds, while `metal` remains its own
**family** so the family-slot limit does not treat every rock and metal genre as
one branch. Genres support **multiple parents** (`folk_metal` is both metal and
folk) because the report's own rules need it.

**Every slot earns its place.** Three is a maximum, not a target: each position
needs a confidence floor plus a relative floor against the position above it, so a
weak artist-only fallback leaves the slot **empty**.

## Where the vocabulary lives

A JSON **data file**, not YAML in `config.yaml`:

* `services/enrichment/genre_vocabulary.json` — the shipped default, tracked so a
  change to it is reviewable;
* `<config_dir>/genre_vocabulary.json` — the user's edits from the Config page,
  which take precedence so a repo update never clobbers them.

Editable from **Config → Analytics → Genre Vocabulary** (load / validate / save /
reset). **An invalid vocabulary is refused rather than saved** — the file is read
during a scan, and a broken one would silently produce no genres at all, which is
far worse than a rejected save that says why. Validation catches dangling parents,
duplicate ids, an alias claimed by two genres, and out-of-range specificity.

## Config

`genres.selection` (`enabled`, `max_genres`, `source_weights`, `entity_weights`,
the bonus/penalty terms, `thresholds`, `family_slot_limit`, `keep_together`,
`conditional_keep`) on both Config pages, with the nested block registered in
`_sanitize_config_sections` — without that entry the page save coerces it to `{}`.

`source_weights` is deliberately **separate** from the legacy `genres.weights`:
those were tuned for the old vote-counting engine (which put Last.fm below
ListenBrainz); these are the report's provider-trust levels, where MusicBrainz
proper genres lead and a manual correction outranks everything.

**`enabled: false` restores the previous engine exactly**, and the legacy path is
kept intact rather than deleted — a switch that cannot be flipped back is not a
switch. A failure inside the new engine also falls back, because an engine that
crashes must not silently stop writing genres.

## Deliberate behaviour changes

Two, both measured rather than assumed:

1. **A track's own lone Last.fm tag is now accepted.** The legacy gate rejected it
   because Last.fm weighed 0.10 against a 0.25 floor; the canonical engine gives
   Last.fm 0.70 (recommended trust for broad coverage). The protection that
   mattered is kept by a different route — an **artist-level** lone tag is
   weighted 0.35 and penalised, so it still cannot define a track.
2. **Genres are written in Title Case** (the vocabulary's display name) where the
   legacy engine echoed the source's spelling. Verified cosmetic: every consumer
   lowercases before comparing (`iter_genre_tokens`, `is_metal_genre`,
   `normalize_genre`, `clean_conflicting_genres`).

## Verification

- `tests/test_genre_selection_engine.py` (118) — organised by pipeline stage,
  because a later stage running early is the defect and each stage's test would
  still pass if a later one were moved in front of it.
- **Oracle: 15 mutations, all 15 caught** — classification skipped, entity weights
  ignored, per-source dedupe removed, condensation removed, `keep_together`
  ignored, absolute floor removed, relative floor removed, family diversity
  removed, whole-value-first tokenising removed, the pipe separator removed,
  diacritic folding removed, geography classified before genre, provenance
  dropping non-selected candidates, the crash fallback removed, and filter tags no
  longer surviving.
- **The oracle found real defects, twice.** First, my tests were **overlapping
  guards** — removing one left the outcome unchanged, so 12 mutations went
  uncaught. Each test now pins its own guard (the entity-weight test asserts the
  contribution's weight, not the total, because the artist penalty already
  guaranteed the total ordering; the family test uses siblings, because a
  parent/child set is capped by condensation instead). Second, it exposed a
  **modelling gap**: §9's `Rock + Death Metal → Death Metal` cannot hold unless
  `rock` is an ancestor of `death_metal`, which forced the single-parent model to
  become multi-parent.
- Baseline sweep (`album|save|metadata|ui|config|template|static_js|maintenance|
  genre|popularity|scan|playlist`) run on BOTH trees: **102 failures at baseline,
  102 with the change, the lists identical** — zero regressions.
- Six regressions were found and fixed on the way: five case-only assertions, and
  one source-text assertion pinning the literal `"navidrome_genres", "navidrome"`
  which the entity-level change renamed to `"navidrome@track"`. The latter now
  matches with a regex allowing an `@entity` suffix, because pinning the literal
  makes a legitimate weighting change look like a regression.

### Also found

`test_admin_tags_never_reach_vote` had been **passing vacuously**: it asserted
`"cover" not in result` against a result containing `"Cover"`. The filter tags are
appended **Title-Cased** by design (they are playlist/filter markers other code
depends on, deliberately preserved in an earlier change), so a lowercase
membership test could never match. Comparing case-insensitively exposed it, and it
now asserts what is actually true: a filter tag takes no part in ranking and
consumes no genre slot, but it is appended after the voted genres.

`metal` was also given `rock` as a parent and its own `family`, which is what lets
`Rock + Death Metal → Death Metal` and the metal family-slot limit both work.
