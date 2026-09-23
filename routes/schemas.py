"""
Pydantic request/response schemas for Popularr API.

Provides type-validated models that can be used across all route modules.
Each model converts ``**data`` to the expected types and raises
``ValidationError`` with a clear message when input is invalid.

Usage in a route::

    from routes.schemas import SearchQuery

    data = SearchQuery(**request.args)
    results = search_service.query(data.query, data.limit)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchQuery(BaseModel):
    """Validate search request parameters."""
    query: str = Field(..., min_length=2, description="Search query string")
    limit: int = Field(default=20, ge=1, le=100, description="Max results")
    type: str | None = Field(default=None, description="Search scope")


class PaginationParams(BaseModel):
    """Common pagination parameters."""
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=100)


class ScanRequest(BaseModel):
    """Validate popularity scan request."""
    mode: str = Field(default="popularity", pattern=r"^(popularity|singles|metadata|all)$")
    force: bool = Field(default=False)
    restart: bool = Field(default=False)
    artist: str | None = Field(default=None)
    album: str | None = Field(default=None)
    #: Explicit resume point, chosen in the dashboard's resume prompt. Overrides
    #: the stored checkpoint so the user's pick is honoured. For an artist that
    #: had already finished, the caller passes the artist AFTER it — the scan
    #: loop processes the named artist itself (see resume_options_service).
    resume_from: str | None = Field(default=None)
