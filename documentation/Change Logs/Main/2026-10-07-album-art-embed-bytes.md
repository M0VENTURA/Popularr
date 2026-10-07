# Album art embed failed with "data has to be bytes" (2026-10-07)

## Reported

```
[ERROR] [services.metadata.tag_file_service] Failed to embed album art in
/music/ivri/2026 - evidence of you/01. ivri - SABOTAGE.mp3:
data has to be bytes
```

…repeated for **every track of that album**, MP3 only.

## Root cause — a Postgres BYTEA read straight into mutagen

Two facts combine:

1. `album_art.image_data` is declared **`BYTEA`** (`db/schema.py`), and the sync
   engine is **`postgresql+psycopg2`** (`db/engine.py`) — psycopg2 returns
   `BYTEA` as a **`memoryview`**, not `bytes`.
2. mutagen requires **exactly** `bytes`. Its `BinaryFrame.data` setter raises
   `TypeError("data has to be bytes")` for a `bytearray`, a `memoryview` or a
   `str`. Verified against the installed mutagen:

   | value | result |
   |---|---|
   | `bytes` | accepted |
   | `bytearray` | **rejected** `TypeError: data has to be bytes` |
   | `memoryview` | **rejected** `TypeError: data has to be bytes` |
   | `str` | **rejected** `TypeError: data has to be bytes` |

The path: `download_completion_service._fetch_import_art` →
`album_art_service.get_or_fetch_album_art` → `fetch_album_art_record` (returns
`row[0]`, the raw BYTEA) → `embed_album_art(file_path, memoryview, mime)` →
`APIC(data=memoryview)` → TypeError.

It returned early, before any provider call, exactly when
`navidrome_art_may_replace(source)` was False — i.e. for the sources
`navidrome`, `upload`, `url`. That is why the failing album produced **no**
"Fetched MusicBrainz/CAA art" line while the album that did appear in the log
succeeded: different branches.

**The smoking gun is an inconsistency inside one file.** `write_tags_to_file`
— in the *same* module — already coerces:

```python
tag_obj.add(APIC(..., data=bytes(value)))   # coerced
```

while `embed_album_art` passed the value straight through:

```python
audio.tags.add(APIC(..., data=image_data))  # NOT coerced
```

That is why the album-page art path (`apply_album_art_to_tracks` →
`write_tags_to_file`) worked while the download-import path failed.

## The change

Fixed at **both** boundaries.

**`services/metadata/tag_file_service.py::embed_album_art`** — normalise the
payload before mutagen sees it:

* `bytearray` / `memoryview` → `bytes(...)`;
* anything else (a `str` means a path or base64) is **refused with a clear
  error** rather than encoded, which would have silently embedded garbage.

**`db/repositories/metadata.py`** — new `_as_bytes()` used by
`fetch_album_art_blob` and `fetch_album_art_record`, so a stored blob is always
returned as `bytes`. The conversion belongs at the read boundary where the type
contract is defined, not at each caller. `None` and a `str` pass through
unchanged.

## Files

* `services/metadata/tag_file_service.py` — `embed_album_art` coercion + refusal
* `db/repositories/metadata.py` — `_as_bytes`, applied to both art readers
* `tests/test_album_art_embed_accepts_db_blob_types.py` — 17 tests (new)

## Tests

17 tests: the mutagen premise (real `APIC` rejects `bytearray`/`memoryview`/`str`
with that exact message); `embed_album_art` embeds `bytes`/`bytearray`/
`memoryview` and the frame holds `bytes` unchanged; a `str` is refused and the
file is never even opened; empty/missing inputs stay no-ops; both DB readers
normalise a `memoryview` row; and a CONTROL that the two readers agree.

The MP3 stub replaces only the MPEG/ID3 I/O — the `APIC` frame under test is
genuine mutagen, so the validation is real.

**Oracle:** reverting the two source files → **8 failed / 9 passed** (the
non-bytes embeds, the `str` refusal, and all four reader normalisations); the
`bytes` cases and the mutagen-premise tests stay green as controls. Restored →
17 passed.

**Suite sweep:** 14 art/tag/metadata/import suites, run in a clean
`origin/develop` worktree and in the changed tree —
**BASE `14 failed, 212 passed` = NEW `14 failed, 212 passed`, `Compare-Object`
empty** → identical failing sets, all pre-existing.

> ⚠️ HARNESS NOTE: the first attempt at this sweep reported `base=0` — a FALSE
> all-clear. The shared file list had been built from the CHANGED tree, so it
> contained the brand-new test file; in the baseline pytest aborted with
> `ERROR: file or directory not found: …` and ran **nothing**. The sweep now
> asserts each tree produced a real pytest summary before comparing.
> A related effect: running all 81 matching files in ONE process produced no
> summary for the baseline at all, so the sweep uses a focused list.

## Not changed (flagged, not guessed)

`services/enrichment/album_art_service.py::get_or_fetch_album_art` contains a
branch that can never run:

```python
data = fetch_album_art_from_navidrome(artist, album)   # overwrites the stored blob
if data:
    return data, "image/jpeg"

# "Navidrome has no copy of this album: whatever we already hold STANDS,
#  rather than re-downloading the same provider art it came from (``data``
#  here is still the stored blob from the read at the top of this function)."
if data:                       # ← tests the NAVIDROME result, so always False here
    return data, mime or "image/jpeg"
```

The comment is factually wrong — the assignment above overwrote the stored blob —
so when Navidrome has no art the stored provider art is discarded and re-fetched
from the providers on **every** call. That matches the four identical
"Fetched MusicBrainz/CAA art from release …" lines in the same log (one per
track of the album being imported).

Fixing it means capturing the stored blob in its own variable, which also stops
the redundant per-track provider fetches — but it changes WHICH art is chosen
(the stored copy would stand instead of being re-fetched), and the
Navidrome-first policy was itself a deliberate earlier change
(`2026-09-21-album-art-navidrome-first.md`). Left alone pending that decision.
