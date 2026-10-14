# Track artists now identify same-titled release rows (2026-10-14)

## What was reported
> It looks like track artists are being ignored when matching mbid's via
> listenbrainz and only using album artists

The scan log showed the symptom precisely: seven rows all logged as
`Hypa Hypa`, then

```
[scan_runner] Album-tracklist LB match for 'Hypa Hypa' (Electric Callboy - MMXX): 186 listens (release=186, kept=release)   ×6
```

— six different tracks reading one version's number.

## What the release actually is
`MMXX - Hypa Hypa Edition` (MusicBrainz `5a449751-55e6-41d9-a1e6-93d8934f33e4`,
13 tracks) contains **eight "Hypa Hypa" tracks**: seven share the plain title
(the original, then Saltatio Mortis, The BossHoss, 257ers, Sasha, We Butter the
Bread With Butter, Axel One) and the eighth is the Gestört aber GeiL remix.
**Only the artist credit tells them apart** — and the library carries that
credit in each track's own `artist` field.

## Two defects

### 1. The consumer read the TITLE key (the reported wrong data)
`get_listenbrainz_album_tracklist_with_release` already emitted a
position-qualified per-row alias (`track_identity_key`, e.g. `hypa hypa#1:9`),
and `_build_album_recording_batch` already read that alias FIRST — but the
album-tracklist cache loop in `scan_stage_runner` read the plain title key and
mirrored the result into the title slot. Eight rows sharing one key meant the
**last row processed spoke for all of them**: one recording and one listen
count (186) for six tracks.

### 2. Nothing compared artist credits (the reported cause)
The release tracklist was not even **indexed** with credits, and the
MusicBrainz tracklist fetch asked for `inc="recordings"` — no `artist-credits`
— so there was nothing to compare. Position + duration was the only
discriminator; where it cannot decide (a mis-numbered folder, a different
edition, missing track numbers) the row had no second opinion.

## The change (`services/popularity/popularity_sources.py`)
1. **Credits are captured**: `_release_credit_text` joins a MusicBrainz/LB
   `artist-credit` list into one display string ("Electric Callboy, Saltatio
   Mortis"); `_index_release_tracklist` keeps a per-ROW record
   (`entry["rows"]`) and stamps the position entries too.
2. **Credits are requested**: the MB tracklist fetch now uses
   `inc="recordings+artist-credits"`. (Probed: the LB release metadata endpoint
   404s, so this MB path is the one that actually answers — the "fallback" is
   the normal path.)
3. **A new artist-disambiguation pass** runs before the position pass: for a
   title the release lists more than once, the row's OWN artist is matched
   against the candidate credits and an unambiguous match identifies the row
   outright, published under the row's position-qualified alias.
   Conservative by construction:
   * fires only for a multi-row title (a unique title is already answered);
   * requires EXACT credit-token equality — `names_match`'s subset logic would
     call a plain "Electric Callboy" a match for every featured version;
   * refuses when the local credit is a strict SUBSET of a candidate it did
     not match (a plainly tagged "Electric Callboy" could be that row's short
     form) — that ambiguity stays with position + duration;
   * `length_verified` keeps the position pass's meaning (both lengths known
     AND within ±5s), so the release title is still only auto-applied when the
     duration corroborates it.
   Credits resolve through the **artist-alias map**, so a credit under a band's
   former name ("Eskimo Callboy") matches the current one.
4. **The position pass no longer clobbers a SHARED title key**: the title key
   keeps the TITLE pass's deterministic answer (the release's first row with
   that title) instead of "whichever row ran last"; the per-row alias answers
   the individual rows.

## The change (`services/popularity/scan_stage_runner.py`)
5. The album-tracklist loop reads the per-row alias FIRST (mirroring
   `_build_album_recording_batch`), falls back to the title key, and mirrors
   the row's identity into its OWN slot so the batch builder — which reads the
   alias first — actually receives it. The log line now shows the row's own
   count and `via=row-alias|title`.

## Tests
`tests/test_release_credit_matching.py` (29) — credit joining/token equality
(join spellings equivalent, plain ≠ featured, alias-resolved), the index
carrying credits, **the reported case** (each of the seven plain-titled rows
resolves to its OWN recording and its OWN count; the title key keeps the title
pass's answer), the conservative refusals (plain credit, unknown artist,
missing position), a mis-numbered row rescued by its credit, the length guard,
the batch receiving per-row identities, and the consumer wiring.
**Oracle**: with both sources reverted (tests kept) → **18 failed / 11
passed** — the 11 are controls plus rows the position pass already handled,
which is exactly the split the report predicts.

## Verification
Baseline-compared: `test_album_release_track_identity` 1F/25P at BOTH
baseline and here (pre-existing `test_the_identity_is_marked_unambiguous` —
pinned to an older contract, unchanged by this fix). Green:
`test_lb_album_tracklist_translated_titles` 4,
`test_sparse_library_position_match_guard` 3,
`test_title_update_requires_confirmed_match` 12,
`test_va_track_artist_not_clobbered_by_unconfirmed_alias` 10,
`test_compilation_lb_release_vs_recording` 4,
`test_release_title_promotion_and_file_sync` 10,
`test_live_variant_popularity_separation` 13, `test_scan_db_first_speedups` 15,
`test_va_compilation_release_mismatch_gate` 17,
`test_compilation_cover_and_rating` 12,
`test_greatest_hits_artist_catalogue` 20, `test_artist_alternate_names` 34.
`py_compile` clean on both edited files.

## Known follow-up
The **popularity cache is title-keyed** (`track_popularity_cache`), so per-row
listen counts cannot round-trip through it for same-titled rows; the per-row
identity lives in the scan's memory and is persisted per track as
`tracks.recording_mbid`. Making the cache row-aware (a disc/track column) would
be a separate change.
