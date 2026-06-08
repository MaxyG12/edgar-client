"""
EdgarClient — the single import users need.

from edgar_client import EdgarClient

client = EdgarClient(user_agent="Alice alice@example.com")
company  = client.get_company("AAPL")
facts    = client.get_facts("AAPL")
revenue  = client.get_revenue_history("AAPL")
results  = client.search("Apple")

Architecture
------------
EdgarClient is a thin "smart façade" over EdgarSession.  Its job is to
provide a friendly, forgiving public API:

  • Smart input resolution: accepts tickers ("AAPL"), CIKs ("0000320193"),
    or company names ("Apple") — figure out the right CIK automatically.
  • Convenience methods like get_revenue_history() that wrap common
    multi-step operations.
  • Cache and rate-limit controls the user shouldn't have to think about.

Everything that touches the network goes through EdgarSession (which owns
the rate limiter, connection pool, and ticker cache).  EdgarClient never
calls urllib directly.
"""

from __future__ import annotations

from typing import Any

from .exceptions import DataParseError, InvalidTickerError
from .models import (
    Company,
    CompanyFacts,
    FinancialSeries,
    SearchResult,
    _pad_cik,
)
from .sessions import EdgarSession

# ---------------------------------------------------------------------------
# Revenue concept fallback list
# ---------------------------------------------------------------------------
# Different companies (and different time periods) use different XBRL concept
# names for what is fundamentally the same line item: total revenue.
#
# The order matters — we try the most commonly used concepts first so the
# typical case requires only one dict lookup.
#
# Why so many alternatives?
#   • ASC 606 (2018) introduced the "RevenueFromContractWithCustomer" family,
#     replacing the older "Revenues" / "SalesRevenueNet" concepts.
#   • Pre-2018 filings still use the legacy names.
#   • Banks and financial services often report "RevenuesNetOfInterestExpense".
#   • Some companies use the "IncludingAssessedTax" variant to include sales tax.
#
# If none of these match, the company either files on Form 20-F (foreign
# private issuer, uses IFRS taxonomy) or uses a company-specific extension.

_REVENUE_CONCEPTS: list[tuple[str, str]] = [
    # Modern ASC 606 (most US companies post-2018)
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    # Legacy US GAAP (pre-2018 and certain industries)
    ("us-gaap", "Revenues"),
    ("us-gaap", "SalesRevenueNet"),
    ("us-gaap", "SalesRevenueGoodsNet"),
    # Financial services
    ("us-gaap", "RevenuesNetOfInterestExpense"),
]


# ---------------------------------------------------------------------------
# EdgarClient
# ---------------------------------------------------------------------------


class EdgarClient:
    """The primary entry point for the edgar_client library.

    This is the only class most users need to import.  It ties together
    EdgarSession (HTTP + rate limiting), TickerCache (ticker → CIK map),
    and the domain models (Company, CompanyFacts, FinancialSeries) into a
    single, easy-to-use interface.

    Quick start::

        from edgar_client import EdgarClient

        client = EdgarClient(user_agent="Alice alice@example.com")
        company = client.get_company("AAPL")      # or "Apple" or "0000320193"
        revenue = client.get_revenue_history("AAPL")
        results = client.search("Tesla")

    Context manager (auto-closes connections)::

        with EdgarClient(user_agent="Alice alice@example.com") as client:
            company = client.get_company("MSFT")

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Rate limiting — what it means for you
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    The SEC EDGAR fair-access policy allows at most 10 requests per second
    per user-agent.  EdgarClient enforces this automatically — you never
    need to add time.sleep() calls to your code.

    How it works (in plain English):

        Imagine a turnstile with a stopwatch.  The stopwatch records when
        the last request left.  Before each new request:

          • If 100 ms or more have passed → go through immediately.
          • If less than 100 ms has passed → wait for the remainder, then go.

    This guarantees no more than 10 requests per second regardless of
    how fast your code calls the client's methods.  When multiple threads
    share one EdgarClient, a lock serialises the check-and-update so
    concurrent threads queue correctly rather than all rushing through at once.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    CIK caching — why you don't pay for ticker lookups after the first
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    The SEC publishes a ~500 KB file (company_tickers.json) mapping every
    exchange-listed ticker to its numeric CIK.  On the first call that needs
    a ticker → CIK conversion, EdgarClient downloads this file and keeps it
    in memory.

    Subsequent lookups — whether for "AAPL" or any other ticker — are
    plain dictionary reads (O(1)).  The cache also powers the name-search
    feature in client.search().

    The cache lives for the lifetime of the EdgarClient instance.  To force
    a fresh download (e.g. if a company changed tickers), call
    client.invalidate_cache().
    """

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://data.sec.gov",
        timeout: float | tuple[float, float] | None = 10.0,
    ) -> None:
        """Create an EdgarClient.

        Parameters
        ----------
        user_agent:
            Required by the SEC's fair-access policy.  Must include your
            name and email address, e.g. "Alice alice@example.com".
            Raises InvalidUserAgentError immediately if the format is wrong —
            before any network call is made.
        base_url:
            Base URL for the EDGAR data API.  Override for testing against a
            local mock server.  Defaults to "https://data.sec.gov".
        timeout:
            Per-request timeout in seconds.  Accepts a float (applies to both
            connect and read phases) or a (connect, read) tuple.
            Defaults to 10.0 seconds.
        """
        # InvalidUserAgentError is raised here if format is wrong.
        self._session = EdgarSession(
            user_agent=user_agent,
            base_url=base_url,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def get_company(self, ticker_or_cik: str) -> Company:
        """Fetch a company's profile and most recent filings.

        *ticker_or_cik* accepts three input forms:

        - **Ticker symbol**: ``"AAPL"``, ``"aapl"``, ``"MSFT"``
          Looked up in the cached ticker→CIK map.

        - **Numeric CIK**: ``"0000320193"``, ``"320193"``
          Used directly (padded to 10 digits if needed).

        - **Company name**: ``"Apple"``, ``"apple inc"``
          Falls back to a name search when ticker lookup fails.
          Uses the first (best-ranked) result.

        Returns
        -------
        Company
            Profile with ``name``, ``cik``, ``sic_code``, filings list, etc.

        Raises
        ------
        InvalidUserAgentError
            Raised at construction time if user_agent is malformed.
        InvalidTickerError
            Input doesn't match any known ticker, CIK, or company name.
            Also raised if input is empty or whitespace-only.
        EdgarNotFoundError
            CIK exists but EDGAR has no submissions for it (HTTP 404).
        EdgarRateLimitError
            Too many requests; back off and retry (HTTP 429).
        EdgarConnectionError / EdgarTimeoutError
            Network-level failure.
        DataParseError
            SEC returned malformed JSON (missing required fields).
        """
        # Early validation: reject empty/None input before wasting a cache lookup.
        if not ticker_or_cik or not ticker_or_cik.strip():
            raise InvalidTickerError(
                "Ticker or CIK must not be empty. "
                "Pass a ticker symbol (e.g. 'AAPL'), "
                "a numeric CIK (e.g. '0000320193'), "
                "or a company name (e.g. 'Apple')."
            )

        cik = self._resolve_cik(ticker_or_cik)
        return self._session.get_company(cik)

    def get_facts(self, ticker_or_cik: str) -> CompanyFacts:
        """Fetch all XBRL financial facts for a company.

        *ticker_or_cik* accepts the same three input forms as get_company().

        Returns
        -------
        CompanyFacts
            Access individual series with ``facts.get(taxonomy, concept)``::

                facts = client.get_facts("AAPL")
                revenue = facts.get("us-gaap", "Revenues")
                print(revenue.latest_value)

        The response payload can exceed 10 MB for companies with long filing
        histories.  The default 10-second timeout is usually sufficient, but
        increase it for slow connections: ``EdgarClient(user_agent=..., timeout=30)``.

        Raises
        ------
        InvalidTickerError
            Input doesn't match any known ticker, CIK, or company name.
            Also raised if input is empty or whitespace-only.
        EdgarNotFoundError
            CIK exists but EDGAR has no facts for it (HTTP 404).
        EdgarRateLimitError
            Too many requests; back off and retry (HTTP 429).
        EdgarConnectionError / EdgarTimeoutError
            Network-level failure.
        DataParseError
            SEC returned malformed JSON (missing required fields).
        """
        # Early validation: reject empty/None input before wasting a cache lookup.
        if not ticker_or_cik or not ticker_or_cik.strip():
            raise InvalidTickerError(
                "Ticker or CIK must not be empty. "
                "Pass a ticker symbol (e.g. 'AAPL'), "
                "a numeric CIK (e.g. '0000320193'), "
                "or a company name (e.g. 'Apple')."
            )

        cik = self._resolve_cik(ticker_or_cik)
        return self._session.get_facts(cik)

    def search(self, company_name: str) -> list[SearchResult]:
        """Search for companies by name.

        Searches the SEC's company_tickers.json mapping (downloaded and cached
        on first call, ~500 KB).  Returns all entries whose name contains
        *company_name* as a substring (case-insensitive).

        Results are ranked:
          1. Exact match  (e.g. "apple" → "Apple Inc." would NOT be exact)
          2. Prefix match (e.g. "apple" → "Apple Inc.", "Apple REIT")
          3. Contains     (e.g. "apple" → "Snapple", "Big Apple Bankgroup")

        Note: this searches the ticker map (companies with CIKs), not EDGAR
        filing documents.  For full-text document search use the EDGAR full-text
        search site directly.

        Returns an empty list when nothing matches or when company_name is empty.

        Example::

            results = client.search("Apple")
            for r in results:
                print(r.name, r.ticker, r.cik)

        Raises
        ------
        TypeError
            If company_name is not a string.
        """
        # Type check: prevent TypeError from None or non-string.
        if company_name is None:
            raise TypeError("company_name must not be None.")
        if not isinstance(company_name, str):
            raise TypeError(
                f"company_name must be a string; got {type(company_name).__name__}."
            )

        # Empty/whitespace-only name: return empty list (matches expected behavior).
        if not company_name.strip():
            return []

        return self._session.search_by_name(company_name)

    def get_revenue_history(self, ticker_or_cik: str) -> FinancialSeries:
        """Return the annual revenue series for a company.

        Fetches the full XBRL fact tree (get_facts) and then searches for the
        first available revenue concept from the following list, in order:

        1. RevenueFromContractWithCustomerExcludingAssessedTax  ← ASC 606, modern
        2. RevenueFromContractWithCustomerIncludingAssessedTax
        3. Revenues                                              ← legacy, pre-2018
        4. SalesRevenueNet
        5. SalesRevenueGoodsNet
        6. RevenuesNetOfInterestExpense                          ← banks

        Why try multiple concepts?  The XBRL standard allows companies to choose
        among several valid revenue concepts.  Apple uses #1 (post-2018) and #3
        (historical).  Banks typically use #6.  Trying them in order gives the
        correct series for most companies without requiring the caller to know
        which concept name their company uses.

        Returns
        -------
        FinancialSeries
            Use ``series.latest_value`` for the most recent 10-K figure,
            ``series.as_dict()`` for {year: value} mapping, etc.

        Raises
        ------
        InvalidTickerError
            Input doesn't match any known ticker, CIK, or company name.
        EdgarNotFoundError
            CIK exists but EDGAR has no facts for it (HTTP 404).
        DataParseError
            None of the known revenue concepts were present — common for
            foreign private issuers (IFRS taxonomy) or companies that use
            custom extension concepts.  Also raised if SEC returns malformed JSON.
        """
        # Early validation: reject empty/None input before wasting a cache lookup.
        if not ticker_or_cik or not ticker_or_cik.strip():
            raise InvalidTickerError(
                "Ticker or CIK must not be empty. "
                "Pass a ticker symbol (e.g. 'AAPL'), "
                "a numeric CIK (e.g. '0000320193'), "
                "or a company name (e.g. 'Apple')."
            )

        facts = self.get_facts(ticker_or_cik)
        for taxonomy, concept in _REVENUE_CONCEPTS:
            try:
                return facts.get(taxonomy, concept)
            except KeyError:
                continue

        tried = ", ".join(c for _, c in _REVENUE_CONCEPTS)
        raise DataParseError(
            f"No revenue data found for {ticker_or_cik!r}. "
            f"Tried: {tried}. "
            "The company may use IFRS (foreign private issuer) or a custom "
            "taxonomy extension not covered by the standard concept list."
        )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Force a fresh download of the ticker→CIK map on the next call.

        The SEC updates company_tickers.json when companies list, delist, or
        change their ticker symbol.  If you notice a ticker lookup failing for
        a recently-listed company, call this method and retry.

        Note: invalidating the cache will trigger exactly one HTTP request
        (the company_tickers.json download) before the next lookup, which counts
        against the rate limit the same as any other request.
        """
        self._session._ticker_cache.invalidate()

    @property
    def cache_size(self) -> int:
        """Number of ticker symbols currently in the in-memory cache.

        Returns 0 before any ticker lookup has been made (cache not yet loaded).
        After the first lookup (or after calling search()), this reflects the
        total number of exchange-listed companies in the SEC map (~10 000).
        """
        return len(self._session._ticker_cache)

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def validate_inputs(self, **kwargs: Any) -> list[str]:
        """Validate input parameters; return list of validation error messages.

        Checks all provided parameters and returns a list of human-readable error
        messages. Returns empty list if all inputs are valid.

        This method enables "collect-all-errors" pattern — show the user all
        validation failures at once rather than making them fix one and retry.

        Parameters
        ----------
        cik : str
            Numeric CIK for validation. Must be 10 digits (will be zero-padded
            if necessary).
        ticker : str
            Ticker symbol for validation. Must be 1–5 uppercase ASCII letters.
        company_name : str
            Company name for validation. Must not be empty or whitespace-only.
        user_agent : str
            User-Agent header for validation. Must include name and email.
        base_url : str
            Base URL for validation. Must be a valid URL starting with http:// or https://.
        timeout : float | tuple | None
            Timeout value for validation. Must be non-negative if numeric.

        Returns
        -------
        list[str]
            List of validation error messages. Empty if all inputs valid.

        Example
        -------
        >>> errors = client.validate_inputs(
        ...     ticker="TOOLONG",
        ...     cik="notdigits",
        ...     company_name=""
        ... )
        >>> if errors:
        ...     for error in errors:
        ...         print(f"Error: {error}")
        """
        errors: list[str] = []

        # ── CIK validation ──────────────────────────────────────────────────
        if "cik" in kwargs:
            cik = kwargs["cik"]
            if cik is None:
                errors.append("CIK must not be None.")
            elif not isinstance(cik, str):
                errors.append(f"CIK must be a string; got {type(cik).__name__}.")
            elif not cik.strip():
                errors.append("CIK must not be empty or whitespace-only.")
            elif not cik.strip().isdigit():
                errors.append(
                    f"CIK must be all digits; got {cik!r}. "
                    "Pass a 10-digit CIK (e.g. '0000320193') or a ticker/company name."
                )
            else:
                padded = str(int(cik.strip())).zfill(10)
                if len(padded) != 10:
                    errors.append(
                        f"CIK after zero-padding is not 10 digits: {padded!r}."
                    )

        # ── Ticker validation ────────────────────────────────────────────────
        if "ticker" in kwargs:
            ticker = kwargs["ticker"]
            if ticker is None:
                errors.append("Ticker must not be None.")
            elif not isinstance(ticker, str):
                errors.append(f"Ticker must be a string; got {type(ticker).__name__}.")
            elif not ticker.strip():
                errors.append("Ticker must not be empty or whitespace-only.")
            else:
                stripped = ticker.strip().upper()
                if not stripped.isalpha():
                    errors.append(
                        f"Ticker must be 1–5 letters only; got {ticker!r}."
                    )
                elif len(stripped) > 5:
                    errors.append(
                        f"Ticker must be 1–5 letters; {ticker!r} has {len(stripped)} letters."
                    )

        # ── Company name validation ─────────────────────────────────────────
        if "company_name" in kwargs:
            name = kwargs["company_name"]
            if name is None:
                errors.append("Company name must not be None.")
            elif not isinstance(name, str):
                errors.append(f"Company name must be a string; got {type(name).__name__}.")
            elif not name.strip():
                errors.append("Company name must not be empty or whitespace-only.")

        # ── User-Agent validation ───────────────────────────────────────────
        if "user_agent" in kwargs:
            ua = kwargs["user_agent"]
            if ua is None:
                errors.append("User-Agent must not be None.")
            elif not isinstance(ua, str):
                errors.append(f"User-Agent must be a string; got {type(ua).__name__}.")
            elif not ua.strip():
                errors.append("User-Agent must not be empty.")
            else:
                # Same regex used in EdgarSession.__init__
                import re
                if not re.match(r"^.+\s.+@.+\..+$", ua):
                    errors.append(
                        f"User-Agent must be in the format 'Your Name your@email.com'; "
                        f"got {ua!r}."
                    )

        # ── Base URL validation ─────────────────────────────────────────────
        if "base_url" in kwargs:
            url = kwargs["base_url"]
            if url is None:
                errors.append("Base URL must not be None.")
            elif not isinstance(url, str):
                errors.append(f"Base URL must be a string; got {type(url).__name__}.")
            elif not url.strip():
                errors.append("Base URL must not be empty.")
            elif not url.startswith(("http://", "https://")):
                errors.append(
                    f"Base URL must start with http:// or https://; got {url!r}."
                )

        # ── Timeout validation ──────────────────────────────────────────────
        if "timeout" in kwargs:
            timeout = kwargs["timeout"]
            if timeout is not None:
                if isinstance(timeout, tuple):
                    if len(timeout) != 2:
                        errors.append(
                            f"Timeout tuple must have exactly 2 elements; got {len(timeout)}."
                        )
                    else:
                        for i, t in enumerate(timeout):
                            if not isinstance(t, (int, float)):
                                errors.append(
                                    f"Timeout tuple[{i}] must be numeric; got {type(t).__name__}."
                                )
                            elif t < 0:
                                errors.append(
                                    f"Timeout tuple[{i}] must be non-negative; got {t}."
                                )
                elif isinstance(timeout, (int, float)):
                    if timeout < 0:
                        errors.append(f"Timeout must be non-negative; got {timeout}.")
                else:
                    errors.append(
                        f"Timeout must be a number, (connect, read) tuple, or None; "
                        f"got {type(timeout).__name__}."
                    )

        return errors

    # ------------------------------------------------------------------
    # CIK resolution (the core of EdgarClient's "smart input" feature)
    # ------------------------------------------------------------------

    def _resolve_cik(self, ticker_or_cik: str) -> str:
        """Convert any of ticker / company-name / CIK string → 10-digit padded CIK.

        Three phases, tried in order:

        Phase 1 — Numeric detection
          If the input (after stripping whitespace) is all digits, treat it
          as a CIK and zero-pad it to 10 characters.

              "320193"     → "0000320193"
              "0000320193" → "0000320193"

          This phase is deliberately narrow: only ALL-digit strings qualify.
          "320193.0" or "320 193" fall through to the ticker phase.

        Phase 2 — Ticker lookup (O(1) after first call)
          Look up the input in the cached company_tickers.json map.  The lookup
          is case-insensitive ("aapl" finds the same entry as "AAPL").

              "AAPL"  → "0000320193"
              "msft"  → "0000789019"
              "NOTEX" → InvalidTickerError (suppressed; fall through to Phase 3)

        Phase 3 — Name search fallback
          Search the ticker map for companies whose name contains the input as
          a substring (case-insensitive).  Take the best-ranked result (exact
          match → prefix → contains).

              "Apple"      → "0000320193"   (matches "Apple Inc.")
              "apple inc"  → "0000320193"   (exact match)
              "Berkshire"  → "0001067983"   (matches "Berkshire Hathaway Inc")

          If multiple companies share the same prefix (e.g. "Apple REIT" and
          "Apple Inc."), the highest-ranked result is used.  Use client.search()
          to inspect all matches before committing to one.

          If no matches: raises InvalidTickerError with a helpful message.

        Why allow name fallback in get_company / get_facts?
          Ad-hoc scripts often receive company names from external sources
          (CSV files, user input) and don't know whether they are tickers or
          names.  Silent name fallback is more ergonomic than forcing callers
          to first call search() and pick from results.  The cost is one extra
          pass over the in-memory entries list (< 1 ms).
        """
        stripped = ticker_or_cik.strip()

        # ── Phase 1: numeric CIK ────────────────────────────────────────────
        if stripped.isdigit():
            return _pad_cik(stripped)

        # ── Phase 2: ticker lookup ───────────────────────────────────────────
        try:
            return self._session._ticker_cache.lookup(stripped, self._session)
        except InvalidTickerError:
            pass   # not a known ticker; try name search

        # ── Phase 3: name search ─────────────────────────────────────────────
        # The cache is already loaded by Phase 2 (which called lookup()),
        # so search_by_name() doesn't trigger another HTTP request here.
        results = self._session.search_by_name(stripped)
        if results:
            return results[0].cik

        raise InvalidTickerError(
            f"{ticker_or_cik!r} is not a recognised ticker symbol, CIK, or "
            "company name. "
            "Try client.search(name) to browse companies by name, or verify "
            "the ticker at https://www.sec.gov/cgi-bin/browse-edgar."
        )

    # ------------------------------------------------------------------
    # Context manager + cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release network connections held by the underlying session."""
        self._session.close()

    def __enter__(self) -> "EdgarClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        size = self.cache_size
        status = f"{size} tickers cached" if size else "cache not loaded"
        return f"<EdgarClient [{status}]>"


# ---------------------------------------------------------------------------
# Stateless one-shot functions (mirroring requests.get() style)
# These create a throw-away EdgarSession for a single call.
# For multiple calls, use EdgarClient — it reuses the connection pool.
# ---------------------------------------------------------------------------


def get_company(ticker_or_cik: str, user_agent: str, **kwargs: Any) -> Company:
    """One-shot fetch of a company profile.  Use EdgarClient for multiple calls."""
    with EdgarClient(user_agent=user_agent) as client:
        return client.get_company(ticker_or_cik)


def get_facts(ticker_or_cik: str, user_agent: str, **kwargs: Any) -> CompanyFacts:
    """One-shot fetch of all XBRL facts.  Use EdgarClient for multiple calls."""
    with EdgarClient(user_agent=user_agent) as client:
        return client.get_facts(ticker_or_cik)


def get_concept(
    ticker_or_cik: str,
    taxonomy: str,
    concept: str,
    user_agent: str,
) -> FinancialSeries:
    """One-shot fetch of a single XBRL concept.  Use EdgarClient for multiple calls."""
    with EdgarClient(user_agent=user_agent) as client:
        cik = client._resolve_cik(ticker_or_cik)
        return client._session.get_concept(cik, taxonomy, concept)


def get_revenue_history(ticker_or_cik: str, user_agent: str) -> FinancialSeries:
    """One-shot fetch of the revenue series.  Use EdgarClient for multiple calls."""
    with EdgarClient(user_agent=user_agent) as client:
        return client.get_revenue_history(ticker_or_cik)


def search(company_name: str, user_agent: str) -> list[SearchResult]:
    """One-shot company name search.  Use EdgarClient for multiple calls."""
    with EdgarClient(user_agent=user_agent) as client:
        return client.search(company_name)
