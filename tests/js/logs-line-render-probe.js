/**
 * Behaviour probe for the log viewer's LINE RENDERER.
 *
 * ⚠️ Extracts the REAL `renderLineHtml` / `TS_RE` from
 * test_site/static/js/pages/logs.js rather than reimplementing them, so this
 * cannot drift from the shipped code. The regex is the thing under test: a
 * copy of it here would pass while the shipped one still leaked the fragment.
 *
 * Run with: node tests/js/logs-line-render-probe.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'test_site', 'static', 'js', 'pages', 'logs.js');
const src = fs.readFileSync(SRC, 'utf8');

let failures = 0;
function ok(label, cond, detail) {
  if (!cond) failures += 1;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}`);
  if (!cond && detail !== undefined) console.log(`         ${detail}`);
}

/**
 * Load the module against a stub DOM and expose the line renderer.
 *
 * Same splice technique as logs-viewer-probe.js: the file is an IIFE, so the
 * probe API is injected just before its closing invocation — outside it, the
 * closure variables (including `showTimestamps`) are unreachable.
 */
function loadModule(opts) {
  const state = Object.assign(
    { filterText: '', filterTextLower: '', filterRegex: false,
      filterCaseSensitive: false, levelFilter: 'all', unreadCount: 0 },
    (opts && opts.state) || {}
  );

  const els = Object.assign({
    logOutput: { scrollTop: 0, clientHeight: 100, scrollHeight: 100,
                 addEventListener() {}, style: {} },
  }, (opts && opts.els) || {});

  const windowStub = {
    document: {
      getElementById: (id) => els[id] || null,
      addEventListener() {},
      createElement: () => ({ style: {}, select() {}, value: '' }),
      body: { appendChild() {}, removeChild() {} },
    },
    api: { getJson: () => Promise.resolve({ lines: [] }) },
    toast: { warning() {} },
    location: { href: '' },
    navigator: {},
    EventSource: function () {},
    addEventListener() {},
  };

  const closing = '})(window);';
  const at = src.lastIndexOf(closing);
  if (at === -1) {
    throw new Error('could not find the IIFE closing marker — did logs.js change shape?');
  }

  const spliced =
    src.slice(0, at) +
    `
  global.__probe = {
    render(line, withTimestamps) {
      showTimestamps = !!withTimestamps;
      return renderLineHtml(line);
    },
    timestampSource() { return TS_RE.source; },
  };
  ` +
    src.slice(at) + '\n';

  const factory = new Function('window', 'document', 'globalThis', spliced + '\nreturn window.__probe;');
  return factory(windowStub, windowStub.document, windowStub);
}

console.log('microsecond fragments');
{
  const m = loadModule({});
  const STRUCTLOG = '2026-10-13T11:18:55.200965 [INFO] [db.engine] engine created';

  const off = m.render(STRUCTLOG, false);
  ok('timestamps OFF leaves no fractional-second fragment', !off.includes('.200965'), off);
  ok('timestamps OFF leaves no dot-digit run at all', !/\d\.\d{3,}/.test(off), off);
  ok('timestamps OFF still renders the message', off.includes('engine created'), off);
  ok('timestamps OFF still renders the level', off.includes('INFO'), off);

  const on = m.render(STRUCTLOG, true);
  // The timestamp is emitted VERBATIM from the log (`T` separator and all) —
  // the renderer does not reformat it, and normalising it here would be a
  // display change nobody asked for. What must hold is that the timestamp is
  // SHOWN and that the fraction is not appended to it.
  ok('timestamps ON shows the timestamp', on.includes('2026-10-13T11:18:55'), on);
  ok('timestamps ON does NOT show the microseconds', !on.includes('200965'), on);
  ok('timestamps ON has no stray fraction after the timestamp',
     !/<\/span>\s*\.\d/.test(on), on);
  ok('timestamps ON still renders the message', on.includes('engine created'), on);
}

console.log('\nother timestamp shapes');
{
  const m = loadModule({});

  // The stdlib formatter writes `%(asctime)s.%(msecs)03d` — 3 digits, not 6.
  const ms = m.render('2026-10-13 11:18:55.200 [INFO] [app] ready', false);
  ok('3-digit milliseconds are consumed too', !ms.includes('200 [INFO]'), ms);
  ok('3-digit case keeps the message', ms.includes('ready'), ms);

  // A comma decimal separator (some European loggers).
  const comma = m.render('2026-10-13 11:18:55,200965 [INFO] [app] ready', false);
  ok('a comma separator is consumed', !comma.includes('200965'), comma);

  // No fraction at all — the plain path must be untouched.
  const plain = m.render('2026-10-13 11:18:55 [INFO] [app] ready', false);
  ok('a plain timestamp is unaffected', plain.includes('ready') && !plain.includes('55 ['), plain);

  // ISO with a T separator.
  const iso = m.render('2026-10-13T11:18:55.313367 [WARN] [x] slow', false);
  ok('the reported .313367 fragment is gone', !iso.includes('313367'), iso);

  // A line with NO timestamp must not lose anything.
  const none = m.render('  plain continuation line with 1.234 numbers', false);
  ok('a line with no timestamp is untouched', none.includes('1.234'), none);
}

console.log('\nthe regex itself');
{
  const m = loadModule({});
  const source = m.timestampSource();
  ok('the fractional part is part of the pattern', '[.,]', source);
  ok('the fraction is non-capturing, so group 1 stays seconds-only',
     !source.includes('([.,]'), source);
}

console.log(`\n${failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'}`);
process.exit(failures === 0 ? 0 : 1);
