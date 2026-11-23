"""Album identity — one answer to "are these two rows the same album?".

⭐ WHY THIS EXISTS

Popularr grouped albums by ``(album_artist, album)`` — two human strings. That
is not an identity. Measured on a real library:

    "Here to Stay"  vs  "Here to Stay (T Ray's mix)"

Both files carry the SAME ``musicbrainz_albumid``. Navidrome (with its default
PID rule) treats them as ONE album because it prefers the MBID. Popularr split
them, so the re-release became a one-track album — and a one-track "album"
has no distribution to curve a star rating across, so its album-relative
rating is meaningless.

Two files can also share a name but be genuinely different albums (a 1997
remaster), which is why the name alone is not a safe key either.

THE RULE

Mirror Navidrome's default album PID:

    musicbrainz_albumid|albumartistid,album,albumversion,releasedate

i.e. **prefer the MusicBrainz release id; fall back to the name tuple only when
it is absent.** This is deliberately the same precedence Navidrome uses, so a
folder cannot split in one app and not the other.

⚠️ Two files whose names differ but whose MBIDs match are ONE album. Two files
whose names match but whose MBIDs differ are TWO albums — they are different
releases that happen to share a title. Only rows with no MBID at all are grouped
by name.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Sentinel for "this row has no usable MusicBrainz release id".
NO_MBID = ""


def _field(row: Any, *names: str) -> str:
    """First non-empty value among *names*, tolerant of dicts and mappings."""
    for name in names:
        try:
            value = row.get(name) if hasattr(row, "get") else row[name]
        except Exception:
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def album_mbid(row: Any) -> str:
    """The MusicBrainz release id for a track row, lowercased, or ``""``.

    Both spellings exist in the schema (``musicbrainz_albumid`` is what the tag
    writer uses, ``musicbrainz_album_mbid`` what the proposal service uses), and
    a row can carry either. ``[no id]``/``none`` placeholders that some taggers
    write are treated as "absent" — otherwise two untagged rips would "share"
    an MBID and be merged.
    """
    raw = _field(row, "musicbrainz_albumid", "musicbrainz_album_mbid").casefold()
    if not raw or raw in {"[no id]", "no id", "none", "null", "0", "-"}:
        return NO_MBID
    return raw


def album_fallback_key(row: Any) -> tuple[str, str]:
    """The name tuple used when — and only when — there is no MBID."""
    artist = _field(row, "album_artist", "artist").casefold()
    album = _field(row, "album", "album_name").casefold()
    return (artist, album)


def album_identity(row: Any) -> tuple[str, ...]:
    """A hashable identity for the album a track row belongs to.

    Returned as a tuple whose FIRST element says which rule produced it, so a
    caller can never accidentally compare an MBID key with a name key:

        ("mbid", "3c8d8b38-…")     both rows carry the release id
        ("name", "korn", "here to stay")   neither does

    ⚠️ An MBID key and a name key for the same album are deliberately NOT equal.
    A track that has an MBID belongs with its release; a track that does not
    cannot be proven to belong anywhere, and merging it into the MBID group
    would be a guess.
    """
    mbid = album_mbid(row)
    if mbid:
        return ("mbid", mbid)
    artist, album = album_fallback_key(row)
    return ("name", artist, album)


def group_tracks_by_album(
    rows: Iterable[Any],
) -> dict[tuple[str, ...], list[Any]]:
    """Group track rows into albums, preserving first-seen order.

    Order is kept because the scan's own ordering (filing artist, album, disc,
    track) is what the rating pipeline expects.
    """
    groups: dict[tuple[str, ...], list[Any]] = {}
    for row in rows:
        groups.setdefault(album_identity(row), []).append(row)
    return groups


def describe_identity(identity: tuple[str, ...]) -> str:
    """Human-readable label, for logs and reports."""
    if identity and identity[0] == "mbid":
        return f"mbid:{identity[1]}"
    if len(identity) >= 3:
        return f"{identity[1]} — {identity[2]}"
    return "—"
