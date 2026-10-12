# 2026-10-12 — A batch position alias no longer overwrites the track's real artist

## Report

On a Various Artists compilation with NO album MBID, the ListenBrainz
album-tracklist prefetch resolved a DIFFERENT edition (46 tracks on the release
vs 80 local tracks), so track numbers misaligned. The position alias matched
local "Where Is My Mind?" to release slot "Happy" (artist "Ned's Atomic
Dustbin"). Because the ARTIST write ran unconditionally, the track's real
performer ("Pixies") was overwritten by the slot's artist — which then fed a
`recording.search` that returned nothing (`candidate_count=0`) and the track
saved NO recording MBID.

The TITLE was already protected (`d061adfd`: only a confirmed track#+length
match auto-applies the release title; everything else defers to the approval
panel). The ARTIST was not.

## Fix

`track_stage._resolve_track_mb_metadata` now computes one
`_batch_artist_untrustworthy` decision from the SAME flag/condition the title
gate uses — never a second independent check:

- a batch position alias whose release title DIFFERS from the local title AND
  whose identity is NOT confirmed (`_title_match_confirmed` False) is treated
  as pointing at the WRONG slot → its artist AND `artist_mbid` are NOT written
  (logged `[MB] batch artist ignored — identity not confirmed`);
- a CONFIRMED alias still replaces a placeholder / performs the force-meta
  correction (unchanged);
- a search-resolved (non-batch) identity is untouched — the search ran with
  the local title+artist, so its artist IS the match;
- the position-derived `recording_mbid` STILL lands (downstream listen counts
  read through it) — only the artist + artist-MBID halves are gated.

This is the second half of the `d061adfd`/`2cbafa9c` family: titles were
protected, the artist wasn't. Now both obey the same trust rule.

## Tests

`tests/test_va_track_artist_not_clobbered_by_unconfirmed_alias.py` (10):

- unconfirmed mismatched alias: artist NOT written, artist_mbid NOT written,
  placeholder NOT filled, but recording_mbid still applied;
- confirmed alias: placeholder replaced, force-meta correction works, artist
  MBID written;
- search-resolved (non-batch) control: recording id lands, nothing suppressed;
- source contract: one `_batch_artist_untrustworthy` definition sharing
  `_title_match_confirmed`; the recording-MBID write is NOT gated.

**Oracle** (track_stage stashed, tests kept): **4 failed / 6 passed** — the 4
discriminators (mismatched artist/mbid, placeholder-not-filled, gate source)
fail; the 6 controls pass. The pre-fix oracle log shows exactly the reported
defect (`Compilation placeholder artist replaced ... new=Ned's Atomic Dustbin
old=Various Artists`).

**Regression sweep** (12 title/artist/identity suites): the 9 failing ids on
the changed tree are BYTE-IDENTICAL to the stashed baseline — all documented
pre-existing (the singles-detection red area, `test_detects_when_missing`, the
artist-constraint docstring pin, `test_identity_is_marked_unambiguous`).
0 regressions. `import app` → 399 routes.