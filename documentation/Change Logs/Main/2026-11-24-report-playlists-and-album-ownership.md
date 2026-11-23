# Report playlists, and the album-ownership leak that looked like a VA merge

**Date:** 2026-11-24
**Area:** `services/popularity/popularity_report_service.py` ·
`services/scanning/navidrome_import.py` · `routes/misc_routes.py` ·
`routes/artist_routes.py` · `routes/album_routes.py` ·
`tests/test_album_ownership_and_report_playlists.py` (new)
**Commit:** `fix(report): populate the playlists column, and scope album ownership`

Three reports: the playlist column, an album/page merge symptom, and a request for
recommended corrections.

---

## 1. "Fix the playlists so they are shown on the report"

`playlists_included_in` was blank on all 67,127 rows. The column is real and its
reader is correct — it reads `playlist_entries`, which is populated by
`sync_playlist_index` from the scan's **finalise** stage. A scan still in progress
(or one that stopped part-way) therefore exports a column of blanks, which reads
as "this track is in no playlist" rather than "the index has not been synced".

All three export scopes now sync the index before building the CSV, through one
shared `ensure_playlist_index()`. The sync is incremental — a playlist whose
Navidrome `changed` stamp has not moved is skipped without a request — so a
settled library costs one `fetch_all_playlists` call. Failure is non-fatal: the
export is the point.

Verified against a real schema: `Song A -> "Rock - Top Tracks"`,
`Song B -> ""` (correctly in no playlist).

## 2. "Many albums got merged into the Various Artists albums"

**The export could not have shown this.** It has no `album_artist` column at all —
`artist` on a compilation is the PER-TRACK performer, so a genuinely-VA album and
a merged one are byte-identical in the file. Added `album_artist` immediately
after `artist`:

```
artist=Gorillaz      album_artist=Various Artists   album=Spotify - Essential Alternative
artist=Evanescence   album_artist=Evanescence       album=Fallen
```

That is the column that answers the question, and it was missing.

**A real defect found behind it.** `album_artist_from_sources` scoped the stored
album artist by the bare album NAME:

```python
existing_album_artists[strip_album_edition_marker(str(album))] = str(album_artist)
...
existing_album_artist = existing_album_artists.get(album_name, "")
```

Album names are not unique. One "Various Artists" row for *any* album called
`Essential Alternative` made **every** artist's album of that name resolve to
Various Artists on the next import. Measured:

```
existing='Various Artists'     artist=Weezer -> 'Various Artists'   ← wrong
existing='Original Soundtrack' artist=Weezer -> 'Various Artists'   ← wrong
```

Ownership is now keyed by the album ID (`id:<navidrome album id>`), with the
stripped name only as a fallback for a row that has no id. After the fix the same
input gives `'Weezer'`, while an album that genuinely IS a compilation still
resolves to `'Various Artists'`.

The read and write vocabularies were also inconsistent: `_has_various_artist_part`
treats `"soundtrack"` as VA but `VA_ALBUM_ARTIST_VARIANTS` (which gates the write)
did not, so a row could be recognised as VA and then not written as one. Both now
list it.

**What is NOT a merge.** On the export, all 197 rows of *Spotify - Essential
Alternative* are 5★ across 123 distinct artists, the artists' own albums are
intact (Evanescence has 117 rows across 10 albums), and 105 of the 197 tracks
exist ONLY on that compilation. That is a genuine Various-Artists collection, not
tracks being moved into one. Worth stating plainly: the symptom named is real in
the mechanism above, but the album named is legitimate.

## 3. Recommended corrections

* **`filters.exclude_title_regex` is an invalid regex** —
  `(?i)(...\(intro\|[untitled]))` has one `)` too few, and `[untitled]` is a
  character class, not the word. `album_classification_service.py:218` calls
  `re.search` inside a bare `except Exception: pass`, so it fails SILENTLY and
  intro/interlude exclusion never fires. Suggested:
  `(?i)(^intro$|\bintro$|interlude|\[intro\]|\(intro\)|^untitled$)`.
* **The scan has not finished.** Of 812 artists alphabetically after "Stray Kids",
  688 have never been scanned. Finishing it is the highest-value single action
  here — nothing that reads the library can be trusted until it does.
* 44 rows with a blank album, 111 blank track numbers, 505 blank genres (494 with
  no genre source at all), 568 duplicated `(artist, album, disc, track)` positions,
  and 2 zero-duration tracks.

---

## Verification

`tests/test_album_ownership_and_report_playlists.py` (15 tests); 8 fail with the
fixes stashed.

Full suite compared against the baseline failure set by diff.
