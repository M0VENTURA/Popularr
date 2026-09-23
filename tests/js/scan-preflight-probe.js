/* ==========================================================================
   tests/js/scan-preflight-probe.js
   Drive the SHIPPED ScanPreflight gate and print one JSON line.

   A structural test ("does the file mention /scan/stop-all") would pass even if
   the ordering were wrong — and ordering is the whole feature:
   confirm -> stop -> WAIT for idle -> only then allow the start.

   So this extracts the real module body, runs it inside a sandbox with stub
   `window`/`fetch`/timers, and reports what actually happened.

   Usage: node scan-preflight-probe.js <scenario>
   Scenarios: idle | cancel | proceed | wait-timeout | probe-failed | double-attach
   ========================================================================== */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MODULE = process.argv[3] ||
  path.join('test_site', 'static', 'js', 'services', 'scan-preflight.js');
const SCENARIO = process.argv[2] || 'idle';

const source = fs.readFileSync(MODULE, 'utf8');

/** A window stub that records every call the module makes. */
function makeWindow() {
  const calls = { get: [], post: [], confirm: 0, alerts: [] };

  // Deterministic fake timers: setTimeout runs immediately so the poll loop
  // finishes without real waiting. The module polls every POLL_INTERVAL_MS.
  const fakeSetTimeout = (fn) => { fn(); return 0; };

  const win = {
    escapeHtml: (v) => String(v == null ? '' : v),
    ui: {
      confirm: (opts) => {
        calls.confirm += 1;
        calls.confirmOpts = opts;
        return Promise.resolve(globalThis.__accept === true);
      },
    },
    confirm: (text) => {
      calls.confirm += 1;
      calls.confirmText = text;
      return globalThis.__accept === true;
    },
    alert: (m) => { calls.alerts.push(String(m)); },
    toast: { error: (m) => calls.alerts.push(String(m)), success: () => {} },
    setTimeout: fakeSetTimeout,
  };

  // fetch stub: decided per scenario.
  win.fetch = (url, init) => {
    const method = (init && init.method) || 'GET';
    if (method === 'POST') {
      calls.post.push(url);
      return Promise.resolve({
        ok: true, status: 200,
        json: async () => ({ success: true, message: 'stop requested' }),
      });
    }
    calls.get.push(url);

    if (globalThis.__probeFails) {
      return Promise.resolve({ ok: false, status: 500, json: async () => ({}) });
    }

    // Sequence of /api/scan-progress answers. Each GET consumes the next entry;
    // the last entry repeats.
    const seq = globalThis.__progressSeq || [];
    const idx = Math.min(globalThis.__progressIdx || 0, seq.length - 1);
    globalThis.__progressIdx = (globalThis.__progressIdx || 0) + 1;
    const payload = seq[idx] || { active_scans: [] };

    return Promise.resolve({
      ok: true, status: 200,
      json: async () => payload,
    });
  };

  win.document = {
    readyState: 'complete',
    addEventListener: () => {},
    querySelectorAll: () => [],
    getElementById: () => null,
  };

  return { win, calls };
}

function running(type, opts) {
  return {
    scan_type: type,
    is_running: true,
    percent_complete: (opts && opts.pct) || 0,
    current_artist: (opts && opts.artist) || null,
    current_album: (opts && opts.album) || null,
  };
}

async function main() {
  const { win, calls } = makeWindow();

  // Scenario configuration BEFORE the module loads.
  globalThis.__probeFails = false;
  globalThis.__accept = true;

  const IDLE = { active_scans: [] };

  if (SCENARIO === 'idle') {
    globalThis.__progressSeq = [IDLE];
  } else if (SCENARIO === 'cancel') {
    globalThis.__accept = false;
    globalThis.__progressSeq = [{ active_scans: [running('full_scan', { artist: 'Madball', pct: 40 })] }];
  } else if (SCENARIO === 'proceed') {
    globalThis.__accept = true;
    // 1st GET: something is running. Then the wait-loop GETs: first still
    // running, then idle -> takes 2 poll ticks.
    globalThis.__progressSeq = [
      { active_scans: [running('full_scan', { artist: 'Madball', pct: 40 })] },
      { active_scans: [running('full_scan', { artist: 'Madball', pct: 55 })] },
      IDLE,
    ];
  } else if (SCENARIO === 'wait-timeout') {
    globalThis.__accept = true;
    // Never goes idle -> must abort rather than start into a duplicate guard.
    globalThis.__progressSeq = [{ active_scans: [running('popularity_scan', { artist: 'X' })] }];
  } else if (SCENARIO === 'probe-failed') {
    globalThis.__probeFails = true;
  }

  const sandbox = {
    window: win,
    document: win.document,
    setTimeout: win.setTimeout,
    clearTimeout: () => {},
    console,
    Promise,
    Number,
    Math,
    Date,
    WeakSet,
    WeakMap,
    JSON,
  };
  sandbox.globalThis = sandbox;
  // The module is an IIFE taking `window`; expose it as `global` too since the
  // built file references the parameter name, not window.
  vm.createContext(sandbox);
  sandbox.fetch = win.fetch;

  vm.runInContext(source, sandbox, { filename: MODULE });

  const SP = sandbox.window.ScanPreflight;
  if (!SP) {
    console.log(JSON.stringify({ ok: false, error: 'ScanPreflight not published' }));
    return;
  }

  let proceed = null;
  try {
    proceed = await SP.confirmIfRunning(
      SCENARIO === 'wait-timeout' ? { timeoutMs: 3 } : {}
    );
  } catch (err) {
    console.log(JSON.stringify({ ok: false, error: String(err && err.message) }));
    return;
  }

  const out = {
    ok: true,
    scenario: SCENARIO,
    proceed: proceed === true,
    confirmCalls: calls.confirm,
    posts: calls.post,
    progressGets: calls.get.length,
    alerts: calls.alerts,
    confirmMessage: (calls.confirmOpts && calls.confirmOpts.message) || calls.confirmText || '',
    confirmItems: (calls.confirmOpts && calls.confirmOpts.items) || [],
  };
  console.log(JSON.stringify(out));
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, error: String(err && err.stack || err) }));
});
