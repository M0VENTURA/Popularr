"""Album art must survive the type a Postgres BYTEA comes back as.

REPORTED (production log):

    [ERROR] [services.metadata.tag_file_service] Failed to embed album art in
    /music/ivri/2026 - evidence of you/01. ivri - SABOTAGE.mp3:
    data has to be bytes

every track of one album, MP3 only.

WHY
---
``album_art.image_data`` is **BYTEA**, and psycopg2 returns BYTEA as a
**memoryview**. mutagen requires EXACTLY ``bytes`` — its ``BinaryFrame.data``
setter raises ``TypeError("data has to be bytes")`` for ``bytearray``,
``memoryview`` or ``str``.

``embed_album_art`` (MP3 APIC + FLAC Picture) passed that value straight
through, while ``write_tags_to_file`` in the SAME module already coerced
(``data=bytes(value)``) — so the album-page art path worked and the
download-import path failed on every track of an album whose stored art was kept
as-is, i.e. source ``navidrome``, ``upload`` or ``url`` (exactly the sources
``navidrome_art_may_replace`` protects, which is why no provider call appeared in
the log).

These tests pin the conversion at BOTH ends: the embed boundary (so no caller
can crash mutagen) and the DB read boundary (so the type contract is honest).
"""

from __future__ import annotations

import pytest

from db.repositories import metadata as md
from services.metadata import tag_file_service as tfs

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF-fake-jpeg-bytes"


# ---------------------------------------------------------------------------
# 1. The premise: mutagen really does reject the DB types
# ---------------------------------------------------------------------------


class TestMutagenRejectsTheTypesPostgresReturns:
    """Documents WHY the coercion is needed, using real mutagen."""

    @pytest.mark.parametrize("label,value", [
        ("bytearray", bytearray(JPEG)),
        ("memoryview", memoryview(JPEG)),
        ("str", "/music/cover.jpg"),
    ])
    def test_the_frame_rejects_it(self, label: str, value) -> None:
        from mutagen.id3._frames import APIC

        with pytest.raises(TypeError) as exc:
            APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=value)
        assert "data has to be bytes" in str(exc.value), (
            f"{label}: mutagen's message changed — this test guards the exact "
            "error the production log showed"
        )

    def test_bytes_is_accepted(self) -> None:
        from mutagen.id3._frames import APIC

        frame = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=JPEG)
        assert frame.data == JPEG


# ---------------------------------------------------------------------------
# 2. The embed boundary
# ---------------------------------------------------------------------------


class _FakeTags:
    def __init__(self) -> None:
        self.frames: list = []

    def delall(self, key: str) -> None:
        self.frames = [f for f in self.frames if not f.__class__.__name__ == key]

    def add(self, frame) -> None:
        self.frames.append(frame)


class _FakeAudio:
    def __init__(self) -> None:
        self.tags = _FakeTags()
        self.saved = False

    def save(self) -> None:
        self.saved = True


@pytest.fixture
def fake_mp3(monkeypatch):
    """A real ``APIC`` written through a stubbed ``MP3`` (no audio file needed).

    The stub only replaces the MPEG/ID3 I/O; the frame itself is genuine
    mutagen, so the type validation under test is the real one.
    """
    holder: dict = {}

    def _factory(path, ID3=None):  # noqa: N803 - mirrors mutagen's signature
        audio = _FakeAudio()
        holder["audio"] = audio
        return audio

    monkeypatch.setattr(tfs, "MP3", _factory)
    return holder


class TestEmbedAlbumArtAcceptsEveryStoredBlobType:
    @pytest.mark.parametrize("label,value", [
        ("bytes", JPEG),
        ("bytearray", bytearray(JPEG)),
        ("memoryview", memoryview(JPEG)),
    ])
    def test_it_embeds_and_the_frame_holds_bytes(
        self, tmp_path, fake_mp3, label: str, value
    ) -> None:
        path = tmp_path / "track.mp3"
        path.write_bytes(b"")

        assert tfs.embed_album_art(str(path), value, "image/jpeg") is True, (
            f"{label}: the embed must succeed — this is the reported failure"
        )
        frames = fake_mp3["audio"].tags.frames
        assert frames, "an APIC frame must have been written"
        assert isinstance(frames[0].data, bytes), (
            f"{label} reached mutagen unconverted"
        )
        assert frames[0].data == JPEG, "the image must be unchanged, only retyped"
        assert fake_mp3["audio"].saved is True

    def test_a_str_is_refused_instead_of_encoded(self, tmp_path, fake_mp3) -> None:
        """A str means a path or base64; encoding it would embed garbage."""
        path = tmp_path / "track.mp3"
        path.write_bytes(b"")

        assert tfs.embed_album_art(str(path), "/music/cover.jpg") is False
        assert "audio" not in fake_mp3, (
            "the writer must not even open the file for a non-bytes payload"
        )

    def test_an_empty_or_missing_file_is_still_a_no_op(self, tmp_path) -> None:
        path = tmp_path / "track.mp3"
        path.write_bytes(b"")
        assert tfs.embed_album_art(str(path), b"") is False
        assert tfs.embed_album_art(str(tmp_path / "nope.mp3"), JPEG) is False


# ---------------------------------------------------------------------------
# 3. The DB read boundary
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _FakeSession:
    def __init__(self, row) -> None:
        self._row = row

    def execute(self, *_a, **_k) -> _FakeResult:
        return _FakeResult(self._row)


class _FakeSessionCtx:
    def __init__(self, row) -> None:
        self._row = row

    def __enter__(self) -> _FakeSession:
        return _FakeSession(self._row)

    def __exit__(self, *_exc) -> bool:
        return False


@pytest.mark.parametrize("value,expected", [
    (memoryview(JPEG), JPEG),
    (bytearray(JPEG), JPEG),
    (JPEG, JPEG),
    (None, None),
])
def test_fetch_album_art_record_returns_bytes(monkeypatch, value, expected) -> None:
    """psycopg2 returns BYTEA as a memoryview; the reader must normalise it."""
    monkeypatch.setattr(md, "db_session", lambda: _FakeSessionCtx((value, "image/jpeg", "navidrome")))

    data, mime, source = md.fetch_album_art_record(artist="ivri", album="evidence of you")

    assert data == expected
    if expected is not None:
        assert isinstance(data, bytes), (
            "a memoryview here is what crashed the import embed"
        )
    assert mime == "image/jpeg"
    assert source == "navidrome"


@pytest.mark.parametrize("value,expected", [
    (memoryview(JPEG), JPEG),
    (bytearray(JPEG), JPEG),
    (JPEG, JPEG),
])
def test_fetch_album_art_blob_returns_bytes(monkeypatch, value, expected) -> None:
    monkeypatch.setattr(md, "db_session", lambda: _FakeSessionCtx((value, "image/png")))

    data, mime = md.fetch_album_art_blob(artist="A", album="B")

    assert isinstance(data, bytes)
    assert data == expected
    assert mime == "image/png"


def test_the_two_readers_agree(monkeypatch) -> None:
    """CONTROL — the sibling readers must not diverge again."""
    monkeypatch.setattr(md, "db_session", lambda: _FakeSessionCtx((memoryview(JPEG), "image/jpeg", "musicbrainz")))

    blob, _mime = md.fetch_album_art_blob(artist="A", album="B")
    record, _mime2, _src = md.fetch_album_art_record(artist="A", album="B")

    assert blob == record == JPEG
    assert type(blob) is type(record) is bytes
