"""Prefetch caches follow Config and re-fetch only per stale source.

Reported: *"Is the prefetch information saved to database? This shouldn't
need to be grabbed each time that artist is scanned if it's not a forced
scan … But that artist had ran 3 times each time the server was restarted,
each time doing a prefetch again."*

Two defects, both pinned here:

1. **The release-cache gate combined MusicBrainz and Discogs freshness.**
   ``prefetch_artist_releases`` skipped only when the artist was fresh AND
   (a Discogs row existed OR no Discogs id was known). An artist with a
   Discogs id whose Discogs fetch returned **0 rows** was therefore
   permanently "stale": MusicBrainz was re-fetched on EVERY scan — the
   reported restart symptom (the popularity cache, which is per-track fresh,
   kept working: the same log showed "167 scores pre-loaded").

2. **The TTL was hardcoded to 7 days** and ignored the Scan Behaviour
   windows. ``features.prefetch_cache_days`` (default 7, Config → Scan
   Behaviour) is now the single knob for both caches.

The fix: freshness is checked PER SOURCE, an empty Discogs result is
remembered (a reserved sentinel row every reader filters out), and the TTL
comes from Config.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


_SENTINEL = "__discogs_checked__"


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/cache.db")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE artist_release_cache (
                id INTEGER PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                release_type TEXT,
                category TEXT,
                source TEXT NOT NULL,
                release_id TEXT,
                year INTEGER,
                is_promo BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_artist_release_artist_title_source UNIQUE (artist, title, source)
            )
        """))
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _session(*args, **kwargs):
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    from services.popularity import release_cache_service as rcs

    monkeypatch.setattr(rcs, "db_session", _session)

    def _open():
        return _session()

    yield _open


def _set_prefetch_days(monkeypatch, days: float) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_feature",
        lambda key, default=None: days if key == "prefetch_cache_days" else default,
    )


def _mb_rows():
    return [
        {"title": "Album One", "release_type": "album", "category": "Album",
         "source": "musicbrainz", "release_id": "mb-1", "year": 2020},
        {"title": "Single One", "release_type": "single", "category": "Single",
         "source": "musicbrainz", "release_id": "mb-2", "year": 2021},
    ]


def _patch_fetches(monkeypatch, mb_calls: list, discogs_calls: list, discogs_rows=None):
    """Record calls and return canned rows (no network)."""
    from services.popularity import release_cache_service as rcs

    def _mb(artist):
        mb_calls.append(artist)
        return list(_mb_rows())

    def _discogs(artist, artist_id):
        discogs_calls.append((artist, artist_id))
        return list(discogs_rows or [])

    monkeypatch.setattr(rcs, "_fetch_musicbrainz_releases", _mb)
    monkeypatch.setattr(rcs, "_fetch_discogs_releases", _discogs)


# ---------------------------------------------------------------------------
# The reported bug: an empty Discogs result must not force a MusicBrainz re-fetch
# ---------------------------------------------------------------------------

def test_empty_discogs_result_does_not_refetch_musicbrainz(cache_db, monkeypatch):
    """The exact restart symptom: artist with a Discogs id, 0 Discogs rows.
    The second call must skip BOTH sources."""
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    mb_calls: list = []
    discogs_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, discogs_calls, discogs_rows=[])

    # Run 1: both fetches happen (Discogs returns 0 rows).
    rcs.prefetch_artist_releases("Mono Inc.", "640496")
    assert mb_calls == ["Mono Inc."]
    assert discogs_calls == [("Mono Inc.", "640496")]

    # Run 2 (a "restart" — same DB, fresh process): BOTH must be skipped.
    rcs.prefetch_artist_releases("Mono Inc.", "640496")
    assert mb_calls == ["Mono Inc."], "MusicBrainz must not re-fetch when fresh"
    assert discogs_calls == [("Mono Inc.", "640496")], "Discogs must not re-fetch after an empty check"


def test_fresh_artist_with_discogs_rows_skips_both(cache_db, monkeypatch):
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    mb_calls: list = []
    discogs_calls: list = []
    _patch_fetches(
        monkeypatch, mb_calls, discogs_calls,
        discogs_rows=[{"title": "D-Gram", "release_type": "album", "category": "Album",
                       "source": "discogs", "release_id": "dg-1", "year": 2019}],
    )

    rcs.prefetch_artist_releases("Mono Inc.", "640496")
    rcs.prefetch_artist_releases("Mono Inc.", "640496")
    assert mb_calls == ["Mono Inc."]
    assert discogs_calls == [("Mono Inc.", "640496")]


def test_mb_fresh_but_discogs_stale_refetches_only_discogs(cache_db, monkeypatch):
    """Per-source independence: a stale Discogs source must not re-fetch MB."""
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    mb_calls: list = []
    discogs_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, discogs_calls, discogs_rows=[])

    rcs.prefetch_artist_releases("Mono Inc.", "640496")

    # Make ONLY the discogs source stale by deleting its sentinel row.
    with cache_db() as session:
        session.execute(
            text("DELETE FROM artist_release_cache WHERE source = 'discogs'"),
        )

    rcs.prefetch_artist_releases("Mono Inc.", "640496")
    assert mb_calls == ["Mono Inc."], "MB must stay skipped while fresh"
    assert len(discogs_calls) == 2, "Discogs re-fetches once its rows are gone"


def test_no_discogs_id_skips_discogs_but_fetches_mb_when_stale(cache_db, monkeypatch):
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    mb_calls: list = []
    discogs_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, discogs_calls)

    rcs.prefetch_artist_releases("Some Artist", "")
    assert mb_calls == ["Some Artist"]
    assert discogs_calls == []  # no id -> no Discogs fetch


# ---------------------------------------------------------------------------
# The sentinel is remembered but never leaks into any reader
# ---------------------------------------------------------------------------

def test_sentinel_is_written_for_empty_discogs(cache_db, monkeypatch):
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    mb_calls: list = []
    discogs_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, discogs_calls, discogs_rows=[])

    rcs.prefetch_artist_releases("Mono Inc.", "640496")

    with cache_db() as session:
        row = session.execute(
            text("SELECT title, release_type FROM artist_release_cache "
                 "WHERE artist = 'Mono Inc.' AND source = 'discogs'"),
        ).fetchone()
    assert row is not None
    assert row[0] == _SENTINEL
    assert (row[1] or "") == ""


def test_sentinel_never_appears_in_single_titles(cache_db, monkeypatch):
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    _patch_fetches(monkeypatch, [], [], discogs_rows=[])
    rcs.prefetch_artist_releases("Mono Inc.", "640496")

    singles = rcs.get_artist_single_titles("Mono Inc.")
    # The MB fixture writes a real single; the Discogs sentinel must not add one.
    assert _SENTINEL not in singles
    assert rcs.get_artist_promo_titles("Mono Inc.") == set()


def test_sentinel_excluded_from_cached_rows(cache_db, monkeypatch):
    """DiscogsService must never see the sentinel as a release."""
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    _patch_fetches(monkeypatch, [], [], discogs_rows=[])
    rcs.prefetch_artist_releases("Mono Inc.", "640496")

    rows = rcs.get_cached_artist_release_rows("Mono Inc.", source="discogs")
    assert rows == []


def test_sentinel_excluded_from_missing_releases(cache_db, monkeypatch):
    """refresh_missing_releases_for_artist only reads source='musicbrainz'."""
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 7)
    _patch_fetches(monkeypatch, [], [], discogs_rows=[])
    rcs.prefetch_artist_releases("Mono Inc.", "640496")

    with cache_db() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM artist_release_cache "
                 "WHERE source = 'musicbrainz' AND title = :t"),
            {"t": _SENTINEL},
        ).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# The TTL follows Config
# ---------------------------------------------------------------------------

def test_prefetch_days_zero_never_fresh(cache_db, monkeypatch):
    """features.prefetch_cache_days = 0 means always re-fetch."""
    from services.popularity import release_cache_service as rcs

    _set_prefetch_days(monkeypatch, 0)
    mb_calls: list = []
    discogs_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, discogs_calls)

    rcs.prefetch_artist_releases("Mono Inc.", "")
    rcs.prefetch_artist_releases("Mono Inc.", "")
    assert mb_calls == ["Mono Inc.", "Mono Inc."]


def test_prefetch_days_extends_freshness(cache_db, monkeypatch):
    """A 30-day window keeps a 10-day-old cache fresh (7 would not)."""
    from datetime import datetime, timedelta

    from services.popularity import release_cache_service as rcs

    # Seed a 10-day-old MusicBrainz row.
    with cache_db() as session:
        session.execute(
            text("""
                INSERT INTO artist_release_cache
                    (artist, title, release_type, category, source, release_id, year, is_promo, updated_at)
                VALUES ('Mono Inc.', 'Old', 'album', 'Album', 'musicbrainz', 'mb-1', 2020, FALSE, :ts)
            """),
            {"ts": datetime.utcnow() - timedelta(days=10)},
        )

    mb_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, [])

    # 7 days: stale -> re-fetch.
    _set_prefetch_days(monkeypatch, 7)
    rcs.prefetch_artist_releases("Mono Inc.", "")
    assert mb_calls == ["Mono Inc."]

    # 30 days: fresh -> skip.
    _set_prefetch_days(monkeypatch, 30)
    rcs.prefetch_artist_releases("Mono Inc.", "")
    assert mb_calls == ["Mono Inc."]


def test_default_is_seven_days(cache_db, monkeypatch):
    """Unset feature -> 7-day default (matches the old hardcoded TTL)."""
    from datetime import datetime, timedelta

    from services.popularity import release_cache_service as rcs

    monkeypatch.setattr(
        "helpers.config_helpers.get_feature",
        lambda key, default=None: default,  # feature absent -> default
    )

    with cache_db() as session:
        session.execute(
            text("""
                INSERT INTO artist_release_cache
                    (artist, title, release_type, category, source, release_id, year, is_promo, updated_at)
                VALUES ('Mono Inc.', 'Old', 'album', 'Album', 'musicbrainz', 'mb-1', 2020, FALSE, :ts)
            """),
            {"ts": datetime.utcnow() - timedelta(days=6)},
        )

    mb_calls: list = []
    _patch_fetches(monkeypatch, mb_calls, [])
    rcs.prefetch_artist_releases("Mono Inc.", "")
    assert mb_calls == []  # 6 days old, 7-day default -> fresh


# ---------------------------------------------------------------------------
# Popularity cache shares the same Config knob
# ---------------------------------------------------------------------------

def test_popularity_cache_hours_follow_config(monkeypatch):
    from services.popularity import popularity_cache_service as pcs

    monkeypatch.setattr(
        "helpers.config_helpers.get_feature",
        lambda key, default=None: 30 if key == "prefetch_cache_days" else default,
    )
    assert pcs._prefetch_cache_hours() == 30 * 24

    monkeypatch.setattr(
        "helpers.config_helpers.get_feature",
        lambda key, default=None: 0 if key == "prefetch_cache_days" else default,
    )
    assert pcs._prefetch_cache_hours() == 0


def test_release_cache_hours_follow_config(monkeypatch):
    from services.popularity import release_cache_service as rcs

    monkeypatch.setattr(
        "helpers.config_helpers.get_feature",
        lambda key, default=None: 14 if key == "prefetch_cache_days" else default,
    )
    assert rcs._prefetch_cache_hours() == 14 * 24


# ---------------------------------------------------------------------------
# Schema registry: the freshness column must be sync-added to old databases
# ---------------------------------------------------------------------------

def test_registry_includes_release_cache_updated_at():
    """A database bootstrapped before ``updated_at`` existed must gain it.

    Without this the freshness check (``MAX(updated_at)``) errors on such a
    database and the except-path returns False -> permanent re-fetch.
    """
    from db.schema import COLUMN_REGISTRY

    cols = COLUMN_REGISTRY.get("artist_release_cache") or {}
    assert "updated_at" in cols
    assert "TIMESTAMP" in cols["updated_at"].upper()
