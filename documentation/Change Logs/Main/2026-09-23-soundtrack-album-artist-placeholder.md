# Soundtrack albums/EPs: the artist was the literal placeholder "Soundtrack"

**Date:** 2026-09-23
**Area:** `services/popularity/stages/album_stage.py`
**Status:** fixed, guarded by `tests/test_soundtrack_artist_and_type.py`

---

## Symptom

Albums (and EPs) that are genuinely soundtracks get their **artist** set to the
literal value `Soundtrack`. Confirmed with the user: the artist name is
`Soundtrack`, the stored album type is `album+Soundtrack`, and **no MBID is
saved on the releases**.

That combination was unrecoverable, for three independent reasons.

## Root cause 1 — the repair refused to run without an MBID

`_correct_soundtrack_album_artist` bailed out immediately:

```python
if not release_group_mbid:
    return
```

The legacy scan wrote `album_artist = "Soundtrack"` and never stored a
release-group id, so the guard fired on exactly the reported albums and the
placeholder was **permanent** — nothing else in the codebase ever rewrites
`album_artist`. Verified by probe on `origin/develop`:

```
=== the reported state: placeholder artist, NO MBID saved ===
  album_context after   : {}
  track album_artist    : 'Soundtrack'
  MB lookups attempted  : 0
  -> REPAIRED? NO — placeholder survives
```

Fix: a new `_resolve_soundtrack_placeholders()` resolves the real performer from
two sources, in order of authority:

1. the release-group's **own artist credit** (exact, when an MBID is stored);
2. a MusicBrainz release-group **search by album title** — the path that makes
   the no-MBID case heal at all.

It fails **closed**: a match is adopted only at `match_score >= 0.8`, and a
credit that is itself `Soundtrack` is rejected. A wrong artist is worse than the
placeholder, because the placeholder is at least recognisably wrong and is
filtered out everywhere (`TRACK_ARTIST_PLACEHOLDERS`, `_GENERIC_COMPILATION_ARTISTS`, …).

## Root cause 2 — the correction was overwritten in memory

```python
if album_context:
    album_context["album_artist"] = mb_credit_name
```

The caller routinely passes an **empty dict**, and an empty dict is falsy — so
the corrected artist was written to the DB and then immediately overwritten by
the track stage's stale in-memory copy. Now `is not None`.

## Root cause 3 — the stored TYPE was discarded for these albums

`_detect_album_type` consulted the stored type only **after** the
`_COMPILATION_ARTISTS` branch, and that set contains `"soundtrack"`. So any album
whose artist/album_artist is the placeholder returned `album+compilation` no
matter what was stored:

```
album_artist='Soundtrack'  stored='ep+soundtrack'    -> 'album+compilation'   ← EP identity LOST
album_artist='Soundtrack'  stored='album+soundtrack' -> 'album+compilation'   ← manual choice LOST
```

An EP lost its EP-ness (the registry files `ep+soundtrack` under EPs but
`album+soundtrack` under Soundtracks), and a manual "Album (Soundtrack)" choice
was silently overwritten on the next scan.

Fix: new `_rich_stored_album_type()` is consulted **first**. A stored
`primary+secondary` composite is returned **verbatim** — the primary is what
distinguishes an EP from an album, so `ep+soundtrack` must not become
`album+soundtrack`. A bare `ep`/`single` is also honoured; only a bare `album`
falls through to the heuristics (it carries nothing a title guess cannot refine).

**`"Soundtrack"` is a placeholder album ARTIST, not an album TYPE** — that
separation is the whole point. A genuine soundtrack is identified by its stored
or MusicBrainz type.

### Also: `soundtrack` was missing from the MusicBrainz mapping

`_lookup_musicbrainz_album_type`'s secondary-type table had no `soundtrack`
entry, so a release-group MusicBrainz correctly flagged as a soundtrack resolved
to a bare `album` and the type was silently lost. The table is now ordered by
`release_categories._PRECEDENCE` (compilation → soundtrack → live → …), and a
`single`/`ep` primary is preserved as `ep+soundtrack` rather than flattened.

## Validation

Oracle worktree at `origin/develop` `2e30b5e4`.

| Suite | Unpatched | Patched |
|---|---|---|
| `tests/test_soundtrack_artist_and_type.py` (new, 26) | **14 failed / 12 passed** | **26 passed** |

**Regression sweep** (12 album-type / category / compilation suites):

| Tree | Result |
|---|---|
| unpatched | 29 failed / 331 passed |
| patched | 15 failed / 345 passed |

`Compare-Object` of the failing sets: **14 FIXED (all the new suite), 0 NEW**.
The same 15 pre-existing failures appear on both trees (stale expectations and
harness signature drift, verified identical — e.g.
`test_mb_secondary_live_type_maps_to_live` asserts `('album', 'rg1')` while both
trees return `('album+live', 'rg1')`).

## Files

* `services/popularity/stages/album_stage.py` — `_resolve_soundtrack_placeholders`
  (new), `_rich_stored_album_type` (new), `_RICH_TYPE_MARKERS` (new),
  `_detect_album_type` precedence, `_correct_soundtrack_album_artist`,
  `_lookup_musicbrainz_album_type` secondary mapping.
* `tests/test_soundtrack_artist_and_type.py` — new guard.
