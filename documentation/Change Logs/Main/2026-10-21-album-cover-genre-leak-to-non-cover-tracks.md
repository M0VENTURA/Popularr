# An album's genre list stamped "Cover" onto every non-cover track

**Date:** 2026-10-21
**Area:** `services/enrichment/genre_aggregation_service.py` ·
`services/enrichment/genre_tag_aggregator.py` ·
`services/metadata/metadata_proposal_service.py` · `routes/ui_routes.py`
**Commit:** `fix(genres): an album genre list must not carry a per-track cover marker`

## Report

> "Albums are still assiging the cover genre to non cover tracks if the tag is
> attached to a album genres."

## Root cause — "Cover" is a per-TRACK fact that was being promoted to an ALBUM fact

The codebase already knew the rule: `genre_aggregation_service` carries
`_TRACK_AFFILIATION_FILTER_TAGS` with the comment that these markers "may be
appended to the track whose own title/sources justify them, but must NEVER be
spread to every track of an album by an album-level blend". Three album-level
paths honoured it (`get_track_recommendations` in two places, and
`track_stage._album_top_genres`).

⭐ **Four other album-level paths did not**, and every one of them ends in a
fan-out to tracks. That is why the report says "**still**": the first round of
fixes closed the album blend, and the leak kept arriving through the paths
that were never touched.

The four paths, all reproduced against the real code:

1. **The "Detected Sources" checkboxes** (`genre_tag_aggregator.get_album_genre_sources`).
   The album page pools every track's per-source columns into one panel whose
   entries are **checkboxes**. Selecting one stages it as an ALBUM genre, and
   the album save writes the album's list to every track without genre
   evidence of its own. So one cover track's "Cover" — offered as an
   album-level choice — reached all of the album's tracks.

   Reproduced: with a cover track and an ordinary track on one album, the
   panel offered `Cover` under **three** separate sources (`musicbrainz_genres`,
   `genres`, `navidrome_genres`).

2. **The album save itself** (`routes/ui_routes.py::_apply_album_track_genres`).
   The choke point every album-level genre list passes through, and it wrote
   the list verbatim.

3. **The album genre proposal** (`metadata_proposal_service._album_genres`) —
   the "Lookup MBID" review. It unions the release's per-recording
   `musicbrainz_genres`, and **MusicBrainz tags a cover recording with the
   "cover" genre**. The proposal became the album's genre list, which the
   save then wrote to every track.

4. **The artist page's chips** (`ui_routes.collect_top_genres` and
   `genre_tag_aggregator.get_artist_genre_sources`). "Apply Selected" writes
   the chosen genres to **every track of the artist**, so the same one cover
   could stamp a whole catalogue.

## The fix — one rule, applied at every album/artist-level boundary

`genre_aggregation_service` gains the two shared primitives:

* `is_track_affiliation_tag(value)` — is this name a per-track marker?
* `strip_track_affiliation_tags(values)` — drop the markers from a list,
  accepting any of the shapes the genre columns use (JSON array, delimited
  string, plain list).

The **Christmas family is deliberately not a marker**: a Christmas album IS an
album property, and the genre-playlist builder relies on the tag surviving an
album-level blend. That exemption is inherited from
`_TRACK_AFFILIATION_FILTER_TAGS` and is pinned by a test.

Then the rule is applied at each boundary:

| Path | What it does now |
| --- | --- |
| `get_album_genre_sources` | drops the markers from the album pool |
| `get_artist_genre_sources` | drops the markers from the artist pool |
| `collect_top_genres` (artist page) | drops the markers from the chip list |
| `_apply_album_track_genres` (album save) | drops the markers from the album list — the last line of defence |
| `_album_genres` (Lookup MBID) | drops the markers from the per-recording union |

⭐ **Both guards FAIL CLOSED.** `ui_routes` carries a by-name fallback filter
(`_fallback_is_affiliation_tag`) used only if the helper cannot be imported.
A filter that silently stopped filtering would re-open exactly the bug it
guards, so "the helper is missing" must not mean "let the marker through".

## Second defect found on the way — the list repr reached the genre list

`_album_genres` split its input on commas:

```python
for genre in str(raw or "").split(","):
```

but `musicbrainz_genres` arrives as a **list** (`["Country", "Folk"]`), so the
album genre proposal contained the literal text

```
Alternative Rock, ['Cover', 'Alternative Rock'], ['Alternative Rock']
```

— Python repr, offered to the user as a genre name. The function now uses the
module's existing tolerant parser (`_genre_list`), which already handles
every encoding, instead of a comma split.

## What was NOT changed, and why

* **A cover track still carries "Cover".** The marker is written by the cover
  detector into that track's own `musicbrainz_genres` and into its file tag.
  This fix only stops it from being promoted to an album-level list.
* **The album page still shows the album's own genres**, and the track page
  still shows the per-track sources — the per-track detail is untouched.
* **`_TRACK_AFFILIATION_FILTER_TAGS` itself is unchanged**, so the three
  paths that already honoured the rule keep behaving exactly as before.

## Tests

`tests/test_album_cover_genre_does_not_leak.py` (new, 18 tests):

* the shared helper — the markers are recognised case-insensitively, a real
  genre is not, and **Christmas survives**;
* the album genre proposal — a cover recording does not propose the cover
  genre, the list repr never reaches the proposal, a comma-joined string
  still parses, and Christmas survives;
* the album and artist source pools — no source offers "Cover", the real
  genres are still offered, Christmas is still offered;
* the album save's fan-out — the album list cannot carry a marker, the other
  markers are stripped too, Christmas is kept, and a list of only markers
  writes **nothing**;
* the artist chip list — the filter runs **before** the chip is counted.

**Sweep:** the genre/album/artist suites were run one process per file on the
patched tree and on a **pristine `develop` clone**, and the failing node ids
were diffed — the sets are **identical** (11 pre-existing failures across
`test_album_genre_fallback` 4, `test_genre_consensus_aggregation` 4,
`test_track_genre_navidrome_fallback` 2, `test_genre_backslash_splitting` 1).
**0 new failures.** A wider sweep over the 90 test files that touch the
changed modules produced the same result: every failure is byte-identical to
the baseline.

## Files

* `services/enrichment/genre_aggregation_service.py` —
  `is_track_affiliation_tag`, `strip_track_affiliation_tags`.
* `services/enrichment/genre_tag_aggregator.py` —
  `get_album_genre_sources`, `get_artist_genre_sources`,
  `_format_aggregated(drop_affiliation_tags=…)`.
* `services/metadata/metadata_proposal_service.py` — `_album_genres` (the
  list-repr fix and the marker filter).
* `routes/ui_routes.py` — `collect_top_genres`, `_apply_album_track_genres`,
  `_fallback_is_affiliation_tag`.
* `tests/test_album_cover_genre_does_not_leak.py` — new.
