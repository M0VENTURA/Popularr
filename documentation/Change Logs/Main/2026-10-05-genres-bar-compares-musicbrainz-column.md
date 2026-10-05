# The Genres bar compares the MusicBrainz column, not the main genres (2026-10-05)

**Report:**

> The changes show Genres, but the Genres should only be comparing and
> adjusting the Musicbrainz genres field not the main genres field.
>
> **MusicBrainz:** Genres: *arena rock, ballad, classic rock, hard rock, piano
> rock, pop, pop rock, rock, soft rock, symphonic rock, heavy metal, In Love,
> Other, rock opera* → **arena rock, ballad, classic rock, hard rock, piano
> rock, pop, pop rock, rock, soft rock, symphonic rock**

## What was actually happening

The track-level Genres bar used the track's **main** `genres` column as the
"current" side and MusicBrainz's genres as the proposed side, while the field
key behind it — and therefore everything that *writes* it — was
`musicbrainz_genres`:

| | value used | written on save |
|---|---|---|
| current (italic) | `tracks.genres` | — |
| proposed (bold) | MusicBrainz genres | `tracks.musicbrainz_genres` |

So the bar depicted a change to a field it never touches. The save path has
always been MB-only: `_STAGED_WRITABLE` in `routes/ui_routes.py` contains
`musicbrainz_genres` and **not** `genres`, and the per-field Apply route
(`_APPLIABLE_MB_FIELDS`) does not offer genres at all. Meanwhile the main
`genres` column is an aggregate of several sources — the example's
`heavy metal, In Love, Other, rock opera` are exactly the sort of extra tags
that MusicBrainz never supplied — so comparing it against MusicBrainz produces
a bar that can never be honoured.

Confirmed with the reporter: **track rows only** (the album-level `album_genres`
proposal is untouched) and **stored MusicBrainz genres vs MusicBrainz**.

## The reversal

This deliberately reverses an earlier fix in this same file, which moved the
comparison the *other* way after a report that *"it will only compare the
musicbrainz genre table, but the current genres attached to the tracks"*.
Both readings have now been asked for at different times; the contract is
recorded in the code comment so the next reader sees the whole history rather
than only the most recent instruction.

The bar now means one thing: **your stored MusicBrainz genres are stale.**

## Changes

- `services/metadata/metadata_proposal_service.py`
  - `current_map["musicbrainz_genres"]` reads `local["musicbrainz_genres"]`
    (the JSONB column) instead of `local["genres"]`;
  - the surrounding comment rewritten to record both decisions and why the
    contract flipped.

Nothing else changes: the field key, the label, the unordered-set comparison
(`_genre_sets_equal`, which still normalises JSONB-vs-string, order, case and
spacing) and the write path were already MB-only.

## Tests

`tests/test_metadata_compare_version_markers_and_genres.py` — the class was
rewritten as `TestGenresCompareTheMusicBrainzColumn` (86 tests in the file):

- **the reported example verbatim** — own genres differing while the MB column
  already matches must produce **no** bar;
- the reported `current` is the stored MB column, never the main genres;
- a stale MB column is still reported (control);
- an unpopulated MB column is filled in, with `current == ""`;
- identical genres stay quiet; same genres in a different order/encoding stay
  quiet; a real difference is still reported (all now driven from
  `musicbrainz_genres=`);
- the `_genre_sets_equal` unit tests are unchanged.

Oracle: reverting `metadata_proposal_service.py` fails **9/11** in the class
(the 11 that pass are the helper's own unit tests).
