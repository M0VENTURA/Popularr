# `(edit)` / `(single edit)` no longer break the main-release match

**Date:** 2026-11-02
**Area:** metadata / enrichment (MusicBrainz tracklist comparison)
**Files:** `helpers/normalization_service.py`,
`tests/test_edit_version_marker_matching.py` (new)

## Report

> Tracks with edit or single edit are not matching the main release

The album Compare offered these as title changes:

```
Title: When Doves Cry → When Doves Cry (single edit)
Title: Nothing Compares 2 U → Nothing Compares 2 U (edit)
```

## Root cause

`normalize_title_for_compare` — the single key shared by the Compare button,
the Lookup-MBID preview and the album-mismatch scan — stripped performance
markers (`(Live)`, `(Acoustic)`, `(Remix)` …) but kept `(edit)` as if it were
wording:

```
"Nothing Compares 2 U"        -> "nothing compares 2 u"
"Nothing Compares 2 U (edit)" -> "nothing compares 2 u edit"
```

The tracks WERE paired correctly; the review then asked the user to overwrite
a plain library title with a release-specific qualifier. MusicBrainz routinely
appends `(edit)` / `(single edit)` to the recording title on a release, which
is release context for the same song — the same kind of **soft** marker the
popularity matcher already treats as compatible
(`{"version", "edit", "radio"}`).

## Fix

`_VERSION_MARKER_RE` now recognises `edit` alongside the performance markers.
The word-boundary match means `(Single Edit)`, `(Radio Edit)`, `(Album Edit)`,
`[edit]` are all ignored, while words that merely contain the letters
(`Edited`, `Edition`) remain real title differences.

Because the key is shared, the change lands on every surface at once: the
Compare rows disappear, the Lookup-MBID preview stops proposing the rename,
and the album-mismatch scan stops reporting it — the three can never disagree.

The rule is deliberately symmetric, matching the existing performance-marker
trade-off: a marker difference alone is not reported in either direction, so
`(Radio Edit)` on the library side is not proposed for stripping either.

## Behaviour deliberately preserved

- **Wording differences are still reported**: `Song` vs `Song Two (edit)`,
  `Song One (edit)` vs `Song Two (edit)`.
- **A real length difference still surfaces**: a single edit really is shorter,
  so the Compare still flags `duration` (and `needs_update`) when the lengths
  disagree.
- **The word boundary holds**: `Song (Edited)` / `Song (Edition)` are not
  markers.

## Tests

`tests/test_edit_version_marker_matching.py`:

| Test | Pins |
|---|---|
| `test_the_edit_qualifier_is_not_a_rename` | the reported rows, both directions, all bracket/qualifier spellings |
| `test_the_reported_row_no_longer_raises_a_title_change` | Compare (`_match_mb_tracks_to_library`) emits `diff_fields == []` |
| `test_a_local_edit_marker_is_not_stripped_either` | symmetric soft-marker rule |
| `test_a_real_length_difference_still_surfaces` | title suppression does not hide the length difference |
| `test_a_wording_difference_is_still_a_rename` | controls (`Song Two (edit)`, `Edited`, `Edition`) |
| `test_an_edit_marker_is_detected` / `test_ordinary_words_are_not_markers` | marker detection stays narrow |

`tests/test_album_musicbrainz_matching.py` updated: track 3's
`Song Three (Radio Edit)` vs `Song Three` is matched without a title
recommendation, consistent with the soft-marker rule.
