# A remix album is no longer rejected for lacking the word "remix"

**Date:** 2026-10-26
**Area:** `popularity` / `single` (album type detection)
**Files:** `services/popularity/stages/album_stage.py`,
`tests/test_remix_album_type_not_rejected.py` (new),
`tests/test_live_secondary_corroboration_guard.py` (contract reversal)

## Reported symptom

A scan log for Limp Bizkit's *New Old Songs*:

```
[ENRICH] MusicBrainz secondary type rejected
  reason='album title carries no remix marker'
  musicbrainz_type='album+remix' artist='Limp Bizkit' album='New Old Songs'
[POPULARITY] Album enriched: Limp Bizkit - New Old Songs (type=album+remix)
```

Two defects in two lines.

### 1. A genuine remix album was filed as a studio album

*New Old Songs* (2001) is a remix album. MusicBrainz correctly returned
`album+remix`. The corroboration guard in `_mb_type_is_corroborated` discarded
that answer because the album **title** does not contain the word "remix", and a
remix album is rarely obliging enough to be called one — "New Old Songs",
"Version 2.0", "Further Down the Spiral" all fail the check.

On a **fresh, unbound scan** (no stored type to fall back on) the classification
was lost outright — measured, not inferred:

```
fresh unbound scan  -> detected='album'          (before)
fresh unbound scan  -> detected='album+remix'    (after)
```

### 2. The log contradicted itself

"rejected" was immediately followed by `type=album+remix`, because the **stored**
type survived while MusicBrainz's *agreement* with it was thrown away. The
rejection message described a decision the code had not actually made.

## Root cause

The guard treated `+remix` like `+live` and `+acoustic`. It should not.

The guard exists to stop a **bad match** from **renaming files**. For `+live`
and `+acoustic`, `_apply_live_remix_album_tagging` rewrites `title` — so an
unverified match would stamp "(Live)" onto a studio recording. For `+remix`
the same function only sets `is_remix = 1` and injects the "Remix" genre; **the
title is untouched**. The destructive risk that justifies the guard simply does
not exist for this type.

Losing a real remix classification is worse than one album carrying an
`is_remix` flag it did not earn — and the flag is reversible, a wrong title is
not.

## Fix (three parts, in `_mb_type_is_corroborated`)

1. **`+remix` is accepted** without title/track evidence. The evidence is still
   *recognised* when present, and drives the log line: `INFO` when corroborated,
   `WARNING` when not, so a doubtful classification is auditable. The warning
   names the reason the type is kept (`"remix does not retitle tracks"`).
2. **MBID-bound bypass** (all secondary types): if the album's tracks already
   carry a `musicbrainz_releasegroupid`, there is **no text search left to
   protect** — MusicBrainz's secondary type describes that binding. Accepted,
   logged `INFO`.
3. **Stored-type confirmation** (all secondary types): if the album already
   carries a *rich* stored type equal to the incoming one (`album+remix` vs
   `album+remix`), that is confirmation, not a guess. A stored **plain** `album`
   grants no exemption — that is the case where the guard is needed.

## Contract reversal — documented, not quietly deleted

`tests/test_live_secondary_corroboration_guard.py::test_remix_still_requires_a_title_marker`
pinned the old behaviour:

```python
assert _mb_type_is_corroborated("album+remix", "Some Album", [], {}) is False
```

That assertion **was the defect**. It has been replaced by
`test_remix_is_applied_without_a_title_marker` plus two companions, with the
reversal and its reasoning recorded in the docstring.

A second pre-existing bad test was corrected in the same file:
`test_plain_types_always_pass_the_guard` listed `album+compilation` among
"plain types" and had been **failing at baseline** — an unevidenced compilation
is deliberately refused. The list was describing the author's intent, not the
code. `album+compilation` was removed and a control test added that pins the
compilation branch as guarded.

## Verification

New suite `tests/test_remix_album_type_not_rejected.py` (17 tests) covers:

| Group | Pins |
|---|---|
| The reported album | guard accepts it; a fresh unbound scan detects remix; the title genuinely has no marker |
| Remix is not destructive | `+remix` sets `is_remix` + genre; **`+live` does rewrite titles** (control) |
| Live/compilation unchanged | `+live` still needs evidence; unevidenced compilation still refused |
| MBID-first | bound release group accepted for all four secondary types; unbound gets no exemption |
| The contradictory log | stored-type agreement counts as corroboration; `detected` and `mb_type` never disagree about remix |

- **With the fix:** 55 passed across the new and existing guard suites.
- **Oracle:** with `album_stage.py` stashed, 9 of the new tests fail — the suite
  genuinely detects the bug rather than describing current behaviour.
- **Regression sweep** (`album_type`, `live`, `remix`, `compilation`,
  `release_categories`, `track_sync`, `single`, `scan_state`): failure set
  **64 → 52**, with **zero** tests failing only under the fix. The 52 remaining
  failures are pre-existing in this working tree and unrelated to album typing.
