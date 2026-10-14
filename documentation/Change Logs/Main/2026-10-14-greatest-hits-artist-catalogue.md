# Greatest-hits albums rate on the artist catalogue (2026-10-14)

## What was reported
> Artist greatest hits compilations are meant to be scored on the artist
> catalogue with all tracks being able to score 5 star.

An external analysis of the same scan agreed, and described the symptom
exactly: a single-artist "Best Of" was scored **strictly against the album
median (49.99)** — the studio-album ladder — so almost nothing on it could
reach 5★.

## Root cause
`classify_compilation_category` reads the **type tag**, the generic
album-artist names and a "4+ credited artists" heuristic — **never the
title**. So a "Greatest Hits" album whose MusicBrainz/Spotify match carried no
compilation marking classified as `""` (not a compilation) and took the
studio ladder in `_assign_stars`:

* the album's own median band (`_album_z_band_star`), where every track sits
  around 50,
* the album-rank gate (`album_top_n`), which only lets the album's own top few
  qualify,
* the era 5★ slot cap — none of which belongs on a collection of hits.

Meanwhile the album page's category reader *already* fell back to the title
(`detect_greatest_hits_album`), so the page called the album a compilation
while the rating scored it as a studio album.

## The change
1. **`prepare_album_context`** (`services/popularity/scan_hooks.py`) now
   promotes a greatest-hits title to `is_compilation` +
   `is_single_artist_compilation`, which routes every track into the
   compilation branch of `_assign_stars` — the branch that rates against the
   artist's catalogue: the local z-score distribution when the library holds
   enough of it, otherwise the artist's **online** chart by real-world rank.
   That is what makes "all tracks able to score 5★" true: the top of the
   artist's catalogue is 5★ regardless of where a track sits on the
   compilation's own inflated tracklist.
2. **`_resolve_compilation_flags`** (`finalise_stage.py`) applies the same
   title rule, so the end-of-run `finalise_scan` grouping cannot re-rate the
   album as a studio album and wipe the rating the scan just posted. Its
   try/except was restructured so a **classifier exception** no longer skips
   the remaining sources (it used to return early and silently claim "studio
   album").
3. **Guards — the title is a FALLBACK, never an override:**
   * a **Various-Artists** record titled "Greatest Hits" keeps its VA flags
     (per-track artist context), and
   * a **matched album type stays authoritative** — an album the sources call
     plain "album" is a studio album whatever its title reads. This is the
     same convention the live verdict in the same function uses, and it is
     what the album page's category reader does. Only an album with **no type
     at all** lets the title decide, which is exactly the album that was being
     mis-rated.

## Tests
* **New** `tests/test_greatest_hits_artist_catalogue.py` (20): every title
  token promotes the album; a studio album is untouched; the VA guards hold
  (generic album artist, and the 4-credited-artists heuristic); the type tag
  wins over the title, including the real best-of that caught the first cut of
  this fix; `_assign_stars` gives a mid-album track 5★ through the artist
  catalogue where the studio ladder caps it; and the finalise fallback agrees
  — including when the classifier raises.
* **Oracle**: reverting both source files (keeping the tests) fails 9 of the
  20 — the 11 that still pass are the controls, which is the point.

## Verification
Baseline comparison against `origin/develop` (identical failure sets, so zero
regressions): `test_compilation_more_5star` 2F/2P, `test_compilation_online_catalogue`
1F/27P, `test_track_sync_and_live_detection` 2F/13P (all pre-existing),
`test_release_categories` 1F/99P (pre-existing). Green:
`test_compilation_cover_and_rating` 12, `test_soundtrack_artist_and_type` 26,
`test_various_artists_genre_isolation` 7, `test_forced_pipeline_routing` 9,
`test_navidrome_import_reads_file_tags` 35.

## Known follow-up (unchanged by this commit)
* An album the sources positively type as plain "album" is still rated as a
  studio album. If a reported release turns out to be in that state, the fix
  is to relax the `not album_type_from_field` / `not type_text` guards — but
  that trades a real risk of mis-promoting studio albums whose titles contain
  "collection"/"singles", so it needs a deliberate decision.
* The dead `single_detection.compilation_online_catalogue.min_local_catalogue`
  option: the compilation branch still hardcodes `has_usable_catalogue = False`
  ("force online catalogue lookup"). Wiring it up was measured to break
  `test_compilation_more_5star::test_many_hits_all_stay_five` (3 == 5),
  because the local distribution contains the compilation's OWN tracks — a
  correct fix must exclude them first. Left as-is.
