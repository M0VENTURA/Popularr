# Disc position: one rule for every writer, and an album cache that can finally skip

**Date:** 2026-11-23
**Area:** `services/metadata/disc_number.py` (new) ·
`services/scanning/reconciliation.py` · `services/queue/queue_processing_service.py` ·
`services/downloads/download_completion_service.py` · `services/scanning/payload_builder.py`
**Commit:** `fix(scanning): normalise disc 1 so the album cache can skip and the copy clears it`

## Reported / observed

Two symptoms, one cause.

> "I have it set for the queue that if it finds a track on another release, it
> copies it to downloads, updates the metadata and then copies it to the new
> location. I think something is either not copying the file before updating the
> metadata or not clearing the tags when in the downloads folder."

> "None of these have changed recently, could this be causing the overwrite?"

The Navidrome import log was a wall of:

```
Re-importing album — song IDs match but metadata changed or cannot be proven
unchanged  album='…'  fingerprint_equal=False  dirty=False
```

`dirty=False` on **every** line is the tell. Nothing had been edited — the
comparison itself could not succeed.

## Root cause: three definitions of "disc 1"

`disc_number` is written to the database, written to the audio files, and compared
by the album freshness fingerprint. Three call sites each decided, separately,
what disc 1 means:

| Site | Behaviour |
| --- | --- |
| `payload_builder` | CLEARS `disc_number` on a one-disc release (storing "1" made Navidrome mislabel the album) |
| `download_completion_service` | clears disc 1 unless the release is known multi-disc |
| the queue's group-organize path | **omits the key entirely** |

That last one is the copy bug. `update_file_metadata` treats `None` as "not
supplied" and leaves the frame alone, so a rip tagged `1/2` that was being filed
as a single-disc release kept its downloaded `/2` position.

And because `disc_number` is a FINGERPRINT field, the first and third
disagreement made the fingerprint permanently unequal: the import CLEARS the disc
on a one-disc release, Navidrome keeps reporting `discNumber: 1` for those very
songs, so `should_skip_cached_album` never skipped and **every album re-imported
on every scan**. Reproduced against the real code:

```
Navidrome discNumber=1, DB disc_number=NULL -> False
both disc 1                                  -> True
```

## What changed

New `services/metadata/disc_number.py` — ONE definition, three functions:

* `stored_disc_number()` — what to WRITE. A real position is kept; disc 1 of a
  multi-disc release is `"1"`; disc 1 of a single-disc release is `""` (an
  explicit clear); unknown on a multi-disc release is `None` (leave the stored
  value alone rather than inventing a position).
* `canonical_disc_number()` — what to COMPARE. Disc 1 and "no disc" are the same
  album, which is what lets the cache match. A genuine move (1 → 2) still
  differs.
* `disc_number_int()` / `disc_total_int()` — the `"1/2"` parsing both need.

`album_fingerprint` canonicalises the disc on **both** sides — so the two
builders cannot drift apart again — and the now-dead `_fingerprint` helper is
removed rather than left beside it.

The queue's group-organize payload always supplies `disc_number`, `""` included:
that is what actually clears the frame the rip carried.

## Why NOT simply store disc 1

Tempting, and wrong. The album save CLEARS disc 1 deliberately — it is what fixed
a separate reported split ("disc 1 and disc 0", "one track has disc 1, the rest
are empty"). The fix belongs in the *comparison*, not in writing a meaningless
value back.

## Verification

- On a real MP3: a rip tagged `1/2` filed as a single-disc release ends with the
  frame **cleared**; a genuine `2/2` keeps `2`.
- The reported log line reproduced, then shown to converge:
  `should_skip_cached_album(...) -> True` once the disc agrees, `False` before.
- A real disc move still invalidates the fingerprint, and so does a title change.
- 44 new tests (`test_disc_number_and_album_cache.py`) covering the write rule,
  the compare rule, `"1/2"` parsing, the cache actually skipping, and all three
  call sites using the shared module.
- Full suite 404 failed vs a baseline of 403 — the extra is the confirmed
  pre-existing `playlist_song_id_rejections` ordering flake. The 3
  `test_metadata_fanout_to_files` failures were checked and are pre-existing
  (identical on a clean stash).

## Not done

`tag_file_service.update_file_metadata` still writes a fixed set and never clears
anything else, so downloaded `encoder`, `comment:essentia *`, `acoustid_fingerprint`
and `media` ride through the copy untouched. That is mostly correct — the lookup
cannot replace what it does not know — but if you want a genuine "clear the
downloads tags" pass, that is a separate, larger change.
