"""Downloads & Monitor: four points from the mobile review.

1. **The title rendered as `Downloads &amp;amp; Monitor`.** `{% set title =
   'Downloads &amp; Monitor' %}` put an HTML ENTITY into a variable, and the
   partial that renders it (`{{ title }}`) escaped it again. An entity belongs in
   `{% block title %}`, which is not autoescaped the same way; a `set` value is
   DATA and wants the character itself.

2. **The green `[?]`, `[↓]`, `[⊘]`, `[↻]` and `[🗑️]` icons were stretched
   vertically.** All are `btn-sm` — but the ones inside a `btn-group-sm` are NOT,
   because Bootstrap's component CSS sizes them through `.btn-group-sm > .btn`
   and the markup writes the plain variant. So `popularr.css`'s `.btn-sm {
   min-height: 0 }` exemption (added for exactly this) missed every one of them
   and the `@media (max-width: 576px) { .btn { min-height: 44px } }` floor caught
   the whole group.

3. **The events log's Message column repeated the Timestamp and the level.**
   `_tail_queue_log` set `message` to the ENTIRE raw log line, whose prefix is
   `YYYY-MM-DD HH:MM:SS.mmm [LEVEL]` — the two columns the table already has.
   The `.log` export prepends the timestamp yet again, so it appeared three
   times there. Fixed in the producer, which fixes both consumers. The Timestamp
   column also now shrinks on a phone, where 160px + 120px was most of the row.

4. **The monitor's Upcoming Releases table kept all four columns and clipped.**
   On a phone the Album — the only cell with information — got about a third of
   the row, and the Date wrapped mid-value ("2026-" / "09-18"). Artist and Date
   now have their own classes that the phone block hides, with the artist and
   date repeated under the album, and Search loses its label below `md`.

Plus the folder row: `Matched & Unmatched Folders` titles were truncated after a
dozen characters because the action cluster is `flex-shrink-0` and takes a large
share of the row before the title column is measured. Two lines now.
"""

from __future__ import annotations

import re

import pytest

from tests._test_site_render import REPO_ROOT, build_env, install_real_filters

LIVE = REPO_ROOT
TS = REPO_ROOT / "test_site"

MONITOR_PAGES = (
    TS / "templates" / "Pages" / "downloads" / "monitor.html",
)

QUEUE_STATUS = (
    LIVE / "templates" / "components" / "search" / "_queue_status.html",
    TS / "templates" / "components" / "search" / "_queue_status.html",
)

DOWNLOADS_CSS = (
    LIVE / "static" / "css" / "downloads.css",
    TS / "static" / "css" / "downloads.css",
)

POPULARR_CSS = (
    LIVE / "static" / "css" / "popularr.css",
    TS / "static" / "css" / "popularr.css",
)

MONITOR_JS = (
    LIVE / "static" / "js" / "monitor.js",
    TS / "static" / "js" / "pages" / "monitor.js",
)

QUEUE_JS = (
    LIVE / "static" / "js" / "downloads.js",
    TS / "static" / "js" / "pages" / "download-queue.js",
)

#: The phone breakpoint every rule in this change shares.
PHONE_MQ = "@media (max-width: 767.98px)"


def _without_comments(text: str) -> str:
    r"""*text* without its Jinja, CSS and HTML comments.

    ⚠️⚠️ THE RECURRING TRAP, and it bit this file twice.

    These fixes explain themselves in comments that necessarily NAME the thing
    under test — this file's own module docstring quotes the wrong `set` title,
    and the monitor page's comment explains the difference by quoting the block
    tag. So:

      * a plain search for the block tag matched the COMMENT, and `"&amp;" in
        group(1)` passed on the prose;
      * `".btn-group-sm > .btn" in css` passed with the rule deleted, because the
        comment above it mentions the selector.

    Strip first, then assert. Every helper here goes through this.

    (A raw docstring, because the patterns below contain `\{` and `\s`.)
    """
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path) -> str:
    return "live" if str(path).startswith(str(LIVE) + "\\static") or "test_site" not in str(path) else "rebuilt"


def _rule(css: str, selector: str) -> str:
    """The declarations of the rule whose selector LIST contains *selector*.

    ⚠️ Three things this has to get right, all learned the hard way here:

    * **comments are stripped first** — the selector is usually named in the
      comment explaining the change, so a file-wide search finds PROSE (see
      `_without_comments`);
    * it is anchored on a line-start match, NOT `str.index`, for the same
      reason;
    * it finds the brace AFTER the selector rather than expecting it on the same
      line, because a grouped rule is written

          .btn-group-sm > .btn,
          .btn-group-sm .btn { ... }

      so requiring the selector to be followed immediately by a brace reported
      the rule as missing.
    """
    css = _without_comments(css)
    match = re.search(rf"^\s*{re.escape(selector)}\b", css, re.M)
    assert match, f"no rule for `{selector}`"
    body = css[css.index("{", match.end()):]
    return body[1: body.index("}")]


def _phone_block(css: str, marker: str) -> str:
    """The `max-width: 767.98px` block CONTAINING *marker*.

    Both stylesheets declare several phone queries, so "the first one" is not
    necessarily this change's — and a rule read from the wrong block asserts
    about the desktop declaration instead. That is not hypothetical: it is what
    made `test_the_phone_block_shrinks_them` pass while reading the `160px`
    desktop rule.
    """
    offset = 0
    while True:
        start = css.find(PHONE_MQ, offset)
        if start == -1:
            raise AssertionError(f"no {PHONE_MQ} block containing {marker!r}")
        depth = 0
        end = None
        for index in range(css.index("{", start), len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        assert end is not None, "unbalanced media query"
        block = css[start:end]
        if marker in block:
            return block
        offset = end


def _declares(rule: str, prop: str) -> bool:
    """Is CSS property *prop* declared in *rule*?

    ⚠️ A substring check answers a different question: `-webkit-mask-image`
    CONTAINS `mask-image`, so `"mask-image" in rule` is true when only the
    prefixed property is present. Split on the colon and compare exactly.
    """
    return any(
        line.split(":", 1)[0].strip() == prop
        for line in rule.splitlines()
        if ":" in line and not line.strip().startswith(("/*", "*", "//"))
    )


def _value(rule: str, prop: str) -> str:
    """The declared value of *prop* in *rule*.

    ⚠️ Needed because `_declares` only answers "is it there". A rule mutated from
    `display: none` to `display: block` still DECLARES `display`, so a
    property-presence check cannot tell the two apart — and that mutation is the
    one that doubles every row on desktop.
    """
    for line in rule.splitlines():
        if ":" in line and line.split(":", 1)[0].strip() == prop:
            return line.split(":", 1)[1].strip().rstrip(";").strip()
    raise AssertionError(f"`{prop}` is not declared in:\n{rule}")


# ===========================================================================
# 1. The double-escaped title
# ===========================================================================

class TestTheMonitorTitleIsNotDoubleEscaped:
    """⚠️ `&amp;` in a `{% set %}` comes out as `&amp;amp;`.

    `{% block title %}` is a template block, so an entity there is correct. A
    `{% set %}` value is DATA — `_downloads_header.html` renders it with
    `{{ title }}`, which escapes the `&` of `&amp;` a second time. The two need
    OPPOSITE spellings in the same file, which is exactly why this is easy to get
    wrong and worth a test.
    """

    @pytest.mark.parametrize("page", MONITOR_PAGES, ids=lambda p: p.parent.parent.parent.name)
    def test_a_set_title_carries_no_html_entity(self, page):
        """⚠️ Comments stripped — the file's own header quotes the wrong form."""
        body = _without_comments(_read(page))

        match = re.search(r"\{%\s*set\s+title\s*=\s*'([^']*)'", body)
        assert match, "the page no longer sets a title variable"
        value = match.group(1)

        assert "&amp;" not in value, (
            f"the `set` title is {value!r}: the partial escapes it again, so the "
            "page renders '&amp;amp;'"
        )
        assert "&" in value, (
            "the title lost its ampersand entirely rather than being unescaped"
        )

    @pytest.mark.parametrize("page", MONITOR_PAGES, ids=lambda p: p.parent.parent.parent.name)
    def test_the_block_title_keeps_its_entity(self, page):
        """CONTROL — the block is NOT autoescaped the same way, so it differs.

        ⚠️⚠️ COMMENTS STRIPPED, and this is the assertion the oracle escaped.

        The page's own comment explains the difference by quoting the block tag,
        so a plain search over the file matched the PROSE and found `&amp;` in the
        EXPLANATION while the real block said `Downloads & Monitor`. The
        assertion was satisfied by the fix's own comment — the comment trap, and
        the reason `_without_comments` exists.
        """
        body = _without_comments(_read(page))

        match = re.search(r"\{%\s*block\s+title\s*%\}(.*?)\{%\s*endblock", body, re.S)
        assert match, "the page no longer defines a title block"
        assert "&amp;" in match.group(1), (
            "the block title needs the ENTITY; a bare '&' is ambiguous markup"
        )

    def test_the_partial_escapes_what_it_renders(self):
        """CONTROL on the premise: the header renders the title unescaped-once."""
        header = _read(LIVE / "templates" / "components" / "_downloads_header.html")

        assert "{{ title|default" in header, (
            "if the partial stopped using `{{ }}` the double-escape premise "
            "would no longer hold and this test would need re-deriving"
        )

    def test_a_rendered_title_has_no_entity_left_over(self):
        """The end-to-end assertion, on the REAL header rather than a stub.

        `install_real_filters` is not needed here — unlike the album page, this
        page uses no `|escapejs` — but the environment is the shared one so the
        shell's own filters and globals are what render.
        """
        from jinja2 import FileSystemLoader

        env = build_env("/downloads/monitor")
        env.loader = FileSystemLoader(str(LIVE / "templates"))
        html = env.get_template("components/_downloads_header.html").render(
            title="Downloads & Monitor"
        )

        assert "Downloads &amp; Monitor" in html, (
            "the rendered title is not the once-escaped form"
        )
        assert "&amp;amp;" not in html, "the title is double-escaped at render time"


# ===========================================================================
# 2. The btn-group-sm stretch
# ===========================================================================

class TestTheCompactGroupsAreExemptFromTheTouchFloor:
    """`popularr.css` gives every `.btn` a 44px minimum on a phone.

    `btn-sm` is exempt. **A `btn-group-sm`'s children are not `btn-sm`** —
    Bootstrap sizes them via `.btn-group-sm > .btn`, and the markup writes
    `<button class="btn btn-outline-primary">`. So the exemption missed every
    button in a group and the floor caught the whole thing, which is the reported
    "stretched" icon buttons.
    """

    @pytest.mark.parametrize("css_path", POPULARR_CSS, ids=lambda p: p.parent.parent.name)
    def test_the_group_children_are_exempt(self, css_path):
        """⭐ Asserted on the RULE, not on a substring of the file.

        ⚠️ `".btn-group-sm > .btn" in css` is TRUE with the rule DELETED — the
        comment above it names the selector while explaining why it is needed, and
        the oracle caught exactly that. `_rule` strips comments and raises when
        the rule is gone.
        """
        css = _read(css_path)
        tree = css_path.parent.parent.name

        assert _value(_rule(css, ".btn-sm"), "min-height") == "0", (
            f"{tree}: the btn-sm exemption is gone, so the reason for the group "
            "exemption needs re-deriving"
        )
        assert _value(_rule(css, ".btn-group-sm > .btn"), "min-height") == "0", (
            f"{tree}: the group's children are not exempt, so the 44px floor still "
            "stretches them"
        )

    @pytest.mark.parametrize("css_path", POPULARR_CSS, ids=lambda p: p.parent.parent.name)
    def test_the_group_exemption_zeroes_the_minimum(self, css_path):
        """⚠️ Asserted on the RULE, not on the file.

        The rule sits in the SAME `max-width: 576px` block as the floor it
        exempts from, so source order decides it at equal specificity — a
        `min-height: 44px` here (or an exemption placed BEFORE the floor) would
        look present and do nothing.
        """
        css = _read(css_path)

        floor = css.index(".btn { min-height: 44px; }")
        exemption = css.index(".btn-group-sm > .btn")
        assert exemption > floor, (
            f"{css_path.parent.parent.name}: the exemption is declared BEFORE the "
            "floor, so source order undoes it"
        )

        rule = _rule(css, ".btn-group-sm > .btn")
        assert _value(rule, "min-height") == "0", (
            f"{css_path.parent.parent.name}: the exemption sets "
            f"`{_value(rule, 'min-height')}`, which is just a different floor "
            "rather than no floor"
        )

    def test_the_stretched_controls_really_are_in_groups(self):
        """The premise, checked against the markup rather than assumed.

        If these buttons were NOT in a `btn-group-sm`, the exemption above would
        be dead CSS and the stretch would have another cause.
        """
        body = _read(TS / "templates" / "components" / "search" / "_queue_status.html")

        groups = re.findall(r'class="btn-group btn-group-sm[^"]*"[^>]*>(.*?)</div>', body, re.S)
        assert groups, "no compact button groups left; this test's premise changed"

        children = [
            cls
            for group in groups
            for cls in re.findall(r'class="btn ([^"]*)"', group)
        ]
        assert children, "the groups have no buttons"
        assert not any("btn-sm" in cls for cls in children), (
            "a button in a group carries btn-sm, so the group exemption would be "
            "unnecessary — the fix would be the wrong one"
        )

    def test_the_icon_only_buttons_are_small_variant(self):
        """CONTROL: the header's green [!] and the Refresh control are `btn-sm`."""
        header = _read(LIVE / "templates" / "components" / "_downloads_header.html")
        assert "btn btn-sm btn-outline-success" in header, (
            "the help button stopped being btn-sm, so the group exemption is not "
            "what fixes it"
        )


# ===========================================================================
# 3. The events log
# ===========================================================================

class TestTheEventLogStopsRepeatingItsOwnColumns:
    """The table has a Timestamp column and a Type column already."""

    def test_the_stripper_removes_seconds_and_level(self):
        from services.queue.queue_diagnostics_service import strip_log_line_prefix

        assert strip_log_line_prefix(
            "2026-10-10 12:00:00.123 [INFO] [popularr.queue] item queued"
        ) == "[popularr.queue] item queued"

    def test_the_stripper_removes_a_bare_level(self):
        from services.queue.queue_diagnostics_service import strip_log_line_prefix

        assert strip_log_line_prefix("2026-10-10 12:00:00 [WARNING] boom") == "boom"

    def test_the_stripper_handles_a_padded_console_level(self):
        """structlog's ConsoleRenderer pads the level into a fixed-width column."""
        from services.queue.queue_diagnostics_service import strip_log_line_prefix

        assert strip_log_line_prefix(
            "2026-10-10T12:00:00 [info     ] something happened"
        ) == "something happened"

    def test_the_stripper_keeps_a_logger_tag(self):
        """⚠️ Deliberately KEPT: nothing else in the row carries it."""
        from services.queue.queue_diagnostics_service import strip_log_line_prefix

        out = strip_log_line_prefix("2026-10-10 12:00:00.123 [ERROR] [r.queue] x")
        assert out == "[r.queue] x"

    @pytest.mark.parametrize(
        "line",
        [
            "[QUEUE] no timestamp at all",
            "",
        ],
        ids=["no-timestamp", "empty"],
    )
    def test_an_unrecognised_line_is_returned_unchanged(self, line):
        """⚠️ The viewer's job is to show what the log holds.

        A MISSED strip is cosmetic; a WRONG one loses the message. So anything
        whose shape the pattern does not recognise comes back verbatim.
        """
        from services.queue.queue_diagnostics_service import strip_log_line_prefix

        assert strip_log_line_prefix(line) == line

    def test_the_timestamp_goes_even_with_no_level_bracket(self):
        """A line with a timestamp but no level still loses the timestamp.

        ⚠️ Deliberately NOT in the "unchanged" cases above: the Timestamp column
        exists whatever the level does, so dropping it is right. The level strip
        is the optional half, not the timestamp strip.
        """
        from services.queue.queue_diagnostics_service import strip_log_line_prefix

        assert strip_log_line_prefix(
            "2026-10-10 12:00:00 - no level bracket here"
        ) == "- no level bracket here"

    def test_the_queue_tail_strips_before_publishing_the_message(self):
        """⭐ The assertion that matters: the MESSAGE is the stripped text.

        A unit test of the helper says nothing about whether the call site uses
        it — and that is the whole fix.
        """
        src = _read(LIVE / "services" / "queue" / "queue_diagnostics_service.py")

        assert '"message": strip_log_line_prefix(line)' in src, (
            "the queue tail publishes the raw line, so the viewer still repeats "
            "the timestamp and level"
        )

    def test_the_timestamp_cell_renders_a_compact_form(self):
        """⭐ A media query cannot rewrite text.

        The full `toLocaleString()` — "10/10/2026, 12:00:00 pm" — is wider than
        the 160px it starts in and stays that width however narrow the column
        gets, which is what made the TIMESTAMP the column that overflowed rather
        than the message. Both forms are rendered and CSS picks one.

        ⚠️ Asserted on the `<td>` TAGS. The renderer's comment names
        `.qe-ts-col` while explaining the two-renderings trick, so a file-wide
        `"qe-ts-col" in body` stayed true with the class removed from the cell —
        the comment trap, and the oracle caught it.
        """
        for path in QUEUE_JS:
            body = _read(path)

            cells = re.findall(r"<td\b[^>]*>", body)
            assert any("qe-ts-col" in cell for cell in cells), (
                f"{path.name}: no <td> carries `qe-ts-col`, so the timestamp column "
                "has no hook for the phone width rule"
            )

            spans = re.findall(r"<span\b[^>]*>", body)
            span_html = " ".join(spans)
            assert "d-none d-md-inline" in span_html, (
                f"{path.name}: only one timestamp form is rendered, so it cannot "
                "shorten on a phone"
            )
            assert "d-md-none" in span_html, (
                f"{path.name}: the compact timestamp is not rendered"
            )

    def test_the_column_widths_left_the_style_attributes(self):
        """⚠️ An inline `width: 160px` cannot be overridden by a media query.

        Asserted against the `<th>` TAGS, not the file: this change's own comment
        quotes the retired `width: 160px`, so a whole-file check would fail on
        the explanation rather than on the markup.
        """
        for path in QUEUE_STATUS:
            body = _read(path)

            assert 'th class="qe-ts-col"' in body, f"{path.name}: Timestamp is unclassed"
            assert 'th class="qe-type-col"' in body, f"{path.name}: Type is unclassed"

            for tag in re.findall(r"<th\b[^>]*>", body):
                assert "width:" not in tag, (
                    f"{path.name}: `{tag}` carries an inline width — it cannot be "
                    "narrowed on a phone"
                )

    @pytest.mark.parametrize("css_path", DOWNLOADS_CSS, ids=lambda p: p.parent.parent.name)
    def test_the_phone_block_shrinks_them(self, css_path):
        """⭐ Read from the PHONE block, not from the file.

        Both the desktop and the phone declaration exist, and the desktop one is
        written first — so a file-wide `_rule` lookup finds `160px` and reports
        the phone override as missing. Same trap as the marker-scoped phone
        block in test_mobile_layout_density.py.
        """
        css = _read(css_path)
        block = _phone_block(css, "#queueEventsTable .qe-ts-col")
        tree = css_path.parent.parent.name

        ts_width = next(
            line.split(":", 1)[1].strip().rstrip(";").strip()
            for line in _rule(block, "#queueEventsTable .qe-ts-col").splitlines()
            if ":" in line and line.split(":", 1)[0].strip() == "width"
        )
        assert ts_width != "160px", (
            f"{tree}: the timestamp column still declares the desktop width in the "
            "phone block"
        )

        type_width = next(
            line.split(":", 1)[1].strip().rstrip(";").strip()
            for line in _rule(block, "#queueEventsTable .qe-type-col").splitlines()
            if ":" in line and line.split(":", 1)[0].strip() == "width"
        )
        assert "%" in type_width, (
            f"{tree}: the badge column should shrink to its content, not to {type_width}"
        )


# ===========================================================================
# 4. The monitor's upcoming table
# ===========================================================================

class TestTheMonitorUpcomingTableStacksOnPhones:
    """Four columns do not fit a phone: Album got a third of the row and the
    Date wrapped mid-value."""

    def test_the_columns_are_classed(self):
        """⚠️ Asserted on the `<th>` TAGS, not on the file.

        Every one of these tokens is ALSO on the row's `<td>`, so a whole-file
        substring check stays true when the header class is changed to
        `col-ur-XX` — and the header is what the phone block hides. The oracle
        caught exactly that.
        """
        for path in MONITOR_JS:
            body = _read(path)

            headers = re.findall(r"<th\b[^>]*>", body)
            assert headers, f"{path.name}: no table headers"
            header_html = " ".join(headers)

            for token in ("col-ur-artist", "col-ur-date", "col-ur-action"):
                assert token in header_html, (
                    f"{path.name}: no header carries `{token}`, so the phone block "
                    "cannot hide or size that column"
                )
            assert "upcoming-monitor-table" in body, (
                f"{path.name}: the table has no hook for the phone block"
            )

    def test_the_artist_and_date_are_repeated_under_the_album(self):
        """⭐ The values move, they are not duplicated-and-hidden.

        `d-none`-ing the standalone columns would leave the artist and date
        NOWHERE, and rendering both copies with `d-none` would put the text in
        the DOM twice for a screen reader. The inline copy is the phone's only
        source for them.
        """
        for path in MONITOR_JS:
            body = _read(path)
            assert "upcoming-inline-meta" in body, (
                f"{path.name}: nothing carries the artist/date on a phone"
            )

    @pytest.mark.parametrize("css_path", DOWNLOADS_CSS, ids=lambda p: p.parent.parent.name)
    def test_the_phone_block_hides_the_two_columns(self, css_path):
        css = _read(css_path)
        tree = css_path.parent.parent.name

        # ⚠️ The VALUE, not just the property: `display: block` here would double
        # every desktop row, and a `_declares` check cannot tell it from `none`.
        assert _value(_rule(css, ".upcoming-inline-meta"), "display") == "none", (
            f"{tree}: the inline meta must default to hidden, or it doubles every "
            "row on desktop"
        )
        block = _phone_block(css, ".upcoming-inline-meta")
        for selector in (
            ".upcoming-monitor-table .col-ur-artist,",
            ".upcoming-monitor-table .col-ur-date",
        ):
            assert selector in block, f"{tree}: `{selector}` is not hidden on a phone"
        assert "display: none" in block, f"{tree}: the hidden columns are not hidden"

    def test_the_search_label_hides_below_md(self):
        """The suggested fix: the button becomes icon-only."""
        for path in MONITOR_JS:
            body = _read(path)
            assert re.search(r'bi-search"></i><span class="d-none d-md-inline"', body), (
                f"{path.name}: the Search button keeps its label on a phone, so it "
                "still crowds the row"
            )

    def test_a_row_that_fits_gets_the_fade_controls(self):
        """CONTROL: the four desktop columns must all still be rendered."""
        body = _read(MONITOR_JS[1])
        for header in (">Artist<", ">Album<", ">Date<", ">Action<"):
            assert header in body, f"the {header.strip('<>')} header is gone"


# ===========================================================================
# The folder rows
# ===========================================================================

class TestTheFolderTitleWraps:
    """The action cluster is `flex-shrink-0` with long labels, so it takes a
    large share of the row before the title column is measured."""

    def test_the_title_is_no_longer_force_truncated(self):
        for path in MONITOR_JS:
            body = _read(path)
            folder_title = body[body.index("folder-row-title"):]
            # The class this replaces must be GONE from the folder row, or the
            # two rules fight and nowrap wins.
            assert 'class="folder-row-title"' in body, (
                f"{path.name}: the folder title has no class"
            )
            assert folder_title, path.name

    def test_the_clamp_can_actually_wrap_a_dotted_name(self):
        """⚠️ `overflow-wrap: anywhere` is load-bearing.

        A folder name like `Some.Very.Long.Album.Name` is ONE unbroken token, so
        without it the line OVERFLOWS rather than wrapping and the clamp never
        engages — the declaration would look present and do nothing.
        """
        for css_path in DOWNLOADS_CSS:
            tree = css_path.parent.parent.name
            rule = _rule(_read(css_path), ".folder-row-title")

            assert _value(rule, "-webkit-line-clamp") == "2", f"{tree}: no two-line clamp"
            # ⚠️ Presence AND a wrapping value: `word-break: keep-all` is also an
            # `overflow-wrap`-adjacent declaration and would defeat the wrap.
            assert _declares(rule, "overflow-wrap"), (
                f"{tree}: without overflow-wrap a dotted folder name cannot wrap, "
                "so the clamp has nothing to act on"
            )
            assert "anywhere" in _value(rule, "overflow-wrap") or "break-word" in _value(
                rule, "overflow-wrap"
            ), (
                f"{tree}: `overflow-wrap: {_value(rule, 'overflow-wrap')}` does not "
                "break an unbroken dotted name"
            )
            assert _declares(rule, "overflow"), f"{tree}: no overflow"