/**
 * Shared source-extraction helper for the behavioural probes.
 *
 * WHY THIS IS SHARED. Both probes brace-match a named function out of the
 * SHIPPED source so the code under test cannot drift from the code they drive.
 * They each used to carry their own copy of this walker, and the copies were
 * subtly wrong in the same way — see the comment handling below. One copy means
 * one place to fix, and no third copy to inherit the bug.
 *
 * NOT A PARSER. It handles the constructs these files actually use: strings in
 * all three quotes, template literals, line comments and block comments. It does
 * NOT understand REGEX LITERALS, so a `{` inside `/…/ ` would miscount. If a
 * probe starts failing with "unbalanced braces" on a function that looks fine,
 * that is the first thing to check.
 */
'use strict';

/**
 * Brace-match `function <name>(...) { ... }`, including `async` / export
 * modifiers.
 *
 * ⚠️ COMMENTS ARE SKIPPED BEFORE QUOTES ARE TRACKED, and that ordering is the
 * whole point. An apostrophe in prose — "the flyout's own input" — otherwise
 * opens a phantom string literal, so the walker swallows the rest of the file
 * and throws "unbalanced braces". The function is perfectly valid; the walker
 * was reading English as JavaScript.
 */
function extractFn(source, name) {
  const anchor = 'function ' + name + '(';
  const start = source.indexOf(anchor);
  if (start < 0) return null;

  let begin = start;
  for (const modifier of ['export default ', 'export ', 'async ']) {
    if (source.startsWith(modifier, start - modifier.length)) {
      begin = start - modifier.length;
      break;
    }
  }

  let depth = 0;
  let inStr = null;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    const ch = source[i];
    const next = source[i + 1];

    if (inStr) {
      if (ch === '\\') { i += 1; continue; }   // skip the escaped character
      if (ch === inStr) inStr = null;
      continue;
    }

    if (ch === '/' && next === '/') {
      const nl = source.indexOf('\n', i);
      i = nl < 0 ? source.length : nl;
      continue;
    }
    if (ch === '/' && next === '*') {
      const end = source.indexOf('*/', i + 2);
      i = end < 0 ? source.length : end + 1;
      continue;
    }

    if (ch === '"' || ch === "'" || ch === '`') { inStr = ch; continue; }
    if (ch === '{') {
      depth += 1;
    } else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(begin, i + 1);
    }
  }
  throw new Error('unbalanced braces extracting ' + name);
}

module.exports = { extractFn };
