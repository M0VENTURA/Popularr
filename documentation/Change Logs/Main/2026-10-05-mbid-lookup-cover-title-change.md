# MBID lookup: a differing track name is now marked, with the cover verdict (2026-10-05)

**Report:**

> Currently the matching when doing an mbid lookup doesn't mark when the Track
> Name is different. I want this to pick up if the track name is different
> even if it shows (Cover Version) as this can be selected to skip if this is
> a false cover. Though if the work relationship is downloaded during this
> lookup then it should know whether a cover is likely based on the
> relationship status.

## Root cause

The title rule that governs the Lookup-MBID review treated **every** bracketed
marker as a non-change:

```python
# metadata_proposal_service._titles_match_ignoring_markers
left  = normalize_title_for_compare(current)
right = normalize_title_for_compare(proposed)
return left == right          # "(Cover Version)" → "song" == "song"
```

`normalize_title_for_compare` strips anything matching
`_COVER_ANNOTATION_RE` — any bracketed text containing the word *cover* — so

    Song (Cover Version)   vs   Song

compared equal and **no bar was raised**. The same rule was duplicated in the
Compare button's matcher (`_match_mb_tracks_to_library`, via a second
`normalize_title_for_compare !=` comparison), so both paths agreed — on
hiding the difference.

That rule was itself added for a good reason (a previous report: *"(Live) /
(Acoustic) / (Remix)" describe a performance variant stored in dedicated
columns, so overwriting the title would destroy the marker*). But a **cover
marker is not a performance variant**: `(Cover Version)` asserts *who
performed the recording*, and silently swallowing it meant the review could
never surface a false cover at all — which is exactly what the report asks
for.

The lookup already fetches what is needed to judge it: `_RELEASE_FETCH_INC`
includes `work-rels` / `work-level-rels` / `recording-level-rels`, and
`_flatten_release` derives the verdict per track by comparing **the work's
artist credit against the track artist** — a mismatch sets `is_cover` and
`original_cover_artist`. That verdict existed in the payload but was never
attached to the title bar the user actually looks at.

## Changes

- `helpers/normalization_service.py`
  - New `has_cover_marker(title)` — bracketed cover annotations only
    (`(Artist Cover)`, `(Nirvana Cover)`, `(Cover Version)`), deliberately
    narrower than `has_version_marker`.
  - New `titles_match_for_review(current, proposed)` — **the single** title
    rule for both comparison paths:
    - a difference that is *only* a performance marker (`(Live)`,
      `(Acoustic)`, `(Remix)`, …) is still **not** reported;
    - a difference involving a **cover marker** **is** reported;
    - two empty keys never count as a match.
- `services/metadata/metadata_proposal_service.py`
  - The local `_titles_match_ignoring_markers` is **replaced** by the shared
    rule (one rule, two call sites — a second independently-written title rule
    is what lets the preview and the Compare disagree).
  - New `_cover_verdict(...)` attaches the work-relationship verdict to the
    reported title change: `cover of <artist> (work relationship)`,
    `not a cover (work relationship)` when a work exists and credits the same
    artist (the false-cover case), and **nothing at all** when the recording
    carries no work — a claim is never invented.
- `services/enrichment/musicbrainz_service.py` — `_match_mb_tracks_to_library`
  now calls the same `titles_match_for_review`, so the Compare button and the
  preview can no longer disagree.
- `static/js/metadata-review.js` + `test_site/static/js/services/metadata-review.js`
  — the staged bar renders the optional `note`, e.g.

  > Title: *Song (Cover Version)* → **Song** *(cover of Nirvana (work relationship))*

  Once the bar exists it is a normal staged change: **Included** by default and
  **Ignore** removes it from the staged payload — the "selected to skip" the
  report asks for.

### Not changed

`normalize_title_for_compare` itself is untouched: it still strips cover
annotations, because it is the shared key for lookup/repair/duplicate logic
where a marker must not fork an identity. Only the *review comparison* now
treats a cover marker as meaningful.

## Tests

- `tests/test_metadata_compare_version_markers_and_genres.py`
  - The original suppression parametrization now covers **performance**
    markers only, with a `CONTROL` that a real rename is still reported.
  - New `TestCoverMarkersAreReportedAndJudged` — six cover-marker shapes
    (including the report's own `(Cover Version)`) asserted on **both** paths,
    plus controls proving performance markers and identical titles stay quiet.
  - New `TestTheWorkRelationshipCoverVerdict` — cover of a named artist, cover
    without a named artist, the false-cover disproof, no-work = no verdict,
    no-note control, and that the note rides only the title change.
  - `70 → 84` tests in this file.
- **Oracle:** stashing the five source files (normalization, proposal,
  musicbrainz service and both `metadata-review.js`) fails **10 of the 12**
  new tests; the 2 passing are controls. Markers restored after `stash pop`.
