# Detect remixes via the MusicBrainz "remix of" recording relation

**Date:** 2026-10-21
**Area:** `services/enrichment/musicbrainz_service.py` ·
`services/popularity/stages/track_stage.py`
**Commit:** `fix(tracks): detect remixes via the MusicBrainz "remix of" recording relation`

## Report

> "Can we add the following to detect whether a song is a remix of an original
> song? Currently the track is coming up as the original version. Many tracks
> on New Old Songs by Limp Bizkit are remixes. The tracks show 'Remix of'."

## Why the remix was invisible

MusicBrainz records a remix as a RECORDING-level relation:

    remix of: [Break Stuff] (recording c8e00a85-…)
    remixer:  [DJ Lethal]

The app was already fetching these relations — the recording ``inc`` superset
includes ``recording-rels`` — but the parser in ``_recording_to_metadata``
only looked at ``performance``/``recording of``/``composer``/``writer``/
``lyricist`` relations. The ``remix of`` relation was fetched, cached, and
never read.

So a remix whose title carries no ``(Remix)`` wording was indistinguishable
from the original studio recording — exactly the reported "coming up as the
original version".

## The fix

Two shared helpers read the relations:

* ``remix_of_recording_id(recording)`` — the MBID of the recording this one
  re-records, or ``""``.
* ``remixers_of_recording(recording)`` — the credited remixer(s).

``_recording_to_metadata`` now exposes:

* ``is_remix`` — TRUE only when MusicBrainz itself says this recording
  re-records another one. It is the AUTHORITATIVE signal — a title that
  merely says "remix" is a hint, but a recording whose MB relation says
  "recording X is a remix of recording Y" IS a remix regardless of its
  title. A title with "remix" but no MB relation is NOT marked.
* ``remix_of_recording_mbid``, ``remix_of_title``, ``remixer`` — the details.

``_flatten_release`` (the album-lookup path) sets ``is_remix`` on the
per-track entry too, so the scan path and the album-lookup path agree.

``track_stage`` persists ``is_remix`` on the track when the metadata says
so, so the flag survives and feeds the track page, the duplicate-artist
grouping, and the "is this the original?" decision.

## What was NOT changed

* The ``remaster``/``remastered`` marker is deliberately NOT read as a remix
  — a remaster is the SAME performance (a mastering pass on the studio
  recording) and resolves to the canonical recording, not a variant.
* Title-based ``remix`` detection (via ``GenreDetector`` /
  ``_TRAILING_ALTERNATE_PHRASES``) is untouched — a track titled
  "(Remix)" is still recognized as a variant by those paths. The MB
  relation is the additional, authoritative signal for tracks that
  carry no such wording.

## Tests

`tests/test_remix_of_relation.py` (new, 5 tests):

* the "remix of" relation points at the original's MBID;
* the original has no "remix of" relation;
* a plain title with no relation is NOT a remix;
* other relation types are ignored;
* the remixer is extracted.

Sweep of MB / matching / single-detection suites diffed against a pristine
``develop`` clone — **identical failure sets, 0 new failures.**

## Files

* `services/enrichment/musicbrainz_service.py` — ``remix_of_recording_id``,
  ``remixers_of_recording``, and the remix fields in
  ``_recording_to_metadata`` / ``_flatten_release``.
* `services/popularity/stages/track_stage.py` — ``is_remix`` persistence.
* `tests/test_remix_of_relation.py` — new.
