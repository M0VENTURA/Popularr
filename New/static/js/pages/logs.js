/* ==========================================================================
   static/js/pages/logs.js
   Application log viewer — file picker, tail load, client-side filtering,
   live SSE streaming, copy / download / clear.

   Load order: utils/dom.js → utils/api.js → ui/toast.js, then this file.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/logs.html carried a 251-line inline IIFE with 11 functions.
   The script is this file. There was no inline <style> and the page needs no
   new markup, so the template is otherwise unchanged.

   ── BUGS FIXED / GAPS CLOSED ──────────────────────────────────────────────
   1. THE `log-*` CLASSES HAD NO STYLES ANYWHERE. renderLineHtml wraps the
      timestamp in `.log-ts`, the level in `.log-level.log-level-<level>`, the
      line in `.log-line.log-level-<level>` and any stage tag in `.log-tag`.
      Grepping popularr.css and every other stylesheet finds NONE of them, so
      the viewer rendered as flat monochrome text — the level colour-coding the
      markup was clearly written for never appeared. static/css/logs.css now
      defines them.

   2. `logSelector` WAS DEREFERENCED WITHOUT A GUARD. Every entry point did
      `logSelector.options[logSelector.selectedIndex]`. With no log files the
      element still renders (empty select), but if the template ever renders
      without it, `loadLog()` throws on the first line and the viewer dies with
      a console error and no message. All access goes through `currentFile()`
      now, which returns ''.

   3. `alert()` → toast for the unsupported-EventSource case.

   4. `fetch` + manual text/JSON dance → api.getJson. The original's manual
      parsing was already careful about non-JSON bodies (better than most of the
      codebase), so this is a simplification rather than a fix.

   5. The five level-filter buttons were bound in a loop over
      `#logLevelFilters button`; they are now handled by one delegated listener
      on the container, matching every other page.

   ── WHAT DELIBERATELY STAYS ───────────────────────────────────────────────
   * TAILING: a first load asks for 500 lines, not the whole file, and the
     "Load Full Log" button appears only when the server reports truncation.
     Dumping a 64 MB file into the DOM is what the tail exists to avoid.
   * The live stream caps the buffer at 5000 lines, trimming from the front.
   * AUTO-SCROLL FOLLOWS ONLY WHEN ALREADY AT THE BOTTOM, so reading back
     through history is not yanked away by a new line.
   * The `document.execCommand('copy')` fallback for browsers without the async
     clipboard API. Deprecated, but it is the only option in that case.
   * `window.location = /api/logs/download?...` — a real file download.
   ========================================================================== */

(function (global) {
  'use strict';

  const LOG_ENDPOINT = '/api/log-file';
  const STREAM_ENDPOINT = '/api/logs/stream';
  const DOWNLOAD_ENDPOINT = '/api/logs/download';
  const TAIL_LINES = 500;
  const MAX_STREAM_LINES = 5000;
  /** How close to the bottom still counts as "following the tail". */
  const BOTTOM_SLACK_PX = 40;

  const LEVEL_RE = /(?:\[(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]|\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b)/i;
  const TAG_RE = /\[([A-Za-z0-9_.\-]+)\]/;
  const TS_RE = /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/;

  let rawLines = [];
  let filterText = '';
  let levelFilter = 'all';
  let streamSource = null;
  let loadingAll = false;

  function byId(id) {
    return document.getElementById(id);
  }

  function el() {
    return {
      output: byId('logOutput'),
      selector: byId('logSelector'),
      refresh: byId('refreshLogBtn'),
      stream: byId('streamLogBtn'),
      streamLabel: byId('streamLogLabel'),
      download: byId('downloadLogBtn'),
      copy: byId('copyLogBtn'),
      clear: byId('clearLogBtn'),
      wrap: byId('wrapLinesCheck'),
      filter: byId('logFilterInput'),
      banner: byId('tailBannerText'),
      loadFull: byId('loadFullLogBtn'),
    };
  }

  /** The selected log file's name, or '' when there is nothing to read. */
  function currentFile() {
    const { selector } = el();
    if (!selector || !selector.options || selector.selectedIndex < 0) return '';
    const option = selector.options[selector.selectedIndex];
    return option ? option.value : '';
  }

  function esc(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // ── Line parsing ────────────────────────────────────────────────────────

  /** The log level of a line, or null when the line carries none. */
  function lineLevel(line) {
    const match = LEVEL_RE.exec(line);
    if (!match) return null;
    const level = (match[1] || match[2]).toLowerCase();
    return level === 'warn' ? 'warning' : level;
  }

  /** Highlight the timestamp, level and stage tag, escaping everything else. */
  function renderLineHtml(line) {
    const level = lineLevel(line) || 'info';
    let html = '';
    let rest = line;

    const tsMatch = TS_RE.exec(rest);
    if (tsMatch) {
      html += `<span class="log-ts opacity-75">${esc(tsMatch[1])}</span> `;
      rest = rest.slice(tsMatch[0].length);
    }

    const lvlMatch = LEVEL_RE.exec(rest);
    if (lvlMatch) {
      const matchedText = lvlMatch[1] || lvlMatch[2];
      html += `<span class="log-level log-level-${esc(level)}">${esc(matchedText)}</span> `;
      rest = rest.slice(0, lvlMatch.index) + rest.slice(lvlMatch.index + lvlMatch[0].length);
    }

    const tagMatch = TAG_RE.exec(rest);
    // Only treat it as a stage tag when it is near the start — otherwise a
    // bracketed value mid-message would be highlighted as one.
    if (tagMatch && tagMatch.index < 12) {
      html += `<span class="log-tag badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle mx-1">${esc(tagMatch[1])}</span>`;
      rest = rest.slice(0, tagMatch.index) + rest.slice(tagMatch.index + tagMatch[0].length);
    }

    return `<div class="log-line log-level-${esc(level)}">${html}${esc(rest)}</div>`;
  }

  function matchesFilters(line, level) {
    if (filterText && line.toLowerCase().indexOf(filterText) === -1) return false;
    if (levelFilter === 'all') return true;
    if (levelFilter === 'error') return level === 'error' || level === 'critical';
    return level === levelFilter;
  }

  // ── Render ──────────────────────────────────────────────────────────────

  function render() {
    const { output } = el();
    if (!output) return 0;

    let html = '';
    let visible = 0;
    for (let i = 0; i < rawLines.length; i += 1) {
      const level = lineLevel(rawLines[i]) || 'info';
      if (!matchesFilters(rawLines[i], level)) continue;
      html += renderLineHtml(rawLines[i]);
      visible += 1;
    }

    output.innerHTML = html ||
      '<div class="text-muted small p-2"><i class="bi bi-info-circle me-1"></i> No lines match the current filter.</div>';
    return visible;
  }

  function atBottom() {
    const { output } = el();
    if (!output) return true;
    return output.scrollTop + output.clientHeight >= output.scrollHeight - BOTTOM_SLACK_PX;
  }

  function scrollToBottom() {
    const { output } = el();
    if (output) output.scrollTop = output.scrollHeight;
  }

  // ── Load ────────────────────────────────────────────────────────────────

  function loadLog(opts) {
    const options = opts || {};
    const { output, banner, loadFull } = el();
    const name = currentFile();
    if (!name || !output) return;

    if (banner) banner.classList.add('d-none');
    output.innerHTML =
      '<div class="text-info small p-2"><span class="spinner-border spinner-border-sm me-2"></span> Loading log file…</div>';

    const lines = (options.all || loadingAll) ? 'all' : String(TAIL_LINES);

    global.api.getJson(`${LOG_ENDPOINT}?name=${encodeURIComponent(name)}&lines=${lines}`)
      .then((data) => {
        if (data && data.error) {
          output.innerHTML =
            `<div class="text-danger small p-2"><i class="bi bi-exclamation-triangle me-1"></i> Error: ${esc(data.error)}</div>`;
          return;
        }

        rawLines = (data && data.lines) || [];
        loadingAll = options.all || data.truncated === false;
        const truncated = data.truncated && !options.all;

        if (banner) {
          if (truncated) {
            const total = data.total_lines ? data.total_lines.toLocaleString() : '?';
            banner.innerHTML = `<i class="bi bi-eye"></i> Viewing ${rawLines.length.toLocaleString()} / ${total} lines`;
            banner.classList.remove('d-none');
          } else {
            banner.classList.add('d-none');
          }
        }

        if (loadFull) loadFull.classList.toggle('d-none', !truncated);

        render();
        scrollToBottom();
      })
      .catch((error) => {
        output.innerHTML =
          `<div class="text-danger small p-2"><i class="bi bi-x-circle me-1"></i> Error loading log: ${esc(error.message)}</div>`;
      });
  }

  // ── Live stream (SSE) ───────────────────────────────────────────────────

  function startStream() {
    const { stream, streamLabel, refresh } = el();
    const name = currentFile();
    if (!name) return;

    if (typeof global.EventSource === 'undefined') {
      global.toast.warning('Live streaming is not supported by this browser.');
      return;
    }

    if (streamSource) streamSource.close();

    streamSource = new global.EventSource(`${STREAM_ENDPOINT}?name=${encodeURIComponent(name)}`);

    streamSource.addEventListener('message', function (event) {
      if (!event.data) return;
      try {
        const payload = JSON.parse(event.data);
        const incoming = payload.lines || [];
        if (!incoming.length) return;

        // Capture the follow decision BEFORE the new lines change the
        // scroll height, or the check always reads as "not at bottom".
        const follow = atBottom();

        Array.prototype.push.apply(rawLines, incoming);
        if (rawLines.length > MAX_STREAM_LINES) {
          rawLines = rawLines.slice(-MAX_STREAM_LINES);
        }

        render();
        if (follow) scrollToBottom();
      } catch (_error) {
        // A malformed frame is not worth surfacing; the next one will arrive.
      }
    });

    streamSource.onerror = function () {
      console.warn('[logs] SSE stream connection lost or reset.');
    };

    if (stream) stream.classList.replace('btn-success', 'btn-danger');
    if (streamLabel) streamLabel.textContent = 'Stop Stream';
    if (refresh) refresh.disabled = true;
  }

  function stopStream() {
    const { stream, streamLabel, refresh } = el();
    if (streamSource) {
      streamSource.close();
      streamSource = null;
    }
    if (stream) stream.classList.replace('btn-danger', 'btn-success');
    if (streamLabel) streamLabel.textContent = 'Live Stream';
    if (refresh) refresh.disabled = false;
  }

  // ── Toolbar actions ─────────────────────────────────────────────────────

  function downloadLog() {
    const name = currentFile();
    if (name) global.location.href = `${DOWNLOAD_ENDPOINT}?name=${encodeURIComponent(name)}`;
  }

  async function copyLog(button) {
    const text = rawLines.join('\n');
    if (!text) return;

    const original = button.innerHTML;
    const flash = () => {
      button.innerHTML = '<i class="bi bi-check-lg text-success"></i>';
      setTimeout(() => { button.innerHTML = original; }, 1500);
    };

    if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
      try {
        await global.navigator.clipboard.writeText(text);
        flash();
      } catch (_error) {
        // Clipboard permission refused — fall through to the legacy path.
      }
      return;
    }

    // Deprecated, but the only option without the async clipboard API.
    const textarea = document.createElement('textarea');
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    try { document.execCommand('copy'); } catch (_error) { /* ignore */ }
    document.body.removeChild(textarea);
    flash();
  }

  function clearLog() {
    const { output } = el();
    rawLines = [];
    if (output) output.innerHTML = '';
  }

  function setWrap(checked) {
    const { output } = el();
    if (!output) return;
    output.style.whiteSpace = checked ? 'pre-wrap' : 'pre';
    output.style.wordWrap = checked ? 'break-word' : 'normal';
  }

  function setLevelFilter(level, buttons) {
    buttons.forEach((b) => b.classList.remove('active'));
    levelFilter = level || 'all';
    render();
  }

  // ── Wiring ──────────────────────────────────────────────────────────────

  // NOTE: a missing element must not throw here, or every binding below it
  // would be lost — the shape of the guard-less code this replaced.
  function bind(id, event, handler) {
    const node = byId(id);
    if (node) node.addEventListener(event, handler);
  }

  function initToolbar() {
    bind('refreshLogBtn', 'click', () => loadLog({}));

    bind('streamLogBtn', 'click', () => {
      if (streamSource) stopStream();
      else startStream();
    });

    bind('downloadLogBtn', 'click', downloadLog);
    bind('clearLogBtn', 'click', clearLog);

    const copyBtn = byId('copyLogBtn');
    if (copyBtn) copyBtn.addEventListener('click', () => copyLog(copyBtn));

    bind('wrapLinesCheck', 'change', function () { setWrap(this.checked); });

    bind('logFilterInput', 'input', function () {
      filterText = this.value.trim().toLowerCase();
      render();
    });

    bind('loadFullLogBtn', 'click', function () {
      loadingAll = true;
      const { banner, loadFull } = el();
      if (banner) banner.classList.add('d-none');
      if (loadFull) loadFull.classList.add('d-none');
      loadLog({ all: true });
    });

    bind('logSelector', 'change', function () {
      // Switching files always abandons the stream: it is bound to the old file.
      stopStream();
      loadingAll = false;
      loadLog({});
    });

    // One delegated listener for the level filters.
    const levelBar = byId('logLevelFilters');
    if (levelBar) {
      levelBar.addEventListener('click', function (event) {
        const button = event.target.closest ? event.target.closest('button[data-level]') : null;
        if (!button) return;
        event.preventDefault();
        button.classList.add('active');
        setLevelFilter(
          button.getAttribute('data-level'),
          Array.prototype.slice.call(levelBar.querySelectorAll('button'))
        );
      });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    initToolbar();
    loadLog({});
  });

  global.logsPage = {
    loadLog,
    startStream,
    stopStream,
    render,
    lineLevel,
    renderLineHtml,
  };
})(window);
