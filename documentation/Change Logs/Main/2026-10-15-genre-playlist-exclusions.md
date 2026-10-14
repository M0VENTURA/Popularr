# Genre playlists: a track's OWN genres decide, and metal stays out of rock

**Date:** 2026-10-15
**Files:** `services/catalog/album_classification_service.py`,
`services/popularity/stages/finalise_stage.py`, `helpers/config_helpers.py`,
`routes/ui_routes.py`, both Config pages + their JS

Requested:

> **Alternative Rock - Top Tracks** — exclude tracks that ALSO carry any
> `*metal*` tag (alternative metal, nu metal, heavy metal, industrial metal,
> progressive metal, metalcore, sludge metal, death metal). Stops Tool, Manson,
> Slipknot and Deftones taking over the Alt-Rock playlist.
>
> **Alternative Metal - Top Tracks** — allow `alternative rock` co-tags ONLY if
> `alternative metal` (or `nu metal`) is present on the track/album — not just
> inherited from artist fallback when the track itself is only tagged
> `alternative rock` / `pop rock` / `electronic`. Keeps Tool, Deftones, SOAD,
> Manson while dropping Linkin Park's *Iridescent*.
>
> *"The genre fallback may have to be added to the finalise pass for albums
> rather than the playlists."*

## What was actually wrong

**Not a filter problem — a data-shape problem.**

`tracks.genres` is **not a per-track value** on an ordinary album.
`sync_album_file_tags` reads every track's source columns, blends them, and
writes ONE string to every row:

```sql
UPDATE tracks SET genres = :g
WHERE album_artist = :a AND album = :alb
```

So every track of a Linkin Park album carries the *same* `genres` string. The
playlist builder could only ever answer **"is anything on this album metal?"** —
never **"is THIS track metal?"**. Measured consequence: *Iridescent*
(alternative rock / electronic / pop rock in its own right) joined the metal
playlists, and the band's nu-metal tracks joined the rock one. The two playlists
mirror each other because they are reading one value.

The per-track truth lives in the **source columns** (`lastfm_tags`,
`musicbrainz_genres`, `discogs_genres`, …), which the track stage writes per
track and the album sync never touches.

**On the fallback question.** No new fallback was needed in the finalise pass.
`tracks.genres` already *is* the album-level fallback — that is precisely why
the sparse-track case keeps working. `_album_top_genres` (the helper written for
"a sparse track inherits the album's top genres") is **wired to nothing**: it is
tested but has no production caller, in either copy
(`track_stage` *and* `finalise_stage`). Wiring it would have produced the same
value as the blend already does, so it was left alone — but it is dead code
worth a decision, and `_artist_dominant_genres` alongside it is dead too.

## What was built

### 1. One genre vocabulary, beside the Christmas test

`services/catalog/album_classification_service.py` gained:

* `iter_genre_tokens(genres)` — ONE parser for every stored form (list, JSON
  list string, delimiter-separated string), lower-casing and stripping
  punctuation but **keeping hyphens** (so `nu-metal` is one genre, not two).
  `is_christmas_genre` now uses it: a pure extraction, pinned as
  behaviour-preserving.
* `is_metal_genre(genres)` / `genre_tokens_contain(genres, needle)` —
  a **substring** test, not a closed list. The subgenre vocabulary is unbounded
  and every spelling contains the word, so a fixed set would stop matching the
  moment a source returns "groove metal".

### 2. The track's own tags win

`finalise_stage` resolves a track's genre identity as:

| track | identity used |
|---|---|
| has its own source tags | **those** — the album blend is ignored |
| no tags of its own (sparse) | the album blend — the existing fallback |

That is the rule that drops *Iridescent* while keeping Tool, and it is a config
toggle (`own_tags_win`) because it is a real behaviour change for every playlist.

### 3. The exclusion rule

`metal_excluded_from` (default `["alternative rock"]`): a track whose identity is
metal may not enter the listed playlists. The test runs against the **full**
identity, never the truncated `max_genres` window — the same trap the Christmas
check documents, where a fourth-position tag was invisible.

Both rules report themselves: `[FINALISE_STAGE] Genre exclusion — 12 metal
track(s) kept out of 1 playlist(s): alternative rock: 12`. A rule that silently
drops tracks reads as a bug.

### 4. Config

New `playlists.genre_exclusions` block: `enabled`, `own_tags_win`,
`metal_excluded_from`. Three controls on **both** Config pages; both `config.js`
collectors save them; and `playlists` was added to `_sanitize_config_sections`'
`nested_children` — without that the Config-page save coerces the dict to `{}`
and the rules silently stop applying (the documented way a nested block dies).

## Tests

`tests/test_genre_playlist_exclusions.py` — **57 tests**: the reported roster
driven through the **real** builder (Tool, both Linkin Park tracks, Foo
Fighters, a sparse track), the vocabulary, the resolver, and the full config
plumbing (both pages, both JS collectors, the nested registration, and that the
playlist query actually selects the per-track columns).

`metal_excluded_from` and `own_tags_win` are pinned as **independent** rules,
each with its own control:

* empty exclusion list → Schism takes over Alternative Rock again (proves rule 1
  is the mechanism), while *Iridescent* stays out of Alternative Metal (proves
  rule 2 does **not** depend on it);
* `enabled: false` → the takeover returns;
* `own_tags_win: false` → the verdict changes (the switch is not dead).

⚠️ Two of my first-draft probes were **mis-specified and one was vacuous**: the
Tool fixture's genres did not include `alternative rock`, so "Schism stays out of
Alternative Rock" passed for the wrong reason (it never qualified). Fixed by
giving the fixture both tags, which is what makes the control meaningful.

Oracle (sources reverted, tests kept): **47 failed / 10 passed** — the 10 are the
controls. `import app` clean (407 routes).

Sweeps: `test_christmas_playlist_exclusion` 29 · `test_genre_playlists` 4 ·
`test_genre_filter_tags_do_not_leak` 12 · `test_essential_collection_m3u` 15 ·
`test_essential_playlist_scan_sections` 8 · `test_playlist_dedupe_unicode_titles`
12 · `test_playlist_ordering` 67 · `test_playlist_sync_auth_and_cap` 34 ·
`test_va_track_artist_genre_fallback` 23 · `test_interlude_lb_config_page` 3 ·
`test_compilation_tier_guards` 27 · `test_compilation_online_catalogue` 28 ·
`test_nightmare_revisited_scan_findings` 22 · `test_scan_boundary_unwinds` 11 ·
`test_two_phase_star_reconcile` 20 · `test_no_python_syntax_errors` 6 ·
`test_no_undefined_names_in_production` 7.

Four suites fail **identically with the change stashed** (verified by stash, not
by memory), so they are pre-existing:
`test_album_genre_fallback` 4F, `test_genre_consensus_aggregation` 4F,
`test_genre_backslash_splitting` 1F, `test_track_genre_navidrome_fallback` 2F.
`test_per_album_star_posting` 8F is the long-documented set.

## Known limitation, deliberately out of scope

**Pooling still reads `tracks.genres`.** The two rules above are applied to the
track's own identity, but which playlists a track *joins* is still decided by the
album blend. Consequence, measured: Papercut (own tags `nu metal` +
`alternative metal`) lands in **Nu Metal** but not in **Alternative Metal**,
because its album's blend says `nu metal, alternative rock`.

That is defensible — the track is in a metal playlist, which is what was asked —
and it keeps the SET of generated playlists unchanged. Switching pooling to
per-track tags as well would be more consistent, but it would also start
producing `Live` / `Cover` / `Acoustic` playlists on ordinary albums (those tags
are stripped from the blend today, and are not per-track), which is a visible
product change well beyond this request. Flagged rather than done.
