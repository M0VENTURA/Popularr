/*
  Help / documentation viewer.

  Extracted from the inline <script> in templates/pages/help.html.

  Behaviour is unchanged; it is now a module rather than a page-scoped script so
  it is cached, linted and testable like the rest of the tree. The three
  features are all local to this page, so nothing is exported:

    * filter the "All Documents" list as you type in the search box
    * smooth-scroll same-page anchor links instead of jumping
    * expand/collapse the mobile docs bar (collapsed by default so the document
      is visible immediately after navigation)

  Every lookup is guarded: this module is loaded from the shared shell, so it
  must not throw on a page where the help markup is absent.
*/
(function (global) {
  'use strict';

  function initSearchFilter() {
    var input = document.getElementById('docSearch');
    if (!input) return;

    input.addEventListener('input', function (event) {
      var searchTerm = String(event.target.value || '').toLowerCase();
      var docItems = document.querySelectorAll('.doc-item');

      docItems.forEach(function (item) {
        var docName = item.getAttribute('data-doc-name') || '';
        var listItem = item.parentElement;
        if (!listItem) return;
        listItem.style.display = docName.includes(searchTerm) ? '' : 'none';
      });
    });
  }

  function initSmoothScroll() {
    var anchors = document.querySelectorAll('.help-content a[href^="#"]');
    anchors.forEach(function (anchor) {
      anchor.addEventListener('click', function (event) {
        event.preventDefault();
        var href = anchor.getAttribute('href');
        if (!href || href === '#') return;
        var target = document.querySelector(href);
        if (target) {
          target.scrollIntoView({ behavior: 'smooth' });
        }
      });
    });
  }

  function initMobileToggle() {
    var sidebar = document.getElementById('helpSidebar');
    var toggle = document.getElementById('helpMobileToggle');
    if (!sidebar || !toggle) return;

    toggle.addEventListener('click', function () {
      var open = sidebar.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  function init() {
    initSearchFilter();
    initSmoothScroll();
    initMobileToggle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
