"""Every /api path a shipped script calls must actually be served.

WHY THIS EXISTS
---------------
The rebuilt dashboard called ``/api/scans/recent`` while the route is
``/api/recent-scans``, and the poll that used it was wrapped in a bare
``catch`` — so the request 404'd every five seconds for as long as the page was
open, the error was swallowed, and the server-rendered list stayed on screen.
The panel looked like it worked. Nothing in the suite could see it, because no
test compared what the JS ASKS FOR against what the app SERVES; the route's own
tests pass because the route is fine.

⭐ A WRONG URL IS NOT COVERED BY EITHER SIDE'S TESTS. The server tests prove the
endpoint works, the JS tests prove the renderer works, and nobody checks that
they are talking about the same address.

── IT IS A RATCHET, NOT A BLANKET ASSERTION ──────────────────────────────
Six OTHER paths are already wrong (see ``_KNOWN_BROKEN`` — qBittorrent was
removed from the app entirely, and two playlist builders were renamed). Failing
on those would make this test red on arrival and useless; silently ignoring them
would lose the information. So they are PINNED, with the reason, and the test
fails on anything new. It also fails when a pinned entry is FIXED, so the list
can only shrink.
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Both trees. The rebuilt one is served first under the cutover, but the LIVE
#: tree is the DEFAULT mode, so a wrong path in either is a user-visible 404.
JS_ROOTS = (
    REPO_ROOT / "test_site" / "static" / "js",
    REPO_ROOT / "static" / "js",
)

#: api paths that are ALREADY wrong, with why. Each entry is checked, so fixing
#: one makes this test FAIL until the entry is deleted — the ratchet only turns
#: one way.
_KNOWN_BROKEN: dict[str, str] = {
    "/api/qbittorrent/search":
        "qBittorrent was removed from the app; there are no /api/qbittorrent "
        "routes at all (see the Beets-removal commit series)",
    "/api/qbittorrent/add":
        "same — the integration is gone, so this button is dead",
    "/api/smartplaylist/create":
        "no /api/smartplaylist route exists; the served builder is "
        "/api/playlist/create",
    "/api/playlist/create-custom":
        "the served route is /api/playlist/create — NOT changed here because "
        "the two may differ in payload, and guessing an endpoint for a write is "
        "worse than leaving a dead button",
    "/api/playlist/download":
        "the served routes are /api/playlist-downloads and "
        "/api/playlist-downloads/create — same caveat as above",
    "/api/genres/recommendations":
        "the served routes are /api/genres/artist/<name> and "
        "/api/artist/genre-recommendations",
}

# ⚠️ `/api/slskd/download-single` WAS PINNED HERE AND IS NOT ANY MORE. Its only
# caller is `static/js/artist_detail.js`, which is in ``_IGNORED_FILES`` (a Jinja
# template served as JavaScript, so the browser discards the file and the call
# can never run). A pin for a call that cannot execute only hides a future
# re-introduction — and the check below found it the moment the ratchet started
# looking for stale exemptions as well as served ones.

#: Files whose api calls do not matter, with why.
_IGNORED_FILES: dict[str, str] = {
    # A Jinja page template loaded via <script src>, so the browser discards
    # the whole file on the first `{%` — none of its calls ever run.
    "static/js/artist_detail.js":
        "dead file: a Jinja template served as JavaScript, discarded on parse",
}

_CALL_RE = re.compile(
    r"""(?:getJson|postJson|deleteJson|putJson|fetch)\(\s*[`'"](?P<path>/api/[^`'"]*)""",
    re.IGNORECASE,
)


def strip_js_comments(source: str) -> str:
    """Blank out JavaScript comments, preserving every newline and offset.

    ⚠️ WHAT THIS IS FOR. Two separate problems, and both are real here:

    1. This file asserts that a wrong path does NOT appear. The fix for that
       path is DOCUMENTED in the source — the comment names the wrong spelling
       on purpose — so a raw text search is satisfied by the explanation rather
       than by the code. That trap has now cost seven runs in this codebase.
    2. The audit finds calls with a regex. A COMMENTED-OUT call (or a string
       mentioning one) would be counted as a live call, inventing an unserved
       path that no user can reach.

    ⚠️ A NAIVE ``//`` STRIP IS WRONG: ``https://…`` appears constantly inside
    string literals, so splitting on ``//`` truncates a URL and every path
    after it on that line. The scanner therefore tracks quote state (including
    backtick templates, which is what the code under audit uses for URLs) and
    only treats ``//`` or ``/*`` as a comment OUTSIDE a string.

    Newlines are preserved so reported line numbers still match the file.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]

        if quote is not None:
            # Inside a string: copy verbatim, honouring backslash escapes.
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "/":
                while i < n and source[i] != "\n":
                    out.append(" ")
                    i += 1
                continue
            if nxt == "*":
                while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                    out.append("\n" if source[i] == "\n" else " ")
                    i += 1
                out.append("  ")
                i += 2
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def _served_rules() -> set[str]:
    app_mod = importlib.import_module("app")
    rules: set[str] = set()
    for rule in app_mod.app.url_map.iter_rules():
        # Normalise a converter to a wildcard so segment matching is uniform.
        rules.add(re.sub(r"<[^>]+>", "*", str(rule.rule)))
    return rules


def _literal_prefix(path: str) -> str:
    """The path up to its first dynamic segment or query string."""
    cut = len(path)
    for token in ("${", "?"):
        idx = path.find(token)
        if idx != -1:
            cut = min(cut, idx)
    return path[:cut]


def _is_served(prefix: str, rules: set[str]) -> bool:
    """Segment-wise prefix match against the route table.

    Segment-wise rather than a plain ``startswith``: ``/api/a`` must not be
    satisfied by ``/api/abcdef``, which a string prefix test would allow.
    """
    pref_parts = prefix.strip("/").split("/")
    for rule in rules:
        if rule == prefix:
            return True
        rule_parts = rule.strip("/").split("/")
        if len(pref_parts) > len(rule_parts):
            continue
        if all(rp == "*" or rp == pp for rp, pp in zip(rule_parts, pref_parts)):
            return True
    return False


def _called_paths() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for root in JS_ROOTS:
        if not root.is_dir():
            continue
        for js in sorted(root.rglob("*.js")):
            rel = str(js.relative_to(REPO_ROOT)).replace("\\", "/")
            if rel in _IGNORED_FILES:
                continue
            try:
                source = strip_js_comments(js.read_text(encoding="utf-8"))
            except Exception:
                continue
            for match in _CALL_RE.finditer(source):
                path = match.group("path")
                prefix = _literal_prefix(path)
                if not prefix or prefix == "/api/":
                    continue
                found.append((rel, source[: match.start()].count("\n") + 1, prefix))
    return found


class TestTheAuditItselfWorks:
    """Guards against the harness going vacuous.

    ⭐ Without these, a regex that stops matching (a helper renamed, quotes
    changed) would report zero calls and the suite would pass while checking
    nothing at all — the failure mode this whole file exists to prevent.
    """

    def test_it_finds_rules(self):
        assert len(_served_rules()) > 200

    def test_it_finds_calls(self):
        calls = _called_paths()
        assert len(calls) > 100, f"only {len(calls)} api calls matched — regex rot?"

    def test_it_checks_both_trees(self):
        files = {rel for rel, _line, _path in _called_paths()}
        assert any(f.startswith("test_site/") for f in files)
        assert any(f.startswith("static/") for f in files)

    def test_segment_matching_is_not_a_string_prefix(self):
        """`/api/a` must not be considered served by `/api/abcdef`."""
        assert not _is_served("/api/a", {"/api/abcdef"})
        assert _is_served("/api/album/1", {"/api/album/*"})
        assert _is_served("/api/recent-scans", {"/api/recent-scans"})


class TestEveryCalledPathIsServed:
    def test_no_new_unserved_paths(self):
        rules = _served_rules()
        unknown = [
            (rel, line, path)
            for rel, line, path in _called_paths()
            if path not in _KNOWN_BROKEN and not _is_served(path, rules)
        ]
        assert not unknown, (
            "these scripts call api paths the app does not serve — a 404 that "
            "no server-side test can see:\n"
            + "\n".join(f"  {rel}:{line}  ->  {path}" for rel, line, path in unknown)
        )

    def test_the_ratchet_only_tightens(self):
        """A pinned path must still be PINNED FOR A REASON.

        ⚠️ THE PIN IS AN EXEMPTION, AND A STALE EXEMPTION IS A HOLE. There are
        two ways an entry stops being justified, and an earlier version of this
        test only checked the first:

        1. the path became SERVED (someone added the route) — the exemption now
           suppresses nothing, and a later regression to the wrong spelling
           would be tolerated;
        2. nothing CALLS it any more (someone fixed the callers) — the pin is
           now a promise about code that no longer exists, and it would let a
           future re-introduction of the same wrong path through unnoticed.

        ⭐ (2) was found by an oracle mutation that had been reported as an
        "escape": fixing the only caller while leaving the pin changed nothing
        observable, because the check only looked at whether the path was
        served. The escape was the test's fault, not the mutation's.
        """
        rules = _served_rules()
        called = {path for _rel, _line, path in _called_paths()}

        served_but_pinned = [path for path in _KNOWN_BROKEN if _is_served(path, rules)]
        uncalled_but_pinned = [path for path in _KNOWN_BROKEN if path not in called]

        problems: list[str] = []
        if served_but_pinned:
            problems.append(
                "these paths are SERVED now — delete them from _KNOWN_BROKEN:\n"
                + "\n".join(f"  {path}" for path in served_but_pinned)
            )
        if uncalled_but_pinned:
            problems.append(
                "nothing calls these any more — delete them from _KNOWN_BROKEN, or "
                "the exemption will hide a re-introduction of the same wrong path:\n"
                + "\n".join(f"  {path}" for path in uncalled_but_pinned)
            )
        assert not problems, "\n\n".join(problems)

    def test_every_pinned_entry_has_a_reason(self):
        for path, reason in _KNOWN_BROKEN.items():
            assert reason.strip(), f"{path} is pinned with no explanation"
            assert len(reason) > 20, f"{path}'s reason is too terse to act on"

    def test_every_ignored_file_has_a_reason(self):
        for rel, reason in _IGNORED_FILES.items():
            assert reason.strip()
            assert (REPO_ROOT / rel).is_file(), (
                f"{rel} is ignored but does not exist — stale exemption"
            )


class TestTheCommentStripper:
    """The stripper is load-bearing for both halves of this file, so it is
    tested rather than trusted."""

    def test_it_removes_a_line_comment(self):
        assert "secret" not in strip_js_comments("var a = 1; // secret\nvar b = 2;")

    def test_it_removes_a_block_comment(self):
        out = strip_js_comments("var a = 1; /* secret */ var b = 2;")
        assert "secret" not in out
        assert "var a" in out and "var b" in out

    def test_it_preserves_newlines_so_line_numbers_hold(self):
        src = "a\n// gone\nb\nc"
        assert strip_js_comments(src).count("\n") == src.count("\n")

    def test_it_does_NOT_truncate_a_url_in_a_string(self):
        """⚠️ The bug a naive ``//`` strip would introduce.

        ``https://`` appears constantly in this codebase, so a stripper that
        split on the first ``//`` would cut the string and every path after it
        on that line — silently shrinking the audit's coverage.
        """
        src = 'var u = "https://example.com/api/thing"; // note'
        out = strip_js_comments(src)
        assert "https://example.com/api/thing" in out
        assert "note" not in out

    def test_it_handles_a_template_literal_with_a_url(self):
        src = "const u = `/api/album/art`; // trailing"
        out = strip_js_comments(src)
        assert "/api/album/art" in out
        assert "trailing" not in out

    def test_it_keeps_a_backslash_escape_inside_a_string(self):
        src = r'var s = "a\"//b"; // x'
        out = strip_js_comments(src)
        assert "//b" in out
        assert " x" not in out

    def test_a_commented_out_call_is_not_audited(self):
        """Problem 2: an unreachable call must not invent an unserved path."""
        src = "// global.api.getJson('/api/nonexistent/thing')\nvar a = 1;"
        stripped = strip_js_comments(src)
        assert not _CALL_RE.findall(stripped)


class TestTheReportedPath:
    """The specific 404 that prompted this file."""

    def test_the_scan_feed_uses_the_served_spelling(self):
        """⚠️ `/api/recent-scans`, not `/api/scans/recent`.

        Pinned on BOTH files because the page inherited the dashboard's wrong
        path — the two must agree, and both must agree with the route.

        Asserted on COMMENT-STRIPPED source: the fix is documented in the code,
        and that documentation names the wrong spelling on purpose.
        """
        for rel in (
            "test_site/static/js/pages/dashboard.js",
            "test_site/static/js/pages/activity.js",
        ):
            source = strip_js_comments((REPO_ROOT / rel).read_text(encoding="utf-8"))
            assert "/api/scans/recent" not in source, (
                f"{rel} still calls the 404ing path"
            )
            assert "/api/recent-scans" in source, (
                f"{rel} does not call the recent-scans endpoint"
            )

    def test_the_live_tree_agrees(self):
        """The live dashboard always used the right spelling — if it ever
        changes, the two trees would disagree and one of them would 404."""
        source = strip_js_comments(
            (REPO_ROOT / "static/js/dashboard.js").read_text(encoding="utf-8")
        )
        assert "/api/recent-scans" in source

    def test_the_route_still_exists(self):
        """The other half of the contract: a fixed caller needs a live route."""
        rules = _served_rules()
        assert "/api/recent-scans" in rules
