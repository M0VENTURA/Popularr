# MBID-first album type, and a completeness gate that cannot loop (2026-10-09)

## Reported

> Does the metadata scan check whether a track/album already has an MBID and
> use that to confirm the release is correct? If not, would it speed the scan
> up?
>
> Does "metadata scan is recent" check that all the metadata fields are filled
> in and run for any missing the main fields — genres from each source, MBIDs,
> Discogs id, etc.?
>
> When a track has no genres from Discogs/Last.fm/MusicBrainz, are the album
> genres added to the track? Then would it only check again on a forced scan?

Three changes, one rule: **ask only for what is actually missing, and only
require what a re-run can actually produce.**

## 1. MBID-first album type (`album_stage.py`)

`_lookup_musicbrainz_album_type` did a blind release-group **search** on every
call — `search_releasegroup_matches(artist, album, limit=3)` → `best =
matches[0]` → `score < 0.6` → `(None, None)`. The album's *own*
`musicbrainz_releasegroupid` was never consulted.

That is expensive as well as wrong: `api_clients/musicbrainz_http.py` caches
recordings, ISRCs and release details, but has **no cache for release-group
searches**. Each one takes a slot on the shared 1 req/s `_strict_throttle()`
budget. So an album that already knew its type and its binding still spent a
throttled request and then threw the answer away.

**Tier 1 — already bound, don't ask.** New `_stored_album_type(tracks)` and
`_stored_release_group_mbid(tracks)` (each returns `""` when the tracks
disagree, so there is no majority vote to invent). When the stored type is
*rich* — `_rich_stored_album_type` truthy, i.e. anything but a bare `"album"`
— **and** a release-group MBID is stored, the function returns immediately with
zero MusicBrainz requests, logging
`[ENRICH] MusicBrainz album type skipped — already on the row`.

A bare `"album"` deliberately does **not** take the fast path. It is the one
value MusicBrainz can still refine — a live, soundtrack or EP hiding behind a
generic tag — so it must keep asking.

**Tier 2 — the stored MBID confirms a weak match.** The `score < 0.6` gate is
what left bound albums typeless: a text match scoring 0.41 was rejected even
when the album's own binding was sitting in the candidate list. After the score
is read, a below-threshold result now scans the candidates for
`id == stored_rg` and adopts it at `score = 1.0`
(`… confirmed by the stored release-group MBID`).

The gate itself is untouched, and only bypassed for a proposal **the album
itself made**:

* a confident (≥ 0.6) match still wins — the stored MBID does not override it;
* a stored id that is not among the candidates is not invented;
* no stored id → the original rejection stands.

## 2. `is_album_incomplete` requires only what a re-run can produce

The check drives "metadata scan is recent", so a rule it can never satisfy
means the album re-runs on every scan forever.

**The endless re-run.** The genre rule was an **OR**:

```python
if not genres or not has_any_source_genre:
    return True, f"track '{title}' is missing genres/tags"
```

When `genres` arrived from an **album blend, a manual edit or a Navidrome tag**,
the per-source columns (`_GENRE_SOURCE_FIELDS` — lastfm, MusicBrainz, Discogs,
ListenBrainz, Spotify, Essentia, manual, navidrome) stay empty *by definition*,
so `not has_any_source_genre` was permanently true. The album was flagged on
every metadata scan and the answer never changed.

Now it needs **both** halves missing, and a source that was consulted counts as
tried even when it returned nothing usable:

```python
sources_tried = has_any_source_genre or bool(
    track.get("lastfm_last_updated") or track.get("listenbrainz_last_updated")
)
if not genres and not sources_tried:
    return True, f"track '{title}' is missing genres/tags"
```

Re-checking a source that has already answered produces the same nothing.

**The two external ids the report named.** A release-group MBID and a Discogs
album id are now checked, each **gated on the evidence that makes it
fillable**:

* release-group → required only when `musicbrainz_releasegroupid` *and*
  `musicbrainz_album_mbid` are both columns on the row, a release is held, and
  the release group is missing. You cannot resolve a release-group you are not
  holding.
* Discogs id → required only when `discogs_album_id` *and* `discogs_genres`
  are both columns and Discogs has genres for this album. If Discogs answered
  for the album, its id exists and we simply failed to store it; if it never
  answered, requiring the id would re-run an album that legitimately has none.

The `"column" in track` guards matter: a caller whose `SELECT` omits a column
must not be penalised for it. *A column the row never had is not a field the
row is missing.*

## Tests

`tests/test_metadata_scan_mbid_first_and_completeness.py` — **24**:

* `TestAnAlreadyBoundAlbumIsNeverLookedUp` — a rich type + MBID spends **zero**
  searches; every rich form (`ep`, `single`, `album+live`, `album+remix`,
  `album+soundtrack`) skips; **CONTROL** a bare `album` still asks, a stored
  type without a binding still asks, and disagreeing rows still ask;
* `TestAStoredMbidConfirmsABelowThresholdMatch` — the reported `score < 0.6`
  rescue; **CONTROL** an unconfirmable proposal is still rejected, a confident
  match still wins, an id outside the candidates is not invented;
* `TestTheGenreRuleNoLongerLoops` — a blended-genre track is complete; **CONTROL**
  a track with no genres and no sources still re-runs, and consulted-but-empty
  sources count as tried;
* `TestTheExternalIdsAreGatedOnFillability` — missing release-group / Discogs id
  fire only when fillable; **CONTROL** no release MBID, no Discogs answer, and
  an absent column all stay complete, and the four original rules still fire.

## Verification

* New suite → **24 passed**.
* **Oracle** — reverting the two source files → **12 failed / 12 passed**. The
  12 failures are exactly the new behaviour; the 12 that pass either way are
  the guards, which must hold before *and* after. Restored → 24 passed.
* **Sweep** — the **39** test files that reference `scan_stage_runner` or
  `album_stage`: **20 baseline failures vs 20 with the change**, `Compare-Object`
  on the sorted `^FAILED` lines = **no regressions, nothing newly passing**.
  (Those 20 are pre-existing stale expectations, unchanged in both directions.)
* Checked that `tests/test_compilation_tracklist_guard.py::TestLookupGate`
  still exercises the real gate: its `_library(*titles)` fixture carries no
  album type or release-group id, so neither tier short-circuits it.

## Not changed

* **Answering the third question directly.** Whether *album-level* genres are
  blended *down* onto a track that has no per-source genres is a separate
  aggregation concern (`genre_aggregation_service` / album tag sync), not part
  of the completeness gate. Only the gate was changed here — say the word and
  I will audit the blend path next.
* **No new genre sources and no per-source staleness windows.** `sources_tried`
  reads the two timestamps that already exist (`lastfm_last_updated`,
  `listenbrainz_last_updated`); the other sources have no update column, so
  requiring one would be a schema change.
* The `score < 0.6` threshold itself is unchanged — it now has a second,
  album-specific way to be satisfied rather than a lower bar.
