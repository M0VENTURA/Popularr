/* ==========================================================================
   static/js/ui/scan-modal.js
   The shared scan sheet (components/_scan_modal.html).

   Requires: utils/api.js, ui/toast.js (all loaded globally by base.html).
   Optionally: ui/button-state.js, services/scan-preflight.js.

   Public API (window.ScanModal):
     ScanModal.open({ scope, label, artist, album })
     ScanModal.close()
     ScanModal.TYPES        the seven scan types, in display order

   ── WHY THIS EXISTS ───────────────────────────────────────────────────────
   Three UIs answered one question. The dashboard had a tabbed engine panel
   with a type <select>; the artist and album pages had an inline <select> and
   a Force checkbox in a form. Each grew its own idea of what "Full" means and
   which options it accepted — and the Restart checkbox on two of them was read
   by no route at all, so it did nothing wherever it appeared.

   One sheet, one list of types, one dispatch table.

   ── THE DISPATCH TABLE IS THE CONTRACT ────────────────────────────────────
   `TYPES` drives both the radio list AND the run. A type cannot appear in the
   UI without a `run` (it would be a button that does nothing) and cannot have
   a `run` without appearing (it would be unreachable) — the two halves cannot
   drift because they are the same array.

   ── SCOPES ────────────────────────────────────────────────────────────────
   `scope` decides WHERE a type is dispatched. Only `library` is wired: it is
   the dashboard's, and all three of its endpoint families already exist and
   are already used by the dashboard's own buttons. `artist` and `album` are
   declared so the component can be included there next, and calling them
   reports that rather than silently doing nothing.
   ========================================================================== */
(function (global) {
  'use strict';

  const SHEET_ID = 'scanSheet';
  let _scope = 'library';
  let _context = {};

  /**
   * The seven scan types.
   *
   * `family` picks the ENDPOINT rather than the type name deciding it inline,
   * because the three families take genuinely different payloads:
   *
   *   popularity  POST /api/popularity/run   {mode, force, restart}
   *               The only family that honours BOTH options, and the reason
   *               Restart works in this sheet at all.
   *   navidrome   POST /api/navidrome/import {mode: 'force'|'all'}
   *               Its own "force" is a FULL RE-IMPORT, not "ignore the skip
   *               window" — a different question, which is why Force maps to
   *               `mode` here rather than being passed through.
   *   essentia    POST /api/essentia/run     {}
   *               Takes no options; the sheet hides what it cannot honour by
   *               simply not reading them.
   *
   * `mode` is the value `/api/popularity/run` expects. `all` is the whole
   * pipeline; the rest run one stage.
   */
  const TYPES = [
    {
      id: 'full', label: 'Full Scan', recommended: true, family: 'popularity', mode: 'all',
      desc: 'Every stage for every artist: Navidrome import, metadata, popularity, singles, cover detection and file tags.',
    },
    {
      id: 'navidrome', label: 'Navidrome', family: 'navidrome',
      /*
        ⚠️ THIS DESCRIPTION USED TO PROMISE THE OPPOSITE.

        It read "Re-import rows from Navidrome only — no external API calls and
        no scoring." The import makes NO such promise: it triggers a Navidrome
        LIBRARY RESCAN and waits for it (up to 30 min) before reading a single
        row, because importing before Navidrome has re-read its files returns
        stale rows whose upsert overwrites metadata just saved. The wording is
        now the truth rather than the intent.

        That wait is why this type can appear to "do nothing" on a large
        library: it is waiting for Navidrome, not idling. Said plainly here so
        the user is not left watching a spinner with no explanation.
      */
      desc: 'Import Navidrome\u2019s rows into Popularr. Rescans the Navidrome library first and waits for that scan to finish, so recently edited files are not imported stale.',
    },
    {
      id: 'popularity', label: 'Popularity', family: 'popularity', mode: 'popularity',
      desc: 'Recompute scores from Last.fm / ListenBrainz. A track that already has a score is kept unless you tick Force.',
    },
    {
      id: 'metadata', label: 'Metadata', family: 'popularity', mode: 'metadata',
      desc: 'MusicBrainz / Discogs enrichment, genres and file tags.',
    },
    {
      id: 'singles', label: 'Singles', family: 'popularity', mode: 'singles',
      desc: 'Single / EP verdicts. A verdict that is still fresh is reused rather than re-fetched.',
    },
    {
      id: 'finalise', label: 'Finalise', family: 'popularity', mode: 'finalise',
      desc: 'Database only: visits every album and recomputes star ratings. It fills in missing values — it does not run metadata or Essentia.',
    },
    {
      id: 'essentia', label: 'Essentia', family: 'essentia',
      desc: 'Analyse audio with Essentia to derive mood and audio features.',
    },
  ];

  const DEFAULT_TYPE = 'full';

  /** Scope → the label, and which families it supports. */
  const SCOPES = {
    library: { label: 'Library', families: ['popularity', 'navidrome', 'essentia'] },
    // Declared, not wired. Both endpoints exist and take the same body shape the
    // popularity family does, so wiring them is a `run` function each rather
    // than a redesign — see the note in _scan_modal.html.
    artist: { label: 'Artist', families: [], pending: true },
    album: { label: 'Album', families: [], pending: true },
  };

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function el(id) {
    return document.getElementById(id);
  }

  function selectedType() {
    const list = el('scanTypeList');
    if (!list) return null;
    const checked = list.querySelector('input[name="scanType"]:checked');
    if (!checked) return null;
    return TYPES.find((t) => t.id === checked.value) || null;
  }

  /**
   * Does the SELECTED type consume a given option?
   *
   * The sheet keeps Force and Restart visible for every type — hiding them
   * would make the list jump around as the selection changes, and a reader
   * cannot compare options across types if the options disappear. Instead the
   * ones a type ignores are DISABLED with a reason, so the control is never a
   * lie: an unchecked, disabled Force on Essentia says "this does not apply",
   * where a hidden one would say nothing.
   */
  function optionApplies(type, option) {
    if (!type) return false;
    if (type.family === 'popularity') return true;   // honours force AND restart
    if (option === 'force' && type.family === 'navidrome') return true;
    return false;
  }

  function renderTypes() {
    const list = el('scanTypeList');
    if (!list) return;

    const allowed = SCOPES[_scope] ? SCOPES[_scope].families : [];
    const runnable = TYPES.filter((t) => allowed.includes(t.family));

    // ⚠️ If a scope supports nothing, say so rather than rendering an empty
    // list with a live Run button. The button is disabled too — a scan sheet
    // that cannot scan should look like it.
    if (!runnable.length) {
      list.innerHTML =
        '<p class="text-muted small mb-0">Scanning from this page is not available yet.</p>';
      syncOptionState();
      return;
    }

    list.innerHTML = runnable.map((t) => `
      <label class="scan-type-option" for="scanType-${esc(t.id)}">
        <input class="form-check-input mt-1" type="radio" name="scanType"
               id="scanType-${esc(t.id)}" value="${esc(t.id)}"
               ${t.id === DEFAULT_TYPE ? 'checked' : ''}>
        <span class="min-w-0">
          <span class="scan-type-label">
            ${esc(t.label)}${t.recommended ? '<span class="scan-type-rec">recommended</span>' : ''}
          </span>
          <span class="scan-type-desc">${esc(t.desc)}</span>
        </span>
      </label>`).join('');

    list.addEventListener('change', syncOptionState);
    syncOptionState();
  }

  /**
   * Enable/disable the advanced options for the SELECTED type.
   *
   * Disabling is not the same as ignoring: the values are still READ on run
   * (the server decides what applies), but a disabled control cannot be
   * ticked, so the sheet never implies an option took effect when it did not.
   */
  function syncOptionState() {
    const type = selectedType();
    const force = el('scanForceOption');
    const restart = el('scanRestartOption');
    const secondary = el('scanSecondaryAction');
    if (!force || !restart) return;

    const forceApplies = optionApplies(type, 'force');
    const restartApplies = optionApplies(type, 'restart');

    force.disabled = !forceApplies;
    restart.disabled = !restartApplies;
    if (!forceApplies) force.checked = false;
    if (!restartApplies) restart.checked = false;

    // Explain WHY a disabled control is disabled, in the control's own title —
    // otherwise it reads as a bug.
    force.closest('.form-check').classList.toggle('opacity-50', !forceApplies);
    restart.closest('.form-check').classList.toggle('opacity-50', !restartApplies);
    force.title = forceApplies ? '' : `${type ? type.label : 'This scan'} does not use this option`;
    restart.title = restartApplies ? '' : `${type ? type.label : 'This scan'} does not use this option`;

    // The Navidrome server rescan belongs to the Navidrome type only — it is a
    // different action from the import (rescan WITHOUT importing), and offering
    // it beside Essentia would imply Essentia talks to Navidrome.
    if (secondary) {
      secondary.classList.toggle('d-none', !(type && type.family === 'navidrome'));
    }
  }

  /**
   * Ask Navidrome to re-read its own files, WITHOUT importing anything.
   *
   * ⚠️ Different endpoint from the Navidrome scan type, and deliberately kept:
   * the dashboard's Navidrome pane was the only place that could reach it, and
   * that pane is now gone. The sheet stays OPEN so it can be used before
   * deciding whether to import.
   *
   * ⚠️ It is NOT true that "neither implies the other". The IMPORT already
   * triggers a Navidrome rescan as its first step and waits for it (see
   * ``run_navidrome_import_scan`` → ``sync_remote_navidrome_before_import``),
   * so this button is the rescan WITHOUT the import — not the only way to get
   * a rescan. Saying otherwise sent the user looking for a rescan that had
   * already happened.
   *
   * GATED like every other start. The dashboard function this replaced
   * (`startNavidromeServerScan`) ran the pre-flight check before its POST, and
   * a server rescan contends for the same Navidrome instance as an import — so
   * dropping the gate here would have quietly removed a guard that existed
   * before the sheet. It asks about THIS action rather than the selected type,
   * because the button sits beside the type list and may be pressed with
   * nothing selected.
   */
  async function triggerNavidromeRescan() {
    const status = el('scanSheetStatus');
    const button = el('scanNavidromeRescanBtn');
    const withBusy = (global.buttonState && global.buttonState.withBusy)
      ? global.buttonState.withBusy
      : ((_b, _l, fn) => fn());

    if (global.ScanPreflight && !await global.ScanPreflight.confirmIfRunning({
      scanName: 'Navidrome Server Rescan',
    })) return;

    return withBusy(button, 'Triggering…', async () => {
      try {
        const data = await global.api.postJson('/api/navidrome/scan/start', {});
        if (status) {
          status.innerHTML = data.success
            ? '<span class="text-success">Navidrome server rescan triggered.</span>'
            : `<span class="text-danger">${esc(data.error || 'Failed')}</span>`;
        }
      } catch (error) {
        if (status) {
          status.innerHTML = `<span class="text-danger">${esc(error.message)}</span>`;
        }
      }
    });
  }

  /** Build and send the run request for the selected type. */
  async function run() {
    const type = selectedType();
    const status = el('scanSheetStatus');
    const button = el('scanSheetRunBtn');
    const scopeMeta = SCOPES[_scope] || SCOPES.library;

    if (!type) {
      if (status) status.innerHTML = '<span class="text-danger">Choose a scan type first.</span>';
      return;
    }
    if (!scopeMeta.families.length) {
      if (status) {
        status.innerHTML =
          '<span class="text-warning">Scanning from this page is not available yet.</span>';
      }
      return;
    }

    const force = !!(el('scanForceOption') || {}).checked;
    const restart = !!(el('scanRestartOption') || {}).checked;

    const withBusy = (global.buttonState && global.buttonState.withBusy)
      ? global.buttonState.withBusy
      : ((_b, _l, fn) => fn());

    if (status) status.textContent = '';

    return withBusy(button, 'Starting…', async () => {
      try {
        // The pre-flight prompt belongs here rather than on the opening button:
        // the user has now committed to a specific type, so "a scan is already
        // running" is a question about THAT scan.
        if (global.ScanPreflight && !await global.ScanPreflight.confirmIfRunning({
          scanName: global.ScanPreflight.label
            ? global.ScanPreflight.label(type.mode || type.id)
            : type.label,
        })) return;

        if (type.family === 'essentia') {
          await global.api.postJson('/api/essentia/run', {});
        } else if (type.family === 'navidrome') {
          // ⚠️ `force` here is a FULL re-import, so it maps to `mode` — passing
          // it as a boolean would be interpreted as nothing at all.
          await global.api.postJson('/api/navidrome/import', {
            mode: force ? 'force' : 'all',
          });
        } else {
          const body = { mode: type.mode || 'all', force, restart };
          if (_context.artist) body.artist = _context.artist;
          if (_context.album) body.album = _context.album;

          // ── Resume prompt ───────────────────────────────────────────────
          //
          // ⚠️ This MOVED here from `pages/dashboard.js::runDashboardPopularityScan`
          // when that function was deleted. It is not optional polish: without
          // it, every continuation silently starts from the stored checkpoint
          // with no way to choose, which is the whole reason
          // `/api/scan/resume-options` exists.
          //
          // Only when CONTINUING (`!restart`). Restart means "from the top", so
          // asking which artist to resume from there would contradict the tick
          // the user just made.
          //
          // `resume_from` is honoured by every popularity mode — the load stage
          // skips to that artist — so a metadata/popularity/singles run gets the
          // same choice as a full scan. Null means no checkpoint was ever
          // written, in which case nothing is added to the body.
          if (!restart && global.ScanResumePicker) {
            const choice = await global.ScanResumePicker.chooseResumeArtist();
            if (!choice) return;   // cancelled — do not start
            if (choice.resume_from) body.resume_from = choice.resume_from;
          }

          await global.api.postJson('/api/popularity/run', body);
        }

        if (global.toast) global.toast.success(`${type.label} scan started`);
        close();
      } catch (error) {
        // Stay OPEN on failure: closing would hide the reason, and the user
        // would have to reopen and re-choose the type to retry.
        if (status) {
          status.innerHTML = `<span class="text-danger">Could not start: ${esc(error.message)}</span>`;
        }
        if (global.toast) global.toast.error('Could not start scan: ' + error.message);
      }
    });
  }

  function toggleAdvanced() {
    const options = el('scanAdvancedOptions');
    const toggle = el('scanAdvancedToggle');
    const chevron = el('scanAdvancedChevron');
    if (!options || !toggle) return;
    const open = options.classList.contains('d-none');
    options.classList.toggle('d-none', !open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (chevron) {
      chevron.className = 'bi float-end ' + (open ? 'bi-chevron-down' : 'bi-chevron-right');
    }
  }

  let sheet = null;

  function open(opts) {
    const config = opts || {};
    _scope = config.scope || 'library';
    _context = { artist: config.artist || '', album: config.album || '' };

    const node = el(SHEET_ID);
    if (!node) return;
    node.dataset.scanScope = _scope;

    renderTypes();
    if (el('scanSheetStatus')) el('scanSheetStatus').textContent = '';
    // Options start clear: a Force ticked on a previous open would silently
    // re-scan the world.
    if (el('scanForceOption')) el('scanForceOption').checked = false;
    if (el('scanRestartOption')) el('scanRestartOption').checked = false;

    if (!sheet && global.bootstrap && global.bootstrap.Offcanvas) {
      sheet = global.bootstrap.Offcanvas.getOrCreateInstance(node);
    }
    if (sheet) sheet.show();
  }

  function close() {
    if (sheet) sheet.hide();
  }

  function init() {
    const node = el(SHEET_ID);
    if (!node) return;
    if (el('scanAdvancedToggle')) {
      el('scanAdvancedToggle').addEventListener('click', toggleAdvanced);
    }
    if (el('scanSheetRunBtn')) {
      el('scanSheetRunBtn').addEventListener('click', run);
    }
    if (el('scanNavidromeRescanBtn')) {
      el('scanNavidromeRescanBtn').addEventListener('click', triggerNavidromeRescan);
    }
    // The sheet's scope comes from the template, so a page that includes it
    // with a different `scan_scope_kind` is correct before any JS runs.
    _scope = node.dataset.scanScope || 'library';
    renderTypes();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  global.ScanModal = { open, close, init, TYPES, SCOPES, selectedType, triggerNavidromeRescan };
})(window);
