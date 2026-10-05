# Track-page album links, edition matching, and two uncovered behaviours

**Date:** 2026-10-06 · **Area:** ui / downloads / scan
**Commit:** `fix(ui): the track page links albums by album artist`

## 1. The track page's album links went nowhere on a compilation

**Reported:** on a compilation — or any album whose track credit differs from
the release credit — the album link *and* the album art on the **track page**
are dead.

The album route resolves its artist segment with:

```sql
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
```

…i.e. the **album artist**, falling back to the track artist only when the
album artist is empty. The track page built those URLs from `track.artist`
(after `split_artist_collabs(...)[0]`), so:

| stored album_artist | stored artist | URL built | result |
|---|---|---|---|
| Various Artists | Midnight Oil | `/album/Midnight%20Oil/…` | no rows → dead page |
| Weezer | Rivers Cuomo | `/album/Rivers%20Cuomo/…` | dead page |

Every other album link in the app is built from the album's own artist; only
this page disagreed with the route.

**Fix** — both trees (`templates/pages/track_detail.html`,
`test_site/templates/Pages/track_detail.html`):

```jinja
{% set album_link_artist = track.album_artist or track.artist %}
```

used by all three URL sites (hero art, breadcrumb, album link). The value is
deliberately **not** split on collaborations: the route matches the stored
string *whole*, so `artist_parts[0]` would break an album whose stored credit
itself contains a feat. credit.

## 2. An edition download could never match its OWN queue item

`filename_matches_queue_item` normalised the two sides differently:

- the **filename** through `normalize_core_filename`, which *strips brackets*;
- the **queue title** through `normalize_match_text`, which only turns brackets
  into spaces and **keeps the words**.

So the score compared `"valhalla epic edition"` against `"feuerschwanz
valhalla"` — and because the edition gate above had already proved both sides
carry the same annotation (or neither), that gate carried no discriminating
information at this point. Two baseline failures came from it.

**Fix** (`services/downloads/match_engine.py`): `strip_brackets(title)` before
normalising, so both sides are reduced the same way.

## 3. Two behaviours nothing pinned (coverage only)

`tests/test_finalise_essential_refresh_and_dedup.py`:

- **The dedup winner.** `compute_track_artist_scores` merges in-memory scan
  results with the artist's DB history and promises to drop the DB copy of any
  `(album, title)` the scan already scored. If that ever flipped, the same song
  would count twice and drag the artist's distribution — and every star rating
  on a compilation — with it. The **scan result wins**.
- **An empty scan still refreshes the collections.** `finalise_scan(results=[])`
  (a scan that found nothing to rate) refreshes the Essential collections
  anyway, unless the scan already did. Otherwise a library that scans clean
  keeps last season's playlists forever.

## Verification

See the commit message for the suite numbers.

## Files

- `templates/pages/track_detail.html` + `test_site/templates/Pages/track_detail.html`
- `services/downloads/match_engine.py`
- `tests/test_track_detail_album_link_artist.py` (new)
- `tests/test_edition_download_matching.py`
- `tests/test_finalise_essential_refresh_and_dedup.py` (new)
