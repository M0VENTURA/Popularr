# FLAC→M4A conversion and M4A tag writing (2026-10-05)

**Report:**

> Along with FLAC conversion, can we add m4a conversion as well as the ability
> to update tags on m4a files?

Two independent gaps.

## 1. Conversion was a two-value enum checked by hand

`downloads.conversion.mode` only ever understood `flac_to_mp3` and `none`, and
the comparison was written inline in seven places:

```python
if mode in ("flac_to_mp3", "none"):            # download_organize_helpers — a WHITELIST
settings["mode"] == "flac_to_mp3"               # download_organize_helpers
settings.get("mode") == "flac_to_mp3"           # filesystem_service
str(conversion_cfg.get("mode", ...)) == "flac_to_mp3"   # album_service
```

`download_organize_helpers` **whitelisted** the modes it accepted, so any value
it did not know was silently dropped back to `flac_to_mp3` — adding a mode
meant finding every comparison, and missing one meant the new mode quietly did
nothing.

### Changes

- `helpers/config_helpers.py` gains the single mapping everything now uses:
  - `conversion_target(mode)` → `"mp3"` / `"m4a"` / `""`;
  - `is_flac_conversion(mode)` → does this mode convert at all?
- Every inline comparison replaced with one of those two helpers
  (`download_organize_helpers`, `filesystem_service`, `album_service`), and the
  whitelist now accepts anything the mapping knows.
- `tag_file_service.convert_flac_to_m4a()` added alongside
  `convert_flac_to_mp3()`; both delegate to a shared `_convert_flac(target=…)`
  so the ffmpeg availability check, the timeout, the failure reporting and the
  "delete the original FLAC afterwards" behaviour stay in one place. Only the
  codec arguments differ: MP3 keeps `libmp3lame` + `-q:a 0`, M4A uses
  `-c:a aac -b:a <bitrate>`.
- The destination path is derived from the mode everywhere:
  `get_import_destination_path()`, `download_organize_helpers`' target path and
  `album_service`'s `rel_target` all produce `.m4a` when asked.
- Config dropdowns offer **FLAC to M4A (AAC)** in both trees *and* the setup
  wizard (`templates/pages/config.html`, `test_site/…/config.html`,
  `templates/auth/setup.html`, `test_site/…/setup.html`).

## 2. M4A files could be written to at all

`write_tags_to_file` gated on `suffix in (".mp3", ".flac")` and logged
`Unsupported file format` for everything else — so an m4a could be imported but
never updated by the album page, the review, a scan or the queue.

### Changes

- The suffix gate now accepts `.m4a` and `.mp4`, and `_write_tags_atomic`
  dispatches them to a new `write_mp4_tags()`.
- `_mp4_tag_items()` builds the atom map (pure, so it is testable without a
  real audio file):
  - **native atoms** — `\xa9nam` title, `\xa9ART` artist, `\xa9alb` album,
    `aART` album artist, `\xa9day` year, `\xa9gen` genre, `\xa9wrt` composer,
    `trkn`/`disk` as `(position, total)` with `0` = "total unknown";
  - **iTunes freeform atoms** (`----:com.apple.iTunes:<name>`) for the
    MusicBrainz identity fields — `MusicBrainz Track Id`, `Album Id`,
    `Artist Id`, `Release Group Id`, `Release Track Id`, `Work Id`, plus ISRC,
    ISWC, original year/date/title, writer, lyricist and the cover fields.
    That is the scheme Picard uses, so the values round-trip through other
    taggers rather than being trapped in Popularr.
  - the same contract as the ID3/Vorbis writers: `""` **clears** an atom, an
    absent key is left alone, `None` is never written.
- `rating` is deliberately **not** written: MP4 has no standard rating atom, so
  a star written there would be invisible to every player. It would have
  silently claimed success while changing nothing.

## Tests

`tests/test_m4a_conversion_and_tags.py` — 33 tests:

- the mode vocabulary (including a case-insensitive config value and junk);
- `get_import_destination_path` **behaviour**: `.flac` → `.m4a` when asked,
  `.flac` → `.mp3` by default (control), conversion off leaves the path alone,
  and a non-FLAC source is never converted;
- the MP4 atom map: native atoms, freeform MusicBrainz atoms, clear-vs-absent
  semantics, `rating` never written, and the tag shapes real files carry
  (`"1/2"` → `(1, 0)`);
- wiring: all four dropdowns offer the mode, the hand-written whitelist and the
  inline `== "flac_to_mp3"` comparisons are gone, and each conversion module has
  an AAC branch.

Oracle: reverting the ten source files fails **28/33** (the 5 that pass are
behaviours the old code already had — conversion off, non-FLAC source, MP3
default).

Pre-existing failures confirmed in the baseline tree (not caused here):
`test_album_mb_tags_to_files`, `test_metadata_fanout_to_files`,
`test_slskd_backslash_sanitisation`, `test_track_sync_and_live_detection`.
