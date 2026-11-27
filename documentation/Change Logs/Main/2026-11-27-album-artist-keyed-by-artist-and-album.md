# Albums imported as "Various Artists": an artist-keyed slot

**Date:** 2026-11-27
**Commit:** `fix(import): key the stored album artist by artist AND album`

## Reported

> "A heap of albums are being imported as various artists even though they are in
> Navidrome under their regular artist name."

Then, unprompted and exactly right:

> "It seems to be artists that also appear as a track artist on other various
> artist collections."

Confirmed against the supplied album — Gorillaz, *The Mountain*, whose own tags
say `albumartist: Gorillaz`, `artist: Gorillaz`, `musicbrainz_albumartistid:
e21857d5-…`.

## The mechanism

`existing_album_artists` was a map keyed by **ARTIST alone**, written once per row
during the prefetch with "last row wins":

```python
existing_album_artists[str(canonical_artist_name)] = str(album_artist)
```

The prefetch is deliberately wide — `_artist_identity_clause` matches the **track**
artist as well as the album artist, because a row stored under a different credit
was otherwise invisible and got rewritten forever. So for the artist view
`Gorillaz` it loads:

- *The Mountain* — `album_artist = Gorillaz`, `artist = Gorillaz`
- *Plastic Beach* — `album_artist = Gorillaz`, `artist = Gorillaz`
- *Now That's What I Call Music 42* — `album_artist = Various Artists`, `artist = Gorillaz`

That last row set `existing_album_artists["Gorillaz"] = "Various Artists"`, and
`album_artist_from_sources` returned `"Various Artists"` for **every** Gorillaz
album — its first check was `existing_is_va`.

`_normalise_va_album_artist` then hard-wrote it:

```sql
UPDATE tracks SET album_artist = 'Various Artists' WHERE id IN (…)
```

and because `album_artist` is a local-authority field, the ordinary upsert
*protects* that value afterwards. So it was sticky, not transient: an artist with
one track on one compilation lost their entire discography's album artist.

The docstring claimed the map was "that artist's OWN stored album artist, which
is what makes the guard safe". It was not — it was the last-loaded row's, and the
prefetch loads contributors by design. The old test suite missed this because it
keyed its fixtures by **album title** while production keyed by **artist**, so the
artist-keyed slot was never actually exercised.

## The fix

1. **`_album_artist_state_key(artist, album)`** — the slot is now one artist AND
   one album, case-folded, with the same `strip_album_edition_marker` the rest of
   the importer uses so a "(Deluxe)" suffix cannot split it. The entry now means
   what its docstring always claimed: *is THIS album stored as a compilation*.

   The writer and reader both go through it, so they cannot drift again.

2. **`_heal_contaminated_va_album_artist`** — prevention alone would not repair
   an affected library, because the local-authority guard defends the bad value.
   After a successful album persist, if Navidrome resolved a **specific** artist
   and **every track artist on the album matches it**, the rows are rewritten
   from "Various Artists" to that artist.

   The discriminator is sound: a genuine compilation has several different track
   artists, so it can never satisfy "every track artist equals the resolved album
   artist". It also requires at least two tracks — a single-track "album" is not
   evidence either way. Composite credits (`"Various Artists • Gorillaz"`) are
   healed by the same rule.

   DB-only. The supplied file already had `albumartist: Gorillaz`, so the tags
   needed no write; the row was the only thing wrong.

## What was deliberately NOT changed

The preservation behaviour itself. A stored `"Various Artists"` is still
preserved when it belongs to **this** album — that is what stops a genuine
compilation being flattened to one contributor, which was the earlier
`Various Artists • HIM` report. The two existing regression suites pin it, and
both still pass (with their fixtures moved to the scoped key).

## Tests

- `tests/test_navidrome_va_album_artist.py` — the reported case (a compilation
  the artist contributed to must not mark their own album as a compilation), the
  album's own stored VA still preserved, another artist's entry not consulted,
  and the key's case/edition normalisation.
- `tests/test_va_album_artist_heal.py` — end-to-end DB: a polluted album is
  repaired; a real compilation with several performers is left alone; a
  compilation cannot be rewritten even when a specific target is supplied; an
  unpolluted album is untouched; the guard returns before any write.
- `tests/test_album_ownership_and_report_playlists.py` — the pre-existing
  preservation tests, now keyed per album, plus the reported bug itself.
