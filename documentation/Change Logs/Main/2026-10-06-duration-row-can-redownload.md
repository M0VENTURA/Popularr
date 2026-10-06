# The "Length differs" row can redownload the correct version

**Date:** 2026-10-06 · **Area:** album / metadata review
**Commit:** `feat(album): a wrong-version track can be re-downloaded`

## Reported

> When a track has **"Length differs:** *3:53* → **4:00** — this is probably a
> different version of the recording. Nothing to save: a file's length cannot
> be changed by a metadata update." Can this track be given an option to add to
> Soulseek to redownload the correct version?

## Why the row had no action

`renderDurationChecks` is deliberately informational — *"these rows carry NO
Include/Ignore toggle, because there is nothing to stage"*. A metadata import
cannot rewrite audio, so there was genuinely nothing for **Save** to do.

But the diagnosis in that message — *the file is a different version* — has an
obvious remedy: **get the right file**. The row now offers it.

## The change

**`services/metadata/metadata_proposal_service.py::_duration_checks`** — the
payload gains the three things a queue row needs:

| field | why |
|---|---|
| `recording_mbid` (`mb_recording_mbid`) | the recording *is* the identity — it's what makes the search find the right take |
| `disc_number` (`mb_disc_number`) | where the file belongs |
| `duration` (raw `mb_duration`, **milliseconds**) | the queue stores a number; `4:00` is a display string |

**Both trees' `metadata-review.js`** — the row renders a **"Redownload correct
version"** button that goes through the *existing* queue path:

* live → `window.queueMissingTrack(btn)` (reads `data-*` off the button)
* test_site → `global.albumDetail.queueMissingTrack(payload, btn)`

Reusing it (instead of POSTing `/api/queue/add` itself) keeps the dedupe
handling, the busy popup and the *"a dedupe is NOT an insert"* rule in **one**
place rather than three. That required **exporting** `queueMissingTrack` on
test_site's `global.albumDetail` — it was module-private, and metadata-review
is a separate module.

The button is built with `createElement`/`setAttribute`, never string
concatenation, so a track title can never break out of an HTML attribute.

## Tests

`tests/test_duration_redownload_button.py` — **7 new**:

* both trees build the button **and append it** to the row;
* the title is passed as a value (live via `dataset`, test_site via its payload
  object — the two trees differ by signature) and never concatenated into
  markup;
* each tree calls its own queue implementation the right way;
* test_site really exports `queueMissingTrack`;
* the payload carries `recording_mbid`, `duration`, `disc_number`, `title`.

`tests/test_metadata_lookup_duration_check.py` — `test_the_check_carries_everything_the_queue_needs`
asserts the server emits the three new fields with a raw millisecond duration.

**Oracle:** stashing the four source files → **7 failed, 32 passed** = exactly
the tests that depend on this change; with it, 39/39.

## Verification

- targeted: **39 passed**
- **Oracle:** stashing the four source files → **7 failed, 32 passed** = the
  tests that depend on this change; with it 39/39
- affected set (22 files): **499 passed, 0 failed → 0 new vs baseline**
- full suite: **225 failed, 4672 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (the only ID absent from the
  baseline is the known native-flaky `test_sibling_torrents_root_is_searched`)

## Files

- `services/metadata/metadata_proposal_service.py`
- `static/js/metadata-review.js`
- `test_site/static/js/services/metadata-review.js`
- `test_site/static/js/pages/album.js`
- `tests/test_duration_redownload_button.py` (new)
- `tests/test_metadata_lookup_duration_check.py`
