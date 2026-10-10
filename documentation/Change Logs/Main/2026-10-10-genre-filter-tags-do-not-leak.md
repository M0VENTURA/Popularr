# Genre filter tags must not leak onto non-cover songs (2026-10-10)

**REPORT:** "Something is still saving track genres to album and attaching
genres such as acoustic or cover to non cover songs."

## Three confirmed leaks in the genre aggregation layer

1. **Cover/Tribute word-boundary false positives.** `_append_extra_genres`
   matched `\b(cover|tribute)\b` across TITLE+ALBUM. An ordinary song whose
   title merely contains the word ("Cover Me Now") — and every track of an
   album named "Covers: A Tribute …" — was tagged "Cover". The tag is now
   strict, matching only the forms the cover detector itself uses: a trailing
   "(X Cover)" suffix, "cover of", or "originally by" — on the TITLE only.

2. **A corrupted regex (the "acoustic" half).** `de12b3cf` (2026-09-21)
   mangled `(live|acoustic|unplugged)` into
   `(live\vert{}acoustic\vert{}unplugged)`. In Python regex `\v` is a
   VERTICAL-TAB escape, so the alternation NEVER matched: "(Acoustic)" /
   "(Unplugged)" / "(Live)" tracks silently lost their filter tag, and
   `is_live_track_from_genre` etc. had nothing to read. Restored to the
   original alternation.

3. **The album blend spread one track's affiliation to the whole album.**
   `aggregate_genres` appends every `_FILTER_TAGS` token found in ANY track's
   sources, and the album-wide `UPDATE tracks SET genres` in
   `sync_album_file_tags` wrote that blend to EVERY track. One genuine cover's
   "cover" source, or one track's Last.fm "acoustic", stamped the whole album —
   the reported "saving track genres to album / attaching Cover to non-cover
   songs". New `_TRACK_AFFILIATION_FILTER_TAGS` constant
   (`_FILTER_TAGS − {christmas, holiday}`) is stripped from:
   - `genre_aggregation_service.get_track_recommendations` (the album blend
     the tag-sync writes),
   - `track_stage._album_top_genres` (the album-genres inheritance list a
     sparse track inherits),
   - `album_service.get_track_recommendations` (the album page's recommended
     box).
   The **Christmas family survives the album blend on purpose**: a Christmas
   album IS an album property, and the genre-playlist builder depends on the
   tag being present outside its 3-genre window.

Per-track aggregation (track_stage §5, VA track sync) is unchanged: a track
whose OWN sources or title justify "Cover"/"Live" still gets its tag.

## Tests

`tests/test_genre_filter_tags_do_not_leak.py` (12): strict-cover cases
(word-in-title no, album-named-Covers no, "(X Cover)" yes, "cover of" yes),
restored live/acoustic regex, album blend never spreads cover/acoustic but
keeps Christmas, and the album-top-genres inheritance stripping.

Regression sweep (12 genre/cover/VA/christmas suites, one process per file):
failing sets IDENTICAL to the pristine baseline (`dad27349`) — 0 new failures
(the 10 pre-existing failures in `test_album_genre_fallback` /
`test_genre_consensus_aggregation` / `test_track_genre_navidrome_fallback`
fail identically on both trees).