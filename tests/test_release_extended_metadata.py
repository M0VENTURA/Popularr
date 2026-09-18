"""Regression tests: the album page's Extended Metadata panel is populated.

Reported symptom: after a scan, these fields were always blank —

    Record Label, Catalog Number, Barcode, Release Date, Media Format,
    Release Country

TWO independent gaps caused it:

1. ``fetch_musicbrainz_release_metadata`` never EXTRACTED them. It requested
   ``inc="recordings+artist-credits+release-groups+media"`` — no ``labels`` —
   and had no ``label-info`` / ``barcode`` / ``release-events`` parsing at all.
   Because an explicit ``inc`` bypasses the client's superset fallback, the
   label data was never even fetched.

2. Nothing in the scan WROTE them. The only album-level write to any of the six
   was ``album_stage``'s ``releasecountry`` backfill, which set it from the
   ARTIST's area, not the release.

Guards:
- ``_release_extended_fields`` reads all six off the release payload.
- ``fetch_musicbrainz_release_metadata`` returns them and requests ``labels``.
- ``_persist_release_extended_fields`` writes them album-wide, is fill-only,
  and resolves the release id from the DB so a rescan still fills.
"""

from __future__ import annotations

import pytest

ARTIST = "Metallica"
ALBUM = "72 Seasons"


def _rich_release():
    """A MusicBrainz release payload with every extended field present."""
    return {
        "id": "rel-72",
        "title": "72 Seasons",
        "date": "2023-04-14",
        "country": "US",
        "barcode": "810100230801",
        "release-events": [
            {"date": "2023-04-14", "area": {"name": "United States"}},
            {"date": "2023-04-21", "area": {"name": "Europe"}},
        ],
        "label-info": [
            {
                "catalog-number": "BLCKND0552-1",
                "label": {"name": "Blackened Recordings"},
            }
        ],
        "artist-credit": [
            {"name": ARTIST, "artist": {"id": "artist-mb", "name": ARTIST}}
        ],
        "release-group": {
            "id": "rg-72",
            "title": ALBUM,
            "first-release-date": "2023-04-14",
            "primary-type": "Album",
            "secondary-types": [],
        },
        "media": [
            {
                "position": 1,
                "format": "CD",
                "tracks": [
                    {
                        "position": 1,
                        "title": "72 Seasons",
                        "length": 457000,
                        "recording": {"id": "rec-1", "title": "72 Seasons"},
                    }
                ],
            }
        ],
    }


class _FakeClient:
    def __init__(self, release):
        self.release = release
        self.requested_inc = None

    def get_release(self, release_id, inc="", **kwargs):
        self.requested_inc = inc
        return self.release


class TestReleaseExtendedFieldsExtraction:
    """``_release_extended_fields`` reads all six values off the release."""

    def _extract(self, release, media=None):
        from services.enrichment.musicbrainz_service import _release_extended_fields

        return _release_extended_fields(release, media if media is not None else release.get("media"))

    def test_all_six_fields_are_extracted(self):
        fields = self._extract(_rich_release())
        assert fields["recordlabel"] == "Blackened Recordings"
        assert fields["catalognumber"] == "BLCKND0552-1"
        assert fields["barcode"] == "810100230801"
        assert fields["releasedate"] == "2023-04-14"
        assert fields["media"] == "CD"
        assert fields["releasecountry"] == "United States"

    def test_release_country_uses_the_earliest_release_event(self):
        # A release issued in several countries: the earliest event is the
        # closest analogue of "where this release is from".
        release = _rich_release()
        release["release-events"] = [
            {"date": "2024-01-01", "area": {"name": "Japan"}},
            {"date": "2023-04-14", "area": {"name": "United States"}},
        ]
        assert self._extract(release)["releasecountry"] == "United States"

    def test_release_country_falls_back_to_the_singular_country_field(self):
        release = _rich_release()
        release.pop("release-events")
        assert self._extract(release)["releasecountry"] == "US"

    def test_repeated_media_formats_are_de_duplicated(self):
        # MusicBrainz repeats the format per medium; the column is one text
        # field, so "CD / CD" would be wrong.
        media = [
            {"format": "CD", "tracks": []},
            {"format": "CD", "tracks": []},
            {"format": "DVD", "tracks": []},
        ]
        assert self._extract(_rich_release(), media)["media"] == "CD / DVD"

    def test_multiple_labels_and_catalog_numbers_are_joined(self):
        release = _rich_release()
        release["label-info"] = [
            {"catalog-number": "CAT-1", "label": {"name": "Label One"}},
            {"catalog-number": "CAT-2", "label": {"name": "Label Two"}},
        ]
        fields = self._extract(release)
        assert fields["recordlabel"] == "Label One / Label Two"
        assert fields["catalognumber"] == "CAT-1 / CAT-2"

    def test_missing_fields_are_empty_strings_never_placeholders(self):
        # A placeholder would be written to the audio files as a real tag.
        fields = self._extract({"id": "rel-bare"})
        for key in (
            "recordlabel", "catalognumber", "barcode",
            "releasedate", "media", "releasecountry",
        ):
            assert fields[key] == ""

    def test_snake_case_label_info_variants_are_accepted(self):
        release = {
            "label_info": [
                {"catalog_number": "CAT-S", "label": {"name": "Snake Label"}}
            ]
        }
        fields = self._extract(release)
        assert fields["recordlabel"] == "Snake Label"
        assert fields["catalognumber"] == "CAT-S"

    def test_malformed_label_info_does_not_crash(self):
        release = {"label-info": ["nonsense", None, 42, {"label": "not-a-dict"}]}
        assert self._extract(release)["recordlabel"] == ""


class TestFetchRequestsLabels:
    """The fetch must ASK for labels — an explicit ``inc`` bypasses the
    client's superset fallback, so omitting it silently loses label data."""

    def test_labels_is_in_the_inc_and_fields_are_returned(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        client = _FakeClient(_rich_release())
        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: client)

        meta = mbs.fetch_musicbrainz_release_metadata("rel-72")
        assert meta is not None
        assert "labels" in (client.requested_inc or ""), (
            "the release fetch must request `labels`, or label-info is never "
            "returned and Record Label / Catalog Number stay blank"
        )
        assert meta["recordlabel"] == "Blackened Recordings"
        assert meta["catalognumber"] == "BLCKND0552-1"
        assert meta["barcode"] == "810100230801"
        assert meta["releasedate"] == "2023-04-14"
        assert meta["media"] == "CD"
        assert meta["releasecountry"] == "United States"

    def test_existing_contract_fields_are_still_returned(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient(_rich_release()))
        meta = mbs.fetch_musicbrainz_release_metadata("rel-72")
        # No regression on the identity fields the rest of the scan relies on.
        assert meta["release_mbid"] == "rel-72"
        assert meta["release_group_mbid"] == "rg-72"
        assert meta["album_artist_mbid"] == "artist-mb"
        assert meta["disc_count"] == 1
        assert len(meta["tracks"]) == 1


class TestPersistExtendedFields:
    """The scan persists the six fields album-wide, fill-only."""

    def _persist(self, monkeypatch, release, *, existing_mbid=""):
        from services.popularity.stages import album_stage as a

        executed: list[tuple[str, dict]] = []

        class _Result:
            rowcount = 3

        class _Session:
            def execute(self, statement, params=None, *args, **kwargs):
                executed.append((str(statement), dict(params or {})))
                return _Result()

        import contextlib

        @contextlib.contextmanager
        def _fake_session(*args, **kwargs):
            yield _Session()

        monkeypatch.setattr(a, "db_session", _fake_session)
        monkeypatch.setattr(
            a, "_resolve_album_release_mbid", lambda artist, album: existing_mbid
        )

        import services.enrichment.musicbrainz_service as mbs

        monkeypatch.setattr(
            mbs, "fetch_musicbrainz_release_metadata", lambda rid: release
        )

        rows = a._persist_release_extended_fields(ARTIST, ALBUM, "rel-72")
        return rows, executed

    def test_all_six_columns_are_written_album_wide(self, monkeypatch):
        rows, executed = self._persist(monkeypatch, _rich_release())
        assert rows == 3
        assert executed, "no UPDATE issued"
        statement, params = executed[0]

        for column in (
            "recordlabel", "catalognumber", "barcode",
            "releasedate", "media", "releasecountry",
        ):
            assert f"{column} = CASE WHEN" in statement, f"{column} not in the UPDATE"
            assert params[column], f"{column} value missing from the parameters"

        # Album-scoped, so every track of the folder agrees.
        assert "COALESCE(NULLIF(album_artist, ''), artist) = :artist" in statement
        assert "album = :album" in statement

    def test_update_is_fill_only(self, monkeypatch):
        # A manual edit must never be clobbered by a rescan: each column is
        # written only when it is currently empty.
        _, executed = self._persist(monkeypatch, _rich_release())
        statement, _ = executed[0]
        assert "COALESCE(NULLIF(TRIM(" in statement
        assert "THEN :" in statement and "ELSE " in statement

    def test_no_release_id_means_no_write(self, monkeypatch):
        rows, executed = self._persist(monkeypatch, _rich_release(), existing_mbid="")
        assert rows == 0
        assert executed == []

    def test_release_id_is_resolved_from_the_db_when_not_supplied(self, monkeypatch):
        # A rescan: the release MBID was persisted on an earlier pass, so the
        # extended fields must still fill from the stored id.
        from services.popularity.stages import album_stage as a

        fetched: list[str] = []
        executed: list[str] = []

        class _Result:
            rowcount = 2

        class _Session:
            def execute(self, statement, params=None, *args, **kwargs):
                executed.append(str(statement))
                return _Result()

        import contextlib

        @contextlib.contextmanager
        def _fake_session(*args, **kwargs):
            yield _Session()

        monkeypatch.setattr(a, "db_session", _fake_session)
        monkeypatch.setattr(
            a, "_resolve_album_release_mbid", lambda artist, album: "rel-stored"
        )

        import services.enrichment.musicbrainz_service as mbs

        def _fetch(rid):
            fetched.append(rid)
            return _rich_release()

        monkeypatch.setattr(mbs, "fetch_musicbrainz_release_metadata", _fetch)

        rows = a._persist_release_extended_fields(ARTIST, ALBUM)
        assert rows == 2
        assert fetched == ["rel-stored"]
        assert executed

    def test_empty_release_yields_no_write(self, monkeypatch):
        rows, executed = self._persist(monkeypatch, {})
        assert rows == 0
        assert executed == []

    def test_release_with_no_extended_values_yields_no_write(self, monkeypatch):
        # Every field blank: do not issue a no-op UPDATE.
        rows, executed = self._persist(monkeypatch, {"id": "rel-72"})
        assert rows == 0
        assert executed == []
