# Download import: album MBIDs inherited from the library, multi-disc keeps disc 1 (2026-10-08)

## Reported

> When tracks are added to the queue, when they are downloaded and copied into
> the directory, they don't have the same mbid data added to them as the rest of
> the album they were added from so are coming in as separate albums. Also, some
> multiple disk albums are adding the tracks as disk 0 rather than disk 1 when
> importing the tracks.

Two defects, both in
`services/downloads/download_completion_service.py::_apply_stored_metadata`.

## 1. Missing album MBIDs → a separate album

The album-scoped fields came from **(a)** what was stored on the queue row,
else **(b)** a LIVE MusicBrainz call at import time. There was no third source.

Measured with the shipped function (a queue row with nothing stored, MusicBrainz
raising `MusicBrainz overloaded`):

```
--- C. no album metadata + MusicBrainz UNREACHABLE (disc 1) ---
  MBID-ish keys: []
```

Nothing. And that state is reachable in production: the global MusicBrainz
throttle is **1 req/s**, imports run **per track**, and both failures are logged
at **DEBUG** — invisible in a normal log tail.

The artist page has keyed albums on `musicbrainz_releasegroupid` since
`2026-10-06-albums-split-by-release-group`, so a file written without it does not
join its siblings' card: it becomes its own album. That is the report.

**Fix — inherit before asking.** `_resolve_album_level_metadata` gained a step
*between* "stored" and "MusicBrainz": read one existing row of the same album
from the library and take its album-scoped fields.

```sql
SELECT <album-level columns> FROM tracks
WHERE LOWER(COALESCE(NULLIF(album_artist,''), artist)) = LOWER(:artist)
  AND LOWER(COALESCE(album, '')) = LOWER(:album)
  AND COALESCE(file_path, '') NOT LIKE '__queued_for_download__%'
ORDER BY CASE WHEN COALESCE(musicbrainz_releasegroupid,'') <> '' THEN 0 ELSE 1 END, id
LIMIT 1
```

* **LOCAL**, so it spends nothing from the MusicBrainz budget (the fetch now
  runs only for fields neither source could supply);
* prefers a sibling that carries the release group — the field the grouping
  hangs on;
* excludes `__queued_for_download__` stubs, so a placeholder row can never donate
  MBIDs to a real album.

And because the failure used to be reachable **silently**, an unresolvable
release-group id is now a **WARNING** naming the reason:

```
Imported track has no release-group MBID after stored, inherited and
MusicBrainz sources — it may render as a separate album
```

## 2. Disc 0 instead of disc 1

The strip rule was unconditional:

```python
meta["disc_number"] = str(_disc_raw) if _disc_num >= 2 else ""
...
if _disc_num < 2:
    meta["disc_number"] = ""     # re-added after the empty-value filter
```

So the FIRST disc of a **multi-disc** release had its TPOS cleared too.
Navidrome then reported `discNumber` **0** for those tracks while disc 2+ kept
their number — one album rendering as "disc 0" + "disc 2".

**Fix — decide by the release, not the number.** `_resolve_album_level_metadata`
now runs **before** the disc logic so `disctotal` is available:

| release | track disc | written |
|---|---|---|
| multi (`disctotal >= 2`) | 1 | `1` ✅ (was `''`) |
| multi | 2 | `2` (unchanged) |
| multi | unknown | *frame left alone* — guessing produced the 0 |
| single (`disctotal < 2`) | 1 | `''` (unchanged — the deliberate strip) |
| single | 2 | `2` (unchanged) |

The single-disc strip itself is preserved on purpose: a stray `1` renders as its
own "disc 0" group on a one-disc release (pinned by
`tests/test_album_missing_and_disc_cleanup.py`).

## Files

* `services/downloads/download_completion_service.py` — sibling inheritance,
  the unresolvable-MBID warning, album level resolved once before the disc
  decision, single-disc-only disc strip
* `tests/test_download_import_mbids_and_disc.py` — 14 tests (new)

## Tests

`tests/test_download_import_mbids_and_disc.py` (14): the disc matrix above
(incl. the unknown-disc "leave it alone" case), the sibling inheritance with
MusicBrainz down, stored values winning over inheritance, **no MusicBrainz call
at all** when the row is fully resolvable, the query's stub exclusion +
release-group preference, "library first, MusicBrainz second", album level
resolved once and before the disc decision, and a degradation case where
nothing can be inherited (the import still succeeds). No file I/O and no
network: the tag writer and MusicBrainz are replaced, `db_session` is a canned
row, and the recorder captures what `update_file_metadata` would have written.

**Oracle:** reverting `download_completion_service.py` → **8 failed / 6 passed**
(the 8 are the discriminators — every disc inversion, both inheritance guards,
the ordering guards, the warning); restored → **14 passed**.

**Sweep:** 15 download/queue/import suites, clean `49bb81fd` worktree vs this
change — base `12 failed / 250 passed`, new `13 failed / 249 passed`.

The single differing id is
`test_download_completion_not_found_loop.py::TestDeepFileSearch::test_sibling_
torrents_root_is_searched` — the **known hash-order flake**, and it was proven
to flip in *both* directions rather than assumed: the baseline run failed it
while the changed run passed it, the same file failed in isolation on the
BASELINE and passed on the CHANGED tree, and 4 repeated isolated runs gave
**1 passed / 3 failed**. It is unrelated to this change (`_search_roots`'s
`{torrents, Torrents, TORRENTS}` set order on a case-insensitive filesystem).

> ⚠️ **A single before/after failing-set difference is not proof of a
> regression** — the differing test has to be re-run repeatedly. Recorded again
> because it nearly cost this change a false-positive.

## Not addressed

* Whether the queue's own `album` field matches the library's album name (it is
  stored from the MusicBrainz **release** title) — a genuinely different album
  name would split regardless of MBIDs. Worth a look if the symptom persists
  after this fix, together with the query above (it keys on the queue row's
  `album`/`album_artist`, so a spelling mismatch simply finds no sibling and
  falls through to MusicBrainz).
* Rows written before this fix keep whatever they were imported with; there is
  no automatic repair. Re-running the import for the affected album (or a
  metadata scan) now fills them from their siblings.
