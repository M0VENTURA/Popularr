# The Genre vocabulary's `scope` and `noise_values` blocks are now enforced

**Date:** 2026-11-23
**Area:** `services/enrichment/genre_vocabulary.py` ·
`services/enrichment/genre_scope.py` (new) ·
`services/enrichment/genre_selection.py` ·
`services/enrichment/genre_aggregation_service.py` ·
`services/enrichment/genre_resync_service.py` ·
`services/enrichment/genre_vocabulary.json`
**Commit:** `fix(genres): enforce the vocabulary's per-genre scope and noise_values`

## Reported / observed

The Genre → Playlist Resync did not follow the vocabulary's own rules. The
vocabulary is schema **v4**; the parser and the shipped default were **v2**, and
nothing ever read the newer blocks.

Three things were declared in the JSON and enforced by nobody:

| Declared | Consequence measured |
| --- | --- |
| `soundtrack` / `film_score` / `video_game_music`: `require_confirmation: true`, `confirm_release_types: ["Soundtrack"]` | Three track-level "Soundtrack" tags on a plain studio album produced `Soundtrack` as a final genre |
| `christmas` / `comedy` / `soundtrack`: `slot: "marker"` | Markers did not exist as a concept — markers were the hardcoded `_FILTER_TAGS` list in Python, so a vocabulary could not change them; and a lone Christmas tag was dropped as a lone family outlier |
| `noise_values`: 38 listed values | 4 still reached the final genres, and every one contains a separator |

The four survivors were exactly the values that would fail:

```
"alternative & punk"      -> Alternative Rock + Punk
"folk, world, & country"  -> Country
"& country"              -> Country
"folk, world"            -> Folk + World
```

## Root cause: the schema and the parser had drifted

`GenreEntry` had **no `scope` field**, so `_parse_scope` did not exist and the
whole block was discarded on load. `Vocabulary` had no `noise_values` field, for
the same reason.

Even with the field added, the rules still could not have fired:
`select_album_or_track_genres()` took **no album title and no release type**, so
`confirm_album_title_patterns` had nothing to match against and
`confirm_release_types` had nothing to compare — and
`_aggregate_genres_v2` accepted `context_album` only to drop it before calling
the selector.

Finally, `_append_extra_genres` re-added `Soundtrack` and `Christmas` from the
hardcoded `_FILTER_TAGS` list *after* selection, reinstating exactly what
`require_confirmation` exists to prevent. That is why fixing the selector alone
still produced `['Soundtrack']` on a plain album.

## What changed

### Scope is parsed, validated and applied

New `GenreScope` (frozen dataclass) with `levels`, `slot`,
`confirm_release_types`, `confirm_album_title_patterns`,
`confirm_title_patterns`, `require_confirmation` and
`artist_requires_min_sources`. `_parse_scope` accepts it, and the family
back-fill in `_build` now **carries it through** — dropping it in that rebuild
re-discarded every rule a second time, with the file parsed and the value still
arriving empty.

New `services/enrichment/genre_scope.py` applies the rules in one place:

1. **`levels`** — checked before the candidate exists, so a genre that requires
   album evidence cannot be created by a stray track tag and then merely rank
   low. An empty `levels` means "the policy default", never "no levels".
2. **`artist_requires_min_sources`** — judged on the finished candidate, using
   `source_group_count` so `musicbrainz` + `listenbrainz` count as ONE voice
   (ListenBrainz genres are a projection of MusicBrainz's).
3. **`require_confirmation`** — a genre is rejected unless a confirm rule
   matches. Without it the confirm rules are pure support: they never reject.
4. **`slot: "marker"`** — split out **before** `trim_to_final`, so a marker
   cannot occupy one of the four slots and silently displace a musical genre.
   Markers are appended after the musical ones and returned separately.

Validation now rejects an unknown level, an unknown slot, and an unrunnable
regex — a bad pattern simply never matches, which would mean a genre is either
never created or never confirmed, with no error anywhere.

### The context reaches the selector

`select_album_or_track_genres`, `collect_candidates`, `_aggregate_genres_v2`,
`_aggregate_from_sources` and `aggregate_genres` all take
`album_title` / `track_title` / `release_types`. The resync selects
`musicbrainz_albumtype` and splits it, because MusicBrainz reports
`"Album + Soundtrack"` and the scope names the secondary type.

### `noise_values` is parsed and honoured

Stored on the `Vocabulary` as normalised tokens. The check runs on the **whole
raw value before tokenisation**, which is the only place it can work: by the
time `"alternative & punk"` has become `["alternative", "punk"]` the phrase that
was listed is gone. A per-token check also runs, so a delimited list
(`"Rock; vocal"`) still drops the listed token.

Matching is exact on the normalised form, so `"1–4 wochen"` (en dash) matches
`"1-4 wochen"` while listing `"folk, world"` does not drop plain `"folk"`.

### Markers are exempt from the outlier rule

`remove_family_outliers` drops a family that is a tiny minority of the evidence.
A marker is its own family by construction, so the only Christmas tag on a metal
album was removed — and the playlist builder lost the marker it filters on. A
declared marker is now exempt, because a marker never competes for a slot so the
rule has nothing to protect.

### The shipped default documents v4

The repo's default declared none of these keys, which is how a schema came to be
written down without ever being implemented. `christmas` and `soundtrack` now
declare their real scope, `noise_values` is present, and the header documents
the fields.

## Verification

- **The reported cases, end to end**, against the shipped default:

  ```
  plain studio album              -> []
  album title says "…Soundtrack"  -> ['Soundtrack']
  release type "Soundtrack"       -> ['Soundtrack']
  ```

  and `max_genres=3` with four metal tags plus a Christmas tag yields
  **three** musical genres plus `Christmas` as a marker.
- 29 new tests (`test_genre_scope_is_enforced.py` 19,
  `test_genre_noise_values.py` 10) covering all four scope rules, the
  parse/validate path, the family-back-fill trap, and every reported noise
  value.
- Genre-related suites: **305 passed**.
- Full suite **404 failed** against a baseline of **403**. The single extra is
  `test_playlist_song_id_rejections::test_repeated_scan_does_not_rewrite_for_a_known_rejected_id`,
  a collection-order flake previously confirmed pre-existing in a clean worktree
  at HEAD. `test_genre_metal_over_rock_policy::test_a_weakly_supported_companion_still_loses_its_slot`
  was also checked and **fails identically without these changes** (it pins a
  reason code the engine no longer emits).

## Still not implemented

`evidence_policy` — `level_weights`, `positions`, `corroboration`,
`tag_dilution`, `hybrid_support` and `markers` — is still inert: the scores come
from `genres.selection` in `config.yaml`. It is a coherent second design of the
same scoring, and implementing it would need its own pass with a preview before
the library is re-rated. `markers.max_markers` is the one key now read, from
config, as the append cap.

`scope.confirm_title_patterns` is honoured, but a Christmas marker with
`require_confirmation: false` is still created from any track tag — that is what
the declaration says, so it is left as written.
