# Mislabelled track numbers hid missing tracks, and the composer log wall

**Date:** 2026-11-27
**Commit:** `fix(missing): a position taken by another song proves nothing; dedupe debris logs`

## 1. "Missing tracks doesn't pick up a track if another track has the wrong track number"

Reported as: *"A track is track 4, but labelled as one, so it didn't properly
load 1 or 4."*

**The mechanism.** `lib_by_position` maps `(disc, track_number)` to whatever file
sits there. Track 4's audio tagged as track 1 therefore occupies position 1, and
the release's track 1 looked **present**. The pairing rule could not tell,
because it rejects a position only on an agreeing title or a *disagreeing*
length — and a freshly imported row usually has no duration yet, while an unknown
length is deliberately permissive (that leniency exists so an untagged file is
not lost).

I reproduced it before changing anything: an album whose library held tracks
2, 3, 5 plus track 4's audio mislabelled as 1 returned `missing: []`, under both
known and unknown durations. Track 1's audio is genuinely absent and it was
never offered.

**The fix.** The occupant's own title says whose slot it is. Before a position is
treated as proof, the check now resolves whether that row names a *different*
track of this release — and if it does, the number belongs to that track and
cannot prove this one present:

```python
_occupant_key = _title_match_key(_lib_title)
_belongs_to_another = bool(
    _occupant_key
    and _occupant_key != norm
    and any(position != (mb_disc, mb_num)
            for position in _mb_positions_by_title.get(_occupant_key, set()))
)
```

This does **not** loosen the pairing rule itself. It only decides whether the
position is evidence at all. A correctly numbered album, and a row whose title
disagrees with every track of the release, both behave exactly as before — the
second case still gets the number's benefit of the doubt.

After the fix the same album returns `['One']` under both known and unknown
durations: track 1 is offered, and track 4 is not (its audio is present, just
mislabelled).

The dropped position is counted as `excluded["mislabelled_position"]` so it is
visible in diagnostics rather than silently vanishing.

## 2. The composer log wall

The pasted log was hundreds of near-identical lines:

```
Ignoring a malformed local value instead of protecting it field='composer'
local="Joacim Cans, Oscar Dronjak, ['Joacim Cans', 'Oscar Dronjak']"
```

**The detection and the repair are correct** — that value really is list-repr
debris, and skipping protection for it is what lets Navidrome's clean value
repair the row. The problem is volume: `local_authority_conflicts` runs for every
authority field of every imported track, so a library polluted by an old writer
emitted the same line hundreds of times per scan and buried everything else.

Now logged once per `(field, value)` pair, with a bounded set (500 entries, then
it clears rather than stop logging — resuming is the safer failure). A
genuinely different corruption still reports.

## Note on the rest of that log

- `Removed N stale track(s) no longer in library` — expected import behaviour.
- `Navidrome rescan already running — coalesced into follow-up` — the coalescer
  working as designed.
- `getScanStatus` / `getCoverArt` `ReadTimeout` warnings — Navidrome was busy
  scanning 70,120 files. Retried and succeeded.

## Tests

- `tests/test_mislabelled_track_number_and_missing.py` — 6 cases: the reported
  album (known and unknown durations), a correctly numbered album still reporting
  exactly its real gap, an agreeing title at its own number still pairing, a
  title variant at its own number, and the diagnostic counter.
- `tests/test_debris_logging_is_deduplicated.py` — 6 cases: one line per value,
  different values and different fields still logged, the set stays bounded, the
  debris repair still happens, and a real local correction is still protected.
