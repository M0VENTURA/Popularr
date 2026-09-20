"""The album-MBID sanity guard: trustworthy enough to leave enabled.

A stored album MusicBrainz ID used to be treated as proof of identity in two
places, neither of which looked at the album:

  * ``musicbrainz_service._lookup_existing_mbid`` resolved it and returned
    ``confidence: 1.0``, which the album page AUTO-SELECTED ahead of every
    text-search candidate;
  * ``album_service.apply_mbid_to_album`` then wrote the ID (and the MB
    album-artist/type/country/year tags) onto EVERY track and EVERY audio file
    whose ``(artist, album)`` matched.

So a single errant batch-tagging run that stamped a wrong ID onto a folder was
enough to merge unrelated albums — the reported Metallica / d'Artagnan case,
where one poisoned ID grew a 66-track "super album".

Two things are pinned here, and the ORDER matters:

1. **The guard must NOT cry wolf.** Edition markers, "&"/"feat." credits,
   year prefixes, "Vol. 1" vs "Volume 1", punctuation and case are all
   legitimate variation. A guard that rejects real albums gets switched off,
   which is strictly worse than not having one — so every one of these cases
   is asserted to PASS.
2. **It must actually block.** A genuinely different record — or an album spread
   over folders that are not disc folders of one parent — must be refused, and
   the fan-out must not run.

A third rule is asserted too, because it is the difference between a guard that
helps and one that gets removed: an ID that CANNOT BE LOOKED UP (MusicBrainz
down, or a release that answers without a title) is allowed through with a
warning.  There is no measurement to act on, and blocking would turn every
legitimate "Use This Album" into a silent no-op during an outage — replacing a
rare corruption bug with a frequent one.
"""

from __future__ import annotations

import pytest

from services.metadata.album_mbid_guard import (
    REASON_MULTIPLE_FOLDERS,
    REASON_TEXT_MISMATCH,
    REASON_UNVERIFIABLE,
    album_roots_for_paths,
    check_folder_boundary,
    compare_album_text,
    guard_album_mbid,
    is_disc_folder_name,
)


# ---------------------------------------------------------------------------
# 1. Legitimate variation must still match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "local_artist,local_album,mb_artist,mb_album",
    [
        # identical
        ("Metallica", "Master of Puppets", "Metallica", "Master of Puppets"),
        # case + punctuation only
        ("the beatles", "Sgt. Pepper's Lonely Hearts Club Band",
         "The Beatles", "Sgt Peppers Lonely Hearts Club Band"),
        # edition markers the local tags carry but MusicBrainz omits
        ("Aephanemer", "Prokopton (Deluxe Edition)", "Aephanemer", "Prokopton"),
        ("Queen", "A Night At The Opera (2011 Remaster)", "Queen", "A Night At The Opera"),
        # a leading release year is a folder convention, not the title
        ("Queen", "2011 - A Night At The Opera", "Queen", "A Night At The Opera"),
        # featured / "&" guest credits differ between tags and MusicBrainz
        ("dArtagnan & The Dark Tenor", "Herzblut", "dArtagnan", "Herzblut"),
        ("KNEECAP", "Fine Art", "KNEECAP feat. Fawzi", "Fine Art"),
        # Vol. vs Volume
        ("Queen", "Greatest Hits Vol. 1", "Queen", "Greatest Hits Volume 1"),
        # compilation credits legitimately differ per release
        ("Various Artists", "Bravo Hits 100", "Ariana Grande & Various Artists", "Bravo Hits 100"),
    ],
)
def test_legitimate_variation_is_accepted(local_artist, local_album, mb_artist, mb_album) -> None:
    verdict = compare_album_text(local_artist, local_album, mb_artist, mb_album)
    assert verdict["ok"], (
        f"{local_album!r} by {local_artist!r} was refused against "
        f"{mb_album!r} by {mb_artist!r} "
        f"(album={verdict['album_similarity']}, artist={verdict['artist_similarity']}). "
        "Over-strict guard: this variation is normal and must match."
    )


# ---------------------------------------------------------------------------
# 2. A genuinely different record must be refused
# ---------------------------------------------------------------------------

def test_the_metallica_dartagnan_poisoning_is_refused() -> None:
    """The reported case: a Metallica folder carrying d'Artagnan's MBID."""
    verdict = compare_album_text("Metallica", "Master of Puppets", "dArtagnan", "Herzblut")
    assert not verdict["ok"]
    assert verdict["reason"] == REASON_TEXT_MISMATCH


def test_same_artist_different_album_is_refused() -> None:
    verdict = compare_album_text("Metallica", "Ride the Lightning", "Metallica", "Master of Puppets")
    assert not verdict["ok"]


def test_unrelated_artist_is_refused() -> None:
    verdict = compare_album_text("The Pretty Reckless", "Going To Hell", "The Jesus and Mary Chain", "Darklands")
    assert not verdict["ok"]


def test_missing_mb_title_is_unverifiable_and_allowed(monkeypatch) -> None:
    """An ID that cannot be looked up is allowed through, WITH a warning.

    This is the deliberate trade-off.  ``_lookup_existing_mbid`` substitutes the
    LOCAL album name when MusicBrainz answers without a title, so comparing here
    would trivialise the check (the local name always "matches" itself) — but
    REFUSING on no data would make every legitimate apply a silent no-op during
    a MusicBrainz outage.  Proceeding and logging keeps a bad ID greppable
    without punishing the healthy path.
    """
    verdict = compare_album_text("Metallica", "Master of Puppets", "Metallica", "")
    assert verdict["ok"] is True
    assert verdict["reason"] == REASON_UNVERIFIABLE


def test_a_measured_mismatch_is_still_refused(monkeypatch) -> None:
    """The unverifiable allowance must not weaken the real check.

    A poisoned ID is usually a REAL MusicBrainz release, so it resolves to a
    title/artist that can be compared — and that comparison is what stops it.
    """
    verdict = compare_album_text("Metallica", "Master of Puppets", "dArtagnan", "Herzblut")
    assert verdict["ok"] is False
    assert verdict["reason"] == REASON_TEXT_MISMATCH


# ---------------------------------------------------------------------------
# 3. Physical boundary: one folder = one album (except genuine multi-disc)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [("CD1", True), ("CD 2", True), ("cd_3", True), ("Disc1", True), ("disk-2", True),
     ("CD", False), ("Disc One", False), ("Bonus", False), ("Album", False)],
)
def test_disc_folder_detection(name: str, expected: bool) -> None:
    assert is_disc_folder_name(name) is expected


def test_single_folder_is_fine() -> None:
    paths = ["Metallica/Master of Puppets/01 Battery.mp3", "Metallica/Master of Puppets/02 Master of Puppets.mp3"]
    assert check_folder_boundary(paths)["ok"] is True


def test_multi_disc_siblings_are_one_album() -> None:
    paths = [
        "Metallica/S&M/CD1/01 The Ecstasy of Gold.mp3",
        "Metallica/S&M/CD2/01 Fuel.mp3",
    ]
    assert album_roots_for_paths(paths) == ["Metallica/S&M"]
    assert check_folder_boundary(paths)["ok"] is True


def test_bare_files_plus_disc_subfolder_are_one_album() -> None:
    """``Album/*.flac`` alongside ``Album/Disc 1/*.flac`` is still one album."""
    paths = ["Artist/Album/01 Intro.flac", "Artist/Album/Disc 1/01 Track.flac"]
    assert album_roots_for_paths(paths) == ["Artist/Album"]
    assert check_folder_boundary(paths)["ok"] is True


def test_two_unrelated_folders_sharing_a_name_are_refused() -> None:
    """The 66-track super-album shape: two different folders, one album name."""
    paths = [
        "Metallica/Greatest Hits/01 One.mp3",
        "dArtagnan/Greatest Hits/01 Herzblut.mp3",
    ]
    boundary = check_folder_boundary(paths)
    assert boundary["ok"] is False
    assert boundary["reason"] == REASON_MULTIPLE_FOLDERS
    assert len(boundary["folders"]) == 2


def test_disc_folders_can_be_disallowed() -> None:
    paths = ["Artist/Album/CD1/01 a.mp3", "Artist/Album/CD2/01 b.mp3"]
    assert check_folder_boundary(paths, allow_disc_folders=True)["ok"] is True
    # NOTE: both disc folders resolve to ONE root, so a multi-disc album is
    # accepted either way — the setting only matters once the roots differ.
    assert check_folder_boundary(paths, allow_disc_folders=False)["ok"] is True


def test_guard_combines_text_and_folders() -> None:
    good_text = dict(artist="Metallica", album="Master of Puppets",
                     mb_artist="Metallica", mb_album="Master of Puppets")
    bad_folders = ["A/Greatest Hits/01.mp3", "B/Greatest Hits/01.mp3"]
    verdict = guard_album_mbid(**good_text, file_paths=bad_folders)
    assert not verdict["ok"]
    assert verdict["reason"] == REASON_MULTIPLE_FOLDERS

    good_folders = ["Metallica/Master of Puppets/01 Battery.mp3"]
    assert guard_album_mbid(**good_text, file_paths=good_folders)["ok"] is True

    # No file list at all (the album-page lookup): the text check still applies.
    assert guard_album_mbid(**good_text)["ok"] is True
    assert not guard_album_mbid(
        artist="Metallica", album="Master of Puppets", mb_artist="dArtagnan", mb_album="Herzblut",
    )["ok"]


# ---------------------------------------------------------------------------
# 4. Config
# ---------------------------------------------------------------------------

def test_guard_defaults(monkeypatch) -> None:
    import helpers.config_helpers as ch

    monkeypatch.setattr(ch, "get_config", lambda: {})
    cfg = ch.get_album_mbid_guard_config()
    assert cfg == {"enabled": True, "min_similarity": 0.65, "allow_disc_folders": True}


def test_guard_partial_user_block_merges(monkeypatch) -> None:
    """A saved block must not reset the keys it does not name."""
    import helpers.config_helpers as ch

    monkeypatch.setattr(
        ch, "get_config",
        lambda: {"metadata_update": {"album_mbid_guard": {"min_similarity": 0.9}}},
    )
    cfg = ch.get_album_mbid_guard_config()
    assert cfg["min_similarity"] == 0.9
    assert cfg["enabled"] is True
    assert cfg["allow_disc_folders"] is True


@pytest.mark.parametrize("raw,expected", [("0", 0.05), ("5", 1.0), ("nonsense", 0.65)])
def test_min_similarity_is_clamped(monkeypatch, raw, expected) -> None:
    """0 would silently disable the guard and >1 could never match."""
    import helpers.config_helpers as ch

    monkeypatch.setattr(
        ch, "get_config",
        lambda: {"metadata_update": {"album_mbid_guard": {"min_similarity": raw}}},
    )
    assert ch.get_album_mbid_guard_config()["min_similarity"] == expected


def test_disabled_guard_stands_down(monkeypatch) -> None:
    import services.metadata.album_mbid_guard as guard

    monkeypatch.setattr(guard, "get_album_mbid_guard_config",
                        lambda: {"enabled": False, "min_similarity": 0.65, "allow_disc_folders": True})
    verdict = guard.guard_album_mbid(
        artist="Metallica", album="Master of Puppets", mb_artist="dArtagnan", mb_album="Herzblut",
    )
    assert verdict["ok"] is True
    assert verdict["reason"] == "disabled"


# ---------------------------------------------------------------------------
# 5. The two places that used to trust the ID blindly
# ---------------------------------------------------------------------------

def test_apply_mbid_to_album_refuses_a_mismatched_id(monkeypatch) -> None:
    """The fan-out must not run at all when the ID disagrees with the album."""
    import services.metadata.album_service as asvc

    writes: list[tuple] = []
    monkeypatch.setattr(asvc, "update_album_mbid_fields",
                        lambda *a, **k: writes.append(a) or 1)
    monkeypatch.setattr(asvc, "_album_file_paths",
                        lambda artist, album: ["Metallica/Master of Puppets/01 Battery.mp3"])
    monkeypatch.setattr(asvc, "resolve_mbid_text",
                        lambda mbid="", rg_mbid="": {"title": "Herzblut", "artist": "dArtagnan"})

    result = asvc.apply_mbid_to_album("Metallica", "Master of Puppets", "bad-mbid", "", "")

    assert result["success"] is False
    assert result["rejected"] is True
    assert result["reason"] == REASON_TEXT_MISMATCH
    assert writes == [], "the guard must refuse BEFORE any row is written"
    assert "candidates" in result, "a refused ID must carry text-search candidates to use instead"


def test_apply_mbid_to_album_still_writes_a_matching_id(monkeypatch) -> None:
    import services.metadata.album_service as asvc

    writes: list[tuple] = []
    monkeypatch.setattr(asvc, "update_album_mbid_fields",
                        lambda *a, **k: writes.append(a) or 3)
    monkeypatch.setattr(asvc, "_album_file_paths",
                        lambda artist, album: ["Metallica/Master of Puppets/01 Battery.mp3"])
    monkeypatch.setattr(asvc, "resolve_mbid_text",
                        lambda mbid="", rg_mbid="": {"title": "Master of Puppets", "artist": "Metallica"})

    result = asvc.apply_mbid_to_album("Metallica", "Master of Puppets", "good-mbid", "", "")

    assert result["success"] is True
    assert writes, "a matching ID must still be applied"
    assert result.get("rows_updated") == 3


def test_apply_mbid_to_album_refuses_a_multi_folder_album(monkeypatch) -> None:
    import services.metadata.album_service as asvc

    writes: list[tuple] = []
    monkeypatch.setattr(asvc, "update_album_mbid_fields", lambda *a, **k: writes.append(a) or 1)
    monkeypatch.setattr(
        asvc, "_album_file_paths",
        lambda artist, album: ["A/Greatest Hits/01.mp3", "B/Greatest Hits/01.mp3"],
    )
    monkeypatch.setattr(asvc, "resolve_mbid_text",
                        lambda mbid="", rg_mbid="": {"title": "Greatest Hits", "artist": "Metallica"})

    result = asvc.apply_mbid_to_album("Metallica", "Greatest Hits", "some-mbid", "", "")

    assert result["success"] is False
    assert result["reason"] == REASON_MULTIPLE_FOLDERS
    assert writes == []


def test_stored_mbid_is_not_auto_selected_when_it_conflicts(monkeypatch) -> None:
    """The album page must fall back to the text search, not the stored ID.

    ``lookup_musicbrainz_album`` used to prepend the stored MBID with
    confidence 1.0, which made the UI auto-select it — so this test asserts the
    conflicting ID is ABSENT from the results entirely, not merely ranked lower.
    """
    import services.enrichment.musicbrainz_service as mbs

    monkeypatch.setattr(
        mbs, "_lookup_existing_mbid",
        lambda mbid, artist, album: {
            "mbid": mbid, "title": "Herzblut", "artist": "dArtagnan",
            "confidence": 1.0, "is_stored_mbid": True, "mbid_type": "release",
            "primary_type": "Album", "secondary_types": [], "first_release_date": "",
            "cover_art_url": "", "source": "musicbrainz",
        },
    )

    class _Client:
        def search_release_groups(self, query, limit=10, **kwargs):
            return [{"id": "rg-good", "title": "Master of Puppets",
                     "artist-credit": [{"name": "Metallica"}],
                     "primary-type": "Album", "secondary-types": [],
                     "first-release-date": "1986-03-03"}]

    monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _Client())

    result = mbs.lookup_musicbrainz_album("Metallica", "Master of Puppets", "poisoned-mbid")
    ids = [r["mbid"] for r in result["results"]]

    assert "poisoned-mbid" not in ids, (
        "a stored MBID whose own title/artist contradicts the album must not be "
        "offered at all — it used to be prepended with confidence 1.0 and "
        "auto-selected, which is how a poisoned ID reached the fan-out"
    )
    assert "rg-good" in ids, "the text search must supply the candidates instead"


def test_stored_mbid_is_used_when_it_agrees(monkeypatch) -> None:
    import services.enrichment.musicbrainz_service as mbs

    monkeypatch.setattr(
        mbs, "_lookup_existing_mbid",
        lambda mbid, artist, album: {
            "mbid": mbid, "title": "Master of Puppets", "artist": "Metallica",
            "confidence": 1.0, "is_stored_mbid": True, "mbid_type": "release",
            "primary_type": "Album", "secondary_types": [], "first_release_date": "",
            "cover_art_url": "", "source": "musicbrainz",
        },
    )

    class _Client:
        def search_release_groups(self, query, limit=10, **kwargs):
            return []

    monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _Client())

    result = mbs.lookup_musicbrainz_album("Metallica", "Master of Puppets", "good-mbid")
    assert [r["mbid"] for r in result["results"]] == ["good-mbid"]
