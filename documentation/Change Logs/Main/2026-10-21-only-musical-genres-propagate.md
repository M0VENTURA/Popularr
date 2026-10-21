# Only musical genres propagate to tracks that have none of their own

**Date:** 2026-10-21
**Area:** `services/enrichment/genre_aggregation_service.py` ·
`routes/ui_routes.py` · `services/popularity/stages/track_stage.py` ·
`services/popularity/stages/finalise_stage.py`
**Commit:** `fix(genres): only musical genres propagate to a track with none`

## Report

> "There are a few genre's that shouldn't spill out to every track from the
> album genres list. Genres like Holiday, Christmas, Cover, shouldn't
> automatically assign to all tracks if the track doesn't have any genre's.
> Only musical genres should propegate to all tracks."

## Root cause — the album list is a FILL, and it carried non-musical tags

The album's genre list fills a track that has no genre evidence of its own.
That is right for a musical genre ("this album is Country, so its untagged
tracks are Country too") but wrong for two families:

* the per-track affiliation markers (Cover/Live/Acoustic/Remix/…) — they
  describe ONE recording's performance;
* the Christmas/Holiday family — they describe the ALBUM's **occasion**, and
  a plain track on a Christmas compilation is not itself a Christmas track.

So one cover on an album stamped "Cover" on every untagged sibling, and one
holiday record on a compilation stamped "Holiday"/"Christmas" on all of them.

⭐ The Christmas exemption in `_TRACK_AFFILIATION_FILTER_TAGS` is NOT a
contradiction. It exists because the **album's own** genre list keeps
Christmas — a Christmas album IS a Christmas album, and the genre-playlist
builder reads it from there. This rule is about a **track inheriting** that
list, which is a different question.

## The fix — one shared rule, applied at every inheritance point

`genre_aggregation_service` gains:

```python
def is_propagatable_genre(value) -> bool:
    """True when an ALBUM-level genre may be INHERITED by a track that has none."""
    return not is_admin_genre(value)
```

`is_admin_genre` already treats the filter tags (Cover/Live/Acoustic/…) and
the admin words (tribute, soundtrack, karaoke, …) as non-genres, so the rule
needs no new vocabulary — it just applies the existing one to the question
that was never asked.

Applied at every place an album/artist-level list reaches a track:

| Path | What it does now |
| --- | --- |
| `ui_routes._apply_album_track_genres` | the album save's FILL writes only musical genres |
| `ui_routes.collect_top_genres` | the artist page's mass-apply chips carry only musical genres |
| `track_stage._album_top_genres` | the album-inheritance list holds only musical genres |
| `finalise_stage._album_top_genres` | same, for the finalise stage's copy (it had NO filter at all) |

The `ui_routes` fallback filter (used only if the helper cannot be imported)
**fails closed** and now includes `christmas`/`holiday` too.

## What was NOT changed

* The **album's own** genre list still carries Christmas/Holiday — a
  Christmas album is a Christmas album, and the playlist builder depends on
  the tag.
* The per-track sources are untouched: a track whose own sources say
  "Christmas" still gets it.

## Tests

`tests/test_only_musical_genres_propagate.py` (new, 11 tests):

* musical genres propagate; Cover, Holiday, Christmas and the other
  affiliation markers do not;
* the album's own list still keeps Christmas;
* the album save's fill writes only the musical genres (and writes nothing
  when the list is all non-propagating);
* the track stage's inheritance list holds only musical genres.

**Oracle:** with the rule reverted, **8 of the 11 fail**; restored → 11
passed.

`tests/test_album_cover_genre_does_not_leak.py` — one assertion was
**changed**: `test_christmas_is_kept` became
`test_christmas_does_not_propagate_to_a_track`, because the new rule
supersedes the old one. The change is documented in the test.

Sweep of 27 genre/cover/album suites diffed against a pristine `develop`
clone — **identical failure sets, 0 new failures.** `import app` → 425
routes.

## Files

* `services/enrichment/genre_aggregation_service.py` —
  `is_propagatable_genre`.
* `routes/ui_routes.py` — `_apply_album_track_genres`,
  `collect_top_genres`, the fallback filter.
* `services/popularity/stages/track_stage.py` — `_album_top_genres`.
* `services/popularity/stages/finalise_stage.py` — `_album_top_genres`.
* `tests/test_only_musical_genres_propagate.py` — new.
* `tests/test_album_cover_genre_does_not_leak.py` — the one changed
  assertion.
