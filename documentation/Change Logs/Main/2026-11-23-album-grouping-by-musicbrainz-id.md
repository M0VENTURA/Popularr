# Albums are grouped by MusicBrainz release id, not by their title

**Date:** 2026-11-23
**Area:** `services/popularity/album_identity.py` (new) ·
`services/popularity/mixed_rip_audit.py` (new) ·
`services/popularity/stages/load_stage.py` ·
`services/popularity/star_resync_service.py` · `routes/misc_routes.py` ·
`test_site/templates/Pages/config.html` · `templates/pages/config.html` ·
`test_site/static/js/pages/config.js` · `static/js/config.js`
**Commit:** `fix(popularity): group albums by MusicBrainz release id, not album title`

## Reported / observed

Two files of one Korn release:

```
15. Here to Stay (T Ray's mix)   musicbrainz_albumid 3c8d8b38-…   releasedate 2002-06-19
01. Here to Stay                 musicbrainz_albumid 3c8d8b38-…   releasedate (missing)
```

Navidrome, with its **default** album PID, files these as ONE album: the rule is
``musicbrainz_albumid|albumartistid,album,albumversion,releasedate`` and the
MusicBrainz id wins whenever it exists. Popularr split them.

## Root cause: the grouping key was two human strings

Both pipelines keyed albums on the album TITLE:

```python
# load_stage.load_candidates (the scan) — before
grouped.setdefault(artist, {}).setdefault(album, []).append(row)

# star_resync_service._load_album_groups (the resync) — before
groups[(key_artist, track["album"].strip())].append(track)
```

So two files of one release became two albums whenever they disagreed about the
album title. The consequence is worse than a cosmetic split: a one-track
"album" has **no distribution to curve a rating across**, so
``_album_curve_star`` returns ``None`` and every album-relative rule — the
curve, the album-z bands, the standout ratios — is measuring an album that does
not exist. The rating was not wrong for the album; it was right for a
one-track fiction.

The resync ALSO never selected the column: ``_TRACK_COLUMNS`` had no
``musicbrainz_albumid`` at all, so even a corrected key would have fallen back
to the name for every row.

## What changed

### One identity rule, shared by both paths

New ``services/popularity/album_identity.py`` mirrors Navidrome's precedence:

| Situation | Key |
| --- | --- |
| both rows carry the release id | ``("mbid", id)`` |
| neither does | ``("name", artist, album)`` |

Deliberately not merged:

* **A row with an MBID and a row without one.** An untagged row cannot be
  *proven* to belong to a tagged release; merging would be a guess.
* **Same title, different MBIDs.** A remaster is a different release that
  happens to share a name.

The first element of the tuple records which rule produced it, so no caller can
compare an MBID key with a name key by accident. Placeholder ids that taggers
write (``[no id]``, ``none``, ``0``) are treated as absent — otherwise two
different untagged rips would "share" an id and be merged.

Both ``musicbrainz_albumid`` (the tag writer's spelling) and
``musicbrainz_album_mbid`` (the proposal service's) are accepted.

### The scan keeps its display name

``load_candidates`` still reports one title per release — the first seen — so
logs, playlists and the album page read as before. Because a merged release now
carries several spellings, the **album filter matches any of them**:
``?album=Here to Stay (T Ray's mix)`` finds the release whose display name is
``Here to Stay``. Matching only the display name would have made the filter miss
an album that exists.

### The resync selects what it groups on

``_TRACK_COLUMNS`` gained ``musicbrainz_albumid`` / ``musicbrainz_album_mbid``,
``_row_to_track`` carries them onto the working dict, and the three
``for (artist, album), rows in groups.items()`` sites derive their display
strings from the rows instead of unpacking a key that no longer has a fixed
shape.

### Mixed-rip audit

New card under **Export Popularity Data** and ``GET /api/popularity/mixed-rips``
(read-only). It lists the releases whose own files disagree about
``releasedate``, ``originaldate``, ``media`` or ``album`` within one MBID —
i.e. the folders that *will* split if Navidrome's PID is customised or it has
not had a **full** scan since a retag. A missing date is shown as
``(blank)`` rather than skipped, because "one file has no date" is exactly the
reported case.

Sorting is most-conflicted first, and unscored rows are excluded: the audit
describes the music library, not an in-progress import.

## Verification

- **The reported case, end to end.** ``load_candidates`` on two Korn rips with
  one MBID returns **one** candidate with both tracks, and the album filter finds
  it by either title.
- 36 new tests across three files. ``test_album_identity.py`` pins the rule
  table above; ``test_mixed_rip_audit.py`` pins that a consistent release is NOT
  reported and that the audit writes nothing; ``test_album_grouping_is_by_mbid.py``
  pins that both pipelines use the shared function and that no site still
  unpacks a fixed two-element key.
- The audit, run against a real-shaped SQLite library, returns exactly the Korn
  release and ignores the clean one.
- Full suite **404 failed / 9818+ passed** against a baseline of 403. The single
  extra is ``test_playlist_song_id_rejections::test_repeated_scan_does_not_rewrite_for_a_known_rejected_id``,
  a collection-order flake confirmed pre-existing in a clean worktree at HEAD.

### One test re-baselined

``test_album_name_filter_matches.py::test_the_load_stage_uses_the_shared_rule``
pinned the literal string ``album_names_match(album_filter, album)``. The intent
of that test — "use the shared tolerant matcher, not an exact comparison" — is
preserved; the matcher is now applied per title. The assertion was updated to
check the shared matcher AND that a merged release is findable by any title,
which is a stronger guarantee than before.

## What this does NOT fix

The Navidrome side. If the album is still split there, the causes are outside
this codebase and are worth checking in order:

1. **A customised album PID.** ``ND_PID_ALBUM`` / ``PID.Album`` set to
   ``album_legacy``, or to a rule with the date before the ``|``, includes
   ``releasedate`` even when an MBID exists — so the two dates split it. The
   default ``musicbrainz_albumid|albumartistid,album,albumversion,releasedate``
   does not.
2. **No full scan since the retag.** PID and tag changes need a full scan; a
   quick scan reuses the stored album ids.
3. **The two albums really did get different ids.** Navidrome's *Get Info* on
   each shows the album id and MBID it actually stored.

Retagging the folder in one Picard pass (match the release, *Clear existing
tags*, save as ID3v2.4 only) writes consistent album-level tags and removes the
duplicate and malformed frames — which is also what makes the files pass the
audit above.
