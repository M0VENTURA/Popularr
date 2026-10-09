/* ==========================================================================
   tests/js/mobile-navbar-flicker-probe.js
   Simulate the ResizeObserver ↔ CSS feedback loop behind the mobile flicker.

   The reported symptom ("pressing the hamburger, the bottom of the page keeps
   flickering") is a LOOP, not a layout mistake:

       .navbar-collapse.show { max-height: calc(100dvh - var(--navbar-height) - 8px) }

   clamps the open menu with the token, while syncNavbarHeight() DERIVES the
   token by measuring the navbar — which, with the menu open, INCLUDES the
   menu. Grow → token grows → max-height shrinks → menu clamps → the navbar's
   height changes → ResizeObserver fires → repeat.

   A structural test ("the file mentions collapsing") cannot see that, so this
   extracts the REAL syncNavbarHeight from the shipped file, gives it a stub
   DOM whose layout obeys the same rule, and drives the loop the way a browser
   would: apply the function, recompute layout, fire the observer, repeat.

   It reports one JSON line. `stable: true` and `applied: <chrome>` is a pass.

   Usage: node mobile-navbar-flicker-probe.js <main.js> [viewportHeight]
   ========================================================================== */

'use strict';

const fs = require('fs');
const path = require('path');

const DEFAULT_FILE = path.join('static', 'js', 'main.js');
const FILE = process.argv[2] || DEFAULT_FILE;
const VIEWPORT = parseInt(process.argv[3] || '720', 10);

const source = fs.readFileSync(FILE, 'utf8');

/**
 * Pull the browser-side logic out of either tree's main.js.
 *
 * The live tree declares these at module scope; the rebuilt tree wraps them in
 * an IIFE. Both are copied verbatim from `const NAVBAR_MOBILE` through the end
 * of `syncNavbarHeight`, so the probe always runs the SHIPPED code.
 */
function extractModule(src) {
  const start = src.indexOf('const NAVBAR_MOBILE');
  const anchor = src.indexOf('function syncNavbarHeight', start === -1 ? 0 : start);
  if (anchor === -1) throw new Error('syncNavbarHeight not found');

  // The pre-fix shape had no mobile guard at all; run that too, so the probe
  // can be pointed at an older revision and show the loop it used to cause.
  const from = start === -1 || start > anchor ? anchor : start;
  const open = src.indexOf('{', anchor);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(from, i + 1);
    }
  }
  throw new Error('unbalanced braces in syncNavbarHeight');
}

/** Layout + ResizeObserver model of the mobile navbar. */
function makeEnv({ chrome = 110, menuContent = 430, viewport = VIEWPORT } = {}) {
  const state = { open: true, token: 0, domWidth: 390 };

  const collapse = {
    className: 'collapse navbar-collapse nav-links-wrapper show',
    classList: {
      // The base classes are always there; 'show' only while the menu is open.
      contains: (name) => name === 'collapse'
        || name === 'navbar-collapse'
        || name === 'nav-links-wrapper'
        || (state.open && name === 'show'),
    },
    // The clamp is the CSS rule, expressed exactly as the stylesheet has it.
    get offsetHeight() {
      if (!state.open) return 0;
      return Math.min(menuContent, Math.max(0, viewport - state.token - 8));
    },
  };

  const nav = {
    // LIVE, not a snapshot: the navbar's height is the chrome PLUS whatever
    // the collapse currently occupies, so it must be recomputed after every
    // token change — that dependency is the loop being tested.
    get offsetHeight() {
      return chrome + collapse.offsetHeight;
    },
    contains: () => true,
  };

  const observers = [];
  const doc = {
    documentElement: {
      style: { setProperty: (name, value) => {
        if (name === '--navbar-height') state.token = parseFloat(value);
      } },
    },
    getElementById: (id) => (id === 'navbarNav' ? collapse : null),
    querySelector: (sel) => (sel.indexOf('nav.navbar.fixed-top') === 0 ? nav : null),
  };

  const win = {
    matchMedia: () => ({ matches: true }),   // mobile viewport
    ResizeObserver: class {
      constructor(cb) { this.cb = cb; observers.push(this); }
      observe() {}
    },
  };

  return { state, nav, collapse, doc, win, observers };
}

function run() {
  const body = extractModule(source);
  const env = makeEnv();

  // The function is called with `global`/`document` bound the way the bundle
  // would see them. Function declarations inside the slice close over this.
  const factory = new Function(
    'window', 'document', 'global', 'globalThis',
    `${body}\nreturn { sync: syncNavbarHeight,`
      + ' isOpen: typeof navbarCollapseIsOpen === "function"'
      + ' ? navbarCollapseIsOpen : function () { return false; } };'
  );
  const api = factory(env.win, env.doc, env.win, env.win);

  const first = env.state.token;
  api.sync();
  const applied = env.state.token;

  // Drive the observer loop: every token change can resize the navbar, which
  // fires the observer, which calls sync() again — 50 iterations is far more
  // than a browser's settle window.
  const history = [applied];
  for (let i = 0; i < 50; i += 1) {
    api.sync();
    history.push(env.state.token);
  }

  const unique = Array.from(new Set(history));
  const afterFirst = history.slice(1);
  const stable = afterFirst.every((v) => v === afterFirst[0]);

  console.log(JSON.stringify({
    file: FILE,
    viewport: VIEWPORT,
    initial_token: first,
    applied_token: applied,
    unique_tokens: unique.length,
    stable,
    chrome: env.nav.offsetHeight - env.collapse.offsetHeight,
    menu_height: env.collapse.offsetHeight,
    sequence: unique.slice(0, 4),
  }));
}

run();
