# Popularity report review: two real defects, and what the rest actually is

**Date:** 2026-10-10 · **Area:** `popularity` / `lastfm` / `report`

## The review

The AFI popularity report (238 tracks) was analysed by an external tool, which
raised seven anomalies. Verdict on each, with the code-level mechanism:

| # | Anomaly | Verdict |
|---|---|---|
| 1 | Live track outranks Miss Murder | **Real, fixed** — see below (ordering) |
| 2 | 53 tracks: listeners > 0 with playcount 0 | **Real, fixed** — see below (playcount) |
| 3 | 24 tracks with zero ListenBrainz data | By design — LB coverage of live/demo recordings is sparse; "Girl's Not Grey (Live)" etc. genuinely have no scrobbling of those recordings |
| 4 | Bottom three tracks score 0.00 with thousands of listeners | Stale score + refreshed listener cache (the album-tracklist pass writes listeners without scoring); a forced re-scan recomputes — the stored numbers were not produced by one pass |
| 5 | Missing `disc_number` on 39 rows | By design — the single-disc strip clears it, and the affected albums are bootleg/odd releases ("The Weight of Words", the "AFI" comp) |
| 6 | Duplicate/variant entries ("Who Knew?" twice, "Reiver's"/"Reivers'") | Data, not scoring — the same song exists on several releases in the library |
| 7 | "Afi feat. Wings" as a separate artist row, 5★ on a remix | Feat.-relocation artifact (2026-09-21 feature) — the credit lives on the artist field; its per-track-artist catalogue is one track, which is the thin-catalogue path. Album-normalisation still runs under "Afi" (album_artist), so the rank/stars here come from the studio album's distribution |

## Defect 1 — the report ranked an ALBUM-RELATIVE score across albums

`final_score` is `apply_album_relative_popularity(raw)` — it re-normalises
EVERY album onto the same band. The live album's peak ("Totalimmortal (Live)",
4,660 Last.fm listeners + 168k ListenBrainz) therefore read **97.68** and
ranked #1, while "Miss Murder" (1.1M listeners) read 88.7. "Silver and Cold
(Live)" (7,172 listeners, zero LB) hit #5 at 83.12 the same way: the live
album's distribution is uniformly small, so its middle tracks remap high.

The report promises **"most popular first"**. The codebase already knows the
album-relative value is wrong for cross-album ordering — that is why
`playlist_popularity_score` exists and why `raw_score` was added (migration
016: "the PRE-remap blend… the whole point of raw_score"). The report now
orders by `COALESCE(NULLIF(raw_score, 0), final_score, popularity, stars, 0)
DESC` — the cross-album blend leads, the remapped value is still shown in its
column, and pre-016 rows fall back to the old chain.

The star ratings were NOT changed: 4★ on "Totalimmortal (Live)" is the live
album's **album-z promotion path** (a decisive standout within its own live
record at ≥2.0 album-z with a respectable ≥0.5 artist-z → 4★), which is
deliberate — it is what lets a live peak be recognised when the studio
catalogue dominates.

## Defect 2 — `track.search` was stripping the playcount

`search_track()` mapped every match to `name`/`artist`/`listeners`/`url` and
dropped **playcount**, which the raw API returns in the same payload. Every
track resolved through the search path (outside the artist's top-200
catalogue: live renditions, bootlegs, feat. variants, demos) then stored
listeners > 0 with playcount **0 for ever** — the 53 rows. The same hole
existed in the `get_track_info` search fallback when the follow-up
`track.getInfo` came back empty. Both now keep the playcount.

Playcount is currently **display/report data only** — the scoring math uses
listeners — so nothing re-ran; existing rows keep their 0 until a re-scan
recomputes them.

## Tests

- `tests/test_search_track_playcount.py` (6): the mapping carries both
  numbers; a match without playcount maps to 0; the search aggregation sums
  both; multiple variants sum; the getInfo-search fallback keeps the search
  playcount when getInfo is empty; a real getInfo playcount still wins.
- `tests/test_artist_popularity_report.py` (+2): the AFI case (live cut with a
  high remapped score sorts BELOW the studio hit on raw), and the legacy
  fallback control.

**Oracle:** reverting the two source files → 5 failed (3 playcount + the
ordering case; the legacy control passes both sides by design); restored →
20 passed.

**Sweep:** 14 test files grepping the changed modules, one process each —
baseline 30 failing IDs vs changed 26, `Compare-Object`: **0 new**, 4 gone
(= the fixes). `test_compilation_online_catalogue`'s healthy-catalogue failure
reproduces identically on the clean baseline (pre-existing).

## Not changed, flagged

- The artist PAGE's own top-tracks ordering uses the same album-relative
  `final_score` chain — the report was designed to agree with it, and they now
  legitimately disagree (the report fixes its promise, the page keeps its
  chain). Aligning the page is a one-line follow-up if wanted.
- The three 0.0-score tracks and the live album's scores need a **forced
  re-scan** to recompute; the report/playcount fixes only affect future writes.