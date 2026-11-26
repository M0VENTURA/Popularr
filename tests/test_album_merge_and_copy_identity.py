"""Merging split releases, and the MBIDs a library copy must not keep.

Reported
--------
1. *"When two releases aren't aligned with the same MBID, it currently shows as
   a warning on the album page that one or two other releases are different, but
   when I go to one of the other releases and look up mbid, it says it matches,
   but doesn't offer the corrections. On the main album page, can we have a
   button to select merge releases that will merge all other releases in to the
   release on the page that's being used?"*

2. *"When the downloader copies a track from another item in the collection, it
   doesn't update MBIDs to the new release ... It keeps the original that it's
   copied."*

For (1) there are TWO cases and they need different machinery:
  * same release group  -> ``unify_album_release_identity`` (fan the one id)
  * different groups     -> it REFUSES ("multiple_groups"), so the route must
    fall back to an unfiltered ``update_album_ids`` fan-out.

For (2) the copied file arrives with the SOURCE release's
MUSICBRAINZ_RELEASETRACKID — a position on a release it no longer belongs to —
and that frame was never written by the import at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    import re

    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


# ---------------------------------------------------------------------------
# 1. The merge button
# ---------------------------------------------------------------------------

class TestTheAlbumPageOffersAMergeButton:
    def test_the_button_is_inside_the_split_warning(self):
        """It belongs where the SPLIT is reported, not in a menu elsewhere."""
        template = _strip_comments(_read(TEMPLATE))
        notice_at = template.index('id="albumMixedReleasesNotice"')
        button_at = template.index('id="mergeReleasesBtn"')
        # The button must be inside the banner, i.e. before the banner's <ul>.
        list_at = template.index("<ul", notice_at)
        assert notice_at < button_at < list_at, (
            "the merge button is outside the mixed-releases banner"
        )

    def test_the_button_carries_the_ids_the_endpoint_needs(self):
        template = _strip_comments(_read(TEMPLATE))
        start = template.index('id="mergeReleasesBtn"')
        button = template[start : template.index(">", start)]
        for attr in (
            "data-artist",
            "data-album",
            "data-release-mbid",
            "data-release-group-mbid",
        ):
            assert attr in button, f"the merge button is missing {attr}"

    def test_the_button_says_how_many_tracks_it_will_move(self):
        template = _strip_comments(_read(TEMPLATE))
        start = template.index('id="mergeReleasesBtn"')
        end = template.index("</button>", start)
        assert "unscoped_track_count" in template[start:end], (
            "the action should state the number of tracks it will rewrite"
        )

    def test_the_handler_exists_and_uses_the_themed_confirm(self):
        js = _strip_comments(_read(ALBUM_JS))
        assert "mergeReleases" in js
        assert "'/api/album/merge-releases'" in js
        # ``global.confirm`` is the NATIVE dialog; the themed one is ui.confirm.
        assert "global.ui" in js and "global.ui.confirm" in js, (
            "the merge must ask with the app's themed dialog, not the native one"
        )


class TestTheMergeEndpoint:
    def test_the_route_is_registered(self):
        from app import app

        assert "/api/album/merge-releases" in {str(r) for r in app.url_map.iter_rules()}

    def test_the_page_supplies_its_own_release_as_the_target(self):
        """⚠️ The target must come from the SCOPED rows.

        A folder can show release A while most of its rows are release B (that
        is exactly what the split warning reports). Merging toward the majority
        would overwrite the release the user was looking at — the button lives on
        THIS page and must make THIS page's release win.
        """
        source = _read(REPO_ROOT / "routes" / "ui_routes.py")
        assert "_page_release_mbid" in source
        assert "album_mbid=_page_release_mbid" in source
        assert "album_rg_mbid=_page_release_group_mbid" in source

        block = source[source.index("_page_release_mbid = \"\"") :]
        block = block[: block.index("_other_releases.append")]
        assert "for _t in tracks" in block, (
            "the target must be read from the page's own (scoped) tracks"
        )

    def test_it_declines_without_a_release_id(self):
        """A page scoped to no release cannot be merged toward anything."""
        source = _read(REPO_ROOT / "routes" / "album_routes.py")
        block = source[source.index("api_album_merge_releases") :]
        block = block[: block.index("_MERGE_SKIP_MESSAGES")]
        assert "release_mbid" in block
        assert "nothing to merge toward" in block.lower() or "no release id" in block.lower()

    def test_it_handles_the_cross_group_case(self):
        """⚠️ ``unify_album_release_identity`` REFUSES two release groups.

        Two groups normally mean two genuinely different albums, so the unifier
        returns ``skipped: multiple_groups``. When the user says they are one
        album, the route must fall back rather than just failing.
        """
        source = _read(REPO_ROOT / "routes" / "album_routes.py")
        block = source[source.index("api_album_merge_releases") :]
        block = block[: block.index("_MERGE_SKIP_MESSAGES")]
        assert "multiple_groups" in block
        assert "update_album_ids" in block, (
            "the cross-group fallback must fan the id out unscoped"
        )

    def test_it_refuses_a_release_group_as_the_target(self):
        """The forbidden fallback, enforced by the service it delegates to."""
        source = _read(REPO_ROOT / "services" / "metadata" / "album_service.py")
        assert "is a release GROUP" in source


# ---------------------------------------------------------------------------
# 2. The copied file's release identity
# ---------------------------------------------------------------------------

class TestACopiedTrackTakesTheTargetReleaseIdentity:
    def test_the_import_resolves_the_release_track_id(self):
        """It is per RELEASE, and the copy inherits the source's."""
        source = _read(
            REPO_ROOT / "services" / "downloads" / "download_completion_service.py"
        )
        assert '"musicbrainz_releasetrackid"' in source, (
            "the import never resolves the release-track id, so a copied file "
            "keeps the source release's position"
        )
        assert "_match.get(\"release_track_mbid\")" in source, (
            "the release tracklist already carries the value under "
            "release_track_mbid"
        )

    def test_the_writer_writes_it(self):
        source = _read(REPO_ROOT / "services" / "metadata" / "tag_file_service.py")
        block = source[source.index("def update_file_metadata") :]
        block = block[: block.index("def embed_album_art")]
        assert "musicbrainz_releasetrackid" in block, (
            "update_file_metadata must write the release-track id — it is the "
            "writer the import uses"
        )

    def test_an_explicit_empty_string_clears_the_frame(self):
        """The copy service passes "" to drop the source's identity.

        ``None`` still means "not supplied, leave alone" — confusing the two
        would wipe tags on every import that omits the field.
        """
        source = _read(REPO_ROOT / "services" / "metadata" / "tag_file_service.py")
        block = source[source.index("def update_file_metadata") :]
        block = block[: block.index("def embed_album_art")]
        assert "_release_track_mbid is not None" in block, (
            "empty must be distinguishable from absent, or the copy cannot clear"
        )

    def test_the_copy_is_not_retagged_at_copy_time(self):
        """CONTROL: the copy is a byte-for-byte ``copy2``.

        The stale release-track id is resolved during the IMPORT (which knows
        the target release), not while copying — retagging here would mean
        writing to the staging file before anything knows where it belongs, and
        it broke the "the library file is COPIED, not modified" contract.
        """
        source = _read(
            REPO_ROOT / "services" / "downloads" / "library_copy_service.py"
        )
        assert "shutil.copy2(source_path, destination)" in source
        assert "_strip_stale_release_identity" not in source, (
            "a copy-time retag belongs in the import, not here"
        )

    def test_an_unresolvable_position_is_cleared_not_inherited(self):
        """⚠️ THE ACTUAL FIX for a copied track.

        A copy arrives carrying the SOURCE album's release-track id. The import
        resolves the target release's tracklist and overwrites it — but when the
        target release has no matching track there is nothing to write, and the
        file would silently keep the source's position. Empty is the honest
        value.
        """
        source = _read(
            REPO_ROOT / "services" / "downloads" / "download_completion_service.py"
        )
        assert 'meta["musicbrainz_releasetrackid"] = ""' in source, (
            "an unresolvable release-track id must be cleared, not inherited "
            "from the album the copy came from"
        )

    def test_the_release_track_id_is_read_from_the_queue_row(self):
        """A fully-resolved queue row must not need a MusicBrainz call."""
        source = _read(
            REPO_ROOT / "services" / "downloads" / "download_completion_service.py"
        )
        assert '_stored.get("musicbrainz_releasetrackid")' in source
