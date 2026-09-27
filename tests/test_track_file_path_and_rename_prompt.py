"""The file path is shown per track, and renaming it asks first.

WHAT THIS COVERS
----------------
The test_site album and track pages now display each track's on-disk file path,
and a Rename button sits beside Edit and Delete on every album row.

⚠️ WHY THE PROMPT IS THE POINT, NOT A DETAIL
--------------------------------------------
A rename MOVES A FILE. The track page's rename handler already existed and
renamed the moment its menu item was clicked — **with no confirmation at all** —
so a stray click relocated a file on disk. (The album-level rename has always
prompted, which is what made the omission a bug rather than a policy.) Every
rename path now confirms, and the prompt names the CURRENT path so the user can
see what is about to move rather than being asked to approve an abstraction.

⚠️ These tests CALL the shipped functions where a behaviour is claimed. A
source-text assertion cannot catch a neutered branch (`if False` leaves the text
intact), and an "`ui.confirm` appears in the file" check would pass even if the
call were unreachable — the guard must be shown to actually gate the request.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ALBUM_HTML = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
TRACK_HTML = REPO_ROOT / "test_site" / "templates" / "Pages" / "track_detail.html"
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
TRACK_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "track.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_js_comments(source: str) -> str:
    """Blank out JS comments and string literals, preserving newlines.

    ⚠️ REQUIRED for any "this token must NOT appear" assertion. The shipped code
    documents the mistakes it avoids by NAMING them, so a naive substring check
    matches the explanation rather than the code (the comment-matching trap —
    it has bitten this repo repeatedly).
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if source.startswith("/*", i):
            while i < n and not source.startswith("*/", i):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.extend("  ")
            i += 2
            continue
        if ch in "\"'`":
            # Template literals can span lines, so preserve newlines.
            out.append(" ")
            i += 1
            while i < n:
                if source[i] == "\\":
                    out.extend([" ", " "])
                    i += 2
                    continue
                if source[i] == ch:
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_function(source: str, name: str) -> str:
    """Brace-match one `function <name>` block out of a JS module.

    Handles the `async` prefix and an optional module-IIFE indentation, and
    tracks string/template state so a brace inside a string cannot desync the
    count.
    """
    import re

    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, f"{name} not found"
    start = match.start()
    brace = source.index("{", match.end() - 1)
    depth = 0
    i = brace
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    break
                i += 1
            i += 1
            continue
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
            continue
        if source.startswith("/*", i):
            i = source.index("*/", i) + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start: i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


# ---------------------------------------------------------------------------
# 1. The file path is displayed
# ---------------------------------------------------------------------------

class TestTheFilePathIsShown:
    def test_the_album_page_renders_a_path_for_each_track(self):
        html = read(ALBUM_HTML)
        assert "data-track-file-path=" in html, (
            "each row must carry the path, both to display it and so the rename "
            "prompt can quote the real location"
        )
        assert "track.file_path" in html
        assert "font-monospace" in html, (
            "a filesystem path must render in a monospace font to be readable "
            "and to make separator mistakes visible"
        )

    def test_the_album_page_explains_a_missing_path(self):
        """A blank cell reads as a rendering bug; say there is no path."""
        html = read(ALBUM_HTML)
        assert "No file path recorded" in html, (
            "a track with no file_path must say so rather than render nothing"
        )

    def test_the_track_page_renders_the_path(self):
        html = read(TRACK_HTML)
        assert 'id="trackFilePath"' in html
        assert "data-track-file-path=" in html
        assert "track.file_path" in html

    def test_the_track_page_handles_a_missing_path(self):
        assert "No file path recorded" in read(TRACK_HTML)

    def test_the_path_is_escaped(self):
        """A path can contain quotes or angle brackets; unescaped it breaks out."""
        for path in (ALBUM_HTML, TRACK_HTML):
            html = read(path)
            idx = html.index("data-track-file-path=")
            window = html[idx: idx + 120]
            assert "|e }}" in window or "|e}}" in window, (
                f"{path.name}: the path attribute must be escaped"
            )


# ---------------------------------------------------------------------------
# 2. The rename button exists next to Edit and Delete
# ---------------------------------------------------------------------------

class TestTheRenameButtonIsBesideEditAndDelete:
    def test_the_album_row_has_a_rename_button(self):
        html = read(ALBUM_HTML)
        assert "renameTrackFileFromAlbum(" in html, (
            "the album row needs a per-track rename control beside the others"
        )

    def test_the_rename_button_is_in_the_same_button_group(self):
        """Order matters for muscle memory: Edit, Rename, Delete."""
        html = read(ALBUM_HTML)
        group_idx = html.index("btn-group btn-group-sm")
        # The first group is a track row's action cell.
        window = html[group_idx: group_idx + 1800]
        edit = window.index("openEditTrackFromAlbum(")
        rename = window.index("renameTrackFileFromAlbum(")
        delete = window.index("deleteTrack(")
        assert edit < rename < delete, (
            "Rename must sit between Edit and Delete — placing it after Delete "
            "puts a destructive control directly beside the irreversible one"
        )

    def test_the_rename_button_is_an_outline_not_a_solid_danger_button(self):
        """It moves a file, but it is recoverable — it must not look like Delete."""
        html = read(ALBUM_HTML)
        idx = html.index("renameTrackFileFromAlbum(")
        window = html[max(0, idx - 300): idx]
        assert "btn-outline-primary" in window, (
            "the rename control must be visually distinct from Delete"
        )
        assert "btn-outline-danger" not in window

    def test_the_button_has_a_title_and_passes_the_track_id(self):
        html = read(ALBUM_HTML)
        idx = html.index("renameTrackFileFromAlbum(")
        line_start = html.rindex("<button", 0, idx)
        button = html[line_start: html.index("</button>", idx)]
        assert "title=" in button, "an icon-only button needs a tooltip"
        assert "track.id" in button, "the handler needs the track id"

    def test_the_track_page_rename_is_reachable(self):
        html = read(TRACK_HTML)
        assert 'data-action="track-rename"' in html
        assert "track-rename" in read(TRACK_JS), (
            "the action must be bound, or the button is inert"
        )

    def test_the_track_page_rename_button_sits_beside_the_path(self):
        """⚠️ Scoped to THE NEW BUTTON, not just the action name.

        `data-action="track-rename"` appears THREE times in this template — my
        new button plus two pre-existing "Rename File" dropdown items. An
        assertion on the bare attribute, or a search for the next occurrence
        anywhere AFTER the path field, therefore still passed after my button's
        action was renamed away (it found the line-737 dropdown instead). The
        button must be found inside a BOUNDED window starting at the path field.
        """
        html = read(TRACK_HTML)
        path_idx = html.index('id="trackFilePath"')
        # A bounded window: the input-group that wraps the path field and the
        # button. Deliberately NOT "anything later in the file".
        window = html[path_idx: path_idx + 900]
        assert 'data-action="track-rename"' in window, (
            "the file-path row must offer the rename action, INSIDE the path's "
            "input-group — not merely somewhere later in the template"
        )
        assert "input-group" in html[html.rindex('class="input-group"', 0, path_idx): path_idx], (
            "the button must sit inside the path's input-group, beside the field"
        )
        assert "</div>" in window, "sanity: the window should cover the group"
        # And the binding must still exist, or the button is inert.
        assert "'track-rename'" in read(TRACK_JS), (
            "the action must be bound in track.js"
        )


# ---------------------------------------------------------------------------
# 3. ⭐ The approval prompt — the behavioural part
# ---------------------------------------------------------------------------

class TestRenamingIsConfirmedFirst:
    """⚠️ Driven by CALLING the real handlers with a stubbed api/confirm, so a
    neutered branch or a deleted guard fails rather than passing on text alone.
    """

    @staticmethod
    def _load(monkeypatch, path: Path, extra: str = "", guards: str = ""):
        """Load a page JS module in Node with stubs, exposing the named fn.

        Returns the function's name so the caller can invoke it through a small
        harness. Node is used because these are real browser modules with an
        IIFE; re-implementing them in Python would test a copy, not the code.
        """
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:
            pytest.skip("node is not available")

        source = read(path)
        assert source.rstrip().endswith("})(window);"), (
            f"{path.name}: the module no longer ends with `}})(window);` — the "
            "probe injection below needs updating"
        )
        module = source.rstrip()[: -len("})(window);")]

        # Stubs must exist BEFORE the module body runs.
        #
        # ⚠️ The modules are `(function (global) { ... })(window)`, so everything
        # the code reaches as `global.document` / `global.api` / `global.ui` lives
        # on the object passed in as WINDOW — not on `globalThis`. Stubbing only
        # `globalThis` left `global.document` undefined and the probe died with
        # "Cannot set properties of undefined (setting 'querySelector')".
        # All stubs therefore go on ONE `win` object, exposed as `window`.
        prelude = textwrap.dedent(
            """
            const calls = { confirm: 0, confirmOpts: null, posts: [], reloads: 0 };
            const notifications = [];
            const doc = new Map();

            const win = {};
            win.CSS = { escape: (s) => String(s) };
            // ⚠️ `#page-data` must exist BEFORE the module body runs: the track
            // page parses it at load time into `const trackId`, and `renameTrackFile`
            // returns early when trackId is empty. Without this the probe saw
            // confirm === 0 and looked like a missing prompt — a harness bug.
            doc.set('page-data', {
              textContent: JSON.stringify({
                trackId: 'TRACK-1', title: 'Song', artistName: 'Artist', album: 'Album',
              }),
            });
            win.document = {
              getElementById: (id) => doc.get(id) || null,
              // ⚠️ MUTABLE so a probe can make the row lookup resolve. The album
              // helper finds the row with `document.querySelector(rowSel)` and
              // then reads `[data-track-file-path]` off it, so a probe that only
              // stubbed getElementById left currentPath empty and the assertion
              // failed as if the path were never included.
              querySelector: (sel) => (win.__rowFor ? win.__rowFor(sel) : null),
              querySelectorAll: () => [],
              addEventListener: () => {},
              createElement: () => ({ style: {}, classList: { add() {} }, dataset: {} }),
            };
            win.location = { reload: () => { calls.reloads++; }, href: '' };
            win.confirm = () => { calls.confirm++; return true; };
            win.addEventListener = () => {};
            win.ui = {
              confirm: async (opts) => {
                calls.confirm++;
                calls.confirmOpts = opts;
                return calls.confirmResult !== false;
              },
            };
            win.api = {
              postJson: async (url) => {
                calls.posts.push(url);
                return calls.postResult || { success: true, renamed: true, new_path: '/new/path.mp3' };
              },
              getJson: async () => ({}),
            };
            win.toast = {
              error: (m) => notifications.push(['error', m]),
              success: (m) => notifications.push(['success', m]),
              info: (m) => notifications.push(['info', m]),
            };
            win.notifyError = (m) => notifications.push(['error', m]);
            win.notifySuccess = (m) => notifications.push(['success', m]);
            win.notifyInfo = (m) => notifications.push(['info', m]);
            win.alert = (m) => notifications.push(['alert', m]);
            win.busyPopup = null;
            win.setTimeout = (fn) => { try { fn(); } catch (e) {} return 0; };

            // ⚠️ Expose the stubs as REAL GLOBALS too, not only on `window`.
            // The modules reach things BOTH ways: through the IIFE parameter
            // (`global.document`) and as BARE identifiers (`document`,
            // `location`, `alert`, `setTimeout`). Aliasing only `window` left the
            // bare references undefined, so the probe died with
            // "ReferenceError: document is not defined" and produced no RESULT.
            globalThis.window = win;
            globalThis.document = win.document;
            globalThis.location = win.location;
            globalThis.CSS = win.CSS;
            globalThis.alert = win.alert;
            globalThis.confirm = win.confirm;
            globalThis.addEventListener = win.addEventListener;
            const realSetTimeout = globalThis.setTimeout;
            // After capturing the real one, make the module's own `setTimeout`
            // synchronous so a deferred reload/toast is observable by the probe.
            globalThis.setTimeout = win.setTimeout;
            """
        )

        # The probe runs INSIDE the module's IIFE, where the page functions are
        # in scope as BARE names. Calling `globalThis.<fn>` does NOT work: the
        # module publishes onto the WINDOW object (its `global` parameter), and
        # under Node that is a plain object, NOT `globalThis`. A probe that
        # reached for `globalThis.<fn>` therefore failed with
        # "TypeError: globalThis.<fn> is not a function" — a harness bug that
        # looked exactly like a missing implementation.
        def _guard(name: str) -> str:
            return (
                f"if (typeof {name} !== 'function') "
                f"throw new Error('probe could not reach {name}');"
            )

        probe = textwrap.dedent(
            """
              (async () => {
                try {
                  __GUARDS__
                  const out = await (async () => { __PROBE__ })();
                  globalThis.__PROBE_RESULT__ = { calls, notifications, out };
                } catch (e) {
                  globalThis.__PROBE_RESULT__ = { error: String((e && e.stack) || e) };
                }
              })();
            """
        ).replace("__PROBE__", extra).replace("__GUARDS__", guards)

        harness = (
            prelude
            + module
            + probe
            + "\n})(window);\n"
            + textwrap.dedent(
                """
                realSetTimeout(() => {
                  console.log('RESULT ' + JSON.stringify(
                    globalThis.__PROBE_RESULT__ || { error: 'probe never completed' }
                  ));
                }, 60);
                """
            )
        )

        # ⚠️ Run from a TEMP FILE, not `node -e`. The modules are thousands of
        # lines and Windows caps a command line at ~32k characters, so `-e`
        # failed with `FileNotFoundError: [WinError 206] The filename or
        # extension is too long`. A file has no such limit.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.cjs"
            script.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [node, str(script)],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
            )
        blob = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")]
        assert blob, f"node produced no RESULT: {proc.stdout}\n{proc.stderr}"
        return json.loads(blob[-1][len("RESULT "):])

    def test_the_album_rename_asks_before_moving_anything(self, monkeypatch):
        result = self._load(
            monkeypatch, ALBUM_JS,
            """
            await renameTrackFileFromAlbum('TRACK-1', 'Song');
            return null;
            """,
            guards="if (typeof renameTrackFileFromAlbum !== 'function') "
                   "throw new Error('probe could not reach renameTrackFileFromAlbum');",
        )
        assert result.get("error") is None, result.get("error")
        assert result["calls"]["confirm"] == 1, (
            "the album rename must prompt exactly once"
        )
        assert len(result["calls"]["posts"]) == 1, "and then perform the rename"

    def test_a_declined_prompt_moves_nothing(self, monkeypatch):
        """⭐ THE GUARD ITSELF: cancelling must not reach the endpoint."""
        result = self._load(
            monkeypatch, ALBUM_JS,
            """
            calls.confirmResult = false;
            await renameTrackFileFromAlbum('TRACK-1', 'Song');
            return null;
            """,
            guards="if (typeof renameTrackFileFromAlbum !== 'function') "
                   "throw new Error('probe could not reach renameTrackFileFromAlbum');",
        )
        assert result.get("error") is None, result.get("error")
        assert result["calls"]["confirm"] == 1
        assert result["calls"]["posts"] == [], (
            "cancelling the prompt must NOT issue the rename request — this is "
            "the whole point of the prompt"
        )

    def test_the_album_prompt_names_the_current_path(self, monkeypatch):
        result = self._load(
            monkeypatch, ALBUM_JS,
            """
            const pathEl = {
              getAttribute: () => '/music/Artist/Album/01 - Song.mp3',
            };
            const row = { querySelector: () => pathEl };
            win.__rowFor = () => row;
            await renameTrackFileFromAlbum('TRACK-1', 'Song');
            return null;
            """,
            guards="if (typeof renameTrackFileFromAlbum !== 'function') "
                   "throw new Error('probe could not reach renameTrackFileFromAlbum');",
        )
        assert result.get("error") is None, result.get("error")
        opts = result["calls"]["confirmOpts"] or {}
        import json as _json

        text = _json.dumps(opts)
        assert "01 - Song.mp3" in text, (
            "the prompt must quote the file's CURRENT location, or the user is "
            "approving an abstraction they cannot check"
        )

    def test_the_album_prompt_says_it_moves_a_file(self, monkeypatch):
        result = self._load(
            monkeypatch, ALBUM_JS,
            """
            await renameTrackFileFromAlbum('TRACK-1', 'Song');
            return null;
            """,
            guards="if (typeof renameTrackFileFromAlbum !== 'function') "
                   "throw new Error('probe could not reach renameTrackFileFromAlbum');",
        )
        opts = result["calls"]["confirmOpts"] or {}

        # ⚠️ Assert on the MESSAGE specifically. An earlier version searched the
        # whole serialised `opts` for "move"/"moved", which the `detail` sentence
        # ("...will be moved into the folder...") satisfied even after the message
        # was changed to "Rename the file?" — so the mutation survived.
        message = str(opts.get("message") or "").lower()
        assert "move" in message or "moved" in message, (
            "the prompt MESSAGE must be explicit that the file is relocated on "
            "disk; 'rename' alone does not tell the user the path changes"
        )
        assert opts.get("tone") == "warning", (
            "a filesystem move is not a neutral action"
        )

    def test_the_track_prompt_says_it_moves_a_file(self, monkeypatch):
        result = self._load(
            monkeypatch, TRACK_JS,
            """
            await renameTrackFile();
            return null;
            """,
            guards="if (typeof renameTrackFile !== 'function') "
                   "throw new Error('probe could not reach renameTrackFile');",
        )
        opts = result["calls"]["confirmOpts"] or {}
        message = str(opts.get("message") or "").lower()
        assert "move" in message or "moved" in message, (
            "the track prompt message must say the file moves, not merely that "
            "it is renamed"
        )
        assert opts.get("tone") == "warning"

    def test_the_track_page_rename_asks_before_moving_anything(self, monkeypatch):
        result = self._load(
            monkeypatch, TRACK_JS,
            """
            await renameTrackFile();
            return null;
            """,
            guards="if (typeof renameTrackFile !== 'function') "
                   "throw new Error('probe could not reach renameTrackFile');",
        )
        assert result.get("error") is None, result.get("error")
        assert result["calls"]["confirm"] == 1, (
            "⚠️ the track page's rename used to fire WITHOUT any prompt"
        )
        assert len(result["calls"]["posts"]) == 1

    def test_the_track_page_declining_moves_nothing(self, monkeypatch):
        result = self._load(
            monkeypatch, TRACK_JS,
            """
            calls.confirmResult = false;
            await renameTrackFile();
            return null;
            """,
            guards="if (typeof renameTrackFile !== 'function') "
                   "throw new Error('probe could not reach renameTrackFile');",
        )
        assert result.get("error") is None, result.get("error")
        assert result["calls"]["posts"] == [], (
            "declining must not rename — this is the regression the prompt fixes"
        )

    def test_the_track_prompt_names_the_rendered_path(self, monkeypatch):
        result = self._load(
            monkeypatch, TRACK_JS,
            """
            doc.set('trackFilePath', {
              getAttribute: (a) => a === 'data-track-file-path'
                ? '/music/Artist/Album/01 - Song.mp3' : null,
            });
            await renameTrackFile();
            return null;
            """,
            guards="if (typeof renameTrackFile !== 'function') "
                   "throw new Error('probe could not reach renameTrackFile');",
        )
        import json as _json

        assert result.get("error") is None, result.get("error")
        opts = result["calls"]["confirmOpts"] or {}
        assert "01 - Song.mp3" in _json.dumps(opts), (
            "the track prompt must quote the path shown on the page"
        )

    def test_the_track_prompt_falls_back_when_no_path_is_rendered(self, monkeypatch):
        """A track with no file_path must still be confirmable, not crash."""
        result = self._load(
            monkeypatch, TRACK_JS,
            """
            await renameTrackFile();
            return null;
            """,
            guards="if (typeof renameTrackFile !== 'function') "
                   "throw new Error('probe could not reach renameTrackFile');",
        )
        assert result.get("error") is None, result.get("error")
        assert result["calls"]["confirm"] == 1


# ---------------------------------------------------------------------------
# 4. The prompt must not rely on newlines inside `detail`
# ---------------------------------------------------------------------------

class TestThePromptRendersItsPath:
    """`ui.confirm` escapes `detail` into a single <p>, so a `\\n` collapses to
    a space and the sentence runs into the path. The path therefore goes in
    `items`, which the dialog renders as a <ul>."""

    def test_the_confirm_helper_renders_detail_as_a_single_paragraph(self):
        src = read(REPO_ROOT / "test_site" / "static" / "js" / "ui" / "confirm.js")
        assert "<p class=\"small text-secondary mb-0 mt-2\">${esc(opts.detail)}</p>" in src, (
            "if this rendering changed, re-check that the rename prompts still "
            "show the path on its own line"
        )

    @pytest.mark.parametrize("path", [ALBUM_JS, TRACK_JS], ids=["album", "track"])
    def test_the_path_is_passed_via_items_not_detail(self, path):
        fn = "renameTrackFileFromAlbum" if "album" in path.name else "renameTrackFile"
        body = extract_function(read(path), fn)
        stripped = strip_js_comments(body)
        assert ".items = [" in stripped, (
            f"{path.name}: the current path must go in `items` so it renders as "
            "its own line; `detail` is a single escaped <p>"
        )
        # No newline escapes smuggled into the message/detail strings.
        for token in ("detail: `", "detail: '"):
            if token in stripped:
                idx = stripped.index(token)
                chunk = stripped[idx: idx + 400]
                assert "\\n" not in chunk, (
                    f"{path.name}: a \\n inside `detail` collapses to a space"
                )


# ---------------------------------------------------------------------------
# 5. After a rename the page must show the NEW path
# ---------------------------------------------------------------------------

class TestThePathIsRefreshedAfterARename:
    def test_the_album_flow_reloads_the_page(self):
        body = extract_function(read(ALBUM_JS), "renameTrackFileFromAlbum")
        assert "location.reload()" in body, (
            "the row's path is server-rendered; without a refresh the page keeps "
            "showing the OLD location after a successful move"
        )

    def test_the_track_flow_reloads_the_page(self):
        body = extract_function(read(TRACK_JS), "renameTrackFile")
        assert "location.reload()" in body

    def test_an_unchanged_file_is_reported_as_such(self):
        """Returning silently would look like a failure for an already-correct file."""
        body = extract_function(read(ALBUM_JS), "renameTrackFileFromAlbum")
        assert "unchanged" in body or "renamed === false" in body or "renamed == false" in body, (
            "the endpoint reports {renamed: false, unchanged: true}; the UI must "
            "say 'already correct' rather than nothing"
        )


# ---------------------------------------------------------------------------
# 6. Escaping — a path is user data and routinely contains & " ' and <
# ---------------------------------------------------------------------------

class TestThePathIsEscapedExactlyOnce:
    """The path is rendered into BOTH a text node and an ATTRIBUTE.

    ⚠️ `|e` under an autoescaping environment must escape ONCE. If it
    double-escaped, `/music/A & B/` would reach the page as `A &amp;amp; B`, and
    the JS reading `data-track-file-path` would then ask the user to approve a
    path that does not exist.
    """

    SNIPPET = (
        "{% set p = track.file_path %}"
        '<div data-track-file-path="{{ p|e }}">{{ p }}</div>'
    )
    # Every character that matters for HTML escaping, in one plausible path.
    TRICKY = "/music/A & B/O'Brien \"Live\"/01 - Rock & Roll <Mix>.flac"

    @staticmethod
    def _render() -> str:
        from jinja2 import Environment, select_autoescape

        env = Environment(autoescape=select_autoescape(["html"]))
        return env.from_string(
            TestThePathIsEscapedExactlyOnce.SNIPPET
        ).render(track={"file_path": TestThePathIsEscapedExactlyOnce.TRICKY})

    def test_no_double_escaping(self):
        assert "&amp;amp;" not in self._render(), (
            "the path would be corrupted for every user whose folder names "
            "contain an ampersand"
        )

    def test_the_ampersand_is_escaped_once(self):
        assert "&amp;" in self._render()

    def test_a_quote_cannot_break_out_of_the_attribute(self):
        out = self._render()
        assert "&quot;" in out or "&#34;" in out, (
            "an unescaped quote would truncate data-track-file-path and the "
            "prompt would name half a path"
        )

    def test_angle_brackets_are_escaped(self):
        assert "<Mix>" not in self._render()

    def test_the_real_template_escapes_the_attribute(self):
        """Guards against a future edit dropping the `|e`."""
        html = read(ALBUM_HTML)
        assert 'data-track-file-path="{{ track.file_path|e }}"' in html, (
            "the album row's path attribute must stay escaped"
        )
        track_html = read(TRACK_HTML)
        assert 'data-track-file-path="{{ track.file_path|e }}"' in track_html, (
            "the track page's path attribute must stay escaped"
        )


# ---------------------------------------------------------------------------
# 7. ⭐ The new code must not call a helper its own module lacks
# ---------------------------------------------------------------------------

class TestEveryCalledHelperIsDefined:
    """⚠️ A ReferenceError inside an async click handler is INVISIBLE.

    `album.js` defines `notifyError` and `notifySuccess` but NOT `notifyInfo`
    (only `track.js` defines all three). The per-track rename called
    `notifyInfo(...)` for its "already at the correct path" branch, so that
    branch threw `ReferenceError: notifyInfo is not defined` — no toast, no
    dialog, the button simply looked dead. This is the same failure shape as the
    old `bindActions` defect, which left ten buttons inert with no error shown.
    """

    LOCAL_HELPERS = ("notifyError", "notifySuccess", "notifyInfo")

    @pytest.mark.parametrize(
        "path", [ALBUM_JS, TRACK_JS], ids=["album.js", "track.js"]
    )
    def test_each_local_notify_helper_used_is_also_defined(self, path):
        source = read(path)
        for name in self.LOCAL_HELPERS:
            used = f"{name}(" in source
            defined = (
                f"function {name}(" in source
                or re.search(rf"\b{name}\s*=\s*(?:function|\()", source) is not None
            )
            if used:
                assert defined, (
                    f"{path.name} calls {name}() but never defines it — an "
                    "async handler would throw ReferenceError INVISIBLY"
                )

    def test_the_new_album_handler_only_uses_definable_helpers(self):
        """The specific regression: the rename must not reach for a name that
        its own module does not provide."""
        body = extract_function(read(ALBUM_JS), "renameTrackFileFromAlbum")
        source = read(ALBUM_JS)
        for name in re.findall(r"\b(notify[A-Z]\w*)\s*\(", body):
            assert f"function {name}(" in source, (
                f"renameTrackFileFromAlbum calls {name}() which {ALBUM_JS.name} "
                "does not define"
            )

