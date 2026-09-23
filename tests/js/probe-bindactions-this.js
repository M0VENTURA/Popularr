/**
 * Decisive probe: do download-queue.js's row actions actually fire?
 *
 * Loads the REAL bindActions from test_site/static/js/services/item-groups.js
 * and drives it with the REAL handler map extracted from
 * test_site/static/js/pages/download-queue.js. Nothing under test is
 * reimplemented — a reimplementation would prove nothing about the shipped code.
 *
 * Usage: node tests/js/probe-bindactions-this.js
 * Exit 0 = every handler runs. Exit 1 = at least one is dead.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const REPO = path.join(__dirname, '..', '..');
const IG = fs.readFileSync(path.join(REPO, 'test_site/static/js/services/item-groups.js'), 'utf8');
const DQ = fs.readFileSync(path.join(REPO, 'test_site/static/js/pages/download-queue.js'), 'utf8');

let failures = 0;
const ok = (label, cond, detail) => {
  if (!cond) failures++;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${detail && !cond ? '  [' + detail + ']' : ''}`);
};

// ── Load the real bindActions by injecting into its IIFE ──────────────────
function loadItemGroups() {
  const stubDocument = { addEventListener() {} };
  const win = { document: stubDocument };
  const at = IG.lastIndexOf('})(window);');
  const spliced = IG.slice(0, at) + '\n  window.__probe = { bindActions };\n' + IG.slice(at);
  const fn = new Function('window', 'document', spliced + '\nreturn window.__probe;');
  return fn(win, stubDocument);
}

// ── Extract the real handler map from download-queue.js ───────────────────
const start = DQ.indexOf('const handlers = {');
const end = DQ.indexOf('};', DQ.indexOf("'.group-organize-modal'"));
const mapSrc = DQ.slice(start, end + 2);

const usesThis = (mapSrc.match(/this\.dataset/g) || []).length;
const selectors = [...mapSrc.matchAll(/'(\.queue-[a-z-]+|\.group-[a-z-]+)':/g)].map((m) => m[1]);

console.log('extracted handler map');
ok('map has handlers', selectors.length >= 5, `${selectors.length} found`);
ok('map uses this.dataset', usesThis > 0, `${usesThis} occurrences`);
console.log(`         selectors: ${selectors.join(', ')}`);
console.log();

const { bindActions } = loadItemGroups();

// Effects recorded by the stubbed collaborators the handlers call.
const effects = [];
const mk = (name) => function () { effects.push(name); };

const scope = {
  manualQueueSearch: mk('manualQueueSearch'),
  cancelQueueItem: mk('cancelQueueItem'),
  retryQueueItem: mk('retryQueueItem'),
  organizeFile: mk('organizeFile'),
  deleteQueueItem: mk('deleteQueueItem'),
  cancelGroup: mk('cancelGroup'),
  retryGroup: mk('retryGroup'),
  organizeGroup: mk('organizeGroup'),
  deleteGroup: mk('deleteGroup'),
  lookupGroup: () => ({ key: 'k', items: [] }),
  openOrganizeGroupModal: mk('openOrganizeGroupModal'),
};

// The map's handlers call collaborators that live in download-queue.js's module
// scope, so they are injected as locals. Without this the probe fails with
// ReferenceError and masks the thing under test (the `this` binding).
const harness = `
  const {
    manualQueueSearch, cancelQueueItem, retryQueueItem, organizeFile,
    deleteQueueItem, cancelGroup, retryGroup, organizeGroup, deleteGroup,
    lookupGroup, openOrganizeGroupModal,
  } = scope;
  ${mapSrc}
  return { handlers };
`;

let handlers;
try {
  handlers = new Function('scope', harness)(scope).handlers;
  ok('handler map evaluates', !!handlers);
} catch (err) {
  ok('handler map evaluates', false, err.message);
  process.exit(1);
}

// A button carrying every data-* the handlers read.
function makeButton() {
  const listeners = [];
  return {
    dataset: { query: 'Nine Inch Nails - Hurt', queueId: '7', groupKind: 'active', groupKey: 'k1' },
    addEventListener(type, fn) { if (type === 'click') listeners.push(fn); },
    click() {
      // addEventListener semantics: `this` inside the listener IS the element.
      listeners.forEach((fn) => fn.call(this, { type: 'click' }));
    },
  };
}

console.log('dispatching one click per handler (real bindActions, real handlers)');
let dead = 0;
for (const sel of selectors) {
  const btn = makeButton();
  const root = { querySelectorAll: (s) => (s === sel ? [btn] : []) };
  bindActions(root, { [sel]: handlers[sel] });
  const before = effects.length;
  try {
    btn.click();
  } catch (err) {
    dead++;
    console.log(`  THREW   ${sel}: ${err.constructor.name}: ${err.message}`);
    continue;
  }
  const fired = effects.length > before;
  if (!fired) dead++;
  console.log(`  ${fired ? 'FIRED  ' : 'NO-OP  '} ${sel}`);
}

console.log();
ok('every queue/group handler runs when clicked', dead === 0, `${dead} of ${selectors.length} did not`);

console.log();
console.log(failures
  ? `${failures} check(s) FAILED — the row actions are dead`
  : 'all checks passed');
process.exit(failures ? 1 : 0);
