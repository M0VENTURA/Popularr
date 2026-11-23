# Album merges were relabelling files that belong to other albums

**Date:** 2026-11-24
**Area:** `db/repositories/metadata.py` (`merge_album_names`) ·
`tests/test_merge_albums_keeps_foreign_files.py` (new)
**Commit:** `fix(albums): a merge must not relabel a file that lives under another album`

## Reported

> "Many albums somehow got merged into the Various Artists albums such as
> Spotify - Essential Alternative"

The give-away was the **file paths** in the pasted tracklist — the album page was
showing one album whose rows point at files in several different folders:

```
/music/Various Artists/2026 - Spotify - Essential Alternative/02. Gorillaz - Clint Eastwood.mp3
/music/Jimmy Eat World/2002 - Good To Go (EP)/01. Jimmy Eat World - The Middle (Acoustic).mp3
/music/Coldplay/2002 - A Rush of Blood to the Head/2 - Coldplay - In My Place.mp3
```

The album page filters on `(album_artist, album)` — the TAGS — and never looks at
the path, so all of them render together.

## Root cause

`merge_album_names` renames several album NAMES onto one canonical name. It
updated `tracks.album`, and then:

```python
for fp, title, album_name in _file_rows:
    ...
    update_file_tags(resolved, {"album": album_name})
```

— **every** row of every source album had its ALBUM TAG rewritten, with no check
on where the file actually lives. A file never MOVES: its path keeps saying
`/music/Jimmy Eat World/2002 - Good To Go (EP)/…` while its tag now claims the
compilation. Two consequences, and the second is the serious one:

1. the row renders on the wrong album page forever, because the page reads tags;
2. **the tag that identified the true album is gone.** There is nothing left on
   disk to recover the original album from, so the damage cannot be undone by a
   re-scan.

Confirmed against the export: of the 197 rows on that album, **16 are dated 2002**
(the album is a 2026 release) and are numbered inside their own tracklists
(`1,1,2,2,3,4,4,5,6,7,8,9,10,11,12,13` — not a contiguous 1..N). They came from at
least four unrelated compilations: Triple J's Hottest 100 (2002), MTV2 Handpicked,
a `Good To Go` EP and `Not Another Teen Movie`.

## The fix

`_file_belongs_to_canonical(file_path, source_album, canonical_name)` gates the
file write. It returns True only when:

* the source and canonical names are the **same album differently spelled**
  (`"Spotify Essential Alternative"` vs `"Spotify - Essential Alternative"`,
  edition markers stripped) — which is what a merge is FOR; or
* the file already lives under the canonical album's **folder**.

Otherwise the file is left untouched. The DB rename is unchanged: collapsing two
spellings of one album is still the point.

The guard is deliberately **fail-safe**: an unreadable path or a blank canonical
name returns False, because a DB-only rename is recoverable and an overwritten
file tag is not.

## Verification

Measured on the exact reported paths:

| file | source album | canonical | relabel? |
| --- | --- | --- | --- |
| `…/Spotify - Essential Alternative/02. Gorillaz…` | same | same | yes |
| `…/Spotify - Essential Alternative/02. Gorillaz…` | spelling variant | same | yes |
| `…/Jimmy Eat World/2002 - Good To Go (EP)/01.…` | `Good To Go (EP)` | VA album | **no** |
| `…/Coldplay/2002 - A Rush of Blood…/2.…` | `A Rush of Blood…` | VA album | **no** |

`tests/test_merge_albums_keeps_foreign_files.py` (11 tests). Verified against HEAD
that the guard is absent there and every row's tag was rewritten.

## Already-damaged rows

This stops new damage; it does not repair the 16 existing rows. Recovering them
needs the original album per file, which the overwritten tag no longer holds —
the `file_path` is the only remaining evidence, and the folder name is a reliable
source for it (`…/2002 - Good To Go (EP)/`). A repair pass keyed on the folder
would be a separate, deliberate change.
