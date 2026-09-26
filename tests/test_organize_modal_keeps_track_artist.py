"""Guard: the Organize modal must NOT prefill the per-track Artist field with
the ALBUM artist.

Reported symptom: "When adding an album to the download queue, if it's a various
artist album, its adding the artist as Various Artist. Each track should have
the track artist."

Root cause (UI half): ``openOrganizeGroupModal`` set BOTH the per-track Artist
field and the Album Artist field from ``group.sublabel``, and ``sublabel`` is
``item.album_artist || item.artist`` — i.e. the ALBUM artist.  On a compilation
that put "Various Artists" in the track-artist box, and the server applies that
value to every track.

These probes exercise the REAL shipped functions (extracted from source), not a
reimplementation, so they cannot drift from the code under test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

TREES = {
    "live": REPO / "static" / "js" / "downloads.js",
    "rebuilt": REPO / "test_site" / "static" / "js" / "pages" / "download-queue.js",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(js: str) -> str:
    """Remove /* */ and // comments, preserving string literals.

    Necessary because these files DOCUMENT the traps they avoid, and the
    comments quote the very identifiers the assertions search for.
    """
    out = []
    i = 0
    n = len(js)
    quote = None
    while i < n:
        ch = js[i]
        if quote:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(js[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "*":
            j = js.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append("\n")
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "/":
            j = js.find("\n", i)
            i = n if j == -1 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_function(js: str, name: str) -> str:
    """Return ``function <name>(...) { ... }`` INCLUDING its signature.

    The signature matters: the Node probes in this module call the extracted
    function by name, so returning only the brace-delimited body would produce
    a bare block (a syntax error) instead of a callable declaration.
    """
    match = re.search(
        r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        js,
    )
    if not match:
        pytest.fail(f"function {name} not found")
    head_start = match.start()
    start = match.end() - 1
    depth = 0
    quote = None
    i = start
    while i < len(js):
        ch = js[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js[head_start:i + 1]
        i += 1
    pytest.fail(f"unbalanced braces extracting {name}")


def _run_node(source: str, request: dict, tmp_path) -> dict:
    """Run ``source`` plus a call in Node, returning the JSON it printed."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    script = (
        source
        + "\nvar __out = dominantTrackArtist("
        + json.dumps(request)
        + ");\nconsole.log(JSON.stringify({ result: __out }));\n"
    )
    target = tmp_path / "probe.js"
    target.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [node, str(target)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("tree", sorted(TREES))
class TestOrganizePrefillKeepsPerTrackArtist:
    def test_the_track_field_is_not_filled_from_sublabel(self, tree):
        code = _strip_comments(_source(TREES[tree]))
        body = _extract_function(code, "openOrganizeGroupModal")
        assert not re.search(
            r"orgArtist\.value\s*=\s*group\.sublabel", body
        ), (
            "the per-track Artist field is filled from group.sublabel, which is "
            "the ALBUM artist — a VA compilation would stamp every track with "
            "'Various Artists'"
        )

    def test_the_track_field_uses_the_per_track_artist(self, tree):
        code = _strip_comments(_source(TREES[tree]))
        body = _extract_function(code, "openOrganizeGroupModal")
        assert "dominantTrackArtist(" in body, (
            "the per-track Artist field must be derived from the tracks' own "
            "artist column"
        )
        assert re.search(r"orgArtist\.value\s*=\s*_trackArtist", body)

    def test_the_album_artist_field_still_uses_the_album_artist(self, tree):
        code = _strip_comments(_source(TREES[tree]))
        body = _extract_function(code, "openOrganizeGroupModal")
        assert re.search(r"orgAlbumArtist\.value\s*=\s*group\.sublabel", body), (
            "the Album Artist field must KEEP the album artist"
        )

    def test_dominant_track_artist_prefers_the_most_common_track_artist(
        self, tree, tmp_path
    ):
        body = _extract_function(_source(TREES[tree]), "dominantTrackArtist")
        payload = _run_node(body, {
            "sublabel": "Various Artists",
            "items": [
                {"artist": "Band Alpha"},
                {"artist": "Singer Beta"},
                {"artist": "Band Alpha"},
            ],
        }, tmp_path)
        assert payload["result"] == "Band Alpha", (
            "the modal must prefill the tracks' own (majority) artist, not the "
            f"album artist; got {payload['result']!r}"
        )

    def test_dominant_track_artist_falls_back_to_the_album_artist(
        self, tree, tmp_path
    ):
        body = _extract_function(_source(TREES[tree]), "dominantTrackArtist")
        payload = _run_node(body, {
            "sublabel": "Various Artists",
            "items": [{}, {"artist": ""}],
        }, tmp_path)
        assert payload["result"] == "Various Artists"
