"""
EdgarSession — the core of edgar_client, mirroring requests/sessions.py.

Owns all state that must persist across calls:
  headers       — User-Agent (SEC requirement) and defaults
  adapters      — URL-prefix → transport adapter map
  _rate_limiter — enforces ≤10 req/s SEC fair-use policy
  _ticker_cache — avoids re-fetching the 500 KB company-tickers map

Call chain (mirrors requests exactly):

    session.get(path)
      → session.request("GET", full_url)
        → session.prepare_request(EdgarRequest)  →  PreparedEdgarRequest
          → session.send(PreparedEdgarRequest)
            → session.get_adapter(url)           →  EdgarHTTPAdapter
              → adapter.send(PreparedEdgarRequest)  →  Response
                → domain_model.from_response(Response)  →  Company | CompanyFacts | …
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

from .adapters import BaseEdgarAdapter, EdgarHTTPAdapter
from .exceptions import InvalidSchemaError, InvalidUserAgentError
from .models import (
    Company,
    CompanyFacts,
    EdgarRequest,
    FinancialSeries,
    PreparedEdgarRequest,
    Response,
    SearchResult,
    _pad_cik,
)
from .rate_limiter import RateLimiter
from .structures import CaseInsensitiveDict
from .ticker_cache import TickerCache

# SEC fair-access policy requires this exact header format.
# We validate at construction time so the error surfaces before any HTTP call.
_USER_AGENT_RE = re.compile(r"^.+\s.+@.+\..+$")

# EDGAR full-text search lives on a different host than the data API.
_EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def merge_setting(request_setting: Any, session_setting: Any) -> Any:
    """Return the effective value, giving per-request values priority.

    For dict types, session-level provides the base and per-request keys
    overwrite — this lets callers add headers without losing the session
    defaults.  For scalars, per-request wins if not None.

    This mirrors requests.sessions.merge_setting exactly.
    """
    if request_setting is None:
        return session_setting
    if isinstance(session_setting, dict) and isinstance(request_setting, dict):
        merged = session_setting.copy()
        merged.update(request_setting)
        return merged
    return request_setting


# ---------------------------------------------------------------------------
# EdgarSession
# ---------------------------------------------------------------------------


class EdgarSession:
    """Manages the full request/response lifecycle for SEC EDGAR.

    Persistent state stored here (rather than on each call) because:

    headers         — SEC requires User-Agent on every request; set once.
    adapters        — Connection pool created at init; reused across calls.
    _ticker_cache   — Ticker→CIK map fetched once; ~500 KB payload.
    _rate_limiter   — Shared across all calls; per-call limiter would defeat it.

    Usage::

        session = EdgarSession(user_agent="Alice alice@example.com")
        company = session.get_company("AAPL")
        session.close()

        # Or as a context manager:
        with EdgarSession(user_agent="Alice alice@example.com") as s:
            facts = s.get_facts("MSFT")
    """

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://data.sec.gov",
        timeout: float | tuple[float, float] | None = 10.0,
    ) -> None:
        # Validate before touching anything else so the error message is clear.
        if not _USER_AGENT_RE.match(user_agent):
            raise InvalidUserAgentError(
                f"Invalid User-Agent {user_agent!r}. "
                "The SEC requires the format: 'Your Name your@email.com'"
            )

        # Strip trailing slash so paths like "/submissions/..." join cleanly.
        self.base_url: str = base_url.rstrip("/")
        self.timeout: float | tuple[float, float] | None = timeout
        self.verify: bool | str = True

        # Session-level headers sent with every request.
        # User-Agent is the SEC's primary identifier for rate-limiting.
        self.headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(
            {
                "User-Agent": user_agent,
                # Tell the server we accept gzip; urllib decompresses transparently.
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
                "Connection": "keep-alive",
            }
        )

        # Adapter map: URL prefix → adapter instance.
        # Longer prefixes take precedence (mount() keeps them sorted).
        self.adapters: OrderedDict[str, BaseEdgarAdapter] = OrderedDict()
        self.mount("https://", EdgarHTTPAdapter())
        self.mount("http://", EdgarHTTPAdapter())

        self._ticker_cache: TickerCache = TickerCache()
        self._rate_limiter: RateLimiter = RateLimiter(max_per_second=10)

    # ------------------------------------------------------------------
    # Adapter management
    # ------------------------------------------------------------------

    def mount(self, prefix: str, adapter: BaseEdgarAdapter) -> None:
        """Register *adapter* for all URLs whose prefix starts with *prefix*.

        Mirrors requests.Session.mount.  Longer prefixes win:
        "https://data.sec.gov" beats "https://" for data.sec.gov URLs,
        which lets you mount a mock adapter for one host while leaving
        real HTTP for others.
        """
        self.adapters[prefix] = adapter
        # Re-sort so get_adapter() always finds the longest match first.
        sorted_keys = sorted(self.adapters.keys(), key=len, reverse=True)
        self.adapters = OrderedDict((k, self.adapters[k]) for k in sorted_keys)

    def get_adapter(self, url: str) -> BaseEdgarAdapter:
        """Return the adapter registered for *url*.

        Raises InvalidSchemaError when no prefix matches (e.g. a ``file://``
        URL or a typo in the scheme).
        """
        for prefix, adapter in self.adapters.items():
            if url.lower().startswith(prefix.lower()):
                return adapter
        raise InvalidSchemaError(
            f"No adapter registered for {url!r}. "
            f"Registered prefixes: {list(self.adapters)}"
        )

    # ------------------------------------------------------------------
    # Request lifecycle
    # ------------------------------------------------------------------

    def get(
        self,
        path_or_url: str,
        params: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        """Send a GET request; return a Response.

        *path_or_url* can be:
        - A relative path:  ``"/submissions/CIK0000320193.json"``
          → prepends ``self.base_url`` (``"https://data.sec.gov"``)
        - A full URL:       ``"https://www.sec.gov/files/company_tickers.json"``
          → used as-is (allows cross-domain calls like the ticker map)

        Design decision: accepting both forms keeps domain methods concise
        (they pass short paths) while allowing TickerCache to fetch from
        www.sec.gov without needing a separate session or base_url override.
        """
        if path_or_url.startswith(("http://", "https://")):
            # Already a full URL — could be a different host (e.g. efts.sec.gov
            # for search, or www.sec.gov for the ticker map).
            url = path_or_url
        else:
            # Relative path — prepend base_url.
            # Normalise the join: ensure exactly one slash between base and path.
            sep = "" if path_or_url.startswith("/") else "/"
            url = f"{self.base_url}{sep}{path_or_url}"

        return self.request("GET", url, params=params, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        verify: bool | str | None = None,
        **kwargs: Any,
    ) -> Response:
        """Construct, prepare, and send a request; return the Response.

        Mirrors requests.Session.request — the central dispatch method.
        Every public method (get, get_company, search, …) routes through here.
        """
        req = EdgarRequest(method=method, url=url, headers=headers or {}, params=params)
        prep = self.prepare_request(req)
        settings = self.merge_environment_settings(
            url=prep.url or url,
            timeout=timeout,
            verify=verify,
        )
        return self.send(prep, **settings)

    def prepare_request(self, request: EdgarRequest) -> PreparedEdgarRequest:
        """Merge per-request settings with session defaults → PreparedEdgarRequest.

        The merge order (session first, request overwrites) means:
        - User-Agent is always present (from session.headers)
        - Per-call custom headers can override session defaults for one request
        """
        # merge_setting({}, dict(self.headers)) → session headers dominate
        # merge_setting({"X-Custom": "..."}, dict(self.headers)) → both present
        merged_headers = merge_setting(request.headers or {}, dict(self.headers))
        p = PreparedEdgarRequest()
        p.prepare(
            method=request.method,
            url=request.url,
            headers=merged_headers,
            params=request.params,
        )
        return p

    def merge_environment_settings(
        self,
        url: str,
        timeout: float | tuple[float, float] | None,
        verify: bool | str | None,
    ) -> dict[str, Any]:
        """Resolve per-request overrides against session defaults.

        Returns a kwargs dict ready to unpack into send().
        None means "caller didn't specify; use the session default".
        """
        return {
            "timeout": timeout if timeout is not None else self.timeout,
            "verify": verify if verify is not None else self.verify,
        }

    def send(
        self,
        request: PreparedEdgarRequest,
        *,
        timeout: float | tuple[float, float] | None = None,
        verify: bool | str = True,
        **kwargs: Any,
    ) -> Response:
        """Send a PreparedEdgarRequest via the appropriate adapter.

        Rate-limits before every call, then routes to the adapter whose
        URL prefix matches request.url.  Mirrors requests.Session.send.
        """
        # Acquire a rate-limiter slot *before* any network I/O.
        # This guarantees we never exceed 10 req/s regardless of which
        # thread or domain method is calling us.
        self._rate_limiter.acquire()
        adapter = self.get_adapter(url=request.url or "")
        return adapter.send(request, timeout=timeout, verify=verify)

    # ------------------------------------------------------------------
    # CIK resolution
    # ------------------------------------------------------------------

    def resolve_cik(self, ticker_or_cik: str) -> str:
        """Return a zero-padded 10-digit CIK string.

        Accepts:
          - A ticker symbol ("AAPL") → looks up via TickerCache
          - A numeric string ("320193" or "0000320193") → pads and returns
        """
        stripped = ticker_or_cik.strip()
        if stripped.isdigit():
            return _pad_cik(stripped)
        # Non-numeric → treat as ticker; TickerCache fetches the SEC map lazily.
        return self._ticker_cache.lookup(stripped, self)

    # ------------------------------------------------------------------
    # Domain API methods
    # ------------------------------------------------------------------

    def get_company(self, ticker_or_cik: str, **kwargs: Any) -> Company:
        """Fetch a company's profile and recent filing history.

        Endpoint: GET /submissions/CIK{padded}.json
        """
        cik = self.resolve_cik(ticker_or_cik)
        # Pass a short path — get() prepends base_url.
        return Company.from_response(
            self.get(f"/submissions/CIK{cik}.json", **kwargs)
        )

    def get_facts(self, ticker_or_cik: str, **kwargs: Any) -> CompanyFacts:
        """Fetch all XBRL financial facts for a company.

        Endpoint: GET /api/xbrl/companyfacts/CIK{padded}.json
        """
        cik = self.resolve_cik(ticker_or_cik)
        return CompanyFacts.from_response(
            self.get(f"/api/xbrl/companyfacts/CIK{cik}.json", **kwargs)
        )

    def get_concept(
        self,
        ticker_or_cik: str,
        taxonomy: str,
        concept: str,
        **kwargs: Any,
    ) -> FinancialSeries:
        """Fetch a single XBRL concept for a company.

        Endpoint: GET /api/xbrl/companyconcept/CIK{padded}/{taxonomy}/{concept}.json
        """
        cik = self.resolve_cik(ticker_or_cik)
        return FinancialSeries.from_dict(
            self.get(
                f"/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json",
                **kwargs,
            ).json()
        )

    def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """Search EDGAR full-text search (EFTS) for companies or filings.

        Uses a different host (efts.sec.gov) — get() handles that because it
        detects the full URL and doesn't prepend base_url.
        """
        params = {"q": query, **kwargs.pop("params", {})}
        response = self.get(_EFTS_SEARCH_URL, params=params, **kwargs)
        try:
            data = response.json()
        except Exception:
            return []
        hits: list[dict[str, Any]] = data.get("hits", {}).get("hits", [])
        return [SearchResult.from_hit(h) for h in hits]

    def search_by_name(self, name: str) -> list[SearchResult]:
        """Search for companies by name using the SEC ticker→CIK map.

        Uses TickerCache (loaded once, then in-memory) rather than the EFTS
        full-text search endpoint.  Faster for company-name lookups and avoids
        a separate HTTP call after the initial cache load.

        Results are ranked: exact match → prefix match → contains match.
        Returns an empty list when nothing matches.
        """
        entries = self._ticker_cache.search_by_name(name, self)
        return [SearchResult.from_ticker_map_entry(entry) for entry in entries]

    # ------------------------------------------------------------------
    # Context manager + cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close all adapters and release their resources."""
        for adapter in self.adapters.values():
            adapter.close()

    def __enter__(self) -> "EdgarSession":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
