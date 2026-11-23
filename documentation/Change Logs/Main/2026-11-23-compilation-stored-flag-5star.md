# Compilations were still slot-capped: the resolver ignored the stored verdict

**Date:** 2026-11-23
**Area:** `services/popularity/stages/finalise_stage.py`
(`_resolve_compilation_flags`) · `tests/test_compilation_stored_flag_keeps_5star.py` (new)
**Commit:** `fix(scoring): honour the stored is_compilation flag when re-rating`

## Reported

> "Compilations are meant to be able to score any track as a 5★ track, but it
> seems to still be limited to the 3 or 4 track limit"

> "3 or 4 track is the 5* scoring on non compilation tracks"

So the 3/4-track limit is the **era 5★ slot cap** — `peak` 4, `solid` 3,
`minor` 2 — which is the cap that stops an ordinary album from holding more than
a handful of 5★ tracks. Compilations are explicitly exempt from it:
`post_album_star_ratings` guards every slot pass with `and not is_compilation`
(lines 6929/6930). The exemption is only as good as the `is_compilation` value
it reads, and that is where it failed.

## Root cause: the resolver never read the column

`_resolve_compilation_flags` supplies the verdict whenever the caller cannot hand
it down — which is exactly the **Star Rating Resync**, whose rows come straight
from the `tracks` table. It re-derived the answer from three things:

* the joined type tag (`musicbrainz_albumtype` / `spotify_album_type`),
* a generic album-artist name (`Various Artists`, `VA`, …),
* the album TITLE (`Greatest Hits`, `Best Of`, `Anthology`, …).

It never read the row's **`is_compilation`** column, even though
`_row_to_track` selects and passes it. So a compilation with no hits-style title
and no re-readable type came back as a studio album:

| album | stored | type | before | after |
| --- | --- | --- | --- | --- |
| Singles Collection | 1 | compilation | `True` | `True` |
| Untouchables | 1 | — | **`False`** | `True` |
| Todsünden | 1 | — | **`False`** | `True` |
| Greatest Hits | 1 | — | `True` | `True` |
| Felsenfest (control) | 0 | — | `False` | `False` |

Every false row fell into the studio-album path and was then slot-capped, so the
compilation could produce at most 4 (or 3, or 2) 5★ tracks no matter how many of
its tracks are genuinely the artist's top band.

## The fix

The stored verdict is now consulted, after the VA and type-tag paths so the VA
subtype is still recovered wherever the evidence exists:

```python
if any(int(r.get("is_compilation") or 0) for r in album_results):
    return True, False
```

Checked over ALL rows, not just the first: a release is a compilation if its
tracks say so, and the flag is written per row.

Measured end-to-end with a 24-track tail behind eight hits, `peak` era, slots=4:

```
stored compilation    is_compilation=True   after assign=8   final 5★=8   (cap 4)
plain album (control) is_compilation=False  after assign=0   final 5★=0   (cap 4)
```

## Verification

* `tests/test_compilation_stored_flag_keeps_5star.py` — the resolver honours the
  column for three ordinary-looking titles, does NOT promote a plain album, still
  recovers the VA subtype, and asserts a compilation exceeds the cap at all three
  eras (`peak`/`solid`/`minor`).
* The whole compilation suite is unchanged: the 9 failures in
  `test_compilation_more_5star` / `test_greatest_hits_artist_catalogue` /
  `test_hits_routing_and_type_columns` / `test_compilation_cover_and_rating`
  are all pre-existing at `4cfce1a1` (verified by stashing the change and
  re-running). They pin "a plain type tag wins over the title", which this
  change deliberately does not touch.
* Full suite: 405 failed / 9987 passed vs the documented 403 baseline. The two
  extra are `test_playlist_song_id_rejections` (the documented collection-order
  flake) and `test_work_level_lb_aggregation`, which passes in isolation —
  both reproduced at `4cfce1a1` with the change stashed.

## Not changed

A track on a compilation is still capped by the absolute-reach guards
(`top_tier_min_score` / `top_tier_min_listeners`, `high_tier_*`) — but those are
`is_va_compilation`-only by design, and the VA subtype is preserved here. A
single-artist compilation remains rated on the artist's own catalogue, where any
track can reach 5★.
