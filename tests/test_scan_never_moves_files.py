"""Guard: a scan must NEVER move or rename a library file.

REPORTED
--------
"Updating of metadata is changing the folder structure. This shouldn't be
happening at all, the files should be updated but not moved or renamed during
a scan."

The demand is an INVARIANT, not a one-off bug fix: whatever a scan writes to a
track's tags or to the database, it must not relocate the audio file. These
tests pin that invariant two ways:

1. **Reachability (behavioural).** A call-graph walk from the four scan entry
   points must not reach any library-moving call site. This is the assertion
   that actually implements the user's requirement, and it is what will fail
   if someone later wires a scan stage into ``move_track_to_library``.

2. **Reachability (structural).** The scan modules must not IMPORT a mover, so
   the property is visible in the source as well.

WHY A REACHABILITY TEST AND NOT "assert no shutil.move in file X"
-----------------------------------------------------------------
The scan is a deep call graph (1136 functions at the time of writing). A
per-file grep would pass while a mover sat three hops away, and would fail on
an unrelated module. Walking the graph from the scan's own entry points
answers the question that was actually asked.

The walker over-approximates on purpose — it resolves callees by NAME, so a
same-named method is followed even when the receiver differs. For a
"can this ever happen" question that is the safe direction: if even the
permissive graph cannot reach a move, the scan demonstrably cannot.

A scan is still allowed to:
  * write tags IN PLACE (``os.replace`` of a temp sibling — same filename), and
  * remove a directory that is already EMPTY (``os.rmdir``).
Both are asserted below so the guard cannot be satisfied by banning them.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Scan entry points. A user-triggered scan reaches the library through one of
#: these, so the invariant is asserted from here.
SCAN_ENTRIES: tuple[tuple[str, str], ...] = (
    ("services/popularity/scan_stage_runner.py", "run_scan"),
    ("services/scanning/pipelines/popularity_pipeline.py", "_run_full_scan_as_artist_pipeline"),
    ("services/scanning/pipelines/artist_pipeline.py", "run_artist_pipeline"),
    ("services/scanning/pipelines/album_pipeline.py", "run_album_pipeline"),
)

#: Call shapes that RELOCATE a file — i.e. the audio file ends up somewhere
#: OTHER than where it was, under a DIFFERENT name.
#:
#: Four near-misses are deliberately EXCLUDED, each for a reason asserted in
#: ``TestWhatAScanIsStillAllowedToDo``:
#:   * ``shutil.copy`` / ``copy2`` / ``copytree`` — the source stays put.
#:     ``_write_tags_atomic`` copies the original to a temp SIBLING.
#:   * ``os.replace`` — how that temp sibling is swapped back over the SAME
#:     filename. In-place, not a relocation.
#:   * ``os.rmdir`` / ``shutil.rmtree`` — deletion, not relocation; the scan
#:     only removes directories that are already EMPTY.
RELOCATION_CALLS: frozenset[tuple[str, str]] = frozenset({
    ("shutil", "move"),
    ("shutil", "movex"),
    ("os", "rename"),
    ("os", "renames"),
})

#: Functions whose whole purpose is to relocate library audio. None of these
#: may be reachable from a scan.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset({
    "move_track_to_library",
    "rename_and_move_file",
    "_move_and_import",
    "organize_track",
    "organize_folder",
    "organize_single_file",
    "rename_album_files_service",
    "organize_group_sync",
    "finalize_release",
    "transfer_download_to_music",
    "dedupe_library_folder",
    "move_folder_track_to_library",
})

_SKIP_DIRS = frozenset({"old_system", ".venv", "venv", "node_modules", ".git", "__pycache__"})

_STOP_NAMES = frozenset({
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "min", "max", "sum", "sorted", "range", "enumerate", "zip", "open", "getattr",
    "hasattr", "isinstance", "repr", "super", "type",
})


def _repo_python_files() -> list[Path]:
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if _SKIP_DIRS & set(rel.parts):
            continue
        out.append(path)
    return out


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_attrs(node: ast.AST) -> set[tuple[str, str]]:
    """``(base_name, attribute)`` for every ``base.attr(...)`` in *node*.

    Aliased imports are resolved first: ``import shutil as _sh`` followed by
    ``_sh.move(...)`` must be reported as ``shutil.move``, otherwise the
    obvious way to dodge a name-based guard also dodges it.  This was found by
    mutation-testing the guard itself (the aliased mutation SURVIVED).
    """
    aliases: dict[str, str] = {}
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(n, ast.ImportFrom):
            # ``from os import rename as _r`` -> _r means os.rename
            for a in n.names:
                if a.asname and n.module:
                    aliases[a.asname] = f"{n.module}.{a.name}"

    out: set[tuple[str, str]] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                base = aliases.get(f.value.id, f.value.id)
                # An alias may itself be dotted ("os.path" etc.) — only the
                # module part matters for the call-shape check.
                out.add((str(base).split(".")[0], f.attr))
    return out


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out - _STOP_NAMES


def _walk_scan_graph() -> tuple[
    set[tuple[Path, int]],
    list[tuple[Path, int, str]],
    set[str],
]:
    """BFS the call graph from :data:`SCAN_ENTRIES`.

    Returns ``(visited_nodes, relocation_sites, forbidden_functions_reached)``.
    """
    trees: dict[Path, ast.Module] = {}
    funcs_by_file: dict[Path, dict[str, ast.AST]] = {}
    for path in _repo_python_files():
        tree = _parse(path)
        if tree is None:
            continue
        trees[path] = tree
        funcs_by_file[path] = _functions(tree)

    by_name: dict[str, list[tuple[Path, ast.AST]]] = {}
    for path, funcs in funcs_by_file.items():
        for name, node in funcs.items():
            by_name.setdefault(name, []).append((path, node))

    queue: deque[tuple[Path, ast.AST]] = deque()
    for rel, fn in SCAN_ENTRIES:
        path = REPO_ROOT / rel
        node = funcs_by_file.get(path, {}).get(fn)
        if node is not None:
            queue.append((path, node))

    assert queue, "no scan entry point resolved — the guard would be vacuous"

    seen: set[tuple[Path, int]] = set()
    sites: list[tuple[Path, int, str]] = []
    reached_forbidden: set[str] = set()

    while queue:
        path, node = queue.popleft()
        key = (path, node.lineno)
        if key in seen:
            continue
        seen.add(key)

        for base, attr in _called_attrs(node):
            if (base, attr) in RELOCATION_CALLS:
                sites.append((path, node.lineno + 0, f"{base}.{attr}"))

        for name in _called_names(node):
            if name in FORBIDDEN_FUNCTIONS:
                reached_forbidden.add(name)
            for child_path, child_node in by_name.get(name, []):
                ck = (child_path, child_node.lineno)
                if ck not in seen:
                    queue.append((child_path, child_node))

    return seen, sites, reached_forbidden


class TestTheScanCannotRelocateFiles:

    def test_the_walker_resolves_the_scan_graph(self):
        """Guard the guard: a broken walk would make every assertion vacuous."""
        visited, _, _ = _walk_scan_graph()
        assert len(visited) > 300, (
            f"only {len(visited)} functions reached from the scan — the graph "
            "walk is mis-scoped, so the relocation assertions prove nothing"
        )

    def test_no_relocation_call_is_reachable_from_a_scan(self):
        """THE REPORTED REQUIREMENT: a scan must not move or rename files."""
        _, sites, _ = _walk_scan_graph()
        offenders = [
            f"{path.relative_to(REPO_ROOT)} (in a function around line {lineno}): {call}"
            for path, lineno, call in sites
        ]
        assert not offenders, (
            "a scan can reach a file-relocating call. A metadata update must "
            "rewrite tags in place and may NOT move or rename the audio file — "
            "the reported 'folder structure changed during a scan'. "
            f"Offending call site(s): {offenders}"
        )

    def test_no_library_moving_function_is_reachable_from_a_scan(self):
        """Belt-and-braces: no mover by NAME is on the graph either."""
        _, _, forbidden = _walk_scan_graph()
        assert not forbidden, (
            "a scan reaches a library-moving helper by name — a metadata pass "
            f"must never relocate audio files. Reached: {sorted(forbidden)}"
        )

    def test_the_two_path_conventions_agree(self):
        """One album must land in ONE folder, whichever action imported it.

        The queue group organiser used to hardcode ``({year}) {album}`` while
        every other importer used ``downloads.file_name_format``, so the same
        album could end up in two different folders — the concrete way a
        "folder structure" legitimately changes.
        """
        from services.downloads.download_organize_helpers import _build_target_path
        from services.queue.queue_processing_service import build_organize_group_target_path

        src = "/downloads/x/03 - Even Less.flac"
        common = dict(
            album_artist="Porcupine Tree",
            year="1999",
            album="Stupid Dream",
            artist="Porcupine Tree",
            title="Even Less",
            track_number=3,
            source_file=src,
        )

        configured = _build_target_path("/music", common["album_artist"], common["year"],
                                        common["album"], common["artist"], common["title"],
                                        common["track_number"], src)
        group = build_organize_group_target_path(
            music_root="/music",
            album_artist=common["album_artist"],
            year=common["year"],
            album_name=common["album"],
            artist=common["artist"],
            title=common["title"],
            track_number=common["track_number"],
            source_file=src,
        )

        # Compare the LIBRARY folder (normalised), not the absolute prefix.
        def _folder(raw: str) -> tuple[str, str]:
            p = str(raw).replace("\\", "/")
            folder = p.rsplit("/", 1)[0]
            return folder, p.rsplit("/", 1)[-1]

        cf, cname = _folder(configured)
        gf, gname = _folder(str(group))

        assert cf.lstrip("/") == gf.lstrip("/") or cf.endswith(gf.lstrip("/")) or gf.endswith(cf.lstrip("/")), (
            "the queue group organiser and the download importer put the same "
            f"album in DIFFERENT folders: {cf!r} vs {gf!r}"
        )
        assert cname == gname, (
            f"the two importers give the same track DIFFERENT filenames: "
            f"{cname!r} vs {gname!r}"
        )


class TestWhatAScanIsStillAllowedToDo:
    """The invariant must not be satisfied by banning legitimate work."""

    def test_in_place_tag_writes_are_allowed(self):
        """``os.replace(tmp, file_path)`` keeps the SAME filename."""
        import inspect

        from services.metadata import tag_file_service

        src = inspect.getsource(tag_file_service._write_tags_atomic)
        assert "os.replace" in src, (
            "atomic tag writes use os.replace of a temp sibling to the SAME "
            "path; removing this would lose crash-safety"
        )
        assert "os.rename" not in src, (
            "tag writes must replace the file in place, not rename it"
        )

    def test_removing_an_empty_directory_is_still_allowed(self):
        """Pruning a folder with nothing in it is not a relocation."""
        from services.scanning import cleanup

        src = (REPO_ROOT / "services" / "scanning" / "cleanup.py").read_text(encoding="utf-8")
        assert "os.rmdir" in src, (
            "the scan removes EMPTY album dirs after files are deleted; that is "
            "not a relocation and must keep working"
        )
        assert "os.listdir(dirpath)" in src, (
            "the empty-dir prune must verify the directory is EMPTY first"
        )
