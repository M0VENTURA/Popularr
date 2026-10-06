# Track genres outrank album genres on save (2026-10-07)

## Reported

> Album genres seem to be overwriting track genres when an album is saved.
> Genres attached to tracks for Musicbrainz, Last.fm, etc should have a
> greater preference than genres saved to albums.

## Root cause

Two compounding writes in the album-save loop (`routes/ui_routes.py`):

1. `album_genres` is the hidden input behind the album's genre chips, and it
   is **prefilled with `collect_top_genres(tracks)`** — an aggregate of the
   tracks' own source columns (`manual_genres`, `navidrome_genres`,
   `musicbrainz_genres`, `lastfm_tags`, `discogs_genres`, `essentia_genres`,
   `listenbrainz_genres`, …) across the album, capped at 30. Saving the album
   wrote that aggregate back to **every** track's `genres`, so each track's
   precise list was replaced by the album blend on every save — even when the
   user never touched the genre chips.
2. The same repository call (`update_track_genres`) also wrote the album
   string into **`manual_genres`** — a per-track *source* column that
   `collect_top_genres` and the genre aggregators read back. Every track then
   claimed the album blend as its own manual genres, so the overwrite
   disguised itself as track-level evidence: from then on the blend counted
   as "the track's genres" and survived every later save.

The tracks' source columns (`musicbrainz_genres`, `lastfm_tags`, …) were
never touched — only the display column `genres` and the fabricated
`manual_genres` — but that is exactly what the user sees as "track genres
overwritten".

## Fix

**Precedence rule:** a track with ANY genre evidence of its own keeps it; the
album's list only *fills* a track that has no genres at all.

- `_TRACK_GENRE_EVIDENCE_FIELDS` + `_has_genre_value()` +
  `_track_keeps_its_own_genres(track, staged)` in `routes/ui_routes.py` — the
  evidence set is every track-level genre column, plus a **staged** per-track
  `musicbrainz_genres` from the Lookup-MBID review (which lands later in the
  same save and must not be contradicted by the album list winning the race).
- The save loop skips the genre write for a track that keeps its own
  (`genre_tracks_kept`), and `_apply_album_track_genres` now calls
  `update_track_genres(..., write_manual=False)` — the album's list lands in
  `genres` alone and **never** in `manual_genres`.
- `update_track_genres` gained a keyword-only `write_manual: bool = True`
  (default unchanged, so `apply_genres_to_album` and the other callers behave
  exactly as before).
- Honest reporting: the flash now appends
  `· N track(s) kept their own track-level genres.` when some were kept, and a
  genres-only save where *every* track kept its own says
  `Album genres not applied — every track has its own track-level genres`
  instead of the misleading `No changes were made.`

**Deliberately untouched:**

- `/api/album/apply-genres` (an explicit "write these genres to every file"
  action) still applies unconditionally — an explicit override is not the
  accidental fan-out the report describes.
- The scan-time "active genre cleanup" (`album_tag_sync_service` →
  `get_track_recommendations`) still distributes an album-level blend for
  non-VA albums by design (it feeds clean Navidrome playlists); VA albums
  already use the per-track path. Flagged in the reply as a possible
  follow-up if the same symptom is seen after scans.
- Rows saved *before* this fix may still carry a stale album blend in
  `manual_genres`/`genres`; there is no safe automatic repair (a genuine
  per-track manual genre is indistinguishable from a polluted one), so old
  values are left for the user to edit.

## Tests

`tests/test_album_save_genre_precedence.py` (29):

- precedence rule: every source column wins; empty markers (`None`, `""`,
  `[]`, `"[]"`, `"null"`) are not evidence; a bare track loses; a staged MB
  genre counts as evidence;
- real-DB write: the fill sets `genres` and leaves `manual_genres` NULL;
  control — `update_track_genres` with its default still writes the pair;
- end-to-end album POST (real route + real repository write): a track with
  MusicBrainz genres keeps them, a bare track receives the album list, and no
  `manual_genres` is fabricated;
- source guards: the kept-genres branch and `write_manual=False` exist.

Existing suites adjusted (behaviour, not meaning): `_patch_genres` accepts
the new kwarg; the flash-ladder window test grew 1600 → 2400 chars to keep
the whole ladder in view.

## Oracle

Stashed `routes/ui_routes.py` + `db/repositories/metadata.py` →
**27 failed / 2 passed** (the 2 are the intended controls: the fill still
worked before, and the default manual write is unchanged) → popped → markers
present → stash list back to 3.

## Verification

- Affected set (`ui_routes` / `update_track_genres` / `album_genres` /
  `genre_only_writes`): 24 files, 3 failed / 501 passed → **0 new** vs the
  baseline subset (all 3 are baseline ids).
- Full suite → baseline reconciliation (`_gprec_full.txt`).
