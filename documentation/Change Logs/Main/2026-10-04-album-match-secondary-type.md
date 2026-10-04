# Album match turned "Album (Soundtrack)" back into "Album" (2026-10-04)

**Report:**

> When doing an album match if an album has a secondary type, it changes it
> back to album even though it shows it's changing to album+soundtrack.
> The drop down is album (Soundtrack).

## Root cause

Client side, in `fillField()` of `metadata-review.js` — the function that puts
a proposal's value into the form before the user saves.

It chose the matching `<select>` option with one predicate that allowed
containment in **either** direction:

```js
v === wanted || wanted.includes(v) || v.includes(wanted)
```

`wanted` is the proposed value, `album+soundtrack`. **`"album"` is a substring
of `"album+soundtrack"`**, and the bare `Album` option renders *before* every
`Album (…)` option — so `.find()` returned `Album`, the form submitted
`album_type=album`, and the save wrote the bare primary to
`musicbrainz_albumtype`/`spotify_album_type`.

The orange bar meanwhile still showed the proposed `album+soundtrack` (it
renders from `change.proposed`, not from the select), which is exactly the
reported contradiction: *"it shows it's changing to album+soundtrack"* yet the
saved type is `album`.

The same predicate short-circuited every composite: `album+live`,
`album+compilation` and `album+remix` all collapsed to `Album`. An empty
proposal was worse — `"".includes(v)` is true for **every** option, so it
selected the first one.

A second, independent downgrade existed on the category path:
`setAlbumTypeIfPresent()` (test_site `applyAlbumMatch`) applied the search
result's `category` blindly. That string is a **lossy label** — a cached row
can carry plain `Album` while the stored type is `album+soundtrack` — so it
flipped a composite selection back to the bare primary *before* the proposal
could restore it, and if the proposal had no `album_type` change (current ==
proposed) nothing put it back.

### Why only test_site

`applyAlbumMatch()` — the album-match entry point — exists only in the
`test_site/` tree. `metadata-review.js` itself ships in **both** trees and has
the identical `fillField`, so the live album page's `applyAlbumMbid` →
`applyProposal` path had the same defect; both copies are fixed.

## Changes

- `static/js/metadata-review.js` + `test_site/static/js/services/metadata-review.js`
  — `fillField()` now matches **exact first**, and only falls back to
  containment when no exact option exists, preferring the **most specific**
  (longest) option so a bare category like `soundtrack` still reaches
  `album+soundtrack`. An empty proposal returns false and leaves the select
  untouched.
- `test_site/static/js/pages/album.js` — `setAlbumTypeIfPresent()` never
  downgrades a composite selection to a bare primary of the same segment. A
  *deliberate* downgrade still happens: the proposal proposes the bare value
  and `fillField()` lands on it exactly.

## Scope

`#album_type` is the only `<select>` inside `#albumMetadataForm`, so the
`fillField` change affects exactly the reported field. Every other proposal
field is a text input and takes the untouched `else` branch.

## Tests

- `tests/test_album_type_select_match.py` — 28 tests. The option order is
  parsed from **both** templates, so reordering the options cannot silently
  reintroduce the short-circuit; `fillField` is driven in **Node against the
  real module** (both trees) and `setAlbumTypeIfPresent` is probed from inside
  `album.js`'s IIFE (it is not exported).
  - `TestTheRenderedOptions` — both trees render the same order, and bare
    `album` precedes `album+soundtrack` (the precondition for the bug).
  - `TestFillFieldSelectsTheProposedOption` — every composite lands on its own
    option, the bare-category fallback still works, an unknown value changes
    nothing, an empty proposal changes nothing.
  - `TestSetAlbumTypeKeepsTheSecondary` — a lossy `Album` category never
    strips `album+soundtrack` / `album+live` / `album+compilation`; upgrades
    still apply.
- **Oracle:** stashing the three JS sources fails **13 of 28** (including both
  `[soundtrack-*]` cases — the reported defect); markers restored after pop.
- Related suites: `test_album_metadata_review_wiring`,
  `test_findings_persist_*`, `test_busy_popup`,
  `test_album_page_missing_tracks_list`, `test_album_save_*` — 224 passed, 1
  failure (`test_album_type_persistence::test_detects_when_missing`) that is
  **pre-existing**: it fails identically in the clean-HEAD baseline tree and
  `album_stage.py` is untouched by this change.
