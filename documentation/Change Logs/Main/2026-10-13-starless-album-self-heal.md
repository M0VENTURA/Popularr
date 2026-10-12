# Starless-but-scored albums self-heal on the next non-forced scan (2026-10-13)

## Reports

1. **Feature question (answered):** "Doing an artist scan, it goes through
   each album, but doesn't post final results until the entire artist is
   processed. Is this the best way?" — **Yes: it is deliberate and correct.**
   Star ratings are ARTIST-RELATIVE by design (artist z-scores, top-percentile
   `popularity_marked` bands, and the global 5★ pre-pass all need the WHOLE
   artist pool). Posting per album would rate album 3 of 10 against a 3-album
   pool — a different distribution, wrong stars, then a re-rate. It would also
   recompute the artist's cutoffs 200× for a 200-album Various Artists artist.
   Everything ELSE is persisted per album (scores, singles, genres); only the
   star column waits for the artist boundary. Graceful Stop / abandoned-artist
   paths flush the in-progress artist's completed albums before unwinding.

2. **The one real residual gap (now fixed):** a **hard-killed** run (or any
   interruption between per-album score persistence and the deferred artist
   flush) leaves completed albums scored-but-starless. The next non-forced
   scan's skip gate treats them as "no changes detected" (all tracks carry
   `final_score`) and skips them → "when an artist was rescanned with a non
   forced scan later on, it runs the finalize pass on it and no tracks are
   getting 5 star in the scan" — their stars stay NULL forever until a
   forced/Finalise pass.

## Fix (`services/popularity/scan_stage_runner.py`)

* **`_album_scored_but_never_rated(tracks)`** — recognises the interrupted
  signature: at least one non-`exclude_from_stats` track has
  `final_score > 0` AND no track has `stars > 0`.

* **Skip-gate wiring** — after the completeness override and the singles gap
  fill, `if skip_album and not options.get("metadata_only") and
  _album_scored_but_never_rated(tracks):` clears the skip (logged
  "scored but never rated (no stars) — running it to assign them").

  The healed album runs the loop with **stored scores reused**
  (`process_track`'s cached path keeps `raw_score`, so there is no re-scoring
  and no re-metadata — the heal deliberately does NOT set
  `force_metadata_for_this_album`), reaches `_flush_artist_star_ratings` at
  the artist close, and `_post_album_stars` assigns the stars. The anti-erosion
  raw-score contract is pinned so the heal can rely on it.

  **Metadata-only passes are excluded** (they never rate, so forcing them
  would re-run those albums forever). Untouched: a never-scored album, a
  partially-rated album, an all-excluded album.

## Tests — `tests/test_starless_album_self_heal.py` (10)

helper signature (+ rated / partially-rated / unscored / all-excluded / empty
/ live untouched-or-heal cases); wiring (heal in the skip gate, metadata-only
excluded, no `force_metadata_for_this_album` set, and the stored-score cached
path pinned). **Oracle** — `return False` in the helper fails the two
signature tests while the controls stay green.

**Sweep** — 111 missing/album/scan/metadata suites: 59 failures **identical
to the pristine baseline** → 0 regressions.

## Note for the user

* The deferred star posting stays (per-album posting would be wrong); the
  gap it left on a hard interrupt is now self-closing — the next non-forced
  scan of that artist re-rates the starless albums automatically.
* The leftover path for a HALF-posted album (some tracks starred, some not —
  intentionally NOT forced through) is still a Finalise pass, which re-posts
  every album from stored data.