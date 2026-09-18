"""API v1 blueprint.

All stable API routes live under ``/api/v1/``.

Endpoints return consistent JSON responses using ``_ok()`` / ``_fail()``
helpers.  See each module for details.
"""

from quart import Blueprint

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

# Import sub-modules to register their routes.
#
# ``albums`` MUST be listed: it was written and documented as part of this
# package, but never imported here, so NOTHING in it was ever registered —
# every /api/v1/albums/... call the album page makes (musicbrainz-compare,
# bulk-delete) 404'd despite the file existing. The module's own docstring
# asks for this line.
from . import tracks, artists, albums
