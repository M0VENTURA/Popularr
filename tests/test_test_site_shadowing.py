"""The template loader is case-insensitive (Linux) and single-tree.

The old shadow list existed to refuse a bad rebuilt copy and fall back to the
live tree. There is no live tree now, so shadowing is gone; what remains — and
what Linux depends on — is case-insensitive resolution of ``pages/…`` to
``Pages/…``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, TemplateNotFound

from helpers.test_site_mode import CaseInsensitiveFileSystemLoader


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "templates"
    root.mkdir(parents=True)
    _write(root, "base.html", "BASE")
    _write(root, "Pages/downloads/monitor.html", "MONITOR")
    return root


def test_lowercase_lookup_resolves_capitalised_directory(tree: Path):
    env = Environment(loader=CaseInsensitiveFileSystemLoader(str(tree)))
    assert env.get_template("pages/downloads/monitor.html").render() == "MONITOR"


def test_exact_lookup_still_works(tree: Path):
    env = Environment(loader=CaseInsensitiveFileSystemLoader(str(tree)))
    assert env.get_template("Pages/downloads/monitor.html").render() == "MONITOR"


def test_a_missing_template_still_raises(tree: Path):
    env = Environment(loader=CaseInsensitiveFileSystemLoader(str(tree)))
    with pytest.raises(TemplateNotFound):
        env.get_template("pages/downloads/not-there.html")


def test_the_loader_is_single_tree():
    """There is no fallback loader: the served tree is the only source."""
    import helpers.test_site_mode as mode

    assert not hasattr(mode, "_SHADOWED_TEMPLATES")
    assert not hasattr(mode, "LIVE_TEMPLATES")
