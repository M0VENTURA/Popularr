/**
 * Behaviour probe for the scan sheet's TARGETED (artist / album) dispatch.
 *
 * ⚠️ Extracts the REAL functions from the shipped module rather than
 * reimplementing them. Two bugs were found in this code by reading it, and both
 * are the kind a source-only assertion would have missed:
 *
 *   1. the `postJson` shim called ITSELF (infinite recursion on every POST);
 *   2. `run()` read `force` with `const` BELOW the targeted branch, so
 *      `submitTargetedScan(type, force)` threw `ReferenceError` (temporal dead
 *      zone) before it could submit anything.
 *
 * Reading cannot prove either is gone. This submits a real form and inspects
 * what came out.
 *
 * Run with: node tests/js/scan-sheet-targeted-probe.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'test_site', 'static', 'js', 'ui', 'scan-modal.js');
const src = fs.readFileSync(SRC, 'utf8');

let failures = 0;
function ok(label, cond, detail) {
  if (!cond) failures += 1;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}`);
  if (!cond && detail !== undefined) console.log(`         ${detail}`);
}

/**
 * Load the module against a stub DOM and expose the internals under test.
 *
 * The file is an IIFE, so the probe API is spliced in just before its closing
 * invocation — outside it, the closure variables (`_scope`, `_context`, …) are
 * unreachable.
 *
 * The form is captured rather than submitted: `submit()` is replaced so the
 * probe can read the action and fields instead of navigating.
 */
function loadModule() {
  const captured = { forms: [] };
  const els = {};

  function makeEl(tag) {
    const el = {
      tagName: String(tag || 'div').toUpperCase(),
      children: [],
      style: {},
      dataset: {},
      className: '',
      innerHTML: '',
      textContent: '',
      title: '',
      checked: false,
      disabled: false,
      value: '',
      type: '',
      name: '',
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      setAttribute() {},
      removeAttribute() {},
      getAttribute() { return null; },
      addEventListener() {},
      querySelector() { return null; },
      querySelectorAll() { return []; },
      closest() { return null; },
      appendChild(child) { el.children.push(child); return child; },
      // The capture point: record instead of navigating.
      submit() { captured.forms.push(el); },
    };
    return el;
  }

  const windowStub = {
    document: {
      getElementById: (id) => els[id] || null,
      createElement: makeEl,
      addEventListener() {},
      body: makeEl('body'),
      readyState: 'complete',
      querySelectorAll() { return []; },
    },
    bootstrap: null,
    api: { postJson: () => { throw new Error('a LIBRARY endpoint was called for a targeted scan'); } },
    toast: { success() {}, error() {} },
    ScanPreflight: null,
    buttonState: null,
    location: { href: '' },
    addEventListener() {},
  };

  const closing = '})(window);';
  const at = src.lastIndexOf(closing);
  if (at === -1) throw new Error('could not find the IIFE closing marker');

  const spliced =
    src.slice(0, at) +
    `
  global.__probe = {
    open(scope, ctx) {
      _scope = scope;
      _context = ctx || {};
    },
    types() { return TYPES; },
    scopes() { return SCOPES; },
    selected(id) { return TYPES.find(function (t) { return t.id === id; }); },
    applies(id, option) { return optionApplies(this.selected(id), option); },
    reason(id, option) { return optionReason(this.selected(id), option); },
    submit(id, force) { submitTargetedScan(this.selected(id), !!force); },
  };
  ` +
    src.slice(at) + '\n';

  const factory = new Function('window', 'document', 'globalThis', spliced + '\nreturn window.__probe;');
  const probe = factory(windowStub, windowStub.document, windowStub);
  probe.__captured = captured;
  return probe;
}

function formField(form, name) {
  const found = form.children.filter((c) => c.name === name);
  return found.length ? found[0].value : undefined;
}

console.log('the scope table');
{
  const p = loadModule();
  const scopes = p.scopes();

  ok('artist and album are WIRED (no `pending` flag)',
     !scopes.artist.pending && !scopes.album.pending,
     JSON.stringify(scopes));
  ok('artist submits as a form', scopes.artist.submit === 'form');
  ok('album submits as a form', scopes.album.submit === 'form');
  ok('library still submits as JSON', scopes.library.submit === 'json');
  ok('artist action is the artist-custom route',
     scopes.artist.action === '/scan/artist-custom', scopes.artist.action);
  ok('album action is the album-custom route',
     scopes.album.action === '/scan/album-custom', scopes.album.action);
  ok('the form scopes support the whole family set',
     ['popularity', 'navidrome', 'essentia'].every((f) => scopes.album.families.includes(f)),
     JSON.stringify(scopes.album.families));
}

console.log('\nthe submitted form');
{
  const p = loadModule();
  p.open('album', { artist: 'Test Artist', album: 'Nevermind' });
  p.submit('metadata', false);

  const forms = p.__captured.forms;
  ok('exactly one form was submitted', forms.length === 1, `got ${forms.length}`);
  const form = forms[0];

  ok('it POSTs', form.method === 'POST', form.method);
  ok('to the album route', form.action === '/scan/album-custom', form.action);
  ok('scan_type is the selected type id', formField(form, 'scan_type') === 'metadata',
     formField(form, 'scan_type'));
  ok('artist is carried', formField(form, 'artist') === 'Test Artist', formField(form, 'artist'));
  ok('album is carried', formField(form, 'album') === 'Nevermind', formField(form, 'album'));
  ok('force is OMITTED when unchecked', formField(form, 'force') === undefined);
  ok('restart is NEVER sent (the route does not read it)',
     formField(form, 'restart') === undefined);

  const p2 = loadModule();
  p2.open('artist', { artist: 'Test Artist', album: '' });
  p2.submit('full', true);
  const form2 = p2.__captured.forms[0];
  ok('the artist route is used for the artist scope',
     form2.action === '/scan/artist-custom', form2.action);
  ok('force IS sent when checked', formField(form2, 'force') === '1');
  ok('the artist form carries NO album field', formField(form2, 'album') === undefined);
}

console.log('\nwhich options apply (per scope)');
{
  const artistScope = loadModule();
  artistScope.open('artist', { artist: 'A' });

  ok('force applies to a popularity artist scan',
     artistScope.applies('popularity', 'force') === true);
  ok('force applies to a navidrome artist scan',
     artistScope.applies('navidrome', 'force') === true);
  ok('⭐ RESTART is refused for a targeted scan (the route ignores it)',
     artistScope.applies('popularity', 'restart') === false);
  ok('…and the reason says WHY, not just "does not use this option"',
     /restart/i.test(artistScope.reason('popularity', 'restart'))
       && !/does not use this option/.test(artistScope.reason('popularity', 'restart')),
     artistScope.reason('popularity', 'restart'));
  ok('essentia refuses both in a targeted scan',
     artistScope.applies('essentia', 'force') === false
       && artistScope.applies('essentia', 'restart') === false);

  const libraryScope = loadModule();
  libraryScope.open('library', {});
  ok('CONTROL: a LIBRARY popularity scan still allows restart',
     libraryScope.applies('popularity', 'restart') === true,
     'this is the scope where restart genuinely works');
  ok('CONTROL: a library navidrome scan still allows only force',
     libraryScope.applies('navidrome', 'force') === true
       && libraryScope.applies('navidrome', 'restart') === false);
}

console.log('\nthe selected type ids match the route vocabulary');
{
  const p = loadModule();
  // `/scan/artist-custom` branches on these exact strings.
  const ROUTE_TYPES = ['full', 'navidrome', 'popularity', 'metadata', 'singles', 'finalise', 'essentia'];
  for (const id of ROUTE_TYPES) {
    ok(`"${id}" is a type the sheet can pick`, !!p.selected(id));
  }
}

console.log(`\n${failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'}`);
process.exit(failures === 0 ? 0 : 1);
