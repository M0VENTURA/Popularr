# The MBID lookup now offers BOTH the track and the album artist

**Date:** 2026-10-05 · **Area:** metadata / album review
**Commit:** `fix(metadata): the MBID lookup offers both track and album artist`

## Request

> *"MBID should have both the track and album artist available."*

## What was already there

The **album** artist. `_ALBUM_FIELD_SPECS` has always carried
`("album_artist", "Album Artist", "artist")`, so the review panel has always
been able to propose who *released* the record.

The **track** artist never was. `_TRACK_FIELD_SPECS` was exactly:

```
title, track_number, disc_number, mbid, writer, musicbrainz_genres
```

So a lookup could tell you who pressed the single but never who *performs*
each recording — the half that actually changes per track on a compilation.

## The data was already being fetched

`_flatten_release` has been writing a per-track `artist` the whole time:

```python
# Per-track artist: the recording's own credit when it has one,
# otherwise the release's joined credit (NOT the primary only —
# a featured credit belongs on the track).
Track_artist = (
    build_artist_credit_string(Recording.get("artist-credit") or [])
    if Recording.get("artist-credit")
    else ""
) or Joined_artist_credit or Primary_artist
entry["artist"] = Track_artist
```

It was surfaced nowhere.

## The change

**`services/metadata/metadata_proposal_service.py`**

- `_TRACK_FIELD_SPECS` += `("artist", "Track Artist", "artist")`.
- `current_map["artist"]` reads the local row; `proposed_map["artist"]` reads
  the **enrichment entry** (per-recording), not the album — so a compilation
  proposes each performer's own name.
- **The guard:** a proposed track artist *equal to the album artist* is never
  offered (see below).

**`routes/ui_routes.py`**

- `_STAGED_WRITABLE` += `"artist"` — Apply writes the staged track artist to
  both the database and the file tag (`_COLUMN_TO_TAG_FIELD` already had
  `"artist": "artist"`, so no tag-layer change was needed).

Both artists are now side by side: the album credit at album level, the
performer at track level.

## Why the guard is the point

Because of that fallback. MusicBrainz does **not** report whether a track
credit was the recording's own or the release's fallback — the code only
knows which string came out. On a compilation whose recording carries no
credit of its own, `Track_artist` falls back to `Joined_artist_credit`, i.e.
the release credit. Proposing that would offer **`Various Artists`** for every
track and, on Apply, stamp it over each performer.

That is precisely the bug the suite already guards:

> `tests/test_va_track_artist_preserved.py`
> *"Track artist is being incorrectly overwritten on Various Artists
> compilations as Various Artist. It's meant to be set as the Track Artist of
> the song."*

The guard is deliberately narrow — it only blocks a proposed value that equals
the **album** credit:

| Case | Proposed | Album | Result |
|---|---|---|---|
| Real per-track credit differs | `Midnight Oil` | `Various Artists` | **proposed** ✓ |
| Compilation fallback (the bug) | `Various Artists` | `Various Artists` | blocked ✓ |
| Same as what's stored | `X` == local `X` | — | blocked by equality (pre-existing) |

**Known limitation:** a track whose local artist is empty (or wrong) *and*
whose correct value equals the album credit is not auto-filled either — MusicBrainz
gives no way to tell a genuine credit from the fallback. Stamping a release
credit over a real performer was the reported bug; losing that one fill is the
accepted cost.

## Tests

`tests/test_mb_lookup_track_artist.py` — **7 new**:

- both artists are present in their respective specs (album must not regress)
- a differing track artist raises a bar with the right `current`/`proposed`/`label`
- the same artist raises nothing (control)
- **the album credit is not offered as a track artist** — the VA bug
- a real compilation performer **is** still offered (the guard must not swallow
  the feature)
- a blank proposal never wipes a real value

`tests/test_album_review_save_persists.py` — **2 new**:
`test_staged_artist_is_written` (DB) and `test_staged_artist_reaches_the_file`
(tag), mirroring the existing staged-field pairs.

## Verification

- Targeted (proposal + save + VA-preserved + markers/genres): **131 passed**
- Oracle — stashing only the two source files: **5 failed, 31 passed** = exactly
  the 5 tests that depend on the change; markers restored; stash count back to 3.
- Affected set (26 proposal/staged/tag files): **656 passed, 9 failed** —
  **0 new vs the 303-failure baseline**.
- Full suite: **227 failed, 4595 passed, 2 skipped** in 11m04s against the same
  303-failure baseline → **78 fixed, 0 real regressions** (the only 2 IDs not in
  the baseline are the known native-flaky `test_sibling_torrents_root_is_searched`
  and the `test_edition_file_matches_edition_queue` class rename from an earlier
  commit — both already known before this change).

## Files

- `services/metadata/metadata_proposal_service.py`
- `routes/ui_routes.py`
- `tests/test_mb_lookup_track_artist.py` (new)
- `tests/test_album_review_save_persists.py`
