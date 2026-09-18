# Christmas music leaking into ordinary playlists

**Date:** 2026-09-19
**Area:** `playlists` / `popularity` / `catalog`

## Reported symptom

> Christmas songs are being added to playlists, but they were meant to be
> skipped from all playlists other than ones with "Christmas"

There *was* already a Christmas intercept in the genre-playlist builder. Four
independent gaps let tracks through it.

## Gap 1 — the check ran on a TRUNCATED genre list (primary cause)

`_genre_playlist_track_genres(row, max_genres=3)` returns at most three genres,
read from the **front** of the stored string. The genre aggregator intercepts
filter tags (Christmas / Live / Cover / Remaster) and **appends** them *after*
the voted genres, so a Christmas track with three real genres stores:

```
tracks.genres = "pop, rock, metal, Christmas"
               └──────────────────────┘  the builder's window sees only these
```

The builder then tested that window:

```python
norm_track_genres = [re.sub(...) for g in track_genres]   # ['pop','rock','metal']
is_christmas = "christmas" in norm_track_genres           # False
```

So the intercept never fired and the track pooled into the ordinary
Pop / Rock / Metal playlists. Measured against the real code:

```
stored genres     : 'pop, rock, metal, Christmas'
builder window(3) : ['pop', 'rock', 'metal']
is_christmas      : False   <== leaks into pop/rock/metal
BUT the full string DOES contain it: True
```

## Gap 2 — only the literal word "christmas" was recognised

The aggregator stores whichever spelling the **source** used, so a track can
legitimately carry `Holiday`, `Xmas`, `X-Mas`, `Noel`, `Yule`, `Yuletide`,
`Advent` or `Hanukkah` and be Christmas music. Testing for `"christmas"` alone
missed every one of those.

## Gap 3 — Essential Collection had no filter at all

`_sync_essential_playlist` selected every 4★+ track for the artist with no
seasonal check. A Christmas album put its tracks into that artist's Essential
Collection.

## Gap 4 — New Music had no filter at all

`_create_new_music_playlist` is a rolling "recently added" list, so anything
imported in December — i.e. all of it — surfaced there.

## Fixes

### 1. One canonical test — `album_classification_service.py`

```python
CHRISTMAS_GENRE_TOKENS = frozenset({...})   # christmas, xmas, holiday, noel, ...

def is_christmas_genre(genres) -> bool: ...  # whole-token match, handles the
                                             # string / JSON-list / list forms
def is_christmas_track(*, title, album, genres) -> bool:
    return detect_christmas_song(title, album) or is_christmas_genre(genres)
```

This module already owned `detect_christmas_song` and is imported by the
popularity stage, so the definition lives in one place instead of being
re-derived per caller. `is_christmas_genre` matches **whole tokens**, so the
bare `holiday` qualifies while the real genre `Holiday Rock` does not.

### 2. The genre builder checks the FULL stored value

```python
is_christmas = is_christmas_track(
    title=str(row.get("title") or ""),
    album=str(row.get("album") or ""),
    genres=row.get("genres"),      # <-- the full string, never the window
)
```

The `_GENRE_ROWS_SQL` query now also selects `album`, whose name is part of the
canonical test.

### 3. A name-based policy

```python
def _playlist_allows_christmas(playlist_name) -> bool:
    return _christmas_playlist_marker() in playlist_name.casefold()

def _filter_christmas_rows(rows, playlist_name) -> list[dict]: ...
```

Only a playlist whose **name** contains the marker (default `christmas`) may
hold Christmas music. This is what preserves `Christmas - Top Tracks` and
`Christmas Pop - Top Tracks` while excluding everything else. Applied to the
genre builder, `_sync_essential_playlist` and `_create_new_music_playlist`.

### 4. Essential + New Music queries now select what the filter needs

Both were changed to select `genres` (and Essential already needed `album`).
Without those columns the filter is blind to genre-only signals — a mistake the
new guard test caught in my **own** first attempt at the New Music query.

### 5. Config surface

`playlists.exclude_christmas_from_playlists` (default **true**) and
`playlists.christmas_playlist_marker` (default `christmas`), exposed on the
Config page and wired into its save payload in **both** trees, per the project's
config-UX contract.

## Verified

| Check | Result |
|---|---|
| New suite, pre-fix | **26 failed**, 1 passed |
| New suite, post-fix | **27 passed** |
| Related suites (genre, Essential, M3U, consensus) | before **59** failed → after **33** |
| New failures introduced | **0** (the 33 remaining are pre-existing) |

## New guard tests

`tests/test_christmas_playlist_exclusion.py` (27 tests)

1. `is_christmas_genre` — literal, every stored spelling, JSON-list form,
   Python-list form, non-Christmas values, `Holiday Rock` **not** matched, and
   every empty form.
2. `is_christmas_track` — title signal, album signal, genre-only signal, and a
   plain track that must not match.
3. **The truncation regression, pinned.** Asserts `christmas` really is outside
   the 3-genre window for `"pop, rock, metal, Christmas"`, then asserts the
   canonical test still catches it — so the test cannot silently become vacuous
   if the window size changes.
4. A static guard that the builder never tests `norm_track_genres` for
   `"christmas"` again.
5. Playlist-name policy: `Christmas - Top Tracks` and `Christmas Pop - Top
   Tracks` allowed; `Pop - Top Tracks`, `New Music`, `… Essential Collection`,
   `Loved Tracks` not.
6. Row filtering: an ordinary playlist drops **all four** Christmas variants
   (title, album, genre, genre-outside-the-window) while keeping the plain
   tracks; a Christmas playlist keeps everything; the exclusion can be disabled.
7. Every generator (genre / Essential / New Music) applies the policy.
8. Both generators select the columns the filter depends on.
9. Config defaults, Config-page controls, and Config-JS persistence.

## Pre-existing failures (not caused by this change)

33 failures across `test_essential_collection_m3u.py`,
`test_essential_playlist_scan_sections.py`, `test_genre_backslash_splitting.py`
and others are identical before and after, and are test-fixture issues rather
than product defects.
