"""
edgar_client — A requests-style Python HTTP client for SEC EDGAR.

Quick start::

    from edgar_client import EdgarClient

    client = EdgarClient(user_agent="Alice alice@example.com")
    company  = client.get_company("AAPL")          # ticker, CIK, or company name
    facts    = client.get_facts("AAPL")
    revenue  = client.get_revenue_history("AAPL")  # convenience wrapper
    results  = client.search("Apple")              # name search → [SearchResult, ...]
"""

from .api import (
    EdgarClient,
    get_company,
    get_concept,
    get_facts,
    get_revenue_history,
    search,
)
from .exceptions import (
    # New canonical names
    CompanyNotFoundError,    # alias for EdgarNotFoundError
    DataParseError,
    EdgarConnectTimeoutError,
    EdgarConnectionError,
    EdgarError,
    EdgarHTTPError,
    EdgarNotFoundError,
    EdgarRateLimitError,
    EdgarReadTimeoutError,
    EdgarTimeoutError,
    InvalidSchemaError,
    InvalidTickerError,
    InvalidUserAgentError,
    NetworkError,            # alias for EdgarConnectionError
    RateLimitedError,        # alias for EdgarRateLimitError
)
from .models import (
    Company,
    CompanyFacts,
    EdgarRequest,
    EdgarResponse,           # alias for Response
    FactValue,               # alias for FinancialValue
    Filing,
    FinancialSeries,
    FinancialValue,
    PreparedEdgarRequest,
    Response,
    SearchResult,
)
from .sessions import EdgarSession
from .adapters import BaseEdgarAdapter, EdgarHTTPAdapter

__all__ = [
    # Primary entry point
    "EdgarClient",
    # One-shot facade functions
    "get_company",
    "get_facts",
    "get_concept",
    "get_revenue_history",
    "search",
    # Session (advanced)
    "EdgarSession",
    # HTTP layer (advanced)
    "Response",
    "EdgarResponse",
    "EdgarRequest",
    "PreparedEdgarRequest",
    # Adapters (advanced)
    "BaseEdgarAdapter",
    "EdgarHTTPAdapter",
    # Domain models
    "Company",
    "Filing",
    "CompanyFacts",
    "FinancialSeries",
    "FinancialValue",
    "FactValue",
    "SearchResult",
    # Exceptions — canonical names
    "EdgarError",
    "EdgarConnectionError",
    "EdgarTimeoutError",
    "EdgarConnectTimeoutError",
    "EdgarReadTimeoutError",
    "EdgarNotFoundError",
    "EdgarRateLimitError",
    "EdgarHTTPError",
    "InvalidUserAgentError",
    "InvalidSchemaError",
    "InvalidTickerError",
    "DataParseError",
    # Exceptions — backward-compat aliases
    "NetworkError",
    "CompanyNotFoundError",
    "RateLimitedError",
]
