"""
Per-track popularity/enrichment stage.

This is the ONLY place that connects:
- enrichment external APIs
- popularity scoring
- single detection
- persistence

Optimized for high-concurrency: heavy text-search fallbacks are gated to prevent
rate-limit exhaustion and 300s+ timeout stalls on large albums.
"""

from __future__ import annotations

import json
import time
import re
from difflib import SequenceMatcher
from typing import Any

import structlog

# API clients
from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import ListenBrainzClient
from api_clients.listenbrainz import get_recording_tags

# Enrichment services
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    get_shared_mb_service,
)

# Popularity
from services.popularity.popularity_math import (
    apply_log_ratio_audit_to_stored_score,
    calculate_combined_popularity_score,
    calculate_listenbrainz_percentile,
    evaluate_listenbrainz_validity,
    evaluate_log_ratio_deviation,
    fmt_count as _fmt_count,
    is_interlude_lb_outlier,