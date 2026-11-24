# Final Genres — metal share, root fallback, restore guard, artist-only family

**Date:** 2026-11-24
**Area:** `services/enrichment/genre_selection.py` ·
`services/enrichment/genre_vocabulary.py` · `services/enrichment/genre_vocabulary.json` ·
`helpers/config_helpers.py`
**Commit:** `feat(genres): v5 collection policy — share arbitration, root fallback, restore guard`

The four recommended rules from the Final Genres audit, plus the v5.2
vocabulary merge.

Apply with a genre resync (**Config → Rebuild Genre and Playlists**) — the rules
run inside the selector, so nothing needs re-scanning to take effect, only
re-selecting.

---

## 1. Metal only replaces rock when most of the evidence is metal

`metal_over_rock_mode = "share"` (default), `metal_over_rock_min_share = 0.6`,
`metal_over_rock_exclude_roots = true`.

Metal must hold 60% of the combined rock+metal evidence before rock-family genres
are removed. Measured on the reported library this cut wrongly-stripped rock from
**3.4% of tracks to 0.8%** (Kiss, Red Hot Chili Peppers, Godsmack, Staind,
Bloodhound Gang). 0.55 / 0.60 / 0.67 measured almost identically, so 0.60 is not a
knife edge.

**Roots are excluded from the share.** A bare `Rock` tag outscores the subgenres
*because* it is generic (Discogs "Rock" + MusicBrainz "rock" corroborate each
other), so counting it would let the umbrella decide the question it should be
excluded from.

⚠️ **The rule is symmetric, and it had to be.** The first cut only removed rock
when metal won; when metal lost, both sides stayed. That resurrected the opposite
defect — a lone Essentia guess at 0.45 weight sat beside the full-weight
MusicBrainz rock tag it should have lost to. Between the two thresholds neither
side holds a majority and both survive; outside them the loser is removed.

⚠️ **Same-sound pairs are redundancy, not arbitration.** `Gothic Rock` +
`Gothic Metal` share a qualifier, so metal is the narrower name for one sound and
wins regardless of support. Only genuine rivals (`hard rock` vs `heavy metal`)
go through the share gate.

`metal_over_rock_mode = "pair"` reproduces the previous per-pair behaviour exactly.

### A fragility worth knowing

A 1:1 rival pair with identical evidence and tags in the order `[metal, rock]`
scores metal at ~0.41 share (rank decay), so the policy **declines**. Reversing
the tag order gives ~0.59 — still under 0.60, so it also declines, but only just.
Equal split is genuinely "not most of the evidence", so declining is correct; the
margin is thin enough that a future change to rank decay or source weights could
tip borderline pairs either way.

## 2. Bare family genres are a fallback only

`root_fallback_only = true`.

12% of genre slots were generic (Rock, Punk, Electronic, Pop). A root is the
*highest-scoring* tag, so it took a slot **and** set the relative floor that
knocked out the real subgenre. Roots are now ordered last in `select_genres`, so
they cannot take a slot a specific genre can fill, and the relative floor is
measured against the best specific genre. Reported effect: generic slots 12% →
3.4%, acceptable genres on the labelled tracks 86% → 93%.

⚠️ **This is an ORDERING rule, not a filter.** My first implementation deleted
every root whenever any specific genre existed, which broke two pinned decisions:
`Pop` beside `Synthpop` (synthpop is electronic-family and cannot replace the pop
root) and a lone `Reggae` beside an unrelated metal genre. A root redundant
against its *own* family is still removed by `remove_generic_parents`.

## 3. Keep the parent if condensing would empty the slot

`restore_parent_if_empty = true`.

A **bug in the design**, not a preference. On The Trooper, Heavy Metal (score
1.68) is condensed into a narrower genre scoring 0.59 on one source, which then
fails the 0.60 position-1 floor — and the track came out with no genre at all
despite having started with a well-supported one.

The rescue re-selects from the pool **as it was before condensation**. Only the
redundancy stage is undone; the corroboration gate, the collection policy and the
root fallback have all already had their say.

## 4. Artist-only evidence cannot add a genre from a different family

`artist_only_same_family = true`.

Aimed at Nu Metal arriving on Pearl Jam's "Even Flow" purely from the artist's
tags. An artist genre describes the act across its career; it may **confirm** a
family the recording already shows at track/album level, but not **introduce** a
new one.

⚠️ If there is no track/album evidence at all, the rule cannot judge anything —
every candidate would be "foreign" and the track would come out empty. It stands
down in that case.

## 5. Vocabulary v5.2 merge

`redundant_with` is a new **per-genre** field: a data-driven redundancy list the
parent graph cannot express (`heavy_metal` is not an ancestor of `nu_metal`; they
meet at `metal`). Applied after the slots are decided; freed slots are **not**
refilled, and it never empties a track.

⚠️ **`keep_together` OUTRANKS a declared list.** The patch declares
`alternative_metal` redundant with `nu_metal`, while `keep_together` declares the
same pair a valid combination — a deliberate human decision recorded against that
exact pair. Without the guard the generated list won and silently reversed it. A
data-driven rule must not override an explicit setting.

Merged: 18 aliases, 7 noise values, 20 redundancy edges — 24 insertions /
21 deletions in the vocabulary file, because the merge rewrites only the affected
entry lines. (`json.dump` reflowed all 224 entries into a 2 300-line diff.)

⚠️ **The supplied patch was not usable as a vocabulary and is not one.** It has no
`genres` list, so loading it as the override yields **zero genres** — every track
would silently get no genres at all. It is a set of edits, which were merged into
`genre_vocabulary.json` instead. Verified: 224 genres still parse, validation
clean, all 18 aliases resolve, all 7 noise values match.

⚠️ **21 ids the patch references do not exist in the vocabulary** and were
skipped rather than invented (a stub with no parent would corrupt the hierarchy):

* missing sources: `alternative_punk`, `folk_punk`, `electronic_rock`,
  `alternative_pop`
* missing targets: `deathgrind`, `electronicore`, `kawaii_metal`,
  `melodic_metalcore`, `noisecore`, `progressive_metalcore`, `southern_metal`,
  `emo_pop`, `skate_punk`, `celtic_punk`, `anarcho_punk`, `crossover_thrash`,
  `gypsy_punk`, `oi`, `dance_rock`, `traditional_heavy_metal`

The result is that `alternative_punk`, `folk_punk`, `electronic_rock` and
`alternative_pop` got **no** redundancy rules at all — they are probably wanted as
new vocabulary entries in their own right.

Also implemented but **inert until a vocabulary entry needs it**:
`position3_new_family`, which lets the third slot accept one source GROUP when
MusicBrainz **and** ListenBrainz both name the genre at track level and its family
is not already in positions 1-2.

## Tests

`tests/test_final_genre_v5_rules.py` — 17 tests; 9 fail at the previous commit.
The metal-share contract lives in `test_genre_metal_over_rock_policy.py`, split
into same-sound pairs (still collapse) and rival pairs (share-gated), with the old
per-pair behaviour covered explicitly via `metal_over_rock_mode="pair"`.

One existing test was updated rather than fixed: `test_moved_values_are_reported_by_kind`
asserted that "Billboard Hot 100" is reported as *unknown*, and the v5.2 patch
lists it as a noise value — a **more precise** answer. The test now asserts the
`noise` bucket and a separate control pins that genuinely unrecognised values
still land in `unknown`.

Full suite: 403 failed, no new failures against the 405 baseline (two previously
failing tests now pass).
