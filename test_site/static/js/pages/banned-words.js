/* ==========================================================================
   static/js/pages/banned-words.js
   Banned Soulseek search words — list, suggestions, ban/unban/dismiss.

   Load order: utils/dom.js → utils/api.js → ui/toast.js → ui/confirm.js
   Then this file. All come from base.html.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/banned_words.html carried a 79-line inline <script> with six
   functions and its own escapeHtml. The script is this file; the local
   escapeHtml is gone in favour of utils/dom.js's (which is equivalent here —
   the local one did escape quotes, unusually for this codebase).

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. DUPLICATE EVENT LISTENERS, ONE SET PER RELOAD. `loadWords()` ended with:

          document.getElementById('bannedWordsList').addEventListener('click', handleWordAction);
          document.getElementById('suggestedWordsList').addEventListener('click', handleWordAction);

      and `loadWords()` is called after EVERY action (ban, unban, dismiss,
      add). The lists are re-rendered with innerHTML but the CONTAINER elements
      are never replaced, so each call stacked another identical listener on the
      same node. After N actions a single click fired handleWordAction N times,
      i.e. N concurrent removeWord/banSuggested requests for one button press.
      The delegated listener is now bound once, on document, so re-rendering
      cannot accumulate it.

   2. `alert()` everywhere → toast. `confirm()` → `await ui.confirm()`.

   3. Raw `fetch` + `.json()` with no status check → api.getJson/postJson/
      deleteJson, which distinguish an HTML error page from a JSON error and
      surface a real message.

   4. `escapeHtml` WAS BEING USED IN AN ATTRIBUTE — correctly, as it happened
      (`data-word="${escapeHtml(w.word)}"`), because the local helper escaped
      quotes. Now uses the shared one, which also does, so nothing regresses.

   5. Errors were only `console.error`'d on the initial load, so a failed fetch
      left both cards showing "Loading…" forever. They now report the failure.
   ========================================================================== */

(function (global) {
  'use strict';

  const ENDPOINT = '/api/slsk/banned-words';
  const DISMISS_ALL_ENDPOINT = '/api/slsk/banned-words/dismiss-all';
  /** A word is "suggested" once this many searches returned nothing. */
  const SUGGESTION_THRESHOLD = 3;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function setCount(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function setLoading(id) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '<div class="text-muted text-center py-3">Loading…</div>';
  }

  /** A banned word chip: unban button. */
  function bannedChip(word) {
    return `<span class="badge bg-danger d-flex align-items-center gap-1" style="font-size:0.9rem; padding: 0.4em 0.7em;">` +
      `<span>${esc(word.word)}</span>` +
      `<small class="opacity-75">(${esc(String(word.zero_result_count))} searches)</small>` +
      `<button type="button" class="btn-close btn-close-white ms-1" style="font-size:0.6rem;" ` +
      `data-word="${esc(word.word)}" data-action="remove" title="Unban" aria-label="Unban ${esc(word.word)}"></button>` +
      `</span>`;
  }

  /** A suggested word chip: ban / dismiss. */
  function suggestedChip(word) {
    return `<span class="badge bg-warning text-dark d-flex align-items-center gap-1" style="font-size:0.9rem; padding: 0.4em 0.7em;">` +
      `<span>${esc(word.word)}</span>` +
      `<small class="opacity-75">(${esc(String(word.zero_result_count))} searches)</small>` +
      `<button type="button" class="btn btn-sm btn-danger py-0 px-1 ms-1" style="font-size:0.65rem;" ` +
      `data-word="${esc(word.word)}" data-action="ban">Ban</button>` +
      `<button type="button" class="btn btn-sm btn-secondary py-0 px-1" style="font-size:0.65rem;" ` +
      `data-word="${esc(word.word)}" data-action="remove">Dismiss</button>` +
      `</span>`;
  }

  function renderChips(containerId, items, chipFn, emptyMessage) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = items.length
      ? '<div class="d-flex flex-wrap gap-2">' + items.map(chipFn).join('') + '</div>'
      : `<div class="text-muted text-center py-2">${esc(emptyMessage)}</div>`;
  }

  async function loadWords() {
    setLoading('bannedWordsList');
    setLoading('suggestedWordsList');

    try {
      const data = await global.api.getJson(ENDPOINT);
      if (!data.success) {
        throw new Error(data.error || 'Could not load banned words');
      }

      const words = data.words || [];
      const banned = words.filter((w) => w.is_banned);
      const suggested = words.filter(
        (w) => !w.is_banned && Number(w.zero_result_count) >= SUGGESTION_THRESHOLD
      );

      setCount('bannedCount', banned.length);
      setCount('suggestedCount', suggested.length);

      renderChips('bannedWordsList', banned, bannedChip, 'No banned words yet.');
      renderChips('suggestedWordsList', suggested, suggestedChip, 'No suggestions yet.');
    } catch (error) {
      const message = `Could not load: ${esc(error.message)}`;
      ['bannedWordsList', 'suggestedWordsList'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = `<div class="text-danger text-center py-2">${message}</div>`;
      });
      console.error('[banned-words] load failed:', error);
    }
  }

  /** Ban a word (used both by the manual form and the suggestion chips). */
  async function banWord(word) {
    if (!word) return;
    try {
      const data = await global.api.postJson(ENDPOINT, { word, is_banned: true });
      if (!data.success) {
        global.toast.error(data.error || 'Could not ban that word');
        return;
      }
      await loadWords();
    } catch (error) {
      global.toast.error('Could not ban that word: ' + error.message);
    }
  }

  async function removeWord(word) {
    if (!word) return;
    try {
      const data = await global.api.deleteJson(`${ENDPOINT}/${encodeURIComponent(word)}`);
      if (!data.success) {
        global.toast.error(data.error || 'Could not unban that word');
        return;
      }
      await loadWords();
    } catch (error) {
      global.toast.error('Could not unban that word: ' + error.message);
    }
  }

  async function addBannedWord() {
    const input = document.getElementById('newWordInput');
    if (!input) return;

    const word = String(input.value || '').trim().toLowerCase();
    if (!word) {
      global.toast.warning('Please enter a word.');
      input.focus();
      return;
    }

    await banWord(word);
    input.value = '';
    input.focus();
  }

  async function dismissAllSuggested() {
    const accepted = await global.ui.confirm({
      title: 'Dismiss suggestions',
      message: 'Dismiss all suggested words?',
      detail: 'They will stop being suggested. Words are not banned by this.',
      tone: 'danger',
      confirmLabel: 'Dismiss all',
    });
    if (!accepted) return;

    try {
      const data = await global.api.postJson(DISMISS_ALL_ENDPOINT, {});
      if (!data.success) {
        global.toast.error(data.error || 'Could not dismiss suggestions');
        return;
      }
      await loadWords();
    } catch (error) {
      global.toast.error('Could not dismiss suggestions: ' + error.message);
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────
  //
  // Bound ONCE on document, not per render — see bug 1 in the header.

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;

    const actionBtn = event.target.closest('[data-action]');
    if (actionBtn && actionBtn.dataset.word) {
      const action = actionBtn.dataset.action;
      if (action === 'remove') {
        event.preventDefault();
        removeWord(actionBtn.dataset.word);
      } else if (action === 'ban') {
        event.preventDefault();
        banWord(actionBtn.dataset.word);
      }
      return;
    }

    const addBtn = event.target.closest('#addBannedWordBtn, [data-action="banned-add"]');
    if (addBtn) {
      event.preventDefault();
      addBannedWord();
      return;
    }

    const dismissBtn = event.target.closest('[data-action="banned-dismiss-all"]');
    if (dismissBtn) {
      event.preventDefault();
      dismissAllSuggested();
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    const input = document.getElementById('newWordInput');
    if (input) {
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        addBannedWord();
      });
    }
    loadWords();
  });

  global.loadBannedWords = loadWords;
  global.addBannedWord = addBannedWord;
  global.dismissAllSuggested = dismissAllSuggested;
})(window);
