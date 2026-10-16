"""Hermetic Jinja rendering over the rebuilt (test_site) tree.

WHY THIS EXISTS
---------------
Suites that needed to render ``test_site/templates/base.html`` used to swap
``app.jinja_loader`` for the duration of the render and restore it afterwards.
That technique is ORDER-DEPENDENT and fails silently: the test app's Jinja
environment is created once and reused for the whole session, so in a full-suite
sweep the swap does not take effect, the LIVE tree is rendered instead, and the
assertion fails with a confusing "no bottom nav" rather than "wrong tree".

``test_test_site_mobile_and_pwa.TestBottomNavActiveState`` and
``test_mobile_chrome_redesign.TestTheMobileTitle`` both used it and both failed
in a sweep while passing in isolation — which is the fingerprint. The state
guard those tests provide (which bottom-nav tab lights up, and the page heading)
was therefore not actually running, so a regression in the shell would have gone
unnoticed.

This module owns its own ``Environment``, so nothing another test does to the
app can change what these renders produce. It is intentionally NOT named
``test_*.py`` — pytest.ini collects ``test_*.py`` only — so it is a helper, not
a suite.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from helpers.template_filters import encode_path_segment, page_heading

#: Repo root (tests/…).
REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_SITE_TEMPLATES = REPO_ROOT / "test_site" / "templates"


class _RequestForPath:
    """Just enough of ``request`` for the shell's server-side decisions."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.url = path
        self.args = {}


class _EmptyConfig:
    """``config.get(...)`` returns the caller's default, like a bare config."""

    def get(self, _key, default=None):
        return default


def _passthrough(value, *args, **kwargs):
    return value


def build_env(path: str = "/") -> Environment:
    """A standalone environment rendering the rebuilt tree as *path*."""
    env = Environment(
        loader=FileSystemLoader(str(TEST_SITE_TEMPLATES)),
        autoescape=True,
    )
    # The app registers these in helpers/template_filters.py; a standalone env
    # has to add the ones the shell actually uses. Unknown filters raise at
    # render time rather than silently rendering nothing, which is the behaviour
    # we want — a missing filter should be loud.
    #
    # ⚠️ `path_segment` uses the REAL implementation, not a pass-through. It
    # percent-encodes a name TWICE (see its docstring: the ASGI server decodes
    # `%2F` before routing, so a single encoding splits a path). A pass-through
    # stub therefore renders a URL with raw spaces and `&` in it, and a probe
    # reading that cannot tell a correct URL from a broken one. Tests that stub
    # the thing under test prove nothing about it.
    env.filters["page_heading"] = page_heading
    env.filters["title_case"] = _passthrough
    env.filters["path_segment"] = encode_path_segment
    env.filters["artist_sort_name"] = _passthrough
    env.filters["format_duration"] = lambda value: str(value)
    env.globals.update(
        url_for=lambda endpoint, **kw: f"/{endpoint}",
        request=_RequestForPath(path),
        session={"username": "tester"},
        config=_EmptyConfig(),
        get_flashed_messages=lambda *a, **k: [],
        versioned_static=lambda name: f"/static/{name}",
        custom_bookmark_links=[],
    )
    return env


def render_base(path: str = "/") -> str:
    """Render the rebuilt ``base.html`` as *path*."""
    return build_env(path).get_template("base.html").render()


class _CapturingApp:
    """A Flask-shaped shim that records what ``register_filters`` installs.

    Only the three hooks that module actually uses are implemented — add one
    when it starts using another, so the failure is a missing attribute here
    rather than a silently unregistered filter.
    """

    def __init__(self) -> None:
        self.filters: dict = {}
        self.tests: dict = {}
        self.processors: list = []

    def template_filter(self, name):
        def decorator(fn):
            self.filters[name] = fn
            return fn

        return decorator

    def template_test(self, name):
        def decorator(fn):
            self.tests[name] = fn
            return fn

        return decorator

    def context_processor(self, fn):
        self.processors.append(fn)
        return fn


def install_real_filters(env: Environment, *names: str) -> Environment:
    """Install the application's REAL filters called *names* into *env*.

    ``build_env`` registers only the handful of filters the app shell needs, but
    a PAGE can use more — ``album_detail.html`` calls ``|escapejs`` for the
    arguments it embeds in ``onclick`` handlers. An unregistered filter is a
    template *compile* error, so the whole page raised
    ``No filter named 'escapejs'`` and rendered nothing at all, which is what a
    test trying to render a real page runs into first.

    The implementations come from ``register_filters`` rather than being
    redefined here, for the reason in this module's docstring: a hand-written
    stand-in quietly stops agreeing with the real filter, and then the test
    proves things about the stand-in.
    """
    from helpers.template_filters import register_filters

    app = _CapturingApp()
    register_filters(app)

    unknown = [name for name in names if name not in app.filters]
    if unknown:
        raise AssertionError(
            f"register_filters no longer provides {unknown}; it has "
            f"{sorted(app.filters)}"
        )

    for name in names:
        env.filters[name] = app.filters[name]
    return env


def render_child(child_source: str, path: str = "/") -> str:
    """Render a child template that extends the rebuilt ``base.html``."""
    return build_env(path).from_string(child_source).render()
