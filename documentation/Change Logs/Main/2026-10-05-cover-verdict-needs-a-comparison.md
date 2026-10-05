# A cover verdict needs a comparison to exist (2026-10-05)

**Report:** covers on Various-Artists compilations labelled
**"not a cover (work relationship)"**.

## The logic, and the gap

`_flatten_release` derives `is_cover` from the work relationship:

```python
work_credit = work.get("artist-credit") or []
if work_credit:
    work_artist = primary_album_artist(work_credit)
    entry["work_artist"] = work_artist
    if release_key and work_key and release_key != work_key:
        entry["is_cover"] = True
```

`is_cover` is only ever set **when the work carries an artist-credit**. A work
without one leaves `work_mbid` present but `is_cover` *absent* — and `_cover_verdict`
treated absent and False the same:

```python
if mb_track.get("is_cover"):  ... "cover of X"      # absent → skip
if not work_mbid:             ... ""                # work exists → carry on
if has_cover_marker(...):     ... "not a cover"     # ← asserted a negative
```

**Absent is not False.** Silence ("MB gave us no credit to compare") became
certainty ("MB says this isn't a cover"), which is exactly how a genuine cover
on a compilation got dismissed.

The existing test encoded that ambiguity as a fact:

```python
def test_a_work_by_the_same_artist_disproves_a_cover_marker(self):
    note = _title_note("Song (Cover Version)", "Song", {"work_mbid": "work-1"})
    assert note == "not a cover (work relationship)"
    # docstring: "a work relationship exists and credits the same artist"
```

`work_mbid` alone never said anything about a credit.

## Change

`_cover_verdict` now requires `work_artist` — set **only** when a credit
existed and the comparison ran:

| mb_track | verdict |
|---|---|
| `is_cover=True` | `cover of X (work relationship)` |
| no `work_mbid` | *(nothing — no work to base it on)* |
| `work_mbid`, **no `work_artist`** | *(nothing — no comparison ran)* ← the fix |
| `work_mbid` + `work_artist`, marker present | `not a cover (work relationship)` |

`work_artist` reaches the verdict through the same `metadata["tracks"]` entries
that already carry `work_mbid`.

## Tests

`tests/test_metadata_compare_version_markers_and_genres.py`:

- `test_a_work_by_the_same_artist_disproves_a_cover_marker` now supplies the
  `work_artist` its docstring always claimed — the negative verdict keeps its
  real meaning;
- **new** `test_a_work_with_no_artist_credit_says_nothing` — the reported case;
- **new** control `test_no_artist_credit_is_not_the_same_as_a_missing_work`,
  pinning that both return nothing for *different* reasons.

Oracle: stashing the three source files → **6 of 115 fail** — precisely the six
new tests (this file's two plus the four below).
