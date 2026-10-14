# Four scoring defects from the "Nightmare Revisited" scan (2026-10-14)

Reported from one VA-compilation scan (`Various Artists — Nightmare Revisited`,
21 tracks). The per-track-artist catalogue rating is the intended model for a
VA compilation; these four are mechanical defects around it.

## 1. The short-track cap was silently lost (the "Opening stays 4★" case)

`_short_track_cap_is_protected` read `_global_5star_locked` alone, so a locked
track was exempt from the clamp. But the COMPILATION branch **ignores** the
lock by design (it rates on the credited artist's catalogue — see the comment
above the lock check in `_assign_stars`), so for a compilation the lock never
drove the rating and must not exempt it.

Sequence that produced the log:

1. the per-album pass caps Danny Elfman's 60-second "Opening" 4★ → 2★;
2. the artist-boundary pre-pass then sets `_global_5star_locked` on 5 of the
   album's tracks (including "Opening");
3. the reconcile pass re-rates and the cap now SKIPS "Opening" — final 4★.

**Fix**: the lock is honoured only when it actually drove the rating
(`and not track.get("_compilation_rating_mode")`). A user override and a heart
stay protected on every path (they are separate checks).

## 2. Same-titled rows shared one ListenBrainz count

Two tracks named "Oogie Boogie's Song" (Rodrigo y Gabriela #13, Tiger Army #21)
both scored `LB: 26.3k`. The album-tracklist pass HAD distinguished them
(26285 vs 4669) and published each row's own numbers under the row alias
(`track_identity_key`), but `track_stage` read only the TITLE key — and the
title slot can hold one row's answer by definition.

**Fix**: `track_stage` reads the per-ROW alias FIRST (falling back to the title
key for a row with no usable position), and `scan_stage_runner` mirrors the
row's **counts** — not just its identity — into the row slot
(`listenbrainz_listens`/`listenbrainz_users`/`_album_tracklist`/`source`).
This completes the earlier row-identity work, which had mirrored only the
recording MBID.

## 3. Unicode punctuation never reached Last.fm

"Jack's Lament" (The All‐American Rejects) read LF **99** vs LB 44.2k, and
"Poor Jack" (Plain White T’s) LF **58** vs LB 23.3k. MusicBrainz writes a
U+2010 HYPHEN in "All‐American" and a U+2019 apostrophe in "T’s", and Last.fm
indexes the ASCII spelling — queried as-is the provider returns nothing.

`UNICODE_PUNCT_MAP` folded the curly quotes and the en/em dashes but **not
U+2010** (nor U+2011/U+2012), so even an explicit fold could not have helped.

**Fix**: the map gains U+2010/U+2011/U+2012, and both Last.fm aggregation
functions fold `artist` and `track_title` through
`normalize_unicode_punctuation` BEFORE building the query (the fold also keeps
the artist-catalogue cache key aligned with the spelling the provider knows).

## 4. The online-catalogue path skipped the verified-single floor

The local-catalogue branch (`max(comp_stars, 3)`) and the absolute fallback
both floor a confirmed single at 3★; the ONLINE path did not, so confirmed
singles were buried: "Making Christmas" (Rise Against, `single=high`) and
Tiger Army's "Oogie Boogie's Song" (`single=high`) both landed 2★.

**Fix**: the online path applies the same `max(comp_stars, 3)` floor for a
verified single (`high`/`medium`/`user`). The online rank still decides
anything above the floor — a #1 track still gets 5★.

## Tests
`tests/test_nightmare_revisited_scan_findings.py` (22):
* the cap protection (locked studio track still protected · locked compilation
  track not · user/heart protected · behavioural cap 4★ → 2★ on the exact case);
* the single floor (2★ → 3★ at #86/192 · control without the single · control
  low-confidence · control rank 1 → 5★);
* the row identity (alias read before the title key · counts mirrored ·
  the two positions produce two keys);
* the Unicode fold (U+2010/U+2011/U+2012/U+2019 · the provider is queried with
  the ASCII spelling · the raw spelling really would have missed · the search
  path folds too · an ASCII artist is unchanged).

**Oracle**: reverting the five source files (tests kept) → **10 failed /
12 passed** — the 12 are controls, exactly the split the fixes predict.

## Verification
Baseline-compared (identical failure sets, all pre-existing):
`test_compilation_online_catalogue` 1F/27P, `test_compilation_more_5star`
2F/2P, `test_album_release_track_identity` 1F/25P,
`test_track_sync_and_live_detection` 2F/13P, `test_per_album_star_posting`
8F/40P. Green: `test_greatest_hits_artist_catalogue` 20,
`test_short_track_star_cap` 28, `test_lastfm_aggregation` 8,
`test_release_credit_matching` 29, `test_lb_album_tracklist_translated_titles`
4, `test_sparse_library_position_match_guard` 3,
`test_compilation_cover_and_rating` 12, `test_two_phase_star_reconcile` 20,
`test_lastfm_diacritic_folding` 42, `test_live_variant_popularity_separation`
13, `test_va_compilation_release_mismatch_gate` 17,
`test_cover_work_first_detection` 11, `test_artist_alternate_names` 34,
`test_scan_identity_and_year_tags` 55,
`test_no_undefined_names_in_production` 7, `test_scan_db_first_persistence` 11.
`py_compile` clean on all five edited files.

## Assessed and NOT changed (reported in the same analysis)
* **Heavyweight penalty on VA compilations** (Korn #91/190 → 2★). The online
  percentile IS the intended model for a compilation (per the earlier "rate on
  the track artist's song popularity, not the album" requirement), and the
  alternatives (blend the album z, or floor album standouts) are a scoring
  MODEL change, not a defect. Needs a decision.
* **Last.fm pools same-titled tracks across an artist's albums** (Danny Elfman
  "Opening": LF 99.8k vs LB 3). Real provider semantics — LF counts are
  artist+title global. A guard would be a heuristic; needs a decision.
* **Duplicate "already up to date" lines per playlist.** The marker guard in
  `_run_global_playlist_rebuild` means the rebuild runs once per scan, and the
  log shape (each name twice within one batch) points at TWO Navidrome user
  entries for the same server — `_navidrome_clients()` builds one client per
  configured user, and each syncs the same server-side playlists. Confirm the
  config; a base_url dedupe is the fix if so.
