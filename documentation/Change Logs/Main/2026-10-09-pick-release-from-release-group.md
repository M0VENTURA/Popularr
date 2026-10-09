# Pick which edition of the release group an album is linked to (2026-10-09)

**Area:** musicbrainz routes + album page (identifiers & linking)
**Commit:** `feat(album): choose the release edition from the release group`

## Reported

> Next to release on identifiers and linking, can we add a search that will
> bring up the different releases from the release group to select the best
> one. It could back in to the existing release modal that's used elsewhere.

The album page's **Identifiers & Linking** block holds *MusicBrainz Release ID*
and *MusicBrainz Release Group ID* as two free-text inputs. The releases in
between — the 12-track CD, the 18-track deluxe, the promo — were reachable from
nowhere on that page, so binding a different pressing meant pasting an id.

## What was added (into the existing modal, not a new one)

### 1. `mode=select` on `GET /api/musicbrainz/release-picker`

The **same slideover the queue already uses** — same `browse_releases_for_group`
payload, same edition-grouping, same tracklist preview — plus a *Use this
release* button on each card and a header that says *"Select the version this
album should be linked to"* instead of *"…you want to queue"*.

Queue mode is deliberately untouched: it auto-selects (one release, or every
edition sharing a track count) and is driven by the caller, so it needs no
button. A test asserts `js-pick-release` is **absent** in that mode.

### 2. `openAlbumReleaseSelector()` (both trees)

Opens the slideover with `mode=select`, preferring `#album_release_group_mbid`
and falling back to `#album_mbid` — the endpoint already resolves a concrete
release to its own group when the browse of a non-group id comes back empty, so
either field is a valid starting point. With neither set it says *"Link a
MusicBrainz release first (Lookup MBID), then choose the edition."* rather than
opening an empty list.

### 3. The card buttons are server-rendered — so the handler is delegated

A `document` click listener (the pattern the rest of the app uses for rows a
module does not build itself) catches `.js-pick-release`, runs
**`applyAlbumMbid(releaseId)`** — the same function a Lookup-MBID pick uses —
closes the slideover, and toasts *"…press Save Metadata to persist it."*

Nothing is written by choosing: the id lands in the input and the form's own
**Save Metadata** persists it, so a wrong pick is undone by reloading. That is
the same contract as every other lookup on that page.

### 4. The button (both trees)

Beside `#album_mbid`:

```html
<button type="button" class="btn btn-sm btn-outline-info mt-1" onclick="openAlbumReleaseSelector()">
  <i class="bi bi-collection me-1"></i>Choose from release group
</button>
```

`type="button"` so it never submits the form — a test pins that.

## Tests

`tests/test_release_picker_select_mode.py` — **26**:

* the route reads `mode=select`, renders the button **only** in select mode,
  and passes the flag to the renderer; the rendered header names the right
  action; **control**: edition-grouping still applies (one card per group);
* parametrised over **both** album-page scripts: the opener exists and is
  published for the template's bare `onclick`, requests `mode=select`, prefers
  the release **group** over the concrete id, explains the first step when
  neither id is set, delegates through `applyAlbumMbid`, and closes the
  slideover;
* parametrised over **both** templates: the button is present, follows the
  `#album_mbid` input, and is `type="button"`.

## Verification

* New suite → **26 passed**.
* **Oracle** — reverting all five changed files → **23 failed / 3 passed**.
  Restored → 26 passed.
* **Sweep** — the 36 test files referencing the release picker / album page,
  clean `origin/develop` vs this change: **baseline 14 failures, changed 14**,
  `Compare-Object` on the sorted `^(FAILED|ERROR) tests/` lines = **identical,
  0 regressions**.
* `node --check` on both JS files → OK · Jinja parses both templates → OK ·
  `musicbrainz_routes.py` AST-parses → OK.

## Not changed

* **No new endpoint.** The picker already browsed a release group; only the
  action offered off the list is new.
* **Queue mode.** Its auto-selection (single release / identical track counts)
  and its caller-driven queueing are untouched.
* `applyAlbumMbid` / `applyAlbumMatch` and the concrete-release resolution they
  already do.
* ⚠️ **Observation, not fixed here:** the queue-mode slideover renders only
  *Preview Tracks* — no button to queue the chosen edition, because queueing
  happens through the auto-select paths. Worth a look if a multi-version group
  is ever shown to the user in that mode.
