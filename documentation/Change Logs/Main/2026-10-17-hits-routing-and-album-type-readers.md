# The Hits compilation routing, and one release-type list for both readers

**Date:** 2026-10-17
**Scope:** `services/` + `tests/` — the Python is shared by BOTH trees (the live
tree and `test_site/`); nothing here is tree-specific.

## The report

From a scoring review of one artist's albums:

> **Billy Talent — Hits:** 11 tracks locked as Global 5★ in pre-pass, but 0 tracks
> got 5★ in final output (4★: 11). Meanwhile *Stand Up and Run* got 1★ (28.3)
> while *Chasing the Sun* got 3★ (7.5).

> **Ash — '94–'04: The Singles:** routed to `[COMPILATION: per-track-artist
> rating]`.

Two compilations of the same shape, rated by different ladders.

## Two defects at ONE decision point

### 1. The bare word "Hits" matched nothing

`services/catalog/album_classification_service.py`:

```python
return any(
    token in text
    for token in ["greatest hits", "best of", "anthology", "collection", "singles"]
)
```

`"greatest hits"` is a full **phrase**, so the bare word `Hits` — Billy Talent's
actual album title — matched nothing. `"singles"` is why Ash's
`'94 - '04: The Singles` matched and took the artist-catalogue branch. The whole
symptom is one missing entry in a five-string list.

### 2. The two readers of the album's release TYPE disagreed

* `prepare_album_context` (the **live** rating path) read
  `spotify_album_type or musicbrainz_album_type` — an **`or`-chain**, which stops
  at the first non-empty value. An album whose sources disagreed
  (`spotify="album"`, `musicbrainz="album+compilation"`) therefore **lost** the
  compilation marking.
* `_resolve_compilation_flags` (the end-of-run **fallback**) JOINED five columns.

So the album page could read "Album (Compilation)" while the rating scored a studio
release — and a later `finalise_scan` could flip a rating the scan had just posted.
The live path also never saw `releasetype`, the file-tag column a Navidrome import
writes into a blank row.

## The fix

**ONE list, ONE join, both readers.** `services/catalog/album_classification_service.py`
gains:

* `ALBUM_TYPE_KEYS` — every column that can carry a release type, in a stable read
  order, including **both** MusicBrainz spellings (`musicbrainz_albumtype` is the DB
  column, `musicbrainz_album_type` the in-memory album row's key) and `releasetype`.
* `joined_album_type(row)` — a JOIN, never a chain. Returns `""` for a row with no
  type at all, so a caller can tell "nothing declared a type" from "a type was
  supplied".
* `joined_album_type_from_rows(rows)` — the union across a scan's TRACK rows,
  deduped in first-seen order.
* `classify_compilation_category(..., type_text=None)` — a **pre-joined** string.
  When given it REPLACES `spotify_album_type`/`musicbrainz_album_type`, so a caller
  cannot pass both and double-join.

`detect_greatest_hits_album` gains a **second, anchored** rule:

```python
_GREATEST_HITS_TITLE_RE = re.compile(r"^\s*(?:the\s+)?hits\s*(?:[([].*)?$")
```

⚠️ **Anchored to the whole title, deliberately NOT a substring.** `release_categories`
and `release_cache_service` also call this function with a **TRACK title**, where
"Hits Different" is an ordinary song — a substring match would re-file the album it
sits on. `"Hits"`, `"The Hits"` and `"Hits (Deluxe)"` match; `"Hits Different"`,
`"Hitsville UK"`, `"Hits and Misses"`, `"Big Hits of the 80s"` and `"Dance Hits"`
do not. A qualifier BEFORE the word is **not** matched on purpose: the anchored form
cannot tell "Big Hits of the 80s" from "Hits Different", and a false positive
re-files an ordinary song's album, whereas a miss leaves the album rated exactly as
it was.

The live path (`scan_hooks.prepare_album_context`) now builds its type text from the
passed Spotify/MusicBrainz values **unioned with the track rows' own columns**, and
uses that JOIN for `album_type_from_field` — so the live verdict and the
greatest-hits guard read the same evidence the fallback reads. `prepare_tracks_for_album`
forwards an optional `type_text`. `_resolve_compilation_flags` drops its hand-rolled
six-line join for `joined_album_type(row)`.

## Tests

`tests/test_hits_routing_and_type_columns.py` — **33 tests**:

* the bare-`Hits` positives and the deliberate negatives (track-title shapes);
* **both readers agree** on the two reported albums, on the disagreeing-source case,
  and on a `releasetype` tag that says plain "album" (a tag stays authoritative over
  the title, in both paths);
* the `joined_album_type` / `joined_album_type_from_rows` helpers themselves;
* `classify_compilation_category`'s `type_text` REPLACES contract, plus a CONTROL
  that the old two-key callers are unchanged.

`tests/test_greatest_hits_artist_catalogue.py` gains `"Hits"`, `"The Hits"` and
`"Hits (Deluxe)"` in its parametrized `GREATEST_HITS_TITLES`.

### Oracle — the new tests are discriminators, not decoration

Sources reverted to `HEAD` with the suites kept:

| | failed |
|---|---|
| `HEAD` (sources reverted) | **45** |
| with the fix | **24** |

The 21-test difference is exactly the new module's 18 failures plus the 3 new
`GREATEST_HITS_TITLES` parameters — i.e. **every new assertion fails at `HEAD`**.
The remaining 24 are pre-existing failures in these suites, byte-identical between
the two runs (`test_per_album_star_posting` 8, `test_live_alternate_title_and_mbid` 4,
`test_release_category_persistence` 6, `test_release_categories` 1,
`test_compilation_more_5star` 2, `test_track_sync_and_live_detection` 2,
`test_album_type_persistence` 1) → **0 new failures**.

## Files

* `services/catalog/album_classification_service.py`
* `services/popularity/scan_hooks.py`
* `services/popularity/stages/finalise_stage.py`
* `tests/test_hits_routing_and_type_columns.py` (new)
* `tests/test_greatest_hits_artist_catalogue.py`
