"""The configuration export must be shareable.

An export is only useful if it can leave the machine, so the contract is:

  * every credential and account name is replaced, in EVERY position a config
    can hold one (top-level, nested under api_integrations, inside the
    navidrome_users list);
  * the marker still distinguishes "configured" from "not configured", or a
    support thread cannot tell whether a key is missing;
  * nothing that is NOT a secret is destroyed — an over-eager redactor that
    blanks the whole settings file is as useless as a leaky one.
"""

from __future__ import annotations

import json

import pytest

from helpers.config_export import (
    REDACTED,
    build_config_export,
    redact_config,
)


@pytest.fixture
def secret_config() -> dict:
    return {
        "nav_user": "aaron",
        "nav_pass": "hunter2",
        "pg_password": "pgpass",
        "lastfm_api_key": "KEY123",
        "lastfm_api_secret": "SEC456",
        "discogs_token": "DTOK",
        "api_integrations": {
            "lastfm": {"api_key": "K1", "api_secret": "S1", "enabled": True},
            "gemini": {"api_key": "G1"},
            "listenbrainz": {"token": "LB1"},
            "audiodb": {"api_key": "ADB1"},
            "slskd": {"api_key": "SLK1", "host": "localhost"},
        },
        "navidrome_users": [
            {"user": "u1", "password": "p1", "base_url": "http://nav"},
            {"user": "u2", "password": "p2", "base_url": "http://nav2"},
        ],
        "music_folder": "/music",
        "playlists": {"favourite_artists_name": "Secret Garden", "essential_max_tracks": 50},
        "num_users": 3,
    }


def _every_leaf(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _every_leaf(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _every_leaf(child, f"{prefix}[{index}]")
    else:
        yield prefix, value


def test_no_credential_survives_the_export(secret_config: dict) -> None:
    blob = json.dumps(build_config_export(secret_config))
    secrets = [
        "aaron", "hunter2", "pgpass", "KEY123", "SEC456", "DTOK",
        "K1", "S1", "G1", "LB1", "ADB1", "SLK1", "u1", "u2", "p1", "p2",
    ]
    leaked = [s for s in secrets if s in blob]
    assert not leaked, f"credentials leaked into the export: {leaked}"


def test_every_credential_position_is_marked(secret_config: dict) -> None:
    out = build_config_export(secret_config)
    marked = set(out["redacted_keys"])
    expected = {
        "nav_user",
        "nav_pass",
        "pg_password",
        "lastfm_api_key",
        "lastfm_api_secret",
        "discogs_token",
        "api_integrations.lastfm.api_key",
        "api_integrations.lastfm.api_secret",
        "api_integrations.gemini.api_key",
        "api_integrations.listenbrainz.token",
        "api_integrations.audiodb.api_key",
        "api_integrations.slskd.api_key",
        "navidrome_users[0].user",
        "navidrome_users[0].password",
        "navidrome_users[1].user",
        "navidrome_users[1].password",
    }
    assert expected <= marked, f"not marked: {sorted(expected - marked)}"


def test_the_marker_distinguishes_configured_from_missing(secret_config: dict) -> None:
    """A blank secret stays blank; a set one becomes the marker.

    Without this a reader cannot tell "no Last.fm key configured" from "the key
    was stripped for sharing", which is the first thing support needs to know.
    """
    cfg = dict(secret_config)
    cfg["lastfm_api_key"] = ""
    cfg["discogs_token"] = None
    out = redact_config(cfg)
    assert out["lastfm_api_key"] == ""
    assert out["discogs_token"] == ""
    assert out["lastfm_api_secret"] == REDACTED


def test_non_secrets_are_preserved(secret_config: dict) -> None:
    out = redact_config(secret_config)
    assert out["music_folder"] == "/music"
    assert out["playlists"]["favourite_artists_name"] == "Secret Garden", (
        "a playlist whose NAME happens to contain 'Secret' must not be redacted"
    )
    assert out["playlists"]["essential_max_tracks"] == 50
    assert out["api_integrations"]["lastfm"]["enabled"] is True
    assert out["api_integrations"]["slskd"]["host"] == "localhost"
    assert out["navidrome_users"][0]["base_url"] == "http://nav"


def test_a_count_is_not_an_identity(secret_config: dict) -> None:
    """``num_users`` is a number, not a username."""
    assert redact_config(secret_config)["num_users"] == 3


def test_the_list_shape_and_length_are_kept(secret_config: dict) -> None:
    out = redact_config(secret_config)
    assert len(out["navidrome_users"]) == 2, "the user COUNT is useful for support"


def test_changed_from_default_reports_drift() -> None:
    """This is the audit the Config page otherwise makes you do by hand."""
    config = {
        "single_detection": {"zscore_high_threshold": 0.8, "star5_album_z": 1.0},
        "popularity": {"track_timeout_seconds": 120},
    }
    defaults = {
        "single_detection": {"zscore_high_threshold": 1.0, "star5_album_z": 1.0},
        "popularity": {"track_timeout_seconds": 30},
    }
    changed = build_config_export(config, defaults=defaults)["changed_from_default"]
    assert changed == {
        "single_detection.zscore_high_threshold": 0.8,
        "popularity.track_timeout_seconds": 120,
    }, "0.8 vs 1.0 must count as drift; 1.0 vs 1.0 must not"


def test_numeric_string_and_float_compare_equal() -> None:
    """`0.10` in the page versus `0.1` in the default is NOT drift."""
    config = {"a": {"b": "0.10"}}
    defaults = {"a": {"b": 0.1}}
    assert build_config_export(config, defaults=defaults)["changed_from_default"] == {}


def test_no_defaults_means_no_drift_section() -> None:
    assert build_config_export({"a": 1})["changed_from_default"] == {}

# ── The shipped keys this feature was written for ──────────────────────────────

def test_every_shipped_secret_key_is_caught() -> None:
    """Guard against the abbreviation trap this actually hit.

    ``nav_pass`` was NOT redacted by the first implementation because "pass" is
    a fragment of "password", not a word of its own. These are the credential
    keys the shipped settings model defines.
    """
    for key in (
        "nav_pass", "pg_password", "lastfm_api_key", "lastfm_api_secret",
        "discogs_token", "gemini_api_key", "listenbrainz_token", "slskd_api_key",
    ):
        assert redact_config({key: "value"})[key] == REDACTED, f"{key} not redacted"
