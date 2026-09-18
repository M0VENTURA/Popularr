# Album identity taken from the scanned album, not an arbitrary release

**Date:** 2026-09-19
**Area:** `popularity` / `musicbrainz`

## Reported symptoms

> Albums like 72 seasons were being split into releases such as
> `2023-06-16: M72 World Tour: Gothenburg, Sweden`
>
> The album title was also using the specific release name, not the release
> group name

One studio album shattered into many one-track "albums", each named after a
live tour release, a compilation or a single.

## Root cause

`MusicBrainzService._recording_to_metadata` read the album identity from
`releases[0]`:

```python
releases = recording.get("releases") or []
specific_release = releases[0] if releases and isinstance(releases[0], dict) else {}
specific_title = str(specific_release.get("title") or "").strip()
release_group = specific_release.get("release-group") or {}
release_group_title = str(release_group.get("title") or "").strip()
effective_album = authoritative_album or release_group_title or specific_title
```

A recording lists **every** release it appears on, and MusicBrainz returns them
in **no meaningful order**. A Metallica track from "72 Seasons" that was also
played live on the M72 tour is listed on the tour album too — so `releases[0]`
was frequently the LIVE release. The track then adopted that release group's
name as its album.

The per-track call supplied no album authority at all:

```python
mb_data = mb_service.lookup_recording_metadata(title, artist)
```

whereas the album-batch path already passed `album_name=album` (so
`authoritative_album` won there). That asymmetry is why batch-scanned tracks
behaved and per-track ones did not.

The damage landed via the per-track album guard, which fills an EMPTY album
column — i.e. on any fresh import:

```python
if _mb_album and not _existing_album:
    payload["album"] = _mb_album
```

Each track adopted its own arbitrary release's cluster name ⇒ one folder became
dozens of albums. This is the album-**name** twin of the release-**year** split
fixed in `2026-09-19-album-release-year-unification.md`.

## Fix

### `services/enrichment/musicbrainz_service.py`

New helpers:

- `_release_group_title_of` / `_release_group_primary_type_of` /
  `_release_group_secondary_types_of` / `_release_album_identity` — read a
  release's identity from its **release group**. The release's own `title` is
  the EDITION and must never be the album identity while a group exists.
- `_select_primary_release(releases, album_name)` — chooses the release that
  describes the album being scanned, in order:
  1. the release whose album identity matches `album_name` (fuzzy, floor
     `0.6` — the same floor `_recording_matches_album` already used);
  2. a **canonical studio** release: primary type `album` or untyped, with no
     live/compilation/remix/soundtrack/… secondary type, preferring one that
     carries a release group so the name can never fall back to an edition;
  3. the earliest release date.

  Every stage breaks ties on release id, so the result is **deterministic** and
  does not depend on MusicBrainz's ordering.

- `_recording_to_metadata` now uses the selected release, and
  `release_group_title` comes from `_release_group_title_of`.
- `lookup_recording_metadata(title, artist, *, album=None)` forwards
  `album_name=album`; the module-level wrapper forwards it too.

### `services/popularity/stages/track_stage.py`

The per-track lookup now passes the album being scanned:

```python
_album_anchor = _as_str(album_context.get("album") ...).strip()
mb_data = mb_service.lookup_recording_metadata(title, artist, album=_album_anchor or None)
```

## Behaviour

- A track is pinned to the album being scanned, so a folder stays one album.
- With no anchor available, the canonical studio release still beats a live
  tour album or compilation — so the split is fixed even on paths that supply
  no album context.
- The album name is the release **group**; the specific edition is retained in
  `release_title` for the album page's tagline.

## Tests

`tests/test_album_identity_release_selection.py` — 16 tests.

- `TestSelectPrimaryRelease` (9): the anchor beats an earlier live release; the
  studio release beats live with **no** anchor; a compilation is not preferred;
  a single-release list is returned as-is; empty/non-dict input is handled;
  selection is identical across three input orderings; non-studio-only input
  falls back deterministically; a missing release group does not crash.
- `TestRecordingMetadataUsesReleaseGroupName` (4): the album is the release
  group, never `2023-06-16: M72 World Tour: Gothenburg, Sweden`; holds with no
  anchor; the edition stays in `release_title` only; the anchor overrides a
  different matching release.
- `TestLookupRecordingMetadataAlbumAnchor` (3): the `album` argument pins the
  identity; without it the studio release still wins; incomplete input → `{}`.

Verified by restoring only the two pre-fix modules: the suite fails, and the
final assertion reproduces the reported symptom verbatim —
`assert 'M72 World To...nburg, Sweden' == '72 Seasons'`.

Regression: `tests/test_mb_batch_version_resolution.py` +
`tests/test_single_detection_title_track.py` give **20 failed / 5 passed both
with and without** the change — identical, so no regressions. Those failures
are pre-existing on `origin/develop`.

> Note: those suites share a process-persisted MusicBrainz MBID cache
> (`/config/mbid_cache.json`), so per-test pass/fail can shift between runs.
> They were compared with `MUSICBRAINZ_CACHE_FILE` pointed at a scratch path.
