# "us" in a track title kept renaming to "(us)"

**Date:** 2026-10-27
**Area:** `helpers/normalization_service.py`

## Report

> Tracks that have "us" in the track title keep renaming to "(us)".

## Root cause

`_split_trailing_edition_marker()` feeds `bracket_trailing_edition_marker()`,
which exists so an UNBRACKETED edition marker is stored in its canonical
bracketed form — the fix for "The Mirror's Truth Version" being re-imported as a
second album. It finds the marker by walking BACKWARDS from the last word while
each word is a marker token, glue, or a number.

The marker vocabulary included ordinary English words, because they also appear
inside real markers:

| token | real marker it appears in |
|-------|---------------------------|
| `us` | "US Version" |
| `uk` | "UK Edition" |
| `clean` | "Clean Version" |
| `special` | "Special Edition" |
| `tracks` / `track` | "Bonus Track" |
| `masters` | "Masters Edition" |
| `apple` | "Mastered for iTunes / Apple Digital Master" |
| `european` | "European Edition" |

**Any single one of them was accepted as sufficient.** So every real title
ENDING in one was rewritten:

```
One of Us                       ->  One (of Us)
Abandon Us                      ->  Abandon (Us)
Anarchy In The UK               ->  Anarchy In (The UK)
Coming Clean                    ->  Coming (Clean)
Following My Own Tracks         ->  Following My Own (Tracks)
Secret Track                    ->  Secret (Track)
The Hidden Masters              ->  The Hidden (Masters)
Toffee Apple                    ->  Toffee (Apple)
```

### Why it "kept" happening

The rewrite is not cosmetic. `repair_annotations()` — reached from
`services/metadata/album_name_update_service.py::repair_album_track_annotations()`
— writes the result to `tracks.title`, and the same album pass pushes the title
into the audio file tags via `sync_album_file_tags()`.

The rewrite is also **idempotent**: once a name reads `"One (of Us)"`,
re-running the repair produces the same string, so the damaged value looks
stable and is never revisited. Nothing in the pipeline would ever put the word
back, which is why the rename appeared permanent.

`clean_title()` hit the same function, so the Last.fm lookup key also lost the
final word — `"One of Us"` keyed as `one of`, `"Because We Can"`-style searches
then missed.

## Fix

### 1. Split the vocabulary (`_TRAILING_MARKER_STRONG` / `_TRAILING_MARKER_CONFIRM_ONLY`)

A marker run is only accepted when it contains a **strong** word. The ordinary
words stay in the sweep — `"Song US Version"` and `"Song Japanese Edition"` must
still bracket — but they may only CONFIRM a marker, never establish one.

Strong: `edition, deluxe, version, remaster, remastered, anniversary, expanded,
extended, collector(s), reissue, repress, digital, premium, mastered`.

Confirm-only: `us, uk, japanese, european, itunes, apple, masters, disc,
tracks, track, clean, explicit, special, super, standard, epic, press,
production, limited, ultimate, bonus, tour`.

`deluxe` is deliberately left strong: `"Some Album Deluxe"` → `"Some Album
(Deluxe)"` is a pinned contract. The known casualty is the mashup title
*"Bob Marley and Funkstar Deluxe"*, which still brackets.

### 2. Heal rows already corrupted (`unbracket_ambiguous_edition_marker()`)

Narrowing the vocabulary fixes future writes but leaves existing damage locked
in by idempotency. The new inverse un-brackets a trailing group whose tokens are
**all** from a never-a-real-annotation subset (`us, uk, japanese, european,
itunes, apple, masters, disc, tracks, track, super, special`) plus glue
(`of`, `the`, `and`, `for`, `no`, `s`).

`(Clean)`, `(Explicit)`, `(Bonus)`, `(Deluxe)` all label a real different master,
so they are excluded and their brackets are never undone; likewise any group
containing a strong edition word or a form word.

It runs first inside `bracket_trailing_edition_marker()`, so every caller —
`clean_title()`, `repair_annotations()`, `strip_album_edition_marker()`,
`dedupe_annotations()`, `safe_album_rename()` — heals for free on the next pass.

## Evidence

Measured against the 69,442-row collection export
(`Popularr - collection popularity report.csv`), running
`repair_annotations()` on every title:

| | before | after |
|---|---|---|
| distinct titles mutated | 216 | — |
| **NEW bogus brackets created** | 30 | **0** |
| **prior corruption healed** | 0 | **81** |
| titles ending in `us`/`us?` rewritten | 31 | 0 |

The healed set includes a whole cluster this bug silently produced —
`Secret (Track)`, `Hidden (Track)`, `Another Secret (Track)`,
`The Laugh (Track)` and ~20 more.

## Verification

- `tests/test_ordinary_words_are_not_edition_markers.py` — new, 5 classes,
  108 parametrised cases pinning the report, the heal, the preserved real
  annotations, and the downstream key/strip paths.
- `tests/test_unbracketed_edition_marker.py` — all 52 pinned contract tests
  still pass unchanged (`"The Mirror's Truth Version"` →
  `"The Mirror's Truth (Version)"`, `"Some Album Deluxe"`, form markers, empty
  names, idempotency, no double brackets).
- Regression sweep over 160 suites (normalisation / title / album / rename /
  edition / cover / annotation / tag / metadata / Last.fm / lookup / Navidrome):
  **96 failures before, 96 after, 0 NEW, 0 GONE.** All pre-existing, all
  unrelated (SQLite schema gaps, fake-audio fixtures, cert errors).

## Not fixed here (same function, different defect)

The same backward sweep stops at the first non-vocabulary word, so a QUALIFIED
marker is split mid-phrase:

```
Beat It - Studio Version        ->  Beat It - Studio (Version)
Cinderella - Story Version      ->  Cinderella - Story (Version)
Hellraiser - 30th Anniversary Edition
                                ->  Hellraiser - 30th (Anniversary Edition)
```

The head loses a word. Fixing it needs a phrase-aware boundary (the qualifier
before `Version`), and taking everything back to the last dash would swallow
real title text (`"Song - Part 2 Remaster"` → `"Song"` for lookup keys). Left
alone deliberately; no pinned contract covers it and it is not a regression.
