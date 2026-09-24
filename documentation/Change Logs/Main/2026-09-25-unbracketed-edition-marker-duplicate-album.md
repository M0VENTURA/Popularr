# An unbracketed edition marker duplicated the album on every import

**Date:** 2026-09-25
**Area:** `helpers/normalization_service.py`
**Reported:** Some albums carry the MusicBrainz release comment in the album
name with no brackets — e.g. **`The Mirror's Truth Version`** — and

1. it "isn't being corrected during a metadata update", and
2. it "is overwriting as a new version on every import".

## Root cause

**Every album-name rule in `helpers/normalization_service.py` is
BALANCED-BRACKET anchored** (`\s*\(...\)\s*$`). A tag source that writes the
marker *without* brackets is therefore invisible to all of them **at once**,
producing two independent failures that the reporter saw as one symptom.

### 1. The match/lookup key kept the marker

`normalize_title_for_lookup` runs `strip_parentheses`, so a *bracketed*
annotation is dropped from the key — but an unbracketed one is not:

| album name | lookup key |
|---|---|
| `The Mirror's Truth` | `the mirror s truth` |
| `The Mirror's Truth (Version)` | `the mirror s truth` |
| `The Mirror's Truth Version` | **`the mirror s truth version`** |

So the **same album** under the two spellings yields **two different keys**.
The release then reads as "missing" while also being owned, and every import
writes another copy. Verified by probe before the fix:

```
"The Mirror's Truth (Version)"   lookup-match=True
"The Mirror's Truth Version"     lookup-match=False   <-- the duplicate
```

### 2. The repair was a no-op

`repair_annotations` and `strip_album_edition_marker` both left the unbracketed
form **untouched**, so a metadata update never corrected the stored name — the
reported "isn't being corrected during a metadata update".

`has_edition_annotation` did not even **recognise** it
(`"The Mirror's Truth Version"` → `False`, `"The Mirror's Truth (Version)"` →
`True`), so every caller keyed off that predicate also saw a plain album name.

Measured before the fix — every function is a no-op on the reported string:

```
strip_album_edition_marker("The Mirror's Truth Version") -> unchanged
repair_annotations(          "The Mirror's Truth Version") -> unchanged
clean_album_name_for_storage("The Mirror's Truth Version") -> unchanged
has_edition_annotation(      "The Mirror's Truth Version") -> False
```

## The change

**One transformation, reusing the existing vocabulary.** There is deliberately
**no second parallel rule set** — that is exactly how the historical
`tour` / `(tour edition)` divergence documented in this module happened, where
two keyword lists disagreed and a marker was visible to one rule and invisible
to another.

New `bracket_trailing_edition_marker()` rewrites an unbracketed trailing marker
into its bracketed form **before any other rule runs**:

```
"The Mirror's Truth Version"     -> "The Mirror's Truth (Version)"
"American Idiot Deluxe Edition"  -> "American Idiot (Deluxe Edition)"
```

After that single rewrite, every pre-existing bracket-anchored rule applies
unchanged, so both spellings travel the **same code path**.

Wired into the four places that must see it:

| function | why |
|---|---|
| `clean_title` | both lookup keys are built through it (§1) |
| `has_edition_annotation` | predicate read the bracketed form only (§2) |
| `repair_annotations` | the stored-name repair path (§2) |
| `strip_album_edition_marker` | the lookup-key stripper (§1) |

`clean_album_name_for_storage` and `resolve_album_name` inherit the fix through
`repair_annotations`, so the metadata-update path now emits the correction.

### Guard rails found while implementing

Three traps, each caught by a probe or a test rather than by reading:

- **Double bracketing.** The first implementation re-bracketed an
  *already*-bracketed `"X (Version)"` into the malformed `"X ((Version))"`.
  Fixed by returning bracketed input untouched — the other rules already own
  it. Pinned by `test_no_doubled_brackets_are_ever_produced`.
- **Form markers must survive.** `"X Live Version"` was being rewritten to
  `"X Live (Version)"`, which the bracketed stripper then removed — silently
  deleting the form. The bracketed equivalent `"X (Live Version)"` is
  deliberately preserved (a form names a different **recording**, not a
  different **pressing**), so the two spellings must behave identically. Fixed
  by letting the backward token sweep consume form words so the run can be
  **recognised and then refused**. Pinned by `test_form_markers_are_preserved`.
- **Never blank a name.** A name that is *only* a marker (`"Version"`) returns
  the original, matching the existing convention in
  `strip_album_edition_marker`.

A backward token sweep (rather than a regex) is used so only genuine trailing
marker tokens are consumed: in `"The Mirror's Truth Version"` just `Version`
qualifies, because `Truth` is not a marker token. Pinned by
`test_a_trailing_word_that_is_not_a_marker_is_kept`.

## Verification

**NEW `tests/test_unbracketed_edition_marker.py` — 52 tests**, asserting on the
failure each prevents:

- both spellings collapse to **one** lookup key (the duplicate mechanism);
- every edition variant (`Version`, `Deluxe Edition`, `Remastered`,
  `Collector's Edition`) matches the plain title;
- the repair is **idempotent** and never double-wraps;
- form markers and ordinary titles are **untouched**;
- `has_edition_annotation` now recognises the unbracketed form.

**Oracle (worktree at HEAD, my changes uncommitted):** ran the broadened sweep
`-k "album or normalize or edition or title or naming or metadata_name"`
(1128 tests) on both trees and diffed the failing-id sets:

| | failures |
|---|---|
| baseline (HEAD) | 132 |
| with this change | 132 |

`Compare-Object` reports **zero new failures and zero fixed** — every one is
pre-existing and unrelated. Two of them
(`test_release_categories.py::TestAlbumRow::test_no_type_falls_back_to_title`,
`test_album_release_track_identity.py::...::test_the_identity_is_marked_unambiguous`)
were separately confirmed to fail at HEAD.

## Behaviour change to be aware of

`has_edition_annotation` now returns `True` for `"X Version"`. It has **no
production callers** (verified by grep — defined in
`helpers/normalization_service.py`, referenced only by tests), so this is a
correctness improvement with no behavioural reach today.

The fix is deliberately **conservative about what it rewrites**: it re-brackets
a trailing marker so the existing rules can see it, and never broadens the
edition vocabulary. A name that is not a marker run is returned byte-identical.
