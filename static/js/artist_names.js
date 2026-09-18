/**
 * Artist name filing — the client twin of helpers/artist_sort.py.
 *
 * A record-shop convention: "The Offspring" is filed as "Offspring, The" so it
 * sorts among the O's instead of being buried in a wall of T's.
 *
 *   artistNames.sortKey('The Offspring')     -> 'offspring, the'
 *   artistNames.sortName('The Offspring')    -> 'Offspring, The'
 *   artistNames.sortLetter('The Offspring')  -> 'O'
 *   ['The Cure','Radiohead'].sort(artistNames.compare)
 *
 * ⚠️ DISPLAY ONLY. sortName() is for text a user reads. Never feed its result to
 * an href, an /api/... query string, or a write — the artist's identity is the
 * RAW name. The server's /artists sections are built from the same rules, so
 * these functions must produce the same key or the list and its letter tiles
 * would disagree.
 *
 * ── THIS FILE IS THE LIVE-TREE COPY ───────────────────────────────────────
 * test_site/static/js/utils/artist-names.js is the rebuilt-tree copy and is the
 * one that survives the cutover. While both trees exist the two must stay
 * byte-identical in behaviour — change one, change the other. The live copy is
 * loaded by templates/base.html; delete it (and its <script> tag) once the
 * rebuilt UI is live.
 *
 * ── WHAT THIS DELIBERATELY DOES NOT DO ────────────────────────────────────
 * * Only "The" — not A/An (far more often part of the real title) and not
 *   foreign equivalents (Les/Los/Die). Keep ARTICLES in step with ARTICLES in
 *   helpers/artist_sort.py.
 * * A literal SPACE, not \s. The server matches with SQL `LIKE 'the %'`, which
 *   can only express a space, so a tab is not a separator there either. Using
 *   \s here would file the same name into a different section on each side.
 * * Ordinal string comparison in compare(), not localeCompare(). The server
 *   sorts with Python's `<` on the same lowercase key; localeCompare would
 *   disagree on punctuation and accents and put the client list in a different
 *   order than the identical server-rendered one.
 * * toLowerCase() rather than a full Unicode case-fold. Identical to Python's
 *   casefold() for ASCII artist names, which is the whole realistic range.
 *
 * See also: helpers/artist_sort.py, helpers/template_filters.py
 *           (`artist_sort_name`), routes/ui_routes.py::artists().
 */
(function (global) {
  'use strict';

  /** Articles moved to the end. Mirrors ARTICLES in helpers/artist_sort.py. */
  var ARTICLES = ['The'];

  /**
   * `the<space><rest>` — the space is what stops "Theatre"/"Theodore" matching.
   * `[\s\S]` stands in for Python's re.DOTALL so a name containing a newline is
   * still matched as a whole.
   */
  var LEADING_ARTICLE = /^the +([\s\S]+)$/i;

  /** Split a name into { core, article }; article is '' when there is none. */
  function split(name) {
    if (name === null || name === undefined) return { core: '', article: '' };
    var text = String(name).trim();
    if (!text) return { core: '', article: '' };

    var match = text.match(LEADING_ARTICLE);
    if (!match) return { core: text, article: '' };

    var matched = match[1] || '';
    var core = matched.trim();
    // e.g. "The  " — the article is the whole name, so moving it would produce
    // the nonsense ", The". Leave it alone.
    if (!core) return { core: text, article: '' };

    return { core: core, article: text.slice(0, text.length - matched.length).trim() };
  }

  /** Lower-cased alphabetical key: 'The Offspring' -> 'offspring, the'. */
  function sortKey(name) {
    var parts = split(name);
    if (!parts.article) return parts.core.toLowerCase();
    return parts.core.toLowerCase() + ', ' + parts.article.toLowerCase();
  }

  /** Display label: 'The Offspring' -> 'Offspring, The'. DISPLAY ONLY. */
  function sortName(name) {
    var parts = split(name);
    if (!parts.article) return parts.core;
    return parts.core + ', ' + parts.article;
  }

  /** Section letter: 'O' for 'The Offspring', '#' when not A-Z. */
  function sortLetter(name) {
    var parts = split(name);
    if (!parts.core) return '#';
    var first = parts.core.charAt(0).toUpperCase();
    return (first >= 'A' && first <= 'Z') ? first : '#';
  }

  /** Array.sort comparator over sortKey — matches the server's ordering. */
  function compare(a, b) {
    var ka = sortKey(a);
    var kb = sortKey(b);
    if (ka < kb) return -1;
    if (ka > kb) return 1;
    return 0;
  }

  global.artistNames = {
    ARTICLES: ARTICLES,
    split: split,
    sortKey: sortKey,
    sortName: sortName,
    sortLetter: sortLetter,
    compare: compare
  };
})(window);
