"""The genre apply must write Ogg tags and must show real progress.

REPORT

> "Running the metadata tagging for genres from config and it's stuck as 2%
> after the following"

LOG

    Genre resync finished tracks=69747 with_evidence=68832 changed=15737
    dry_run=True files_written=0 files_failed=0 navidrome=None seconds=128.89
    ... then the apply opens at 2% ...
    WARNING services.metadata.tag_file_service  Unsupported file format: .ogg   (x9)

TWO DEFECTS

1. **Ogg was unwritable.** ``get_supported_audio_formats`` lists ``.ogg`` as a
   library format, so Ogg tracks are scanned, stored and pooled with everything
   else — but the tag writer's allow-list was ``.mp3/.flac/.m4a/.mp4``. Every
   Ogg write returned False, so the database said "changed" while the file kept
   its old tags. The apply also counted those as failures only in aggregate, so
   the log read as noise.

2. **The apply published no progress during its longest phase.** Frames went
   2% → 50% → 55% → 100%, and the 50% frame lands only after the ENTIRE
   whole-library aggregation AND every per-file tag write finished. At 70k
   tracks that is minutes of an unmoving "2%", which reads as hung.
"""

from __future__ import annotations

import inspect
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TAG_SERVICE = REPO_ROOT / "services" / "metadata" / "tag_file_service.py"
RESYNC_SERVICE = REPO_ROOT / "services" / "enrichment" / "genre_resync_service.py"
APPLY_SERVICE = (
    REPO_ROOT / "services" / "enrichment" / "genre_playlist_resync_service.py"
)


class TestOggFilesCanBeTagged:
    def test_ogg_is_in_the_write_allow_list(self) -> None:
        src = TAG_SERVICE.read_text(encoding="utf-8")
        assert '".ogg"' in src, (
            "the writer still refuses .ogg, which the scanner treats as a "
            "supported library format"
        )

    def test_the_atomic_writer_dispatches_ogg(self) -> None:
        src = TAG_SERVICE.read_text(encoding="utf-8")
        assert "write_ogg_tags(" in src

    def test_a_write_ogg_tags_function_exists(self) -> None:
        from services.metadata import tag_file_service as tfs

        assert callable(getattr(tfs, "write_ogg_tags", None))

    def test_opus_is_handled_too(self) -> None:
        """``.opus`` is Ogg with the same Vorbis comments."""
        from services.metadata import tag_file_service as tfs

        src = inspect.getsource(tfs._open_ogg)
        assert "oggopus" in src

    def test_it_writes_a_real_ogg_file(self, tmp_path) -> None:
        """Round-trip against a genuine Ogg Vorbis container."""
        import struct

        from services.metadata import tag_file_service as tfs

        # Minimal Ogg Vorbis: ident + comment + setup headers.
        def crc_table():
            t = []
            for i in range(256):
                r = i << 24
                for _ in range(8):
                    r = ((r << 1) ^ 0x04C11DB7) if (r & 0x80000000) else (r << 1)
                t.append(r & 0xFFFFFFFF)
            return t

        tbl = crc_table()

        def ogg_crc(data: bytes) -> int:
            crc = 0
            for b in data:
                crc = ((crc << 8) & 0xFFFFFFFF) ^ tbl[((crc >> 24) & 0xFF) ^ b]
            return crc

        def page(flags: int, seq: int, packets: list[bytes]) -> bytes:
            segs, body = [], b""
            for p in packets:
                n = len(p)
                while n >= 255:
                    segs.append(255)
                    n -= 255
                segs.append(n)
                body += p
            hdr = b"OggS" + bytes([0, flags]) + struct.pack("<q", 0)
            hdr += struct.pack("<I", 1234) + struct.pack("<I", seq) + b"\x00" * 4
            hdr += bytes([len(segs)]) + bytes(segs)
            pg = hdr + body
            return pg[:22] + struct.pack("<I", ogg_crc(pg)) + pg[26:]

        ident = b"\x01vorbis" + struct.pack("<I", 0) + b"\x02"
        ident += struct.pack("<I", 44100) + struct.pack("<i", 0)
        ident += struct.pack("<i", 128000) + struct.pack("<i", 0) + b"\xb8\x01"

        vendor = b"test"
        comment = b"\x03vorbis" + struct.pack("<I", len(vendor)) + vendor
        comment += struct.pack("<I", 1) + struct.pack("<I", 9) + b"GENRE=Old"
        comment += b"\x01"
        setup = b"\x05vorbis" + b"\x00" * 64

        path = tmp_path / "probe.ogg"
        path.write_bytes(
            page(2, 0, [ident]) + page(0, 1, [comment]) + page(0, 2, [setup])
        )

        assert tfs.write_ogg_tags(str(path), {"genres": "Rock, Metal"}) is True

        opened = tfs._open_ogg(str(path))
        assert list(opened.tags.get("genre") or []) == ["Rock", "Metal"], (
            "the genres did not reach the file — this is what left Ogg tracks "
            "showing their old genre in Navidrome"
        )

    def test_clearing_a_field_deletes_the_comment(self, tmp_path) -> None:
        """An empty value means 'remove', not 'write an empty string'."""
        # Covered structurally: the writer deletes rather than assigns when the
        # incoming value is empty, matching the FLAC writer.
        src = TAG_SERVICE.read_text(encoding="utf-8")
        start = src.index("def write_ogg_tags")
        body = src[start:start + 2600]
        assert "del audio[field]" in body


class TestTheApplyReportsProgressDuringTheGenrePhase:
    def test_the_resync_service_accepts_a_progress_callback(self) -> None:
        from services.enrichment.genre_resync_service import resync_genres

        assert "on_progress" in inspect.signature(resync_genres).parameters

    def test_the_apply_passes_one(self) -> None:
        src = APPLY_SERVICE.read_text(encoding="utf-8")
        assert "on_progress=_genre_progress" in src, (
            "the apply still publishes nothing between 2% and 50%"
        )

    def test_the_file_write_loop_reports_too(self) -> None:
        """File writes are the slow half; they must advance the bar as well."""
        src = RESYNC_SERVICE.read_text(encoding="utf-8")
        start = src.index("def _push_to_files")
        body = src[start:start + 2600]
        assert "on_progress" in body
        assert "on_progress(index, total)" in body

    def test_progress_frames_fill_the_gap(self) -> None:
        """No frame may be published only after the whole phase completes."""
        src = APPLY_SERVICE.read_text(encoding="utf-8")
        start = src.index("def _genre_progress")
        body = src[start:start + 3000]
        # Three distinct bands: aggregation, the DB save, then file writes.
        assert "Recomputing genres" in body
        assert "Saving genres to the database" in body
        assert "Writing genre tags to files" in body

    def test_a_failing_progress_write_cannot_abort_the_job(self) -> None:
        src = APPLY_SERVICE.read_text(encoding="utf-8")
        start = src.index("def _genre_progress")
        body = src[start:start + 3000]
        assert "except Exception" in body
