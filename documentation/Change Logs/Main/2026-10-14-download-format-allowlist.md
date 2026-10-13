# Downloads: configurable supported formats — reject unsupported containers (2026-10-14)

## What was reported
> I've had a few tracks download as aiff files such as
> `/music/Various Artists/2002 - MTV2 Handpicked/07. Various Artists - Sometimes.aiff`
> These tracks have copied over but not applied the metadata. Can we have an
> option in config for supported filetypes? Have only flac and mp3 enabled by
> default, if any accidentally download that arent that file type it should
> reject them from the download queue rather than matching.

## Why the AIFF landed tagless
`services/metadata/tag_file_service.py` writes tags for
`.mp3 / .flac / .m4a / .mp4` only — everything else hits the
`"Unsupported file format"` branch. So the AIFF was copied into the library
with **no metadata applied**. The download pipeline's only type check was the
`LOSSLESS_EXTENSIONS` set, which **includes** AIFF/WAV/ALAC/APE/WV — so an AIFF
was treated as a premium lossless match and imported.

## Change
New config key **`downloads.quality.allowed_formats`** (default
**`["flac", "mp3"]`**), surfaced on the Config page as an
*Allowed Download Formats* checkbox group (both UI trees). An empty list
disables the check (accept any type).

Three enforcement points:

1. **Search gate** — `SearchFile.matches_quality(..., allowed_formats=...)`
   rejects a disallowed container with reason `format_not_allowed`, BEFORE the
   bitrate/sample-rate rules (those cannot apply to a container we will not
   accept, and an AIFF would otherwise sail through as "lossless").
   `SlskdService.filter_results_by_quality` / `search_and_filter` and the
   manual-search route both thread the allow-list from config.
2. **Manual search** — a disallowed container is dropped from the manual
   result list rather than shown as downloadable (the pipeline would refuse it
   at completion).
3. **Completion gate** — `_file_matches_queue_item` returns
   `(False, "unsupported_format")` for a disallowed container, so a file that
   slipped through is deleted and the queue item rescheduled
   (`_queue_reject_failure_reason` gives it a format-specific message) instead
   of being imported tagless.

`get_search_quality_config()` normalises the list to bare lowercase
extensions (`.FLAC`, `FLAC`, `flac` all mean the same type) and returns a
`set`.

## Tests
`tests/test_download_format_allowlist.py` (16): the getter (default flac+mp3,
normalisation, empty list = no restriction, other keys preserved); the search
gate (AIFF rejected with reason, flac/mp3 pass, filename-derived extension,
`None`/empty = unchanged, `filter_results_by_quality` drops it); the completion
gate (AIFF never matches, format-specific failure message); the Config-page
contract (checkbox group on both pages, both collectors read
`.download-format-check:checked`) plus a **rendered** `/config` assertion that
flac+mp3 are checked by default and WAV is not.

Also updated for the widened signature: `test_manual_search_quality_ranking`
(the helper now forwards `allowed_formats`) and the four `filter_results_by_quality`
fakes in `test_queue_search_early_termination`.

Sweep (13 download/queue/config suites): `test_download_match_lifecycle` shows
the **same 10 failures in isolation as pristine `origin/develop`** → 0
regressions.
