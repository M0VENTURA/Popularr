# The metadata review listed "Genres" twice

**Date:** 2026-10-21
**Area:** `services/metadata/metadata_proposal_service.py`
**Commit:** `fix(metadata): propose the album genre change once, not twice`

## Report

> "When checking metadata after either choosing a new release group or using
> lookup mbid on the album page, it lists the Genres twice on the bottom of
> the page under test_site."

## Root cause — the field list contained `album_genres` twice

`_album_level_proposals` builds the album-level "current → proposed" list
from a field order derived from `resolved`:

```python
for form_field in list(resolved) + (["album_genres"] if "album_genres" in resolved else []):
```

`album_genres` has **no** entry in `_ALBUM_FIELD_SPECS` — its value is built
by `_album_genres()` (MusicBrainz release-group genres, release genres, and
the per-recording `musicbrainz_genres` union) and assigned into `resolved`
a few lines above:

```python
resolved["album_genres"] = _album_genres(metadata)
```

So `list(resolved)` **already contained** `album_genres`, and the `+`
appended it a **second** time. The loop ran the album-genres branch twice
and produced **two identical "Genres" proposals**. The review banner renders
one row per proposal, so the same

```
Genres: <current> → <proposed>
```

line was printed twice.

### Why it looked intermittent

The duplication only becomes *visible* when a proposal is actually
generated — i.e. when the MusicBrainz genres differ from the stored ones.
Choosing a new release group and using Lookup MBID are precisely the two
actions that change the MusicBrainz release, so they are precisely the two
actions that surfaced it. On a page where the genres already matched, no
proposal was produced and nothing was duplicated.

## The fix

The field list is now de-duplicated while preserving order:

```python
_field_order: list[str] = list(resolved)
if "album_genres" not in _field_order:
    _field_order.append("album_genres")
for form_field in _field_order:
```

## The MusicBrainz genre surface (confirmed)

The two "Genres" surfaces on the album page are DIFFERENT by design, and both
read MusicBrainz data:

* **"Genres & Moods" → Detected Sources → MusicBrainz** — the *stored*
  per-track `musicbrainz_genres` column, pooled across the album's tracks by
  `genre_tag_aggregator.get_album_genre_sources`. This is what the page
  shows as the MusicBrainz source (the same source list the test_site
  template renders, `('musicbrainz_genres', 'MusicBrainz', …)`).
* **The review banner's "Genres" row** — the *proposed* album-level value
  from the freshly fetched MusicBrainz release: release-group genres, release
  genres, and the union of the release's per-recording `musicbrainz_genres`
  (`_album_genres`).

The duplication was never in the "Genres & Moods" panel — it was the review
banner rendering the same proposal twice.

## Tests

`tests/test_album_genres_proposed_once.py` (new, 6 tests):

* the genres change is proposed **once** (the reported case);
* the single proposal carries the right label, current and proposed values;
* no proposal when the genres already match;
* no proposal when the genres match in a different order (genres are an
  unordered set);
* the other album fields are still proposed (the de-duplication does not
  silence them);
* **no field is proposed twice**, whatever it is.

**Oracle:** with the fix reverted, **3 of the 6 fail**; restored → 6 passed.

Sweep of proposal / album / genre suites diffed against a pristine
``develop`` clone — **identical failure sets, 0 new failures.**

## Files

* `services/metadata/metadata_proposal_service.py` — the de-duplicated
  field order in `_album_level_proposals`.
* `tests/test_album_genres_proposed_once.py` — new.
