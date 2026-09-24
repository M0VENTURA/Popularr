# An album named "B-Sides & Rarities" had an unreachable album page

**Date:** 2026-09-24
**Area:** `helpers/template_filters.py`

## Reported

Selecting *B-Sides & Rarities* from the artist page and opening the album link
loaded a broken page:

```
B-Sides &amp; Rarities
https://…/album/Deftones/B-Sides%2520%2526amp%253B%2520Rarities/2005
```

## Root cause: a Jinja macro's output is already HTML-escaped

`test_site/templates/components/_release_section.html` renders the album name
through a **macro**:

```jinja
{% macro _title_of(album) %}{{ album.album or album.title or '' }}{% endmacro %}
{% set title = _title_of(album)|trim %}
href="/album/{{ artist_name|path_segment }}/{{ title|path_segment }}…"
```

A macro body is rendered with autoescape **on**, so the macro *returns*
`Markup('B-Sides &amp; Rarities')` — the text is already escaped. `path_segment`
then percent-encoded that escaped string, encoding the entity's own `&` and `;`
along with it:

| | segment |
|---|---|
| Reported | `B-Sides%2520%2526amp%253B%2520Rarities` |
| Correct | `B-Sides%2520%2526%2520Rarities` |

The route's `unquote()` yields `B-Sides &amp; Rarities`, which does not match the
stored `B-Sides & Rarities`, so the lookup misses and the page 404s.

⚠️ **Why it hid so well:** `Markup` renders *unescaped*, so the visible page
text read as a perfectly correct "B-Sides & Rarities" and only the href was
broken. The same line in that template proves the asymmetry —
`data-album="{{ title|e }}"` worked, because `|e` on `Markup` is a no-op.

⚠️ **It is not specific to `&`.** Any character autoescape touches (`<`, `>`,
`'`, `"`) would break the same way for the same reason. `B-Sides & Rarities`
was simply the name that surfaced it.

## The fix

`helpers/template_filters.py::_unwrap_markup` recovers the original text from a
`Markup` value with `html.unescape` before encoding, so `path_segment` encodes
the *raw* name. One change at the filter level covers every call site rather
than patching this template.

⚠️ **Only `Markup` instances are unwrapped.** A plain `str` is returned
unchanged, which matters because `path_segment` is also called on raw DB values
throughout the templates. Unwrapping those too would silently rewrite a name
that genuinely contains the literal text `&amp;` — mutation-tested, see below.

`html.unescape` is the right inverse here and the round trip is stable, so a
name that really does contain `&amp;` still works:

| raw | segment | route recovers |
|---|---|---|
| `B-Sides & Rarities` | `B-Sides%2520%2526%2520Rarities` | `B-Sides & Rarities` ✅ |
| `AC/DC` | `AC%252FDC` | `AC/DC` ✅ |
| `B-Sides &amp; Rarities` | `B-Sides%2520%2526amp%253B%2520Rarities` | `B-Sides &amp; Rarities` ✅ |
| `Mötley Crüe` | `M%C3%B6tley%20Cr%C3%BCe` | `Mötley Crüe` ✅ |

`AC/DC` is in there deliberately: the double-encoding exists so a slash stays
inside one segment, and the fix must not disturb it.

## Tests

`tests/test_album_link_html_entity_encoding.py` (**20**):

- the **reproduction** — the shipped template's exact macro shape, asserting the
  segment equals the clean one, is *not* the reported one, and that the route's
  `unquote(unquote(…))` recovers the stored name;
- the premise itself, so if a macro's output ever stops being `Markup` the
  rationale is re-examined rather than silently trusted;
- the filter's round trip across `&`, `/`, accents, apostrophes and `?`; that
  `Markup` and raw input agree; that the slash still stays inside one segment;
- **that the displayed text is not unescaped**, so the fix cannot break the
  rendering it was previously (accidentally) correct about;
- a canary scan for any *other* template feeding macro output to
  `path_segment`.

Mutation-verified, all three caught:

| mutation | result |
|---|---|
| revert to `quote(quote(str(value)))` | 4 tests fail |
| drop the double-encoding | 3 fail (incl. the slash case) |
| unwrap plain strings too | 1 fails — the literal-`&amp;` case |

Sweep: 12 failures across 19 template/album suites, **all pre-existing**, 0 new.
