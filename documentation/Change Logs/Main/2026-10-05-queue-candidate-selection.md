# Queue matching: keep the file we were looking for, and say why we didn't (2026-10-05)

**Report:** *"The download queue doesn't seem to be matching properly"* — backed
by a week of logs showing the same tracks failing as
`no_qualifying_result (50 candidates)` with **attempt 25 / 27 / 30**, i.e.
every cycle for days, while other tracks in the same log download fine.

## What the logs actually showed

`candidates=50` appeared for almost every failure — Interpol, Valkyrie's Fire,
Ignea, Comeback Kid, Everlast — with `queries=4…21`. Exactly 50 is not a
coincidence: `filter_results_by_quality(max_results=50)`.

## Cause 1 — the cap decided what the queue could ever see

```python
qualified.sort(key=lambda item: (-item["bitrate"], -item["sample_rate"]))
return qualified[:max_results]
```

`search_and_filter` calls that on **every poll**, and each poll returns the
*whole* result set so far — so the top-50-by-bitrate of the complete list is
all that ever reaches the accumulator. Combined with the fallback queries
(which deliberately get broader: a bare title, then the artist's first word),
the 50 kept files are chosen by **loudness, not relevance**, and the file
being searched for is frequently not among them. The scorer then correctly
reports that none of those 50 qualified.

**Fix:** rank by how much each file matches the query that produced it, and
only then by bitrate/sample-rate. The cap is unchanged (bounded work), and
the ranking is gated on `query` so a caller without one keeps the old
behaviour.

## Cause 2 — nothing said which gate fired

All four hard gates (`year_mismatch`, `no_artist_evidence`, `title_mismatch`,
`album_mismatch`) log at **DEBUG**, and the accept floor is a bare
`No result met min_score`. The normal log therefore said only *"50
candidates"* — enough to see the symptom, never the cause.

**Fix:** `_score_result` counts each rejection into an optional `rejects`
dict (the parameter is optional, so every existing caller/test is untouched),
and `_select_best_result` emits **one WARNING** when nothing qualifies:

```
No result met min_score — no qualifying download candidate
  artist=… title=… album=… min_score=45.0 top_score=31.0
  candidates=50 rejected={'no_artist_evidence': 47, 'album_mismatch': 3}
  top_candidate='…'
```

`below_floor` is counted separately from the hard gates: a candidate that
scored 31 points is a *different* problem (threshold/quality) from one that
was rejected outright (identity), and the two point at different fixes.

## Tests

`tests/test_slskd_candidate_selection.py` — **8 tests**:

- the query-matching file survives a `max_results=1` cap even though it is
  the *quieter* of the two (the old ordering would fail this);
- without a query, quality still decides (the ranking is query-gated);
- relevance outranks bitrate across many files, and the bitrate floor still
  applies (relevance cannot smuggle in a sub-floor file);
- no candidate qualifies → **one** WARNING naming the gate, with
  `candidates`, `min_score`, `top_candidate`;
- below-the-floor is counted separately (`min_score=999` makes the case
  deterministic — nothing was rejected);
- a qualifying candidate still selects, with no warning;
- `_score_result` keeps its original five-argument signature.

Oracle: stashing both service files → **6 of 8 fail** (the 2 that pass are
the controls that do not depend on the change).

## Not changed

`min_score = 45.0` and every gate's thresholds are untouched. The reported
symptom was that we were scoring the wrong *set* of files and then not saying
so — neither is fixed by moving the threshold.
