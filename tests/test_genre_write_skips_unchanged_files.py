"""The genre file-write phase: do not pay for a write that changes nothing.

REPORTED
--------
"The writing genre tags to files still takes a long time, this has been going
about 20 minutes and at 1000 out of 67000."

That is ~1.2 s per file, which extrapolates to roughly 22 hours.

WHY, AND WHICH PART IS FIXABLE
------------------------------
``_write_tags_atomic`` is a read-modify-write of the WHOLE audio file:

    shutil.copy2(original, tmp)        # full read + full write
    mutagen rewrites tmp               # full read + full write
    filecmp.cmp(tmp, original)         # full read of BOTH
    os.replace(tmp, original)

3-4 full passes PER FILE. The fourth step exists to discover "nothing changed" —
and a genre resync re-derives every track in the library while most of them are
already correct, so that is the common case paying the worst price.

The I/O itself must stay (atomicity is a correctness property). What can go is
doing it when the answer is already known: one tag read settles it.
"""

from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from services.metadata import tag_file_service as tfs


class _FakeFLAC:
    def __init__(self, tags):
        self.tags = tags
        self.pictures = []


@pytest.fixture
def flac_case(monkeypatch):
    """Run ``_tags_already_match`` against a fake FLAC with a forced suffix."""

    def _check(tags_on_disk, incoming, suffix=".flac"):
        fake = _FakeFLAC(dict(tags_on_disk))
        monkeypatch.setattr(tfs, "FLAC", lambda _p: fake)
        monkeypatch.setattr(
            pathlib.Path, "suffix", property(lambda self: suffix)
        )
        return tfs._tags_already_match("x.flac", incoming)

    return _check


class TestTheNoOpPreCheck:
    def test_identical_genres_are_detected(self, flac_case):
        assert flac_case({"genre": ["Heavy Metal"]}, {"genres": "Heavy Metal"}) is True

    def test_a_different_genre_is_not(self, flac_case):
        """CONTROL — the pre-check must never suppress a real correction."""
        assert flac_case({"genre": ["Punk"]}, {"genres": "Heavy Metal"}) is False

    def test_case_and_whitespace_are_not_a_change(self, flac_case):
        assert flac_case({"genre": ["heavy metal"]}, {"genres": "Heavy  Metal"}) is True

    def test_multi_value_matches_the_joined_form(self, flac_case):
        """The writers join with "; ", so that is the form to compare."""
        assert flac_case({"genre": ["a", "b"]}, {"genres": "a; b"}) is True

    def test_a_missing_tag_is_not_a_match(self, flac_case):
        assert flac_case({}, {"genres": "Heavy Metal"}) is False

    def test_an_empty_incoming_value_is_not_a_match(self, flac_case):
        """An empty payload means "clear it" — a write, not a no-op."""
        assert flac_case({"genre": ["X"]}, {"genres": ""}) is False


class TestItIsDeliberatelyNarrow:
    def test_a_multi_field_payload_is_never_short_circuited(self, flac_case):
        """⚠️ ONLY genres. A per-field "already equal" test across a whole album
        payload would produce false verdicts (separator/order/case differences the
        writers normalise on write) and silently skip a real correction."""
        assert flac_case(
            {"genre": ["A"], "album": ["B"]},
            {"genres": "A", "album": "B"},
        ) is False

    def test_an_unreadable_file_falls_through_to_the_real_write(self, monkeypatch):
        """⚠️ UNKNOWN IS NOT "UNCHANGED"."""
        def _boom(_p):
            raise OSError("cannot read")

        monkeypatch.setattr(tfs, "FLAC", _boom)
        monkeypatch.setattr(pathlib.Path, "suffix", property(lambda self: ".flac"))
        assert tfs._tags_already_match("x.flac", {"genres": "Metal"}) is False


class TestTheWritePathUsesIt:
    def test_the_precheck_runs_before_the_atomic_rewrite(self):
        import inspect

        src = inspect.getsource(tfs._write_tags_to_file_impl)
        check_at = src.index("_tags_already_match(file_path, tags)")
        write_at = src.index("_write_tags_atomic(file_path, tags)")
        assert check_at < write_at, (
            "the cheap check must precede the copy+rewrite+compare"
        )

    def test_a_match_returns_success_without_writing(self, monkeypatch):
        monkeypatch.setattr(tfs, "_tags_already_match", lambda *a, **k: True)
        called = []
        monkeypatch.setattr(
            tfs, "_write_tags_atomic", lambda *a, **k: called.append(1) or True
        )
        monkeypatch.setattr(tfs.os.path, "isfile", lambda _p: True)
        monkeypatch.setattr(
            tfs, "_write_tags_to_file_impl", tfs._write_tags_to_file_impl
        )
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_tagging_config", lambda: {
            "write_tags_to_file": True, "ratings_only": False, "fill_missing_only": False,
        })
        tfs._write_tags_to_file_impl("x.flac", {"genres": "Metal"})
        assert called == [], "the atomic rewrite ran despite an unchanged value"
