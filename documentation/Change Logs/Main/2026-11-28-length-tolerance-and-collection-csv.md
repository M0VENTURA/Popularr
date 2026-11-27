# Two false download rejections, and the slow collection CSV

**Date:** 2026-11-28
**Commit:** `fix(downloads): accept a 5s gap and a bonus-track annotation; stop the CSV waiting on a playlist sync`

## 1. "Wrong length … (5.0s apart, tolerance 5s)"

The verdict and the sentence disagreed. The comparison was on the **raw** float
while the message printed the **rounded** one:

```python
if diff > _tolerance:                       # 5.02 > 5.0  -> reject
    _reject(..., length_difference_seconds=round(float(diff), 1))   # prints "5.0"
```

So a gap of 5.02 s displayed as *"5.0s apart, tolerance 5s"* and was still
rejected — a message that contradicts itself, and a rejection for 20 ms of
encoder rounding that the user had explicitly said should pass.

Now the compared number **is** the displayed number:

```python
_reported_diff = round(float(diff), 1)
if _reported_diff > _tolerance:
```

Anything that displays as within tolerance is accepted; only a gap that displays
as larger is rejected. `5.00` and `5.04` pass, `5.06` (displayed `5.1`) does not.

## 2. "Different edition — the file is 'non album bonus track' but the queue item has none"

Two titles for the same recording:

```
file   : (I've Just Begun) Having My Fun (Non-Album Bonus Track)
queue  : (I've Just Begun) Having My Fun (Non-album)          [from MusicBrainz]
```

`extract_edition_annotation` yielded `"non album bonus track"` for the file —
"bonus" is an edition keyword — but **nothing** for the queue title, because
"non-album" is not. `edition_annotations_compatible` treats "one side annotated,
the other plain" as a different edition, so it refused.

That rule is right for *Deluxe Edition* and *Epic Edition*, which denote other
releases (and often other masters). It is wrong here: a bonus/non-album track is
the **same recording**, and the annotation only says which release it appeared
on. Those forms are now neutral annotations, so an annotated side compares as
plain:

`non album`, `non album track`, `non album bonus track(s)`, `bonus track(s)`,
`bonus`.

The gate is not weakened — deluxe / epic / remaster / limited / anniversary all
stay non-neutral and still refuse to collapse onto a plain title. Verified:

```
'Song (Epic Edition)'    vs 'Song' -> False
'Song (Deluxe Edition)'  vs 'Song' -> False
'Song (Remastered)'      vs 'Song' -> False
'Song (Non-Album Bonus Track)' vs 'Song' -> True
```

## 3. The collection CSV was waiting on a Navidrome playlist sync

`_worker` called `ensure_playlist_index()` **before** building anything, and the
job stayed `pending` throughout. That refresh re-fetches every playlist whose
changed-stamp moved — one Navidrome round trip each, ~5 s per ~1,200-track
playlist. The service's own comment records **66 changed playlists taking
5m36s**, and a generated-playlist rebuild moves many stamps. So "Download
Collection CSV" could spend minutes syncing playlists while the UI said
*"Building the CSV…"*.

The playlist column is **derived** — it is read from the `playlist_entries`
table that index maintains. So:

- refresh first **only when the index is empty** (a first export needs it, or the
  column would be blank);
- build and **publish the CSV immediately**;
- refresh the index **afterwards**, for the next export.

The column can therefore be one export behind. It is never wrong, only older, and
the file no longer waits minutes for it.

Also, while in there:

- `job["phase"]` is reported (`Building the playlist index…` /
  `Building the CSV…` / `Refreshing the playlist index…`) and shown in the card,
  so a long wait says what it is actually doing.
- `body = "\ufeff" + report` copied the whole multi-megabyte string to prepend
  one character; the BOM and body are now written separately.
- `len(body.encode("utf-8"))` encoded a second full copy purely to measure it;
  the written file's size is used instead.

## Tests

`tests/test_download_rejections_and_collection_export.py` — 22 cases:

- the reported pair is compatible, and the annotation is still *recognised*
  before being judged neutral; deluxe/epic/remaster/limited/anniversary still
  refuse;
- the length verdict uses the rounded difference, and the boundary arithmetic
  (`5.00` and `5.02` pass, `5.06` fails);
- the export refreshes only on an empty index, writes the file before the
  trailing refresh, reports a phase, probes cheaply, drops the redundant copies,
  and still carries `path` for the download endpoint.

Suite: 411 failed / 10,668 passed against a 413-failed baseline.
