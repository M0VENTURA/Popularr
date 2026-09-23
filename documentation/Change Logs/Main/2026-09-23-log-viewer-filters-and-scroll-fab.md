# Log viewer: scroll-to-bottom FAB, filter modes, match counter, line gutter

**Date:** 2026-09-23
**Area:** `test_site/static/js/pages/logs.js`,
`test_site/templates/Pages/logs.html`, `test_site/static/css/logs.css`

**Scope: `test_site` only**, as requested.

## What was already correct (and therefore not changed)

The source evaluation proposed fixing a live-streaming bug that does not exist.
It claimed new SSE lines "force the view back down or cause jitter" while you
read history. Both trees already implement follow-only-at-bottom, and the
stream captures the decision **before** mutating the DOM:

```js
// ⚠️ Capture the follow decision BEFORE the new lines change the
// scroll height, or the check always reads as "not at bottom".
const follow = atBottom();
...
if (follow) scrollToBottom();
```

`atBottom()` uses a 40px slack, and the file's header lists the behaviour under
**"WHAT DELIBERATELY STAYS."** The live tree has the same guard. Nothing here
needed fixing, and "fixing" it would have been the regression.

Also already present, contrary to the report: the toolbar is partly responsive
(Refresh's label is `d-none d-sm-inline`; the level-filter icons are
`d-none d-md-inline`; Download/Copy/Clear are **already icon-only**), and the log
container already has the `flex-grow-1` + `min-height: 0` the report asked for.
The report also cited `main.js`, which has **nothing** to do with logs.

⚠️ The report's own FAB snippet was malformed — an unclosed quote in
`class="badge bg-danger rounded-pill id="unreadLogBadge"` — so the badge would
never have resolved.

## What was added

### 1. Scroll-to-bottom FAB with unread counter

The real gap was not auto-scroll but the absence of any way to tell that lines
had arrived, or to get back down.

* The button appears whenever the view is **not** at the bottom — useful in a
  static tail too, not only while streaming.
* The badge counts lines that arrived **while you were scrolled away**, so "how
  much have I missed" is answerable without scrolling.
* It clears the moment you are back at the bottom — via the button or by
  scrolling there yourself — so the number means *"since you last looked"*
  rather than *"since the last frame"*.
* It counts **raw arrivals** (`incoming.length`), never `render()`'s return
  value: a filter that hides everything must still report that lines landed.
* `updateJumpButton()` caches the last known state and skips work when nothing
  changed. ⚠️ `atBottom()` reads `scrollHeight`, which **forces layout** —
  calling it unconditionally on every scroll event would reflow once per frame
  while dragging the scrollbar.
* The scroll listener is `{ passive: true }`.

### 2. Regex and case toggles, plus a match counter

Both modes default **OFF**, so an untouched page behaves exactly as before.

* ⚠️ **The pattern is NOT lower-cased.** Case-insensitivity in regex mode is the
  `i` **flag**. The first draft lower-cased `filterText` at the input handler,
  which would have silently narrowed `[A-Z]{3}` to `[a-z]{3}` — matching nothing
  while appearing to work. `filterText` stays raw; `filterTextLower` is a
  separate pre-folded copy used only by the substring path.
* ⚠️ **ReDoS is guarded, not hand-waved.** User-supplied regexes are the classic
  vector (`(a+)+$` backtracks catastrophically and hangs the tab, and that cannot
  be bounded from inside JavaScript). So: a `MAX_PATTERN_LENGTH` cap on the
  pattern, and a `MAX_REGEX_LINE_LENGTH` cap on each subject line.
* An **invalid** pattern is caught, reported as `Invalid pattern`, and matches
  **nothing** — not everything. Showing every line while the filter says
  "invalid" reads as though the filter were ignored.
* The compiled `RegExp` is cached, keyed on **pattern + flags**. `render()` runs
  on every SSE frame, so rebuilding per line would be wasted work; including the
  flags in the key is what stops a case toggle reusing a stale regex.
* The counter shows `visible / total` and stays blank when no filter is active
  (a `500 / 500` label is noise). `visible` was already computed by `render()`,
  so the count costs one `textContent` write.

⚠️ The counter reports **visible / total**, and total is the buffer size (≤5000
while streaming), not the file's real line count — so the label does not imply
the whole file.

### 3. Line-number gutter, zebra striping, hover

CSS-only. The counter is scoped to `#logOutput` (an **id** — the first draft
targeted `.log-output`, a class that does not exist anywhere).

* ⚠️ Numbers are the index **within the rendered view**, not the file position.
  `render()` filters and re-renders, so file positions would read as
  `412, 913, 1502` down a filtered list — indistinguishable from a bug.
* The gutter is `user-select: none` and `pointer-events: none`, so a line number
  can never land in a copied log or intercept a click.
* Zebra uses the `odd` keyword rather than `:nth-child`, and the level-tint rules
  come **after** the striping rule on purpose: both set `background-color`, so
  source order decides, and the error rows are the ones that must stay visible.

## Tests

* `tests/test_log_viewer_filters_and_jump.py` (53) — source contracts the probe
  cannot cover: that every `byId()` in the module resolves to a template id,
  that the FAB is **outside** the scrolling `<pre>`, the two properties got wrong
  on the first attempt (raw pattern; visible/total distinct), the ReDoS guards,
  the ARIA wiring, and the CSS source-order rule.
* `tests/js/logs-viewer-probe.js` (25 checks) — extracts the **real** functions
  from `logs.js` by splicing into the IIFE, so it cannot drift from shipped code.
* `tests/js/mutate-logs-viewer.js` — mutation-tests that probe.

⚠️ **Mutation testing caught two real gaps, both in my own work:**

1. The counter mutation **escaped**, because the probe asserted `7 / 7` — with
   visible == total, a counter that ignores the filter is indistinguishable from
   a correct one. The probe now uses `7 / 40`.
2. Two mutation anchors silently failed to match (escaped `\n` in a JS string
   literal treated as backslash-n rather than a newline), which would have
   reported "anchor not found" as a pass. Anchors are now literal newlines.

Final state: all 5 mutations caught.

**Regression sweep:** 8 suites / 592 tests, pre-existing failing set
**IDENTICAL** (1: `test_static_js_is_not_jinja[static\js\artist_detail.js]`,
a known pre-existing failure) — 0 regressions.

## ⚠️ Not done — the live tree's viewer is unstyled

While verifying, a real defect the report does not mention was confirmed:
**`templates/pages/logs.html` emits `.log-line` / `.log-level-*` / `.log-ts` /
`.log-tag` but has no stylesheet and no inline `<style>`**, and there is no
`static/css/logs.css`. The live log viewer therefore renders as flat monochrome
text with no level colour-coding.

This is precisely the bug `test_site/static/js/pages/logs.js` documents having
fixed, and `test_site/static/css/logs.css` is the fix. Since the live tree
(`templates/` + `static/`) is the **default** serving mode, the unstyled viewer
is the one most likely to be seen.

Left alone because the instruction for this change was `test_site` only, and
mirroring it needs a decision on the live tree's inline-IIFE structure. It is a
one-file port (`logs.css` + a `<link>` in the live template) when wanted.
