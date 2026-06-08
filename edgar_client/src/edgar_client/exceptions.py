"""
Exception hierarchy for edgar_client.

Design principles
-----------------
1. Everything inherits from EdgarError(IOError) — callers can catch the base
   class to handle all library errors uniformly, or catch specific subclasses
   for fine-grained control.

2. Every exception stores the originating ``response`` and ``request`` objects
   (matching requests.RequestException.__init__) so callers can inspect the
   raw payload without re-fetching.

3. The four primary subclasses map to the four failure modes a caller needs
   to distinguish:
       EdgarConnectionError  — the network never responded
       EdgarTimeoutError     — the network was too slow
       EdgarNotFoundError    — the company / concept doesn't exist
       EdgarRateLimitError   — we sent requests too fast

4. Backward-compatible aliases keep any existing code that caught the old
   names (CompanyNotFoundError, RateLimitedError, NetworkError) working
   without modification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid circular imports at runtime; only used for type annotations.
    from .models import PreparedEdgarRequest, Response


class EdgarError(IOError):
    """Base exception for every edgar_client error.

    Stores the HTTP response and the PreparedEdgarRequest that triggered it
    so callers can inspect the payload in their except block:

        try:
            company = client.get_company("ZZZZ")
        except EdgarNotFoundError as exc:
            print(exc.response.status_code)  # 404
    """

    # Declared here so IDEs and type-checkers see them on the base class.
    response: "Response | None"
    request: "PreparedEdgarRequest | None"

    def __init__(
        self,
        *args: object,
        response: "Response | None" = None,
        request: "PreparedEdgarRequest | None" = None,
    ) -> None:
        self.response = response
        self.request = request
        # If only a response is provided, pull the request from it so callers
        # don't need to pass both separately.
        if response is not None and self.request is None:
            self.request = getattr(response, "request", None)
        super().__init__(*args)


# ---------------------------------------------------------------------------
# The four primary subclasses (user-facing hierarchy)
# ---------------------------------------------------------------------------


class EdgarConnectionError(EdgarError):
    """A network-level failure: DNS lookup failed, TCP refused, etc.

    Wraps ``urllib.error.URLError`` causes that are not timeouts.  Safe to
    retry after a brief wait — the SEC servers are generally stable, so a
    connection error is usually transient.
    """


class EdgarTimeoutError(EdgarError):
    """The request took longer than the configured timeout.

    Covers both the connect phase (server not responding) and the read phase
    (server accepted the connection but stopped sending data).  Callers should
    distinguish with the two subclasses below if needed.
    """


class EdgarConnectTimeoutError(EdgarTimeoutError):
    """The TCP connection phase timed out before the server responded.

    Safe to retry — the server never received the request.
    """


class EdgarReadTimeoutError(EdgarTimeoutError):
    """The server accepted the connection but timed out while sending data.

    Indicates the server started processing the request, so retrying may
    result in duplicate work (rare for a read-only API like EDGAR, but
    worth noting for idempotency).
    """


class EdgarNotFoundError(EdgarError):
    """HTTP 404 — the requested company, filing, or concept was not found.

    Raised for any 404 from the EDGAR API.  The most common cause is a
    ticker that resolves to a valid CIK but the company has no XBRL data
    (e.g. foreign private issuers that file on Form 20-F only).
    """


class EdgarRateLimitError(EdgarError):
    """HTTP 429 — the SEC has asked the client to slow down.

    The library raises rather than auto-retrying so the caller controls the
    back-off strategy.  A simple approach::

        import time
        try:
            company = client.get_company(ticker)
        except EdgarRateLimitError:
            time.sleep(5)
            company = client.get_company(ticker)
    """


# ---------------------------------------------------------------------------
# Ancillary subclasses
# ---------------------------------------------------------------------------


class EdgarHTTPError(EdgarError):
    """A non-404, non-429 HTTP error (e.g. 500 Internal Server Error).

    The ``response`` attribute carries the raw status code and body so the
    caller can decide whether to retry.
    """


class InvalidUserAgentError(EdgarError):
    """The User-Agent string does not match the SEC-required format.

    Raised at construction time (before any network call) when the
    ``user_agent`` argument does not match ``r"^.+ .+@.+\\..+$"``.

    The SEC's fair-access policy requires:
        User-Agent: Your Name your@email.com
    """


class InvalidSchemaError(EdgarError, ValueError):
    """No registered adapter handles the URL's scheme.

    Raised by ``EdgarSession.get_adapter()`` when the adapter map has no
    prefix that matches the request URL.  Usually indicates a bug in calling
    code passing a non-HTTP URL.
    """


class InvalidTickerError(EdgarError):
    """A ticker symbol was not found in the SEC's company-tickers map.

    This is not a network error — the ticker map was fetched successfully,
    but the requested symbol is absent.  The company may be:
    - Not exchange-listed (pass the 10-digit CIK directly)
    - Delisted (try the historical tickers endpoint)
    - Misspelled
    """


class DataParseError(EdgarError):
    """The response body could not be decoded as expected JSON.

    Stores the raw bytes on ``raw_content`` for inspection or logging.
    """

    raw_content: bytes | None

    def __init__(
        self,
        *args: object,
        raw_content: bytes | None = None,
        response: "Response | None" = None,
        request: "PreparedEdgarRequest | None" = None,
    ) -> None:
        # Store the offending bytes before calling super().__init__ so the
        # attribute is always set, even if super raises.
        self.raw_content = raw_content
        super().__init__(*args, response=response, request=request)


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# Keep these so existing code that catches the old names still works.
# ---------------------------------------------------------------------------

#: Old name for EdgarConnectionError.
NetworkError = EdgarConnectionError

#: Old name for EdgarNotFoundError.
CompanyNotFoundError = EdgarNotFoundError

#: Old name for EdgarRateLimitError.
RateLimitedError = EdgarRateLimitError
