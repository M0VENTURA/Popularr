"""Jinja2 template filters for Flask.

Registers custom template filters used by the UI templates.
Current filters:
- ``format_duration`` – Convert seconds to ``M:SS`` display format.

Called once during app factory setup.
"""


def _unwrap_markup(value) -> str:
    """Recover the ORIGINAL text from a Jinja ``Markup`` value.

    ⚠️ This is what makes ``path_segment`` safe to apply to a macro's output,
    and it fixes a real, reported bug: an album named "B-Sides & Rarities"
    produced an unreachable album link.

    A Jinja MACRO renders its body with autoescape on, so the macro RETURNS
    ``Markup('B-Sides &amp; Rarities')`` — the text is ALREADY HTML-escaped.
    Applying ``quote()`` to that encodes the entity's own ``&`` and ``;``:

        quote(quote('B-Sides &amp; Rarities'))
            -> 'B-Sides%2520%2526amp%253B%2520Rarities'
        quote(quote('B-Sides & Rarities'))
            -> 'B-Sides%2520%2526%2520Rarities'      # correct

    The route ``unquote()``s that to ``'B-Sides &amp; Rarities'``, which does
    not match the stored ``'B-Sides & Rarities'``, so the album page 404s.

    ⚠️ Why this hides so well: ``Markup`` renders UNESCAPED, so the visible
    page text read as a correct "B-Sides & Rarities" while only the href was
    broken. Nothing looked wrong on screen.

    ⚠️ Use ``html.unescape`` rather than re-deriving the text from the markup.
    The inverse operation is not guessable in general: ``&amp;`` could be a
    literal ampersand written by a user, and re-escaping the result keeps the
    round trip stable, so a name that genuinely contains "&amp;" still works.

    Only ``Markup`` instances are touched. A plain ``str`` is returned
    unchanged, so this cannot alter a value that never went through
    autoescape — which matters because ``path_segment`` is also called on raw
    DB values throughout the templates.
    """
    if value is None:
        return ""
    try:
        from markupsafe import Markup
    except Exception:  # pragma: no cover - markupsafe ships with Jinja2
        return str(value)
    if isinstance(value, Markup):
        from html import unescape
        return unescape(str(value))
    return str(value)


def encode_path_segment(value) -> str:
    """Percent-encode a value for use as a single URL path segment.

    Encodes TWICE: the ASGI server (hypercorn) fully decodes ``%2F`` back to
    a real ``/`` before routing, so a single-encoded slash splits the
    segment and a name like ``AC/DC`` arrives as artist="AC" +
    album="DC/...".  With double-encoding the server decode leaves ``%2F``
    intact, the route keeps it as one segment, and the route's own
    ``unquote()`` restores the raw name.  Unlike ``urlencode`` (which turns
    spaces into ``+``), spaces stay ``%20``.

    Accepts Jinja ``Markup`` as well as ``str`` — see ``_unwrap_markup`` for
    why that matters, and why it is not optional.
    """
    from urllib.parse import quote
    if value is None:
        return ""
    return quote(quote(_unwrap_markup(value), safe=""), safe="")


def register_filters(app):
    """Register all Jinja2 template filters and context processors."""

    @app.context_processor
    def inject_globals():
        """Inject global template variables."""
        return {
            "dist": "/static/dist",
        }

    @app.template_filter('format_duration')
    def format_duration(seconds):
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    @app.template_filter('regex_replace')
    def regex_replace(value, pattern, replacement):
        """Replace all occurrences of *pattern* with *replacement* in *value*."""
        import re
        if not value:
            return ""
        return re.sub(pattern, replacement, str(value))

    @app.template_filter('split')
    def split(value, separator):
        """Split a string on *separator* into a list (``"a/b"|split("/")``)."""
        if value is None:
            return []
        return str(value).split(separator)

    @app.template_filter('safe_id')
    def safe_id(value):
        """Sanitize a value into a CSS/HTML id-safe token.

        Keeps letters, digits, underscore and hyphen; every other character
        (parentheses, slashes, dots, ampersands, ...) becomes an underscore
        and runs are collapsed — so album names like "MMXX (Hypa Hypa
        Edition)" yield valid selectors instead of breaking
        ``querySelector('#collapse-...')``.
        """
        import re
        if value is None:
            return ""
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value))
        return re.sub(r"_+", "_", cleaned).strip("_")

    @app.template_filter('split_artist_collabs')
    def split_artist_collabs(value):
        """Split collaboration artist strings into individual artist names.
        
        Handles ``feat.``, ``ft.``, ``featuring``, ``w/`` delimiters.
        Used by track_detail.html and artist_detail.html to create
        separate artist links for collaborative tracks.
        """
        import re
        if not value:
            return []
        parts = re.split(
            r'\s+(?:w/|feat\.?|ft\.?|featuring)\s+',
            str(value), flags=re.IGNORECASE,
        )
        cleaned = [p.strip() for p in parts if p and p.strip()]
        return cleaned or [str(value).strip()]

    @app.template_filter('path_segment')
    def path_segment(value):
        """Percent-encode a value for use as a single URL path segment.

        See :func:`encode_path_segment` — the double encoding is required so
        names containing slashes (``AC/DC``) survive the ASGI server's path
        decode as one route segment.
        """
        return encode_path_segment(value)

    @app.template_filter('escapejs')
    def escapejs(value):
        """Escape strings for safe embedding in JavaScript contexts.
        
        Escapes quotes, backslashes, newlines, and HTML-special characters
        to prevent XSS when injecting user data into ``<script>`` blocks.
        """
        if value is None:
            return ''
        value = str(value)
        escapes = {
            '\\': '\\\\',
            "'": "\\'",
            '"': '\\"',
            '\n': '\\n',
            '\r': '\\r',
            '\t': '\\t',
            '\b': '\\b',
            '\f': '\\f',
            '<': '\\u003C',
            '>': '\\u003E',
            '&': '\\u0026',
        }
        for char, escape in escapes.items():
            value = value.replace(char, escape)
        return value

    @app.template_filter('title_case')
    def title_case(value):
        """Display-style title casing without rewriting stored metadata.

        Lowercases every word, then capitalises the first letter of the first
        and last words plus all major words, keeping small function words
        (of, the, and, to, ...) lowercase — "the cost of giving up" becomes
        "The Cost of Giving Up". Used for hero headers only; raw tags are
        never rewritten.
        """
        if not value:
            return ''
        small_words = {
            'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in',
            'into', 'nor', 'of', 'off', 'on', 'or', 'per', 'the', 'to', 'up',
            'vs', 'with',
        }
        words = str(value).split()
        if not words:
            return str(value)
        out = []
        for idx, word in enumerate(words):
            lowered = word.lower()
            if idx == 0 or idx == len(words) - 1 or lowered not in small_words:
                for pos, ch in enumerate(lowered):
                    if ch.isalpha():
                        lowered = lowered[:pos] + ch.upper() + lowered[pos + 1:]
                        break
            out.append(lowered)
        return ' '.join(out)

    @app.template_filter('artist_sort_name')
    def artist_sort_name_filter(value):
        """File an artist name with a leading "The" moved to the end.

        ``"The Offspring"`` → ``"Offspring, The"``, so a list reads in the order
        it sorts.  Names without a leading "The" — including "Theatre of
        Tragedy" and "Theodore" — pass through untouched.

        DISPLAY ONLY.  Never feed the result to ``url_for``, an API parameter or
        a database write: the artist's identity is the raw name, and rewriting
        it would break images, links and matching.  See ``helpers/artist_sort``
        for the sort key and section letter that go with this label.
        """
        from helpers.artist_sort import artist_sort_name
        return artist_sort_name(value)

    # ----------------------------------------------------------------------
    # Tests
    # ----------------------------------------------------------------------
    # ``artist_detail.html`` uses ``selectattr('sources', 'contains', src)``
    # and Jinja has no built-in ``contains`` test, so register one.
    @app.template_test('contains')
    def contains_test(container, item):
        """Return True when *item* is found in *container*.

        Mirrors Python's ``item in container`` (usable as ``is contains``
        or as a ``selectattr`` test).
        """
        if container is None:
            return False
        return item in container