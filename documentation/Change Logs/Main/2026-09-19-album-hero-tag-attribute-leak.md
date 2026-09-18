# Album page: `class="..."` leaking as visible text (malformed hero tag)

**Date:** 2026-09-19
**Area:** `ui` (templates)

## Reported symptom

> This is sometimes showing on the top of the album page
>
> ```
>  Hardwired… to Self‐Destruct
> class="d-flex align-items-start gap-3 mb-3">
> ```

A raw CSS class string rendered as page text directly beneath the album title.

## Root cause

In `test_site/templates/Pages/album_detail.html`, the edition-tagline block had
been spliced **into the middle of the hero `<div>`'s opening tag**:

```jinja
      <div{# Edition tagline: the specific release this collection actually
             holds, plus its own year when that differs from the original.
             Rendered only when it adds information (see _show_rel_* above). #}
          {% if _show_rel_title or _show_rel_year %}
            <div class="small text-muted text-truncate album-hero-release mb-1">
              ...
            </div>
          {% endif %}
           class="d-flex align-items-start gap-3 mb-3">
        <div class="flex-shrink-0">
```

The tag name is `<div`, then a Jinja comment, then a `{% if %}` whose body emits
a **real `<div>` element**, and only then the outer tag's own `class=` attribute
and closing `>`.

When `_show_rel_title or _show_rel_year` is **true** the browser sees:

```html
<div
   <div class="... album-hero-release ...">...</div>
    class="d-flex align-items-start gap-3 mb-3">
```

The outer `div` never terminates, so `class="d-flex ..."` is parsed as text and
rendered. When the condition is **false** the inner block emits nothing, the
`>` closes the tag normally, and the page looks correct — which is exactly why
the report says "**sometimes**". `Hardwired… to Self-Destruct` is a deluxe
edition, so it carries a `release_title` that differs from the album name and
the condition evaluated true.

Note that a plain Jinja *syntax* check does not catch this: the template is
perfectly valid Jinja. It only breaks once rendered.

## Fix

Restored the hero opening tag and moved the tagline to its correct position
after the `<h1>` — the same structure the live tree
(`templates/pages/album_detail.html`) already had:

```jinja
      <div class="d-flex align-items-start gap-3 mb-3">
        <div class="flex-shrink-0">
          ...
          <h1 class="h5 fw-bold mb-1 album-hero-title text-truncate">…</h1>
          {# Edition tagline: … #}
          {% if _show_rel_title or _show_rel_year %}
            <div class="small text-muted text-truncate album-hero-release mb-1">…</div>
          {% endif %}
          <div class="small text-muted mb-1 album-hero-sub">
```

No markup is lost — the tagline still renders, just as a sibling of the title
rather than inside the tag's attributes.

## Audit — the live tree was unaffected

A repo-wide scan of all 107 shipping templates (`templates/` +
`test_site/templates/`, excluding the frozen `old_system/`) found **exactly one**
instance of this defect: the file above. Only the rebuilt tree was affected.

The scan deliberately does **not** flag the legitimate conditional-attribute
idiom used in several templates:

```jinja
<tr{% if track_is_queued %} class="table-secondary opacity-75"{% endif %}>
<div{% if is_missing %} data-x="1"{% endif %}>
```

Those emit an *attribute*, not an element, and parse correctly.

## Tests

`tests/test_templates_tag_attribute_integrity.py` — 110 cases.

- `test_no_element_markup_inside_a_tag` — parametrised over every template in
  both shipping trees. Flags a tag whose name is followed by Jinja and whose
  body up to the closing `>` contains a nested element open (`<tag` followed by
  whitespace, `>` or `/`).
- `test_album_detail_hero_tag_is_well_formed` — pins the reported regression
  specifically, asserting the hero tag exists intact, that no bare `class=` line
  remains, and that the tagline markup was not lost by the fix.
- `test_detector_flags_the_reported_shape` — a **self-test** so the detector
  cannot be hollowed out: it must flag the real defect and must NOT flag four
  legal shapes (conditional attribute, Jinja in a value, a multi-line
  conditional attribute, and an `<html` fragment inside a JS string literal —
  the last being a genuine false positive that cost a detection round-trip).

**Oracle verified:** with the broken template restored from `origin/develop` and
the tests kept, the suite fails (`2 failed, 108 passed`); with the fix applied
it passes (`110 passed`).

## Follow-up worth noting

The same "sometimes" failure mode applies to any Jinja logic placed between a
tag name and its attributes, and it is invisible to both Jinja syntax checks and
HTML linters that do not render. `test_detector_flags_the_reported_shape`
guards the detector, so this class of defect now fails loudly in CI.
