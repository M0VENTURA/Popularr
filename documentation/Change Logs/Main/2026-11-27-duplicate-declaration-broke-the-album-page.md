# A duplicate declaration killed the whole album page

**Date:** 2026-11-27
**Commit:** `fix(album): remove a duplicate declaration that broke the entire page`

## Reported

> "A lot of the sections in the album page aren't working when pressed."

```
album.js:2390 Uncaught SyntaxError: Identifier 'trackArtist' has already been declared
Triple J's Hottest 100 (2001):7512 Uncaught ReferenceError: openAlbumLookupModal is not defined
    at HTMLButtonElement.onclick (Triple J's Hottest 100 (2001):7512:109)
```

## My bug, and why it had such a wide blast radius

I added `const trackArtist` to `buildMissingRow`'s payload in `60f123f6`'s
predecessor, not noticing the same function already declared it ~50 lines
earlier for the missing-track badge:

```js
const trackArtist = String(trackComp.track_artist || trackComp.mb_artist || '').trim();  // line 2337
…
const trackArtist = String(trackComp.track_artist || trackComp.mb_artist || '').trim();  // line 2390
```

A duplicate `const` in one scope is a **parse-time** SyntaxError. The browser
discards the **whole file** — not the function, not the statement. So:

- nothing in `album.js` is defined;
- every section handler the file wires up is missing;
- every inline `onclick` pointing at one of its functions fails with
  `ReferenceError` — hence `openAlbumLookupModal is not defined` from markup
  that has nothing to do with missing tracks.

The symptom is "a lot of sections aren't working", which reads like many
unrelated bugs. It was one character-level mistake.

Fix: use the existing binding. Nothing else changed.

## Why none of the existing guards caught it

They all inspect source **text**. `tests/test_operations_report_real_outcomes.py`
checks for substrings, `tests/test_queue_track_artist.py` checks that a payload
expression appears, and `tests/test_artist_page_contract.py` scans for Jinja
tokens. A file that cannot be parsed satisfies all of them.

I did run a JS syntax check earlier in the session, but it used esprima 4 — which
rejects optional chaining and nullish coalescing — so it reported
`SKIP (ES2020 syntax)` for exactly the files that needed checking, `album.js`
among them. The skip was the failure: a check that silently excludes the target
is worse than no check, because it reads as a pass.

## The permanent guard

`tests/test_js_files_parse_without_duplicate_declarations.py`:

- parses **every** file under `test_site/static/js` (69 files) and fails on a
  same-scope redeclaration, reporting the line number;
- tracks block scopes properly, so the five sibling `if`-block reuses already in
  the tree (`coverField`, `message`, `heading`, `item`) are recognised as legal
  rather than reported as noise;
- lowers `?.` / `?.[` / `?.()` / `??` for parsing only, so nothing is skipped;
- carries two guards-on-the-guard: a known-bad snippet **must** be caught, and a
  known-good sibling-block snippet must **not** be;
- re-parses `album.js` specifically.

Verified by re-injecting the exact reported bug:

```
=== with the reported duplicate re-injected ===
   album.js: 'trackArtist' redeclared near line 2388
CAUGHT
```

## Note on the console

`Tracking Prevention blocked access to storage` is the browser's own
first-party-storage protection, unrelated to any of this.
