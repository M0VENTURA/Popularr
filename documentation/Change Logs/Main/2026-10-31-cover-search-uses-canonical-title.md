# Cover detection searched MusicBrainz with the raw library title

**Date:** 2026-10-31
**Area:** `enrichment` (cover detection)
**Files:** `services/enrichment/cover_detector_impl.py`,
`tests/test_cover_search_title_normalization.py` (new)

## Reported symptom

> Still some tracks not matching the covers. The ones that are working have
> successfully matched the recording mbid, but the others haven't.

On *Spotify - Metal Covers* (Various Artists) the tracks whose title already
carried an annotation resolved a recording MBID and were detected; the rest
were not. The annotated titles were:

* `Doomed (Bring Me the Horizon Cover)` — Maphra
* `Everytime We Touch - TEKKNO Version` — Electric Callboy
* `Mea Culpa (Ah! Ça ira!)` — Gojira

## Root cause

`_search_recordings_by_title` sent the library title verbatim into a quoted
MusicBrainz Lucene **phrase**:

```python
query = f'recording:"{escape_lucene_special_chars(title)}"'
```

A phrase query requires **every** token in the supplied text to appear in the
recording's title, so the annotation tokens made the lookup return zero
recordings even though the canonical song exists. Verified against the live
API:

| query | recordings |
|---|---|
| `recording:"Doomed (Bring Me the Horizon Cover)" AND artist:"Maphra"` | **0** |
| `recording:"Doomed" AND artist:"Maphra"` | 2 (including the cover) |
| `recording:"Everytime We Touch - TEKKNO Version" AND artist:"Electric Callboy"` | **0** |

With no recording MBID, step 3 (recording relations) is skipped, and for these
playlist compilations — which never matched an official release — it is the
only route cover detection has.

## Fix

`_recording_query_titles(title)` builds the query forms, most canonical first:

1. the title with bracketed annotations (`(...)`, `[...]`) and a
   dash-delimited suffix (` - TEKKNO Version`, ` — Ghost`) removed — the same
   annotations `canonical_track_title` already drops, but **case-preserving**
   so the emitted query stays readable;
2. the raw title, folded through `normalize_unicode_punctuation`, as a fallback
   for titles whose brackets are part of the real name.

`_search_recordings_by_title` tries each variant until one returns results, and
retries **without** the `artist:` clause before giving up: a file's ARTIST tag
need not match MusicBrainz's credit spelling, and `_resolve_recording_mbid`
re-filters every candidate through `_titles_match` + `_artist_matches` anyway,
so the unconstrained pass cannot introduce a wrong artist.

## Verification

`tests/test_cover_search_title_normalization.py` (11 tests):

| Group | Pins |
|---|---|
| Query variants | cover annotation stripped first; `- Version` suffix stripped; em-dash performer stripped; a clean title queried once; empty title yields nothing |
| Search behaviour | stripped title queried and matched; raw title retried when the stripped one misses; artist clause dropped before giving up |
| Resolver | the three reported *Metal Covers* titles resolve their recording MBID |
