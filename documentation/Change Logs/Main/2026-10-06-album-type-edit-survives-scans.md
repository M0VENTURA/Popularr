# Album metadata edits survive scans (Album Type no longer reverts)

**Date:** 2026-10-06 · **Area:** album / scan
**Commit:** `fix(album): a chosen Album Type survives the next scan`

## Reported

> I'm updating the metadata for albums, but something is bringing back the old
> versions. I thought it was due to Navidrome reimporting it, but Navidrome is
> showing full information.

Selected as reverting: **Album Type**, Release year / dates, Album title /
album artist, and *"Everything I set"*, noticed *"a few hours later … seems to
be from Navidrome scan"*.

## What the evidence ruled out

The pasted Navidrome **Get Info** for
`/music/Live/1994 - Throwing Copper/01 - Līve - The Dam at Otter Creek.mp3`
matches the album page wherever both are known:

| field | album page | file |
|---|---|---|
| Album Title | Throwing Copper | `album:Throwing Copper` ✓ |
| Record Label | Radioactive Records | `recordlabel:Radioactive Records` ✓ |
| Genres | 8 chips | `genre:` 8 values ✓ |

so the edits **do** reach the file — the database is what loses them. Also
checked and cleared:

* the save writes **both** stores (`insert_or_update_track` + `update_file_tags`);
* empty form fields are never written (`for field, value in release_values.items(): if value:`), so a save cannot blank its own fields;
* `_persist_release_extended_fields` is **fill-only** (`CASE WHEN … = '' THEN`);
* `PRESERVE_WHEN_EMPTY_FIELDS` keeps a Navidrome import from blanking the tags its Subsonic API cannot carry.

## The proven mechanism — Album Type

The album page's select reads **`musicbrainz_albumtype` first**:

```jinja
{% set album_type_raw = album_data.spotify_album_type ... %}
album_data["spotify_album_type"] = first_value("musicbrainz_albumtype", "spotify_album_type", "album_type")
```

and the Save writes `musicbrainz_albumtype` + `spotify_album_type`. But
`_persist_album_type_to_tracks` (run by **every popularity scan**) selected
*every track whose stored value merely **differed** from MusicBrainz's* and
overwrote all three columns with the MB value. So:

1. user picks *Album (Soundtrack)* → DB written → the page shows it;
2. hours later a scan detects `album` from MusicBrainz;
3. every track is "pending" (they differ) → `musicbrainz_albumtype = 'album'`;
4. the select reads it back as the **old version**.

The file never carried the choice either — the Save did not write `releasetype`
— so Navidrome kept reporting the original type, which is why "Navidrome is
showing full information" (but old).

## The change

**`services/popularity/stages/album_stage.py`** — `_persist_album_type_to_tracks`
is now **fill-only**, and enforced **in SQL**, not in the caller's data:

```sql
WHERE CAST(id AS TEXT) IN :track_ids
  AND COALESCE(NULLIF(TRIM(musicbrainz_albumtype), ''), '') = ''
```

A track with no type is still filled (that is what the detection is for, and it
is how the field gets populated for albums nobody edited); a track that already
has one keeps it.

*Enforcing it in the WHERE clause matters:* the old `pending` list was computed
from the **in-memory rows the caller passed**, so a caller that did not load the
column was treated as "empty" and the row was overwritten anyway — the first
draft of the test reproduced exactly that.

**`routes/ui_routes.py`** — the Save now also writes
`releasetype = normalize_primary_release_type(album_type)`, so the **file** tag
agrees with the DB and Navidrome reads back the type that was chosen (it is the
primary form, matching what the scan writes to that column).

### Deliberate trade-off

Once a type exists, the scan will not change it — the first detection (or the
last human choice) wins until someone edits it again. MusicBrainz corrections
arrive through the album page's **Lookup MBID / Compare**, not by silently
overwriting a field the user can see and set.

## Tests

`tests/test_album_type_persistence.py` — new `TestAlbumTypePersistenceIsFillOnly`
(**2**, DB round-trip):

* a set type survives a disagreeing MusicBrainz value (the reported revert);
* CONTROL — a track with no type is still filled.

`tests/test_album_save_reports_changes.py` — the existing
`TestAlbumFieldsAreApplied` parametrisation gains
`payload["releasetype"] = normalize_primary_release_type(album_type)`.

**Oracle:** stashing the two source files → **2 failed, 10 passed** = exactly
the new tests; markers restored; stash count back to 3.

## Findings left for follow-up (not fixed here)

* **The Navidrome sync can overwrite user-edited identity fields.**
  `_POPULARITY_PROTECTED_COLUMNS` covers scores, ratings, single-detection,
  provider genres and album type — but **not** `album`, `album_artist`,
  `title`, `artist`, `year`, `genres`, `recordlabel`, `originalyear`,
  `originaldate`, `releasedate`. A sync whose values lag the file (Navidrome
  has not re-scanned since Popularr wrote tags) would restore the older ones.
* **`release_year` is never written by the sync** — it only carries `year` /
  `spotify_release_date`, so the form's *Release Year* box stays empty for
  imported albums even when the file holds a date.

Both need a freshness signal (Navidrome's `updatedAt` vs the file mtime) to
distinguish "the file really changed" from "the mirror is stale".

## Verification

- album-type + save tests: **34 passed** (the 1 remaining failure,
  `TestEnsureAlbumTypeReturnsVerdict::test_detects_when_missing`, is
  **pre-existing** — reproduced with both source files stashed)
- **Oracle:** stashing the two source files → **2 failed, 10 passed** = exactly
  the new tests; markers restored; stash count back to 3
- affected set (40 files): 819 passed / 62 failed → **0 new vs baseline**
- full suite: **225 failed, 4633 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (the only ID absent from the
  baseline is the known native-flaky `test_sibling_torrents_root_is_searched`)

## Files

- `services/popularity/stages/album_stage.py`
- `routes/ui_routes.py`
- `tests/test_album_type_persistence.py`
- `tests/test_album_save_reports_changes.py`
