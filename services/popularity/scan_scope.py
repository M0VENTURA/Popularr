"""Scan-scoped flags that must OUTLIVE a single artist's ``options`` dict.

WHY THIS MODULE EXISTS
----------------------
A full-library scan calls ``run_artist_scan_pipeline(artist)`` once per artist.
Each of those calls builds its **own** ``options`` dict inside
``scan_stage_runner.run_scan()``:

    options = {"verbose": ..., "artist_filter": artist, ...}

So any "do this once per scan" marker stored *on that dict* is re-created for
every artist and can never fire. The state is a property of the SCAN, not of an
artist — exactly the reasoning that made ``scan_cancellation`` a module-level
registry rather than a key on ``options``.

⚠️ THE BUG THIS FIXES
--------------------
``finalise_stage._run_global_playlist_rebuild`` is guarded by
``options["_global_playlists_done"]`` and its own comment promised:

    "It also runs ONCE per scan (this function is marker-guarded), not once
     per artist"

That was false. Because the marker died with each artist's dict, EVERY artist
rebuilt the genre playlists, the New Music playlist and the entire playlist
index (``sync_playlist_index`` — every playlist the server reports, ~615 of them
and ~75–82k entries). Measured on a two-artist window of a real scan, that was
~5–8 minutes of pure waste per artist; across 982 artists it is the single
largest avoidable cost in the pipeline.

WHAT "DEFERRED" MEANS HERE
--------------------------
The global rebuild is not merely *cheap to skip* — running it per artist is
semantically wrong as well. Genre top-tracks, New Music and the index all
aggregate over the WHOLE library, so they belong at the END of a full scan, not
after artist #1. So the full-scan orchestrator:

    1. marks the rebuild DEFERRED before the artist loop
       (``defer_global_playlists``), which makes every per-artist call a no-op;
    2. clears the deferral and runs the rebuild once when the loop finishes
       (``resume_global_playlists`` + a forced call).

A TARGETED scan (artist page / album page / dashboard mode) never defers, so it
keeps its existing behaviour: one rebuild, at the end of that single run. The
per-``options`` marker stays as a second guard for the paths that can call the
rebuild twice within one run.
"""

from __future__ import annotations

import threading

__all__ = [
    "defer_global_playlists",
    "resume_global_playlists",
    "global_playlists_deferred",
    "reset",
]

#: Guards ``_deferred``. Cheap, and the flag is read from artist threads.
_LOCK = threading.Lock()

#: True for the duration of a FULL scan's artist loop. While set, any per-artist
#: call to the global playlist rebuild returns immediately, because the scan end
#: owns it.
#:
#: ⚠️ It stays SET until ``resume_global_playlists`` — including while the
#: orchestrator performs its own end-of-scan flush, which passes
#: ``force=True`` to bypass this check. That is deliberate: an ABANDONED
#: artist's worker thread can still be running (see ``scan_cancellation``), and
#: it must not run a second global rebuild after the orchestrator has already
#: done it.
_deferred: bool = False


def defer_global_playlists() -> None:
    """Mark the global playlist rebuild deferred to the end of this scan.

    Call once, before the full scan's artist loop. Idempotent.
    """
    global _deferred
    with _LOCK:
        _deferred = True


def resume_global_playlists() -> None:
    """Clear the deferral so later (non-full-scan) rebuilds behave normally."""
    global _deferred
    with _LOCK:
        _deferred = False


def global_playlists_deferred() -> bool:
    """True while a full scan's artist loop owns the global playlist rebuild."""
    with _LOCK:
        return _deferred


def reset() -> None:
    """Drop the deferral. For tests and for a clean start of a new scan."""
    resume_global_playlists()
