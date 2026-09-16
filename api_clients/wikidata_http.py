"""Low-level Wikidata/Wikipedia HTTP client.

Follows Wikimedia API etiquette:
1. Compliant User-Agent with version and contact URL.
2. `maxlag` on Action API calls so non-interactive/background jobs back off
   automatically when replica DBs are lagged, instead of hammering primaries.
3. Exponential backoff on transient errors, honoring `Retry-After` on 429s.
4. Bounded concurrency (Wikimedia asks non-interactive clients to keep to a
   small number of parallel requests).
5. Thread-safe in-memory LRU caching, since entity/summary data is fairly
   static and re-querying wastes both our quota and their capacity.
"""

from __future__ import annotations

import copy
import re
import threading
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from api_clients.http_utils import create_retry_client

logger = structlog.get_logger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

USER_AGENT = "Popularr/2.0 ( https://github.com/M0VENTURA/Popularr )"

# Wikidata entity IDs look like Q42, Q2831 etc.
WIKIDATA_QID_RE = re.compile(r"^Q\d+$", re.IGNORECASE)

# Non-interactive/background clients are asked to set maxlag so requests
# are auto-declined (with a 503 + Retry-After-like `lag`) when replica DBs
# fall behind, rather than piling more load on a struggling cluster.
DEFAULT_MAXLAG = 5

# Wikimedia asks bot/background clients to keep concurrency low.
_MAX_CONCURRENT_REQUESTS = 3
_CONCURRENCY_SEMAPHORE = threading.Semaphore(_MAX_CONCURRENT_REQUESTS)


class _LruCache:
    """Thread-safe, size-bounded LRU cache. Values are deep-copied on both
    set and get so callers can freely mutate results without corrupting the
    shared cache entry."""

    def __init__(self, max_size: int):
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return copy.deepcopy(self._data[key])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = copy.deepcopy(value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)


_ENTITY_SEARCH_CACHE = _LruCache(max_size=2000)
_ENTITY_DETAIL_CACHE = _LruCache(max_size=2000)
_WIKIPEDIA_SUMMARY_CACHE = _LruCache(max_size=2000)


def _is_valid_qid(entity_id: str) -> bool:
    return bool(entity_id) and bool(WIKIDATA_QID_RE.match(entity_id))


def _is_retryable_wiki_error(exc: BaseException) -> bool:
    """Retry on transient network drops or rate-limiting/server errors."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    return False


def _wait_for_wiki_retry_after(retry_state) -> float:
    """Honor a server-provided `Retry-After` header on 429/503 responses,
    falling back to exponential backoff otherwise."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    return wait_exponential(multiplier=1.0, min=1.0, max=8.0)(retry_state)


class WikidataHttpClient:
    """Wikidata Action API + Wikipedia REST summary client."""

    def __init__(self, http_session: Any = None):
        self.session = http_session or create_retry_client(
            user_agent=USER_AGENT,
            retries=2,
            backoff=1.0,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=_wait_for_wiki_retry_after,
        retry=retry_if_exception(_is_retryable_wiki_error),
        reraise=True,
    )
    def _get(self, url: str, *, params: dict[str, Any] | None = None, timeout: float = 6.0) -> httpx.Response:
        with _CONCURRENCY_SEMAPHORE:
            resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp

    def search_entities(self, name: str, limit: int = 5) -> list[dict]:
        """Fuzzy text search for Wikidata entities."""
        if not name:
            return []

        cache_key = f"{name.lower()}::{limit}"
        cached = _ENTITY_SEARCH_CACHE.get(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._get(
                WIKIDATA_API,
                params={
                    "action": "wbsearchentities",
                    "search": name,
                    "language": "en",
                    "format": "json",
                    "type": "item",
                    "limit": limit,
                    "maxlag": DEFAULT_MAXLAG,
                },
            )
            results = resp.json().get("search", [])
        except Exception as exc:
            logger.warning("Wikidata entity search failed", name=name, error=str(exc))
            return []

        _ENTITY_SEARCH_CACHE.set(cache_key, results)
        return results

    def get_qid_by_musicbrainz_id(self, mbid: str) -> str | None:
        """Exact reverse-lookup of a Wikidata QID using a MusicBrainz Artist ID."""
        if not mbid:
            return None
            
        cache_key = f"mbid::{mbid}"
        cached = _ENTITY_SEARCH_CACHE.get(cache_key)
        if cached is not None:
            return cached
            
        try:
            resp = self._get(
                WIKIDATA_API,
                params={
                    "action": "query",
                    "list": "search",
                    # P434 is the MusicBrainz Artist ID property
                    "srsearch": f"haswbstatement:P434={mbid}", 
                    "format": "json",
                    "maxlag": DEFAULT_MAXLAG,
                }
            )
            results = resp.json().get("query", {}).get("search", [])
            qid = results[0]["title"] if results else None
            _ENTITY_SEARCH_CACHE.set(cache_key, qid)
            return qid
        except Exception as exc:
            logger.warning("Wikidata MBID search failed", mbid=mbid, error=str(exc))
            return None

    def get_entity(self, entity_id: str) -> dict:
        if not _is_valid_qid(entity_id):
            if entity_id:
                logger.debug("Rejected malformed Wikidata entity id", entity_id=entity_id)
            return {}

        cached = _ENTITY_DETAIL_CACHE.get(entity_id)
        if cached is not None:
            return cached

        entities = self._get_entities_raw([entity_id])
        data = entities.get(entity_id, {})
        if data:
            _ENTITY_DETAIL_CACHE.set(entity_id, data)
        return data

    def get_entities(self, entity_ids: list[str]) -> dict[str, dict]:
        """Batch entity lookup. The Wikidata API accepts up to 50 pipe-delimited
        ids per request (`ids=Q1|Q2|Q3`), which is far more efficient than one
        request per id when resolving several artists/works in a scan pass.
        """
        valid_ids = [eid for eid in dict.fromkeys(entity_ids or []) if _is_valid_qid(eid)]
        invalid_ids = [eid for eid in (entity_ids or []) if eid and not _is_valid_qid(eid)]
        if invalid_ids:
            logger.debug("Rejected malformed Wikidata entity id(s)", entity_ids=invalid_ids)
        if not valid_ids:
            return {}

        results: dict[str, dict] = {}
        to_fetch: list[str] = []
        for eid in valid_ids:
            cached = _ENTITY_DETAIL_CACHE.get(eid)
            if cached is not None:
                results[eid] = cached
            else:
                to_fetch.append(eid)

        # Wikidata's wbgetentities allows up to 50 ids per request.
        for i in range(0, len(to_fetch), 50):
            batch = to_fetch[i : i + 50]
            fetched = self._get_entities_raw(batch)
            for eid, data in fetched.items():
                if data:
                    _ENTITY_DETAIL_CACHE.set(eid, data)
                    results[eid] = data

        return results

    def _get_entities_raw(self, entity_ids: list[str]) -> dict[str, dict]:
        if not entity_ids:
            return {}
        try:
            resp = self._get(
                WIKIDATA_API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(entity_ids),
                    # Appended claims and labels so genres are actually returned
                    "props": "sitelinks|descriptions|claims|labels",
                    "sitefilter": "enwiki",
                    "languages": "en",
                    "format": "json",
                    "maxlag": DEFAULT_MAXLAG,
                },
            )
            return resp.json().get("entities", {}) or {}
        except Exception as exc:
            logger.warning("Wikidata entity lookup failed", entity_ids=entity_ids, error=str(exc))
            return {}

    def get_wikipedia_summary(self, title: str) -> str | None:
        if not title:
            return None

        # Wikipedia's canonical titles use underscores for spaces; normalizing
        # here avoids an extra redirect hop for the common "Artist Name" case.
        normalized_title = title.strip().replace(" ", "_")

        cached = _WIKIPEDIA_SUMMARY_CACHE.get(normalized_title)
        if cached is not None:
            return cached or None

        url = WIKIPEDIA_SUMMARY_API.format(title=quote(normalized_title, safe=""))
        try:
            # Replaced raw session.get with self._get to inherit @retry logic
            resp = self._get(url)
            extract = (resp.json().get("extract") or "").strip()
            _WIKIPEDIA_SUMMARY_CACHE.set(normalized_title, extract)
            return extract or None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                _WIKIPEDIA_SUMMARY_CACHE.set(normalized_title, "")
                return None
            logger.warning("Wikipedia summary lookup failed", title=title, error=str(exc))
            return None
        except Exception as exc:
            logger.warning("Wikipedia summary lookup failed", title=title, error=str(exc))
            return None
