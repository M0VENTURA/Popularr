"""One log file, SEVERAL writer processes — rotation must not silence anyone.

Reported: the Scanner tab of the System Logs modal *\"shows sometimes, but seems
to break when multiple scans run at the same time\"* while the info log keeps
filling in correctly.

Popularr does not have a single process writing its logs:

* ``app.py`` calls ``setup_logging("WebUI")`` and ``entrypoint.sh`` runs it under
  ``hypercorn --workers 4``, so every worker configures its own handlers;
* ``services.queue.queue_worker`` calls ``setup_logging("QueueWorker")`` in a
  separate process.

All of them attach the root logger to the same ``unified_scan.log`` /
``info.log`` / ``debug.log`` / ``error.log``. The stock
``logging.handlers.RotatingFileHandler`` **renames** the live file to ``.1`` when
it rotates, so every other writer is left holding a descriptor on the renamed
inode: from then on its lines land in ``.1`` and never reach a reader that opens
the live file by name. When that orphaned writer rotates in turn it renames the
*new* live file over ``.1``, destroying the other process's history.

The second defect is the rotation trigger: stock trusts ``stream.tell()``, which
is only this process's byte count. ``entrypoint.sh`` copytruncates some logs, so
that counter can be large while the file on disk is empty — and the handler then
rotates a nearly-empty file over a good backup.

Both are pinned here behaviourally. The two handlers used below are separate
objects with separate descriptors, which is exactly what two processes have.
"""
from __future__ import annotations

import glob
import logging
from pathlib import Path

from helpers.logging_config import (
    SharedRotatingFileHandler,
    _build_logging_config,
    _rotate_lock_path,
)

SHARED_HANDLER = "helpers.logging_config.SharedRotatingFileHandler"


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "services.demo", logging.INFO, "demo.py", 10, message, None, None
    )


def _handler(path: Path, max_bytes: int, backup_count: int = 3) -> SharedRotatingFileHandler:
    handler = SharedRotatingFileHandler(
        str(path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


class TestTheConfigUsesTheSharedHandler:
    """Reverting to the stock handler re-introduces the bug silently."""

    def test_every_file_handler_is_the_shared_class(self):
        config = _build_logging_config("WebUI", "/logs", use_structlog=False)
        handlers = config["handlers"]
        assert handlers, "the logging config must define handlers"
        for name, spec in handlers.items():
            assert spec.get("class") == SHARED_HANDLER, (
                f"handler {name} uses {spec.get('class')!r}; several processes "
                f"write these files, so it must be {SHARED_HANDLER}"
            )

    def test_the_stock_handler_is_not_configured_anywhere(self):
        config = _build_logging_config("WebUI", "/logs", use_structlog=False)
        classes = [spec.get("class", "") for spec in config["handlers"].values()]
        assert not any(cls == "logging.handlers.RotatingFileHandler" for cls in classes)

    def test_the_shared_handler_really_is_a_rotating_handler(self):
        assert issubclass(SharedRotatingFileHandler, logging.handlers.RotatingFileHandler)


class TestRotationDecidedByTheSizeOnDisk:
    def test_an_external_truncate_does_not_look_like_a_full_file(self, tmp_path):
        """entrypoint.sh copytruncates; stream.tell() would still say "full"."""
        path = tmp_path / "app.log"
        handler = _handler(path, max_bytes=100)
        try:
            handler.emit(_record("A" * 40))  # this process has now written 40 bytes
            path.write_text("", encoding="utf-8")  # external copytruncate
            assert handler.shouldRollover(_record("x")) is False

            path.write_text("B" * 150, encoding="utf-8")  # genuinely over the cap
            assert handler.shouldRollover(_record("x")) is True
        finally:
            handler.close()

    def test_a_zero_cap_means_never_rotate(self, tmp_path):
        path = tmp_path / "capped_off.log"
        handler = _handler(path, max_bytes=0)
        try:
            handler.emit(_record("payload"))
            assert handler.shouldRollover(_record("x")) is False
        finally:
            handler.close()


class TestOneWriterCannotSilenceAnother:
    def test_a_rotation_leaves_the_other_writers_recording_to_the_live_file(
        self, tmp_path
    ):
        path = tmp_path / "shared.log"
        writer_a = _handler(path, max_bytes=120)
        writer_b = _handler(path, max_bytes=120)
        try:
            # Grow A until it rotates at least once.
            for _ in range(6):
                writer_a.emit(_record("A" * 40))
            assert (tmp_path / "shared.log.1").exists(), "A never rotated — test is vacuous"

            writer_b.emit(_record("FROM_B"))

            live = _text(path)
            assert "FROM_B" in live, (
                "the second writer's line is missing from the live file — its "
                "descriptor followed the renamed file, so the Scanner tab would "
                "show nothing from that process"
            )
        finally:
            writer_a.close()
            writer_b.close()

    def test_both_writers_survive_several_rotations(self, tmp_path):
        path = tmp_path / "both.log"
        writer_a = _handler(path, max_bytes=200)
        writer_b = _handler(path, max_bytes=200)

        def visible() -> str:
            return _text(path) + "".join(
                _text(Path(p)) for p in sorted(glob.glob(str(path) + ".*"))
            )

        try:
            for i in range(12):
                writer_a.emit(_record(f"A{i:02d}" + "x" * 30))
                writer_b.emit(_record(f"B{i:02d}" + "y" * 30))
                # Every line either writer just produced must be readable —
                # a rotation between the two writes may have moved the first
                # one into a backup, but it must never be lost outright.
                seen = visible()
                assert f"A{i:02d}" in seen, f"A{i:02d} lost at iteration {i}"
                assert f"B{i:02d}" in seen, f"B{i:02d} lost at iteration {i}"

            live = _text(path)
            assert "A11" in live and "B11" in live, live[-400:]
        finally:
            writer_a.close()
            writer_b.close()


class TestAnExternalTruncateDoesNotEatTheBackups:
    def test_a_nearly_empty_file_is_not_rotated_over_a_good_backup(self, tmp_path):
        """The shell rotator copytruncates; stock would then rotate a stub."""
        path = tmp_path / "app.log"
        (tmp_path / "app.log.1").write_text("PRECIOUS\n", encoding="utf-8")

        handler = _handler(path, max_bytes=100)
        try:
            handler.emit(_record("X" * 60))          # this process: 60 bytes written
            path.write_text("", encoding="utf-8")    # external copytruncate
            handler.emit(_record("Y" * 60))          # stale counter says 120 >= 100
            handler.emit(_record("Z"))
        finally:
            handler.close()

        assert "PRECIOUS" in _text(tmp_path / "app.log.1"), (
            "the backup was displaced by rotating a file that is only a few "
            "bytes long on disk"
        )
        assert not (tmp_path / "app.log.2").exists(), (
            "a rotation happened even though the live file was far below the cap"
        )


class TestTheFileStaysBounded:
    def test_it_rotates_keeps_backups_and_never_grows_without_bound(self, tmp_path):
        path = tmp_path / "bounded.log"
        handler = _handler(path, max_bytes=500, backup_count=2)
        try:
            for _ in range(60):
                handler.emit(_record("N" * 50))
        finally:
            handler.close()

        size = path.stat().st_size
        assert size <= 550, f"live file grew to {size} bytes past a 500 byte cap"
        assert (tmp_path / "bounded.log.1").exists(), "no backup was kept"
        assert (tmp_path / "bounded.log.2").exists(), "backups did not chain"

    def test_the_backup_holds_the_previous_content(self, tmp_path):
        path = tmp_path / "chained.log"
        handler = _handler(path, max_bytes=200, backup_count=1)
        try:
            # Big enough on its own to leave the file over the cap, so the very
            # next record rotates it into the single backup slot.
            handler.emit(_record("FIRST-CHUNK" + "a" * 200))
            handler.emit(_record("SECOND-CHUNK" + "b" * 40))
        finally:
            handler.close()
        assert "FIRST-CHUNK" in _text(tmp_path / "chained.log.1")
        assert "SECOND-CHUNK" in _text(path)


class TestTheLockIsNotMistakenForABackup:
    def test_the_lock_path_escapes_the_readers_backup_glob(self, tmp_path):
        """``log_service`` tails ``<base> + ".*"`` to find rotated backups."""
        path = tmp_path / "unified_scan.log"
        lock = _rotate_lock_path(str(path))

        assert not lock.endswith(".log"), "the /logs page must never serve it"
        assert lock not in glob.glob(str(path) + ".*"), (
            "the rotation lock would be tailed as if it were a rotated backup"
        )
        assert Path(lock).name.startswith("."), Path(lock).name

    def test_the_lock_lives_next_to_the_file_it_protects(self, tmp_path):
        path = tmp_path / "unified_scan.log"
        lock = _rotate_lock_path(str(path))
        assert Path(lock).parent == path.parent
        assert Path(lock).name == ".unified_scan.log.rotate.lock"
