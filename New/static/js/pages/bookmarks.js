/* ==========================================================================
   static/js/pages/bookmarks.js
   Favourites page — remove a saved artist / album / track bookmark.

   Load order: utils/dom.js → utils/api.js → ui/toast.js → ui/confirm.js
   Then this file. All four come from base.html.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/bookmarks.html carried a 41-line inline <script> with TWO
   functions, plus its own toast markup:

     showToast(title, message, type)   → DELETED. This was a third toast
        implementation in the codebase (after the ones in main.js and
        downloads.js), building its own `#bookmarkToast` element with hand-set
        bg-success / bg-danger / bg-warning classes. ui/toast.js is loaded on
        every page and already does exactly this, including the
        `{title, message, type}` shape. The #bookmarkToast markup is gone too.

     removeBookmark(bookmarkId)        → this file, as removeBookmark(button).

   ── FIXES ─────────────────────────────────────────────────────────────────
   1. `confirm()` → `await ui.confirm()`, and the message now names the item
      being removed instead of the generic "this bookmark".
   2. `fetch(...).then(r => r.json())` → `api.deleteJson()`. The old chain
      called `.json()` unconditionally, so a session-expiry HTML response threw
      "Unexpected token '<'" and the `.catch` reported it as a *network* error —
      the most misleading possible message for an auth redirect.
   3. Inline `onclick` carrying an interpolated id → one delegated listener
      keyed on `data-action`. A JavaScript id cannot contain a quote, so this
      was not an escaping bug; it is consistency (and it lets the button be
      disabled while the request is in flight).
   4. The template rendered `<h4 ...>` closed by `</h1>` — fixed there.
   ========================================================================== */

(function (global) {
  'use strict';

  const DELETE_ENDPOINT = '/api/bookmarks/';

  function removeBookmark(button) {
    const bookmarkId = button && button.dataset ? button.dataset.bookmarkId : '';
    if (!bookmarkId) return;

    // The row's own label is passed as detail so the confirmation is specific.
    const label = (button.dataset.bookmarkLabel || '').trim();

    return (async () => {
      const accepted = await global.ui.confirm({
        title: 'Remove bookmark',
        message: label ? `Remove “${label}” from your favourites?` : 'Remove this bookmark?',
        tone: 'danger',
        confirmLabel: 'Remove',
      });
      if (!accepted) return;

      return global.buttonState.withBusy(button, '', async () => {
        try {
          const data = await global.api.deleteJson(
            DELETE_ENDPOINT + encodeURIComponent(bookmarkId)
          );
          if (!data || data.success === false) {
            global.toast.error((data && data.error) || 'Failed to remove bookmark');
            return;
          }
          global.toast.success('Bookmark removed');
          // The list is server-rendered, so a reload is what re-flows the
          // sections — a removed artist may be the last one in its card.
          setTimeout(() => global.location.reload(), 500);
        } catch (error) {
          global.toast.error('Could not remove bookmark: ' + error.message);
        }
      });
    })();
  }

  // Delegated: the buttons are rendered per bookmark server-side, and the page
  // re-renders on reload, so binding once here avoids re-binding per row.
  document.addEventListener('click', function (event) {
    const btn = event.target.closest ? event.target.closest('[data-action="bookmark-remove"]') : null;
    if (!btn) return;
    event.preventDefault();
    removeBookmark(btn);
  });

  global.removeBookmark = removeBookmark;
})(window);
