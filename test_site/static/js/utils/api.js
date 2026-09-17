/* ==========================================================================
   static/js/utils/api.js
   Shared JSON fetch helpers.

   Load AFTER utils/dom.js and BEFORE any page script:

       <script src="{{ url_for('static', filename='js/utils/dom.js') }}"></script>
       <script src="{{ url_for('static', filename='js/utils/api.js') }}"></script>

   ── WHY THIS FILE EXISTS ──────────────────────────────────────────────────
   Five separate implementations of "fetch some JSON and complain usefully if
   it isn't JSON" exist in the codebase, none of them equivalent:

     downloads.js        fetchJsonOrThrow()   timeout + AbortSignal merge +
                                              524/504/502 handling + HTML
                                              detection. The most complete.
     playlists_index.js  parseJsonOrThrow()   HTML detection, no timeout.
     dashboard.js        postJSON()           no error handling at all —
                                              `.catch(() => ({}))` swallows
                                              every failure into an empty
                                              object, so a 500 is
                                              indistinguishable from success.
     artist_detail.html  parseJsonResponse()  content-type check, defined
                                              INSIDE searchMusicBrainzRelease
                                              so nothing else can use it.
     artist_detail.html  loadArtistCoveredBy  a sixth, inline copy of the
                                              same HTML-vs-JSON logic.

   Beyond the duplication, the split causes real inconsistency: config.js,
   monitor.js and others repeat the bare
   `if (!contentType.includes('application/json')) throw` check inline —
   the literal string "Server returned non-JSON response" appears in eight
   places with three different wordings.

   This module takes downloads.js's version as the base (it is the only one
   that handles timeouts and gateway errors) and adds the missing pieces.
   ========================================================================== */

(function (global) {
  'use strict';

  /** Default request timeout. Matches downloads.js's previous default. */
  const DEFAULT_TIMEOUT_MS = 30000;

  /**
   * Gateway/proxy statuses that mean "upstream took too long", not "the
   * request was malformed". Worth a distinct message because the user's
   * action may actually still be running server-side.
   */
  const GATEWAY_TIMEOUT_STATUSES = [502, 504, 524];

  /**
   * Fetch a URL and return parsed JSON, throwing a useful Error otherwise.
   *
   * Handles, in order:
   *   - caller-supplied AbortSignal, merged with the internal timeout signal
   *   - request timeout (rejects with "Request timed out after Ns")
   *   - 502/504/524 gateway timeouts (distinct message)
   *   - HTML responses (auth redirect / error page) vs other non-JSON
   *   - non-2xx with a JSON `error` field (uses it as the message)
   *
   * @param {string} url
   * @param {RequestInit} [options]
   * @param {number} [timeoutMs=30000]
   * @returns {Promise<Object>}
   */
  async function fetchJson(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const controller = new AbortController();
    const externalSignal = options.signal || null;
    const merged = Object.assign({}, options);
    const onExternalAbort = () => controller.abort();

    // AbortSignal.any is not available everywhere yet; fall back to
    // forwarding the external abort onto our own controller.
    if (externalSignal && typeof AbortSignal.any === 'function') {
      merged.signal = AbortSignal.any([controller.signal, externalSignal]);
    } else if (externalSignal) {
      externalSignal.addEventListener('abort', onExternalAbort, { once: true });
      merged.signal = controller.signal;
    } else {
      merged.signal = controller.signal;
    }

    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    let response;
    let raw;
    try {
      response = await fetch(url, merged);
      raw = await response.text();
    } catch (error) {
      if (error && error.name === 'AbortError') {
        // Distinguish "the caller cancelled" from "we timed out" — the
        // caller's own abort must propagate as-is so callers can ignore it.
        if (externalSignal && externalSignal.aborted) throw error;
        // Sub-second timeouts must not render as "timed out after 0s".
        const secs = timeoutMs < 1000
          ? `${timeoutMs}ms`
          : `${Math.round(timeoutMs / 1000)}s`;
        throw new Error(`Request timed out after ${secs}`);
      }
      throw error;
    } finally {
      clearTimeout(timeoutId);
      if (externalSignal && typeof AbortSignal.any !== 'function') {
        externalSignal.removeEventListener('abort', onExternalAbort);
      }
    }

    if (GATEWAY_TIMEOUT_STATUSES.includes(response.status)) {
      throw new Error(
        `Connection timed out (HTTP ${response.status}). The server took too long to respond.`
      );
    }

    let data;
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_parseError) {
      const contentType = (response.headers.get('content-type') || '').toLowerCase();
      const trimmed = (raw || '').trim();
      const looksLikeHtml =
        contentType.includes('text/html') ||
        trimmed.startsWith('<!DOCTYPE') ||
        trimmed.startsWith('<html') ||
        trimmed.startsWith('<');

      if (looksLikeHtml) {
        throw new Error(
          `Server returned HTML instead of JSON (HTTP ${response.status}). ` +
          'This usually means an auth/session redirect or a server error page.'
        );
      }
      throw new Error(`Server returned non-JSON response (HTTP ${response.status}).`);
    }

    if (!response.ok) {
      const serverMsg = (data && data.error) || response.statusText;
      throw new Error(`HTTP ${response.status}: ${serverMsg || 'Request failed'}`);
    }

    return data;
  }

  /**
   * GET JSON.
   *
   * @param {string} url
   * @param {Object} [opts]
   * @param {number} [opts.timeoutMs]
   * @param {AbortSignal} [opts.signal]
   * @returns {Promise<Object>}
   */
  function getJson(url, opts = {}) {
    return fetchJson(
      url,
      { method: 'GET', signal: opts.signal },
      opts.timeoutMs || DEFAULT_TIMEOUT_MS
    );
  }

  /**
   * POST a JSON body and return parsed JSON.
   *
   * Replaces dashboard.js's `postJSON`, which had NO error handling: its
   * `.json().catch(() => ({}))` turned every failure — a 500, an HTML login
   * page, a network drop — into an empty object, so callers like
   * startPopularityScan() reported success regardless of what happened.
   * This version throws, so callers must handle failure explicitly.
   *
   * @param {string} url
   * @param {Object} [body]
   * @param {Object} [opts]
   * @returns {Promise<Object>}
   */
  function postJson(url, body = {}, opts = {}) {
    return fetchJson(
      url,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: opts.signal,
      },
      opts.timeoutMs || DEFAULT_TIMEOUT_MS
    );
  }

  /**
   * DELETE, returning parsed JSON.
   *
   * @param {string} url
   * @param {Object} [opts]
   * @returns {Promise<Object>}
   */
  function deleteJson(url, opts = {}) {
    return fetchJson(
      url,
      { method: 'DELETE', signal: opts.signal },
      opts.timeoutMs || DEFAULT_TIMEOUT_MS
    );
  }

  /**
   * Parse an ALREADY-FETCHED Response as JSON with the same error handling.
   *
   * For the handful of call sites that need the Response object itself
   * (to read headers, or because they use FormData and build their own
   * request). Replaces playlists_index.js's `parseJsonOrThrow` and the two
   * inline `parseJsonResponse` copies in artist_detail.html.
   *
   * @param {Response} response
   * @param {string} [sourceName] label used in the error message
   * @returns {Promise<Object>}
   */
  async function parseJsonResponse(response, sourceName) {
    const label = sourceName ? `${sourceName} ` : '';
    const raw = await response.text();
    let data;

    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_parseError) {
      const contentType = (response.headers.get('content-type') || '').toLowerCase();
      const trimmed = (raw || '').trim();
      if (
        contentType.includes('text/html') ||
        trimmed.startsWith('<!DOCTYPE') ||
        trimmed.startsWith('<html') ||
        trimmed.startsWith('<')
      ) {
        throw new Error(
          `${label}returned HTML instead of JSON (HTTP ${response.status}). ` +
          'This usually means an auth/session redirect or a server error page.'
        );
      }
      throw new Error(`${label}returned a non-JSON response (HTTP ${response.status}).`);
    }

    if (!response.ok) {
      throw new Error(
        data.error || data.message || `${label}request failed (HTTP ${response.status})`
      );
    }
    return data;
  }

  /**
   * Like fetchJson, but resolves to `fallback` instead of throwing.
   *
   * For genuinely optional calls only — the Discogs fallback in the
   * MusicBrainz search flow, and the `.catch(() => ({}))` polls in
   * dashboard.js that deliberately tolerate a missing endpoint.
   * Do NOT use this to paper over errors the user should see.
   *
   * @param {string} url
   * @param {RequestInit} [options]
   * @param {*} [fallback={}]
   * @param {number} [timeoutMs]
   * @returns {Promise<*>}
   */
  async function fetchJsonOrDefault(url, options = {}, fallback = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
    try {
      return await fetchJson(url, options, timeoutMs);
    } catch (error) {
      console.warn(`[api] ${url} failed, using fallback:`, error.message);
      return fallback;
    }
  }

  const api = {
    fetchJson,
    getJson,
    postJson,
    deleteJson,
    parseJsonResponse,
    fetchJsonOrDefault,
    DEFAULT_TIMEOUT_MS,
  };

  global.api = api;

  // Back-compat aliases so existing call sites keep working while the
  // migration happens. Delete these once every caller uses `api.*`:
  //   downloads.js / monitor.js / playlist_import.js  -> fetchJsonOrThrow
  //   playlists_index.js                              -> parseJsonOrThrow
  //   dashboard.js                                    -> postJSON
  global.fetchJsonOrThrow = fetchJson;
  global.parseJsonOrThrow = parseJsonResponse;
  global.postJSON = postJson;
})(window);
