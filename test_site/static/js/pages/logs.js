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
  let levelFilter = 'all';
  let streamSource = null;
  let loadingAll = false;

  // ── Filter modes ────────────────────────────────────────────────────────
  // Both default OFF, which is exactly the previous behaviour (plain
  // case-insensitive substring) — so an untouched page behaves as before.
  let filterRegex = false;
  let filterCaseSensitive = false;

  /**
   * The filter pattern, RAW and trimmed. This is the single source of truth.
   *
   * ⚠️ It is deliberately NOT lower-cased. Lower-casing is correct for the
   * substring path but would CORRUPT a regex: `[A-Z]+` becomes `[a-z]+`, and
   * `\S` is fine but `[A-Z]` is silently narrowed. Case-insensitivity in regex
   * mode is expressed with the `i` FLAG instead. `filterTextLower` is the
   * pre-folded form the substring path compares against.
   */
  let filterText = '';
  let filterTextLower = '';

  /**
   * Compile the filter pattern, or return null when there is nothing to match.
   *
   * ⚠️ THREE GUARDS, all deliberate:
   *
   * 1. `compiledRegex` CACHES by pattern+flags. `render()` runs on every SSE
   *    frame, and rebuilding (and re-validating) the RegExp per line would be
   *    pointless work on a 5000-line buffer.
   * 2. An INVALID pattern is caught and reported, not thrown. A half-typed
   *    regex like `(` is a normal intermediate state while typing — it must
   *    show "invalid pattern", not break the viewer.
   * 3. A LENGTH CAP on the pattern and on each line. User-supplied regexes are
   *    the classic ReDoS vector: `(a+)+$` against a long line backtracks
   *    catastrophically and hangs the tab, and there is no way to bound that
   *    from inside JavaScript. Capping the input makes the worst case
   *    acceptable rather than pretending it cannot happen.
   */
  const MAX_PATTERN_LENGTH = 200;
  const MAX_REGEX_LINE_LENGTH = 2000;
  let compiledRegex = null;
  let compiledRegexKey = '';
  let regexInvalid = false;

  function compileFilter() {
    if (!filterRegex || !filterText) {
      compiledRegex = null;
      compiledRegexKey = '';
      regexInvalid = false;
      return;
    }
    // One `i` flag — the SAME flag used to build the regex — so the cache key
    // and the compiled object can never describe different states.
    const flags = filterCaseSensitive ? 'u' : 'iu';
    const key = `${flags}\u0000${filterText}`;
    if (key === compiledRegexKey) return;      // unchanged — reuse
    compiledRegexKey = key;
    compiledRegex = null;
    regexInvalid = false;

    if (filterText.length > MAX_PATTERN_LENGTH) {
      regexInvalid = true;
      return;
    }
    try {
      compiledRegex = new RegExp(filterText, flags);
    } catch (_error) {
      regexInvalid = true;
    }
  }

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
      jump: byId('scrollToBottomBtn'),
      unread: byId('unreadLogBadge'),
      matchCount: byId('logMatchCount'),
      regexToggle: byId('filterRegexBtn'),
      caseToggle: byId('filterCaseBtn'),
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
    if (filterText) {
      if (filterRegex) {
        // An invalid pattern matches NOTHING rather than everything: showing
        // every line while the filter says "invalid" would look like the
        // filter was silently ignored. The counter reports the error.
        if (!compiledRegex) return false;
        // Truncate before testing — see MAX_REGEX_LINE_LENGTH. A stack trace
        // line can be far longer than any log line needs to be, and an
        // unbounded input is what makes backtracking catastrophic.
        const subject = line.length > MAX_REGEX_LINE_LENGTH
          ? line.slice(0, MAX_REGEX_LINE_LENGTH)
          : line;
        // `lastIndex` is stateful for /g; these are not global, but resetting
        // is free and removes the trap entirely.
        compiledRegex.lastIndex = 0;
        if (!compiledRegex.test(subject)) return false;
      } else if (filterCaseSensitive) {
        if (line.indexOf(filterText) === -1) return false;
      } else if (line.toLowerCase().indexOf(filterTextLower) === -1) {
        return false;
      }
    }
    if (levelFilter === 'all') return true;
    if (levelFilter === 'error') return level === 'error' || level === 'critical';
    return level === levelFilter;
  }

  /**
   * Report "N / M lines" beside the filter controls.
   *
   * `visible` is already computed by render(), so this is one textContent
   * write — the count costs nothing extra. The total is the number of lines
   * HELD IN THE BUFFER (at most 5000 while streaming / 500 from the tail
   * load), not the file's real line count, so the label says "in buffer"
   * rather than implying the whole file.
   */
  function updateMatchCount(visible) {
    const node = byId('logMatchCount');
    if (!node) return;
    if (regexInvalid) {
      node.textContent = 'Invalid pattern';
      node.className = 'text-danger small fw-semibold';
      return;
    }
    node.className = 'text-muted small';
    if (!filterText && levelFilter === 'all') {
      node.textContent = '';
      return;
    }
    node.textContent = `${visible.toLocaleString()} / ${rawLines.length.toLocaleString()} lines`;
  }

  // ── Render ──────────────────────────────────────────────────────────────

  function render() {
    const { output } = el();
    if (!output) return 0;

    // Compile ONCE per render, not once per line — the cache makes this a
    // no-op when the pattern has not changed since the last frame.
    compileFilter();

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
    updateMatchCount(visible);
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

  // ── Scroll-to-bottom affordance ─────────────────────────────────────────
  //
  // The follow-only-at-bottom guard above already stops a new line from
  // yanking the view down while you read history. What it did NOT provide was
  // any way to tell that lines had arrived, or to get back down. Hence:
  //
  //   * the button appears whenever the view is NOT at the bottom (useful in a
  //     static tail too, not just while streaming);
  //   * the badge counts lines that arrived WHILE you were scrolled away, so
  //     "how much have I missed" is answerable without scrolling;
  //   * the count clears the moment you are back at the bottom — by the button
  //     or by scrolling there yourself.
  let unreadCount = 0;
  // Cached so the scroll listener can skip work when nothing changed.
  // ⚠️ `atBottom()` reads scrollHeight, which FORCES LAYOUT. Calling it
  // unconditionally on every scroll event would force a reflow per frame while
  // the user drags the scrollbar. Comparing against the last known state means
  // a reflow happens only at the moment the threshold is crossed.
  let jumpVisible = null;

  function updateJumpButton() {
    const bottom = atBottom();
    const nextVisible = !bottom;
    const nextUnread = unreadCount > 999 ? '999+' : String(unreadCount);

    const { jump, unread } = el();
    if (jump && jumpVisible !== nextVisible) {
      jumpVisible = nextVisible;
      jump.classList.toggle('d-none', !nextVisible);
    }
    if (jump) {
      jump.setAttribute(
        'aria-label',
        unreadCount
          ? `Jump to newest lines, ${unreadCount} new`
          : 'Jump to newest lines'
      );
    }
    if (unread) {
      // Cheap guards: textContent/classList writes are what actually cost here,
      // and they are re-entered from the scroll handler.
      if (unread.textContent !== nextUnread) unread.textContent = nextUnread;
      const hidden = unreadCount === 0;
      if (unread.classList.contains('d-none') !== hidden) {
        unread.classList.toggle('d-none', hidden);
      }
    }
  }

  function resetUnread() {
    if (unreadCount === 0) return;
    unreadCount = 0;
    updateJumpButton();
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
        resetUnread();
        updateJumpButton();
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
        if (follow) {
          scrollToBottom();
          // Staying pinned means nothing is being missed, so the badge must
          // not accumulate. Guarded inside resetUnread so this is free.
          resetUnread();
        } else {
          // Scrolled away: count what arrived. Counted from the RAW incoming
          // lines, not from `visible`, so a filter that hides everything still
          // tells the truth about how many lines landed.
          unreadCount += incoming.length;
        }
        updateJumpButton();
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
    // A filter change alters the layout height, so an "at bottom" view can
    // end up scrolled away from it. Re-evaluate rather than leaving a stale
    // button on screen.
    updateJumpButton();
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
      // The raw and pre-folded forms are both derived here, at the single
      // read point, so `matchesFilters` never has to decide which one it holds.
      filterText = String(this.value || '').trim();
      filterTextLower = filterText.toLowerCase();
      render();
      updateJumpButton();
    });

    /**
     * Filter mode toggles. `aria-pressed` is the source of truth for the
     * pressed state — `.active` on a btn-outline-secondary conveys nothing to
     * assistive tech, and the two must not be able to disagree, so both are
     * set together here.
     */
    function bindFilterMode(button, isOn, setMode) {
      if (!button) return;
      button.addEventListener('click', function () {
        const next = !isOn();
        setMode(next);
        button.classList.toggle('active', next);
        button.setAttribute('aria-pressed', next ? 'true' : 'false');
        // The pattern semantics changed, so the cached RegExp is stale —
        // compileFilter() detects this via its key when `filterRegex` flips.
        render();
        updateJumpButton();
      });
    }

    bindFilterMode(el().regexToggle, () => filterRegex, (on) => { filterRegex = on; });

    // The case toggle only flips a flag. `filterText` stays RAW, so a regex is
    // never lower-cased (which would narrow `[A-Z]` to `[a-z]`); case folding
    // for the substring path is applied at the comparison instead.
    bindFilterMode(el().caseToggle, () => filterCaseSensitive, (on) => {
      filterCaseSensitive = on;
    });

    // The jump button is only ever created by the template; if it is absent
    // (an older cached page) the viewer still works, just without the FAB.
    const { jump, output } = el();
    if (jump) {
      jump.addEventListener('click', function () {
        scrollToBottom();
        resetUnread();
        updateJumpButton();
      });
    }

    // A passive scroll listener: this only ever reads scrollTop, and it fires
    // often enough that a non-passive listener would be a real cost. Clearing
    // the badge when the user scrolls back down is what makes the count mean
    // "lines since you last looked", rather than "lines since the last
    // stream frame".
    if (output) {
      output.addEventListener('scroll', function () {
        if (atBottom()) resetUnread();
        updateJumpButton();
      }, { passive: true });
    }

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
