# Track matching now trusts the name and the length, not just the number (2026-10-05)

**Report:**

> Track matching is relying too much on track number and not track name and
> length
>
> **MusicBrainz:** Title: *Heaven Can Wait (live)* →
> **Rock and Roll Dreams Come Through (radio edit)**
> MusicBrainz Recording ID: *e662030e-56fb-4a4a-b108-0ab9ae06c0dc* →
> **f743d192-d432-45d2-94c9-738d3538977f**
> Writer: *["Jim Steinman"]* → **Jim Steinman**
>
> `/music/Meat Loaf/2006 - Bat Out of Hell II_ Back Into Hell…/203. Meat Loaf -
> Heaven Can Wait (live).mp3`, 5:00

## Root cause

Both matchers asked the **track number first**, and asked it unconditionally:

```python
# _match_mb_tracks_to_library (the album-page Lookup MBID / Compare path)
Match = next(
    (
        row for row in Remaining
        if _disc_of(row) == Mb_disc and _track_num_of(row) == Mb_number
    ),
    None,
)
```

A number says where a track sits in *one edition* of a release, not what it is.
Two editions of the same album routinely order their tracks differently, a rip
that numbers its files across a whole discography puts track 203 next to
whatever the chosen release happens to have at position 203 — and the pairing
was accepted before the title or the length were ever looked at. The review
then did exactly what it is designed to do and offered to rewrite the title,
recording MBID and writer of a file whose metadata was already correct.

A second, quieter defect sat underneath: `str(None or "")` compared equal, so a
MusicBrainz entry with **no position at all** handed itself the first remaining
file.

Neither matcher consulted the length when deciding, even though
`_match_mb_tracks_to_library` already *reported* a duration difference once a
pairing had been made — the check that could have caught this ran too late to
matter.

## Changes

New shared rule in `services/enrichment/musicbrainz_service.py`:

- `_titles_agree_for_pairing(library, mb)` — normalized equality or similarity
  ≥ `_TRACKLIST_TITLE_FLOOR` (0.55); a blank on either side cannot disagree.
- `_lengths_agree_for_pairing(library, mb)` — both sides through
  `track_duration_seconds()` (library rows are seconds, MusicBrainz is
  milliseconds) and judged with the **same** `DURATION_TOLERANCE_SECONDS` the
  review reports a duration difference with, so "worth pairing" and "worth
  reporting" can never disagree.
- `_track_number_pairing_allowed(...)` — the rule itself: a track number is a
  **tie-breaker, never proof**. It is allowed only when nothing contradicts it:

  | name | length | number allowed? |
  |---|---|---|
  | agree | – | ✅ |
  | disagree | one or both unknown | ✅ (nothing to check against) |
  | disagree | both known, agree | ✅ (a rename of the same recording) |
  | disagree | both known, differ | ❌ |

### `_match_mb_tracks_to_library` (album page / Compare)

Order per track is now **1) exact normalized title → 2) track number, only if
`_track_number_pairing_allowed` → 3) fuzzy title**, and a blank number is no
longer an identity. Asking the name first means a track only falls back to its
number when nothing in the library is named after it.

### `match_mb_tracks_to_files` (folder → release pairing)

Restructured into **three passes over the whole tracklist** rather than one
pass that fully resolves each track before looking at the next:

1. exact normalized title — every track's name is considered before any
   number is;
2. track number (+ disc), gated by the same rule, and skipped entirely when
   either side states no number;
3. fuzzy title ≥ `_TRACKLIST_TITLE_FLOOR`.

Running each pass over the whole tracklist is what stops an *earlier*
MusicBrainz track from claiming a file by number before the track that file is
named after has been considered.

## Behaviour deliberately preserved

The controls are as important as the regressions — an untagged download whose
title came from its filename shares no words with the MusicBrainz title, and
the number is still the only identity those files have:

- "garbage" ↔ "I Remember You", number `1`, no file length → **still pairs**
  (length unknown ⇒ nothing contradicts the number);
- "Rock & Roll Dreams Come Through" ↔ "Rock and Roll Dreams Come Through"
  with equal lengths → **still pairs**;
- a correct title with a wrong number → **pairs on the name**;
- `(Live)` / `(Acoustic)` / `(Cover Version)` marker rules from
  `2026-10-05-mbid-lookup-cover-title-change.md` are untouched.

Unchanged: a track that cannot be paired comes back `matched=False` rather
than guessed — a wrong pairing writes a wrong recording MBID straight into the
file's tags.

## Tests

`tests/test_track_matching_prefers_name_and_length.py` — 19 tests: the
reported shape rejected by **both** matchers, the review no longer offering a
different song, blank numbers claiming nothing, four controls that must keep
pairing, and the rule's five cases including the tolerance agreement.

Oracle: reverting `services/enrichment/musicbrainz_service.py` fails **14/19**
(all four report regressions, the blank-number guard and the rule itself); the
5 that still pass are the behaviour-preserving controls.
