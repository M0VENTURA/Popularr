# Navidrome import reads the file's tags, and gives an unclassified album a type (2026-10-09)

**Area:** scanning / downloads (Navidrome import, manual folder match, album type)
**Commit:** `fix(import): read the file's tags Navidrome never echoes, and type an unclassified album`

## Reported

> I re-imported an album from Navidrome that was downloaded from Soulseek.
> These are the tags added to the track, but the album didn't pick up the tags
> on import from Navidrome.

> I also imported another … a manual match to a release from matched and
> unmatched folders and these are the fields that were imported [a much
> shorter list]. **It also didn't attach the album art for the release.**

> Navidrome imports also aren't assigning an album type. Could it smartly auto
> assign it to Album, EP or Single … Not sure if there is a metadata field
> that shows whether its an album, compilation, EP or Single.

## 1. Navidrome keeps what it cannot send

Verified against Navidrome's own source (`server/subsonic/helpers.go`):
`osChildFromMediaFile` exposes title, ids, ISRC, replaygain, genres/moods,
participants, works and movements — **and nothing else**. The plain `Child`
adds path, disc, year, genre, cover art.

So `barcode`, `catalognumber`, `media`, `script`, `releasecountry`,
`releasestatus`, `language`, `recordlabel`, `releasetype`, `tracktotal`,
`disctotal`, `originaldate`, `musicbrainz_releasegroupid`, `musicbrainz_artistid`
… are read into `model.MediaFile.Tags` and **never echoed on the wire**. An
import of a file tagged elsewhere (a Soulseek download) therefore produced a
row whose album page was blank for exactly those fields.

**Fix — read the file.** It is local and already carries them.

* New `helpers/metadata_reader.read_release_tag_values(path)` →
  `{internal_field: value}`. It reads every MP3 `TXXX` frame plus the standard
  frames (`TSRC`, `TCOM`, `TPUB`, `TLAN`, `TIT1`, `TIT3`, and the `TRCK`/`TPOS`
  `1/3` totals), or every FLAC/Vorbis key, and resolves each through
  `services.metadata.tag_names`. **The key set is derived from
  `clear_keys_for(field)` — the exact set the tag *writer* removes before
  writing** — so reader and writer can never disagree about a spelling, and
  every synonym the app has ever used (`ORIGYEAR`/`ORIGINALYEAR`,
  `TOTALTRACKS`/`TRACKTOTAL`, `LABEL`→`recordlabel`) resolves. An unreadable
  file yields `{}`, i.e. "Navidrome had nothing to say".
* `_merge_release_tags_from_file` in `navidrome_import.py` resolves the path
  (`resolve_music_file_path`, with an absolute-path fallback for
  `ReportRealPath`) and fills **only keys Navidrome left empty** — the wire
  always wins. It runs from `extract_and_backfill_track_metadata`, before
  `build_track_payload`, so `album_tags` still only fills where empty and a
  song-level value still beats an album-level one.

## 2. The manual folder match dropped the release's album half

`_apply_release_metadata_to_files` fetched `fetch_musicbrainz_release_metadata`
— which carries everything — then wrote only what `match_mb_tracks_to_files`
produced: per-track identity. Album-scoped identity (release type/status/
country, album-artist MBID, **release-group MBID**, original date, label,
catalog number, barcode, media) was fetched and discarded. Nothing on that path
fetched artwork either.

**Fix:**

* the same `_album_level_mb_fields` mapping the download-completion import
  uses is merged into every matched file's payload (fill-only). One mapping,
  two paths — they cannot disagree about what "the release's album fields" are;
* new `_release_cover_art(release_mbid, release_group_mbid)` fetches the front
  cover from the Cover Art Archive **once** per folder (release first, then
  group — the album page's own order), caches it on the `album_art` table with
  `source="musicbrainz"` (so a later Navidrome cover may still replace it), and
  embeds `cover_art_data` into every matched file — which is what Navidrome and
  every player actually read. No artwork is not an error.

## 3. There IS a field — and the import now fills it

**Yes:** `musicbrainz_albumtype` is the confirmed column every UI reads
(album list SQL, `category_for_album_row`, the album form, the artist-page
sections); `releasetype` is the tag value. The Navidrome import assigned
neither — `musicbrainz_albumtype` is owned by the album stage of the metadata
scan, and `releasetype` only existed when the file or AlbumID3 carried it.

**The guess goes in `releasetype` ONLY**, never in `musicbrainz_albumtype`.
`_rich_stored_album_type` treats a stored rich type as authoritative, so a
track-count guess written into the confirmed column could outrank MusicBrainz
for ever — and `fa34e0b7` exists precisely because a *local* track count is an
unreliable demoter. Guessing at a blank and overriding an answer are different
questions; the read sites fall back to `releasetype` last, so
`musicbrainz_albumtype` still wins whenever the scan has spoken.

| | |
|---|---|
| `guess_album_type_from_track_count` | 1–2 → `single`, 3–6 → `ep`, 7+ → `album`, unknown → `""` |
| `build_track_payload(album_track_count=…)` | applies it only when neither the file nor AlbumID3 gave a `releasetype` |
| `_FILL_IF_EMPTY_PROTECTED_COLUMNS` | `releasetype` / `musicbrainz_albumtype` move from **never-touch** to **fill-if-empty** — "never" meant an *existing* row could never gain one, because the INSERT half only runs the first time |
| read sites | `category_for_album_row`, the recent-albums SQL, the artist-page album entry, `_stored_type`, and the album form's type all gained `releasetype` as a **last** fallback |

`_POPULARITY_PROTECTED_COLUMNS` is untouched in behaviour for everything else —
scores, ratings, single detection and genres are still never written by a sync.

## Tests

`tests/test_navidrome_import_reads_file_tags.py` — **35**:

* the lookup is built from `clear_keys_for`, a `LABEL` tag lands on
  `recordlabel`, and the synonyms a tagger may have used all resolve;
* file tags **fill gaps only** — a value Navidrome sent is never replaced, an
  unresolvable path and a missing path both read nothing, and the extractor
  really does call the readback;
* the 11-case track-count band table; the guess never touches
  `musicbrainz_albumtype`; a real tag and an AlbumID3 tag both beat it; no
  count means no guess;
* the type is actually *visible* — the artist-page buckets fall back to
  `releasetype`, and the confirmed type still wins;
* the fill-if-empty set holds both type columns, genres/scores are still never
  written, and the UPDATE clause really emits the `CASE`;
* the folder match applies the album-level fields **and** the cover art (CAA
  hit exactly once for a two-file folder), with no artwork available degrading
  quietly.

## Verification

* New suite → **35 passed**.
* **Oracle** — reverting all eight source files → **30 failed / 2 passed /
  3 errors** (the 2 that pass are the controls that must hold either way, plus
  two errors from an import that no longer exists). Restored → 35 passed.
* **Sweep** — the 47 test files referencing the touched modules, clean
  `origin/develop` vs this change: **baseline 38 failures, changed 38**,
  `Compare-Object` on the sorted `^(FAILED|ERROR) tests/` lines = **identical,
  0 regressions**.
* `import app` → **392 routes**.

## Not changed

* **Navidrome's own behaviour.** Fields it does not expose stay unavailable
  through the API; the file is the source, not a patch over the wire format.
* **`musicbrainz_albumtype` is still only ever written by the scan** (and by
  the album page's Save). The import never guesses into it.
* **No album art is fetched for a Navidrome import** — Navidrome supplies its
  own cover, and `album_art_service` already orders Navidrome first. The new
  fetch is specific to the manual folder match, which had no source at all.
* The 0.55 fuzzy floor, `_track_number_pairing_allowed` and the type
  precedence inside `_detect_album_type`.
