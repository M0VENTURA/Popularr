# Path repair, single sources in the export, Essential Collection rules, genre write speed

**Date:** 2026-11-24
**Area:** `services/metadata/album_path_repair_service.py` (new) ·
`services/popularity/popularity_report_service.py` ·
`services/popularity/stages/finalise_stage.py` ·
`services/enrichment/genre_resync_service.py` · `services/metadata/tag_file_service.py` ·
`services/scanning/reconciliation.py` · `migrations/versions/037_tracks_file_path_normalized_idx.py`
**Commit:** `feat(albums): repair from paths, single sources in the export, Essential rules`

Four requests.

---

## 1. "Repair album from paths" on the album page

New **Repair from paths** button in the album's bulk-action toolbar, and
`POST /api/album/repair-from-paths`.

It re-derives `album` / `year` / `album_artist` from the folders the files
actually sit in (`{album_artist}/{year} - {album}/file`), which is the only
evidence a merge left behind. On the reported rows:

```
/music/Jimmy Eat World/2002 - Good To Go (EP)/01. …  ->  Good To Go (EP) / 2002 / Jimmy Eat World
/music/Coldplay/2002 - A Rush of Blood…/2. …         ->  A Rush of Blood to the Head / 2002
```

⚠️ **Two-stage on purpose.** The button asks for a DRY RUN first and shows the
proposed change per track, then requires confirmation. Reading an album out of a
folder name is a heuristic, and the merge that caused the damage was itself an
unprompted bulk rewrite — repeating that silently would be the same mistake.
`dry_run` also defaults to `true` server-side, so a caller that forgets cannot
write by accident.

`write_file_tags` defaults to **false**: the DB is what the album page and the
playlist builders read, and the file tag is the irreversible half.

Repaired rows are stamped as a local correction, so a later import cannot undo
the repair on the next scan.

Guards: a folder that is a library root (`/music`) never becomes an album or an
artist, and `"Various Artists"` is a valid ARTIST folder even though it is not a
valid album name.

## 2. `single_sources` in the config export

The popularity CSV gained **`single_sources`** and `single_sources_used`, sitting
next to `single_confidence`. `is_single` says what the detector CONCLUDED; these
say WHICH SOURCES agreed, which is what makes it possible to judge the scoring
rather than guess at it.

Both are JSONB lists, normalised to `musicbrainz; discogs` — writing the raw
value would put brackets and quotes in a spreadsheet cell. An empty list is an
empty cell, not `[]`, because "no source agreed" is a real answer.

## 3. Essential Collection — the six-step selection spec

Complete rewrite of the selection in `_sync_essential_playlist`. One named helper
per step, so the code and the spec cannot drift:

| Step | Helper | Behaviour |
| --- | --- | --- |
| 1a primary | `_essential_is_primary_credit` | credit STARTS with the artist, at a credit boundary |
| 1b featured | existing featured merge (unchanged) | X as a guest on another act's track |
| 2 versions | `_essential_version_ok` | drop live/remix/acoustic/demo unless a high-confidence single |
| 3 gate | `_essential_percentile` + `_essential_is_high_confidence_single` | 4★+ AND (single OR reach ≥ p80) |
| 4 reach | `_essential_reach` | Last.fm listeners, else ListenBrainz listens |
| 5 order | inline sort | reach descending, ONE list, no features section |
| 6 file choice | duplicate tiebreak | non-compilation copy wins |

Three real defects this fixed:

* **There was no version filter at all.** `_track_is_playlist_eligible` exists and
  the genre builders use it; the Essential builder never called it, so a live take
  survived whenever it happened to be the only copy.
* **The percentile was computed from ALL rows, features included.** The spec says
  features are compared against the threshold but never shift it; they now don't.
* **Ordering was star-tier first,** so every 5★ preceded every 4★ — "the loudest,
  not the biggest". Now one list by reach.

`_essential_percentile` returns **`None`** (not `0.0`) when there is no usable
data, and the caller skips the gate: an artist with no listener figures would
otherwise get an EMPTY collection from a threshold of zero.

⚠️ A bare `startswith` on the artist name was wrong: `"Papa Roach Tribute Band"`
begins with `"Papa Roach "` and was accepted as the same act. The credit must end
at a boundary or be followed by a feature/joiner marker
(`_ESSENTIAL_CREDIT_BOUNDARY_RE`).

Rebuild the playlists from **Config → Rebuild Genre and Playlists** as before;
nothing new is needed to apply it.

## 4. Genre writes were 30s per file

Two separate causes, both fixed.

**(a) The local-authority stamp scanned the whole table per file.**
`write_tags_to_file` ends with `mark_local_tags_written_for_path(file_path)`, a
separate connection doing:

```sql
UPDATE tracks SET … WHERE LOWER(REPLACE(COALESCE(file_path, ''), '\', '/')) IN (…)
```

A function on the indexed column makes it **non-sargable** — PostgreSQL cannot
use `idx_tracks_file_path`, so every tag write scans all ~70k rows. The genre
resync does that once per file.

Fixed on both sides: `write_tags_to_file` gained `defer_stamp=True`, the resync
defers and calls one `mark_local_tags_written_for_paths()` for the whole batch,
and migration **037** adds an expression index on exactly that predicate so even
the per-file callers (album save) can use an index.

**(b) Each write is 4–6 full passes over the audio file.** `_write_tags_atomic`
does `copy2` → mutagen rewrite → `filecmp` (which re-reads both) → `os.replace`.
On a network mount or a spinning disk a large FLAC therefore costs tens of
seconds of pure I/O. That part is by design (atomicity) and is NOT changed here.

As requested, the genre resync remains DB-first and the file write stays opt-in —
the DB is all the playlist builders need, and the tags catch up on the next album
save.

**Diagnostic note:** if per-file timing still tracks FILE SIZE after this, it is
I/O. If it tracks nothing but stays near 30s, check for
`Transient database error, retrying session acquisition` — `db_pool_timeout` is
30s, and a pool checkout wait would produce exactly that constant.
