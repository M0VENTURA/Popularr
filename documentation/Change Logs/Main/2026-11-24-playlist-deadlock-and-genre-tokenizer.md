# Playlist index deadlock, query fix, retry backoff, and genre-list tokenising

**Date:** 2026-11-24
**Area:** `db/repositories/playlists.py` · `db/engine.py` ·
`services/enrichment/genre_vocabulary.py` · `services/enrichment/genre_vocabulary.json` ·
`services/catalog/album_classification_service.py`
**Commit:** `fix(playlists): one lock order, no unfiltered delete, growing probe backoff; fix(tokenizer): lists split before names`

---

## 1. The deadlock

Four changes, and the second was the real cycle:

**(a) No unfiltered delete.** `delete_playlists_not_in([])` ran
`DELETE FROM playlist_entries` and `DELETE FROM playlists` with **no predicate**.
The empty branch is gone: the empty case now renders `NOT IN :ids` like every
other path, which the expanding parameter already treats as "matches
everything". One statement shape for every caller.

**(b) One lock order.** `replace_playlist` upserted the **header** and then
deleted entries; the sweep deleted **entries** and then the header. Two
transactions touching the same two tables in opposite orders is a textbook
cycle. `replace_playlist` now deletes entries first as well.

**(c) `pg_advisory_xact_lock(hashtext('playlist_index_write'))`** in both
writers. `xact` rather than `try`: the sync should WAIT for the other writer, not
skip a pass, and the lock releases itself at COMMIT/ROLLBACK so a crash cannot
strand it. Skipped off PostgreSQL.

**(d) The artist query.** Now `GROUP BY e.track_id, p.name` with
`ORDER BY e.track_id, LOWER(p.name)`. `DISTINCT` over the pair emitted a name
twice when one track sat in a playlist at two positions.

⚠️ **A landmine I hit and had to back out of.** My first cut read
`session.bind.dialect`. Accessing `.bind` MATERIALISES the engine, and
`get_engine()` constructs one — under pytest that means creating the PostgreSQL
engine and importing `psycopg2`, which is not installed here. The resulting
`ModuleNotFoundError` **disposed the shared in-memory SQLite engine and wiped
every table for the rest of the session**, turning 7 suites into 40+ collection
errors. `session.get_bind()` returns the already-bound engine without
construction. `db/repositories/playlists.py`'s own docstring warns about exactly
this hazard.

## 2. Connection retries

The outage WINDOW already backed off 10 → 60 s, but the **probe cooldown** was
pinned to the 5 s constant, so the reconnect cadence stayed flat however long
recovery took — the reported "reconnects every ~5 s".

It now doubles: **5 → 10 → 20 → 40 → 60**, capped at 60. Verified by driving the
breaker: `[5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]`. `db_outage_retry_after`
reports the growing value too — reporting the constant while the gate used the
grown one would wake every waiter early and spin, the same defect the clamp
there already documents.

## 3. Genre lists vs genre names

**You were right that the alias was incidental.** I verified against the real
v4: `pop_rock` had only `["pop-rock"]`, but its own DISPLAY name "Pop Rock"
already normalised to the same key as "Pop, Rock". Deleting punctuation made a
separator and a joiner identical.

**A third normaliser was the deeper fault.** `iter_genre_tokens`
(`album_classification_service.py`) had its own
`re.sub(r"[^\w\s-]", "", …)`, so "R&B" became `rb` there while `classify` —
using `normalize_token` — resolved it to `r_and_b`. `deciding_family` therefore
called a genre it knew "unknown", `classify_main_genre(["R&B"])` returned None,
and the config normaliser fell back to a fabricated id `r_b`. All three now use
`normalize_token`.

### The new tokenizer

* **Hard delimiters** (`;` `,` `|`) split FIRST, before any whole-value match.
* **Protected values** are matched whole first — `tokenizer.protected_values`
  in the JSON, editable, not hardcoded.
* **Soft joiners** (`/` `&` `+`) try the WHOLE value first, so `death/doom`,
  `R&B` and `Drum & Bass` survive while `Funk / Soul` splits.
* `\` is a hard delimiter (sources use it for lists).
* **Punctuation becomes a space**, not nothing.

⚠️ **Two honest limits, both asserted in tests:**

* `"Rock, Pop"` and `"Rock Pop"` still normalise identically. That is
  unavoidable — with punctuation folded to a space, "comma + space" and "space"
  are the same sequence. **The split ORDER is what prevents the collapse**, not
  the key.
* `"Rock / Pop"` resolves whole to Pop Rock and does NOT split. That is your
  spec: a soft joiner splits only when the whole value fails. Splitting it would
  discard the actual reading. `"Funk / Soul"` splits because it resolves to
  nothing.

### The exhaustive check you asked for

For every pair of single-word genre names A and B, `"A, B"` must yield two
tokens: **133 names → 17,556 pairs → 0 failures.** It is now a test, so a future
alias that spells a compound will fail the suite instead of silently swallowing
lists.

Your table, all 10/10: `Rock, Pop` ✓ · `Pop Rock` stays whole ✓ · `Pop, Metal` →
Pop + Metal (not Glam Metal) ✓ · `Punk, Rock` ✓ · `Folk, World, & Country` ✓ ·
`death/doom` → Death-Doom ✓ · `R&B` ✓ · `Drum & Bass` → Drum and Bass ✓ ·
`Funk / Soul` splits ✓ · `Hip-Hop; Rap` ✓

## Tests

`tests/test_genre_tokenizer_lists_vs_names.py` (22, including the exhaustive
pair sweep and a check that no two genres share a lookup key).

⚠️ **About the baseline.** My saved baseline was stale — it predated the v5
vocabulary. Re-measured at HEAD with my work stashed: **451** failures+errors.
With these changes: **447**, and the diff of the two sets shows **nothing added
by me**.

## Still open

`test_covers_genre_playlists::test_selection_is_normalised` asserted the old
`r_b` fallback; it now gets `r_and_b`, which is correct.
