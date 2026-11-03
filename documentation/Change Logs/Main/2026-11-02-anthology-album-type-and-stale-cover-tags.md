# “ANThology” is not a compilation, and stale cover tags are repaired

**Date:** 2026-11-02
**Area:** popularity scan (album type) / cover detection
**Files:** `services/popularity/stages/album_stage.py`,
`services/enrichment/cover_detector_impl.py`,
`tests/test_anthology_album_type_and_stale_cover.py` (new),
`tests/test_cover_stale_original_and_file_tags.py`

## Report

> During a popularity scan, Anthology by Alien Ant Farm was adjusted to be a
> compilation even though MusicBrainz lists it as a regular album.

> Also the cover detection is still saving as Santana for Alien Ant Farm.

## 1. Album type

**Cause.** Two compounding rules:

* `detect_greatest_hits_album` matches the token `anthology`, so the album's
  NAME alone produced `album+compilation`.
* `_rich_stored_album_type("album")` returns `None` (a plain type has no
  secondary), so `_detect_album_type` fell through to the title heuristic even
  when the stored tag positively said plain **album**.
* `_resolve_album_type` only let MusicBrainz override `detected == "album"`, so
  the title guess survived, and `_persist_album_type_to_tracks` only filled
  blanks — a previously-written `album+compilation` could never heal.

**Fix.**

* A plain stored type (`album` / `ep` / `single`) now wins over the title
  heuristic. The one exception is a compilation ARTIST placeholder
  (Various Artists / soundtrack), whose identity is the evidence.
* MusicBrainz’s plain `album` outranks a `+compilation` written by the title
  heuristic, a title-only guess, or an MBID-bound album.
* Persistence accepts the override for that case
  (`replace_secondary=True`) and rewrites the stored composite; ordinary
  fill-only behaviour is unchanged for every other field.
* A truly stored rich type still keeps precedence — an EP answer must not
  overwrite a stored `album+live`/`album+remix`.

## 2. Cover detection still “Santana”

**Cause.** The DB verdict (and the title suffix) had already been corrected to
Michael Jackson, so `_suffix_repair_update` returned `None`. The AUDIO FILE
still carried `ORIGINAL_COVER_ARTIST = Santana`; Navidrome read that tag back
on the next import, so the stale value kept returning even though detection
resolved Michael Jackson again.

**Fix.** `_suffix_repair_update` now checks the file’s
`ORIGINAL_COVER_ARTIST` / `IS_COVER` tags when the title suffix already
matches. If they disagree with the resolved original, it emits one update that
writes the whole verdict (title + is_cover + original) to the file. After that
the tags match and no further write happens; an unreadable/missing file is
treated as “leave it alone” so it cannot churn every scan.

## Track-page cover scan mode

`POST /api/track/<id>/cover-detection` calls
`detect_covers_for_track(track_id)` — whose `force` default is **True** — so a
track-page Cover Detection scan runs in forced mode. It re-assesses the track
instead of trusting the freshness window.

## Tests

* `tests/test_anthology_album_type_and_stale_cover.py` — title heuristic
  premise, MusicBrainz plain-album override, MB secondary control, and the
  persistence `replace_secondary` path.
* `tests/test_cover_stale_original_and_file_tags.py` — a matching suffix with
  stale file tags still produces the repair update; current tags produce none.
