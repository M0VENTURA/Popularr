# Collaborative-track identity, composer corruption, MBID aliases, album-page queue state

**Date:** 2026-11-24
**Area:** `services/scanning/navidrome_import.py` ·
`services/scanning/reconciliation.py` ·
`services/scanning/metadata_extractor.py` ·
`services/metadata/tag_file_service.py` ·
`services/metadata/album_missing_service.py` ·
`services/metadata/duplicate_version_service.py` ·
`test_site/templates/Pages/album_detail.html` ·
`test_site/static/js/pages/album.js` · `test_site/static/js/pages/artist-corrections.js`
**Commit:** `fix(import): find collaborative tracks, repair composer debris, split MBID aliases`

## Reported

> "RATATATA … may repeatedly appear as new … deduplication should not rely heavily
> on view_artist + album + title … The Billie Piper composer values are definitely
> corrupted … Some MusicBrainz ID semantics look questionable …
> the album pages … shows a tick … but when the page is refreshed, it doesn't show
> it's in the queue … If multiple tracks exist … recommend the one to delete based
> on the correct track length … highlight if a track exists in the collection but
> isn't on the tracklist … corrections page … wont allow you to select the release
> to jump to it to correct, not every release should be merged."

## 1. A collaborative track was re-imported as "new" every scan

The Navidrome prefetch — the query that decides which existing rows the import can
see — matched **one** artist identity:

```sql
WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
```

Navidrome stores a collaboration's album artist as the JOINED credit
`"BABYMETAL & Electric Callboy"`, which never equals the scanned view
`"BABYMETAL"`. So the prefetch returned **0 rows**, `existing_album_tracks` was
empty, and every skip gate (album diff, fast-skip, fingerprint) was bypassed: the
album was rewritten in full every scan with `is_new_track=True`, and its Spotify /
suggested-MBID columns were reset.

**The row was never duplicated** — `tracks.id` is the stable Navidrome song id and
the write is `ON CONFLICT (id)`. It was the same row treated as new forever.

`_artist_identity_clause` now matches **either** credit column, exactly OR as a
whole name inside a joined credit, with Unicode punctuation folded in SQL
(`blink‐182` U+2010 vs `blink-182`). The SQL LIKE only narrows the candidate set;
`_credit_contains_name` re-checks in Python so `VA` does not pull in `VAMPIRE`
and `Bad` does not match `Bad Omens`.

## 2. Composer was recursively serialised

A Python list reaching the tag writer became `str(list)` in the TCOM frame
(`"['Dion Rambo', 'Jacques Richmond']"`); the read side joined it back into one
comma string, re-split it with a splitter that does not know `'`/`[`/`]`, and wrote
the debris back — nesting a level deeper each scan. The local-correction guard then
**protected** the garbage (`winner='existing'`) from Navidrome's clean value.

* `composer` now gets the same repair `writer` already had (`_writer_names_text`),
  and that repair now parses a Python repr via `ast.literal_eval` (JSON cannot read
  single quotes).
* `split_credit_names` parses a repr first, flattens one nested level, and repairs a
  mixed `"A, B • ['A', 'B']"` value by cleaning each bullet half and de-duplicating.
* `_looks_like_serialised_debris` makes a MALFORMED local value ineligible for
  local-correction protection, so the provider's clean value can repair the row.
  `AC/DC`, `Tyler "T-Bone" Spencer` and `[Unknown]` are never flagged.

## 3. `musicbrainz_trackid` was read as the recording id

Navidrome's wire field `musicBrainzId` IS the recording id, but the FILE TAG named
`musicbrainz_trackid` is the **release-track** id under the Picard convention.
Accepting it as a recording-id alias stored a release-track id in `recording_mbid`,
`mbid` and `musicbrainz_trackid` at once — three columns, one wrong entity. It is
no longer an alias; the release-track id is read into
`musicbrainz_releasetrackid` only.

## 4. The album page's download tick now survives a refresh

`queue_status` was only computed by the live `refresh=1` path; the page-load read
never selected it, so a reload always showed "Missing" again. It is now read from
`download_queue` on every page load, matched by (title, disc, track). A row shows
while it is in flight (queued / searching / downloading / processing / moving /
matched / backed_off / pending_*), and disappears once it imports or fails — which
is the documented lifecycle.

## 5. Duplicate copies: length decides, and the page shows it

`recommend_duplicate_keeper` now takes the RELEASE's expected length (from the
album's missing tracklist) and, failing that, falls back to a majority length
cluster. `confidence: "length"` with a reason naming both lengths. It never
condemns copies that share the correct length — only the outliers.
`album.js` now renders the recommendation (`Keep` / `Delete` badges with the
reason) instead of a bare `×N`.

## 6. Collection tracks not on this release's tracklist

`markExtraTracks` inserted the "Extra" badge after
`.track-title-display-<id>`, a class the template never rendered — so the badge
silently never appeared. The class is now on the title link. (`extra_tracks`
itself still only appears after a Compare; a page-load version is the follow-up.)

## 7. Corrections page: inspect a variant instead of merging it

The duplicate-MBID row offered only **Merge**. Each spelling is now a link to its
own artist page and the row has an **Open** action, so a variant that is genuinely
a different act (or needs a different release correction) can be fixed there —
"not every release should be merged".

## Tests

`test_navidrome_artist_identity_credits.py` (credit containment, prefetch, diff),
`test_composer_debris_and_mbid_aliases.py` (debris detection, protection bypass,
read/write repair, the MBID alias), `test_album_missing_queue_status_on_load.py`
(tick survives reload; imported/failed drop it),
`test_duplicate_length_recommendation.py` (length winner, majority, no-guess
controls).
