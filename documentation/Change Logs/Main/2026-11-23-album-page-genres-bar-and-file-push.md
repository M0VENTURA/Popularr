fix(album-page): stop re-proposing Genres, and push a saved genre edit to the files

Two reported problems, both confirmed against the real data from the screenshots.

1. "Whenever looking up an album, genres is always picked up as new."

Not a column mix-up — both sides are the same field. `current` for the album
genres bar is `tracks.genres` (the FINAL genres the scanner chose, capped at
`get_genre_track_limit()`), while the proposed side was the raw union of every
release-group, release and recording tag:

    stored   : Nu Metal, Alternative Metal, Industrial Metal        3
    proposed : ... 14 tags, group + recordings ...

Under set equality those can never agree, so the bar appeared on every lookup and
NO save could clear it — the spare 11 were never going to be written.

⚠️ Capping the proposed list at N is NOT the fix: the union is ordered
group-first, so "first 4" picks `Alternative Rock` over the `Industrial Metal`
the scan actually kept. The question that has an answer is whether the album
still SUPPORTS what is stored, so album genres now use a SUBSET test
(`_album_genre_sets_equal`): if every stored genre is still offered, nothing the
save can write has changed. A genre that genuinely left the release still
reports. Track-level genres keep strict set equality — nothing there is capped.

2. "If Genres exist, can we have it push final genres in."

`_apply_album_track_genres` writes the DATABASE only, by design: the file's
genre tag belonged to the popularity scan, which only runs on the next pass. So
a saved genre edit stayed DB-only and Navidrome — which reads the FILES — kept
showing the old set. Visible in the pasted raw tags: track 01 carried the
container's own "Nu Metal/Alternative Metal/Industrial Metal" while the album
page listed a different list.

The save now pushes the stored value with `apply_genres_to_album`, which is the
existing writer that writes the file AND the column together.

⚠️ Deliberately NOT `sync_album_file_tags`, which would have been the obvious
reuse: that helper's "active genre cleanup" re-derives every track's genres from
its own source columns and UPDATEs the database before writing files, so calling
it from a page save silently reverted the album fill the save had just made
(`test_album_save_genre_precedence` caught it — a bare track written "Rock, Pop"
came back "Metalcore"). The push is also gated on `genre_only_writes`, not on
the presence of the `album_genres` field, or every unrelated save would re-read
and rewrite every file of the album.

VERIFICATION

10 new tests. Genre-related suites pass; the 9 failures in the album-save
suites are pre-existing (identical on a clean stash). Full suite 404 vs a
baseline of 403 — the extra is the confirmed pre-existing
`playlist_song_id_rejections` ordering flake.

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>
