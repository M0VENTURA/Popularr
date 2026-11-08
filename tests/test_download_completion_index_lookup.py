"""Completed transfers must resolve from the one-per-pass filesystem index.

The old ``_collect_slskd_completed_map`` walked every download/torrent root
once PER completed transfer. With a long slskd transfer history that alone
exceeded the 120 s maintenance budget.
"""

from __future__ import annotations

from services.downloads import download_completion_service as dcs


class _SlskdWithCompleted:
    def __init__(self, filenames):
        self._filenames = filenames

    def get_completed_transfers(self):
        return [
            {"localFilePath": "", "filename": filename}
            for filename in self._filenames
        ]


def test_completed_map_uses_the_index_without_walking(monkeypatch, tmp_path):
    target = tmp_path / "Artist" / "01 - Song.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    fs_files = [{"full_path": str(target), "rel_path": "Artist/01 - Song.mp3"}]

    def _no_walk(*args, **kwargs):
        raise AssertionError(
            "os.walk ran during the completed-transfer map — it must use "
            "the fs_files index built once per pass"
        )

    monkeypatch.setattr(dcs.os, "walk", _no_walk)

    result = dcs._collect_slskd_completed_map(
        _SlskdWithCompleted(["peer/Artist/01 - Song.mp3"]),
        str(tmp_path),
        fs_files,
    )

    assert result.get("01 - song.mp3") == str(target)


def test_many_completed_transfers_do_not_multiply_walks(monkeypatch, tmp_path):
    files = []
    for i in range(5):
        target = tmp_path / "Artist" / f"0{i} - Song.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        files.append({"full_path": str(target), "rel_path": f"Artist/0{i} - Song.mp3"})

    def _no_walk(*args, **kwargs):
        raise AssertionError("os.walk must not run per transfer")

    monkeypatch.setattr(dcs.os, "walk", _no_walk)

    result = dcs._collect_slskd_completed_map(
        _SlskdWithCompleted([f"peer/{f['rel_path']}" for f in files]),
        str(tmp_path),
        files,
    )

    for f in files:
        basename = f["rel_path"].rsplit("/", 1)[-1].lower()
        assert result.get(basename) == f["full_path"]
