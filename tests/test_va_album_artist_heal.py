"""End-to-end: a row polluted with "Various Artists" is repaired on re-import.

`album_artist` is a local-authority field, so the ordinary upsert deliberately
PRESERVES a stored value. That is what makes the pollution sticky — and why the
prevention fix alone is not enough for a library that already has it.
"""

from __future__ import annotations

from sqlalchemy import text


def _seed(session, rows):
    session.execute(text("DELETE FROM tracks WHERE album = 'The Mountain'"))
    for index, (title, artist, album_artist) in enumerate(rows, start=1):
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album_artist, album, title, "
                "track_number, disc_number, file_path) "
                "VALUES (:id, :ar, :aa, 'The Mountain', :ti, :tn, '1', :fp)"
            ),
            {
                "id": f"m{index}", "ar": artist, "aa": album_artist, "ti": title,
                "tn": str(index), "fp": f"/music/Gorillaz/2026 - The Mountain/{index}.flac",
            },
        )
    session.commit()


def _album_artists(session):
    return [
        str(r[0])
        for r in session.execute(
            text("SELECT album_artist FROM tracks WHERE album = 'The Mountain' ORDER BY id")
        ).fetchall()
    ]


def _payloads(session):
    return [
        {"id": str(r[0]), "artist": str(r[1])}
        for r in session.execute(
            text("SELECT id, artist FROM tracks WHERE album = 'The Mountain' ORDER BY id")
        ).fetchall()
    ]


def test_a_polluted_album_is_repaired(db_session):
    """Every track says Gorillaz, the rows say Various Artists — heal them."""
    from services.scanning.navidrome_import import _heal_contaminated_va_album_artist

    _seed(db_session, [
        ("The Mountain", "Gorillaz", "Various Artists"),
        ("Silent Running", "Gorillaz", "Various Artists"),
        ("The Fixed Point", "Gorillaz", "Various Artists"),
    ])
    assert set(_album_artists(db_session)) == {"Various Artists"}

    changed = _heal_contaminated_va_album_artist(_payloads(db_session), "Gorillaz")

    assert changed == 3
    assert set(_album_artists(db_session)) == {"Gorillaz"}, (
        "the stored VA value is protected by the upsert, so it must be repaired "
        "explicitly or the album never heals"
    )


def test_a_real_compilation_is_left_alone(db_session):
    from services.scanning.navidrome_import import _heal_contaminated_va_album_artist

    _seed(db_session, [
        ("Song A", "HIM", "Various Artists"),
        ("Song B", "Muse", "Various Artists"),
        ("Song C", "Gorillaz", "Various Artists"),
    ])

    changed = _heal_contaminated_va_album_artist(_payloads(db_session), "Various Artists")

    assert changed == 0
    assert set(_album_artists(db_session)) == {"Various Artists"}


def test_a_compilation_cannot_be_rewritten_even_with_a_specific_target(db_session):
    """Several performers: the resolved artist must not win."""
    from services.scanning.navidrome_import import _heal_contaminated_va_album_artist

    _seed(db_session, [
        ("Song A", "HIM", "Various Artists"),
        ("Song B", "Gorillaz", "Various Artists"),
    ])

    changed = _heal_contaminated_va_album_artist(_payloads(db_session), "Gorillaz")
    assert changed == 0
    assert set(_album_artists(db_session)) == {"Various Artists"}


def test_an_unpolluted_album_is_untouched(db_session):
    from services.scanning.navidrome_import import _heal_contaminated_va_album_artist

    _seed(db_session, [
        ("The Mountain", "Gorillaz", "Gorillaz"),
        ("Silent Running", "Gorillaz", "Gorillaz"),
    ])

    assert _heal_contaminated_va_album_artist(_payloads(db_session), "Gorillaz") == 0
    assert set(_album_artists(db_session)) == {"Gorillaz"}


def test_a_composite_credit_row_is_healed_too(db_session):
    """A leftover "Various Artists • Gorillaz" is the same contamination."""
    from services.scanning.navidrome_import import _heal_contaminated_va_album_artist

    _seed(db_session, [
        ("Song A", "Gorillaz", "Various Artists • Gorillaz"),
        ("Song B", "Gorillaz", "Various Artists • Gorillaz"),
    ])

    # Every track artist agrees with the resolved name, so this is not a
    # compilation — the composite VA credit is stale, not a classification.
    changed = _heal_contaminated_va_album_artist(_payloads(db_session), "Gorillaz")
    assert changed == 2
    assert set(_album_artists(db_session)) == {"Gorillaz"}
