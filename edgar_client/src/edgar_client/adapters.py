"""
Transport adapters for edgar_client.

Why adapters at all?
--------------------
The adapter pattern (from requests) decouples the session's request lifecycle
(prepare headers, rate-limit, validate URL) from the actual bytes-on-the-wire
concern.  This makes the session testable without real HTTP calls: swap in a
``FakeAdapter`` that returns canned ``Response`` objects.

Transport choice: stdlib urllib
--------------------------------
Python's ``urllib.request`` is always available — no pip install needed and no
transitive dependency chain to audit.  The trade-off vs urllib3:

  urllib3 wins:          connection pooling, auto-retry, streaming
  stdlib wins:           zero dependencies, auditable, simpler mental model

For EDGAR, the SEC rate-limits us to 10 req/s anyway, so connection pooling
provides modest benefit.  We lose auto-retry on 5xx, but we implement a
simple manual retry loop below to cover transient server errors.

urllib exception taxonomy
--------------------------
urllib.error.URLError       — base; wraps any lower-level failure
  └── urllib.error.HTTPError  — server returned 4xx/5xx (IS a response)

socket.timeout              — raised by urlopen when timeout is hit AND the
                              system raises it before urllib wraps it
urllib.error.URLError with reason=socket.timeout — same timeout, different path

We must handle both timeout paths because CPython's behaviour differs across
platforms.  See _send_once() for the exact try/except ordering.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from .exceptions import (
    EdgarConnectTimeoutError,
    EdgarConnectionError,
    EdgarHTTPError,
    EdgarNotFoundError,
    EdgarRateLimitError,
    EdgarReadTimeoutError,
    EdgarTimeoutError,
)
from .models import PreparedEdgarRequest, Response

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

# How many times to retry a 5xx response before giving up.
# We don't retry 4xx — those are caller errors (bad CIK, rate-limited, etc.)
# and retrying would just waste quota.
DEFAULT_MAX_RETRIES: int = 3

# Seconds to wait between retries (simple exponential: delay * 2^attempt).
_RETRY_BACKOFF_BASE: float = 0.5

# Only retry these server-error status codes, not e.g. 501 Not Implemented.
_RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseEdgarAdapter(ABC):
    """Abstract transport adapter.

    Any class that implements send() and close() can plug into an EdgarSession
    via session.mount().  The default implementation is EdgarHTTPAdapter.

    Test usage::

        class FakeAdapter(BaseEdgarAdapter):
            def send(self, request, *, timeout=None, verify=True, **kwargs):
                return Response(status_code=200, body=b'{}', url=request.url)
            def close(self):
                pass
    """

    def __init__(self) -> None:
        super().__init__()

    @abstractmethod
    def send(
        self,
        request: PreparedEdgarRequest,
        *,
        timeout: float | tuple[float, float] | None = None,
        verify: bool | str = True,
        **kwargs: Any,
    ) -> Response:
        """Send *request* and return a Response.

        Must raise a subclass of EdgarError on failure — never let urllib,
        socket, or ssl exceptions escape to the caller.
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release any held resources (connections, file handles, etc.)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete stdlib implementation
# ---------------------------------------------------------------------------


class EdgarHTTPAdapter(BaseEdgarAdapter):
    """HTTP/HTTPS adapter using Python's built-in urllib.request.

    No third-party dependencies.

    Retry behaviour
    ---------------
    On 500/502/503/504 responses the adapter sleeps and retries up to
    ``max_retries`` times with exponential back-off starting at 0.5 s.
    All other status codes are raised immediately — retrying a 404 or 429
    would just burn quota.

    TLS verification
    ----------------
    Pass ``verify=False`` to disable certificate checking (useful for
    corporate proxies with self-signed certs).  The default is True.
    """

    def __init__(self, max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        super().__init__()
        self.max_retries = max_retries
        # urllib manages its own connection pool internally; we don't hold
        # any persistent socket references, so close() is a no-op.

    def send(
        self,
        request: PreparedEdgarRequest,
        *,
        timeout: float | tuple[float, float] | None = None,
        verify: bool | str = True,
        **kwargs: Any,
    ) -> Response:
        """Send *request* via urllib, retrying on 5xx up to max_retries times.

        Timeout handling
        ----------------
        urllib.request.urlopen accepts a single float timeout that covers both
        the connect and read phases.  If the caller passes a (connect, read)
        tuple (requests-style), we use the *larger* of the two so we don't
        silently truncate long reads on big companyfacts payloads.
        """
        assert request.url is not None, "PreparedEdgarRequest.url is None — call prepare_url() first"

        # Normalise timeout: (connect, read) tuple → single float for urllib.
        effective_timeout = _resolve_timeout(timeout)

        # Build the SSL context once and reuse across retries.
        ssl_ctx = _build_ssl_context(verify)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            # Exponential back-off between retry attempts (not before first try).
            if attempt > 0:
                sleep_secs = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                time.sleep(sleep_secs)

            try:
                return self._send_once(request, effective_timeout, ssl_ctx)
            except (EdgarHTTPError,) as exc:
                # Only retry on transient server errors.
                status = getattr(exc.response, "status_code", None)
                if status not in _RETRYABLE_STATUS or attempt == self.max_retries:
                    raise
                # Store and loop to retry.
                last_exc = exc
            except (EdgarConnectionError, EdgarTimeoutError):
                # Network errors are potentially transient; retry them too.
                if attempt == self.max_retries:
                    raise
                last_exc = exc  # type: ignore[assignment]

        # Should never reach here, but satisfies the type checker.
        assert last_exc is not None
        raise last_exc

    def _send_once(
        self,
        request: PreparedEdgarRequest,
        timeout: float | None,
        ssl_ctx: ssl.SSLContext | None,
    ) -> Response:
        """Execute one HTTP round-trip and return a Response.

        Exception mapping (why each except branch exists)
        --------------------------------------------------
        urllib.error.HTTPError is a subclass of URLError AND an IOBase, so
        it must be caught *before* URLError — otherwise the URLError branch
        swallows it.

        socket.timeout can bubble up unwrapped on some platforms/versions
        (CPython issue #71003), so we catch it separately after URLError.

        OSError is the parent of socket.error and catches miscellaneous
        low-level network failures that escape both urllib wrappers.
        """
        urllib_req = urllib.request.Request(
            url=request.url,
            method=request.method or "GET",
            # urllib expects a plain dict, not our CaseInsensitiveDict.
            headers=dict(request.headers),
        )

        try:
            # urlopen raises HTTPError for 4xx/5xx; for 2xx it returns a
            # file-like http.client.HTTPResponse.
            http_resp = urllib.request.urlopen(
                urllib_req,
                timeout=timeout,
                # Passing context=None uses the default SSL context (verify=True).
                # Passing our custom context overrides certificate checking.
                context=ssl_ctx,
            )
            return _build_response(request, http_resp)

        except urllib.error.HTTPError as exc:
            # HTTPError IS-A response (exc.code, exc.read(), exc.headers).
            # Read the body so callers can inspect it before we discard the
            # socket reference.
            error_body: bytes = exc.read()
            error_resp = Response(
                status_code=exc.code,
                body=error_body,
                url=request.url,
                headers=dict(exc.headers),
                request=request,
            )
            _raise_for_http_status(exc.code, request.url or "", error_resp)

        except urllib.error.URLError as exc:
            # URLError wraps lower-level errors in exc.reason.
            reason = exc.reason
            if isinstance(reason, socket.timeout):
                # The connect phase timed out before the server responded.
                raise EdgarConnectTimeoutError(
                    f"Connection to {_host(request.url)} timed out: {reason}",
                    request=request,
                ) from exc
            if isinstance(reason, ssl.SSLError):
                raise EdgarConnectionError(
                    f"SSL error for {request.url!r}: {reason}",
                    request=request,
                ) from exc
            raise EdgarConnectionError(
                f"Network error for {request.url!r}: {reason}",
                request=request,
            ) from exc

        except TimeoutError as exc:
            # Python 3.3+: socket.timeout is an alias for TimeoutError, so
            # this branch catches both.  We cannot distinguish connect-phase
            # from read-phase timeouts at this level — urllib does not expose
            # that distinction after the consolidation.  Callers who need to
            # tell them apart should use the (connect, read) timeout tuple and
            # inspect which phase triggered the error from the exception chain.
            raise EdgarTimeoutError(
                f"Request to {request.url!r} timed out: {exc}",
                request=request,
            ) from exc

        except OSError as exc:
            # Catch-all for socket.error, ConnectionResetError, BrokenPipeError,
            # and other OS-level failures that escape the urllib wrappers.
            raise EdgarConnectionError(
                f"OS-level network error for {request.url!r}: {exc}",
                request=request,
            ) from exc

        # mypy: _raise_for_http_status always raises; this line is unreachable
        # but the type checker can't prove it without NoReturn annotations.
        assert False, "unreachable"  # pragma: no cover

    def close(self) -> None:
        """No-op: urllib manages connection lifecycles internally."""
        # urllib.request uses http.client.HTTPConnection internally which
        # caches connections in thread-local storage.  There's no public API
        # to flush them, and they're short-lived enough that it doesn't matter
        # for typical SEC crawling workloads.
        pass


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_timeout(timeout: float | tuple[float, float] | None) -> float | None:
    """Normalise a requests-style timeout to a single float for urllib.

    urllib.request.urlopen only accepts one timeout that applies to both
    connect and read.  If the caller passes a (connect, read) tuple, we use
    the larger value so neither phase is silently truncated.

    Why max() rather than sum()?  The timeout governs one phase at a time,
    not both together.  A (5, 30) timeout means "allow up to 5 s to connect
    and up to 30 s to read" — the ceiling for either phase is 30 s, not 35 s.
    """
    if timeout is None:
        return None
    if isinstance(timeout, tuple):
        connect_t, read_t = timeout
        return max(connect_t, read_t)
    return float(timeout)


def _build_ssl_context(verify: bool | str) -> ssl.SSLContext | None:
    """Create an SSL context from the verify argument.

    verify=True  (default) → None (urllib uses its own default context)
    verify=False           → context with cert verification disabled
    verify="/path/to/ca"   → context that trusts only that CA bundle
    """
    if verify is True:
        # Let urllib use its own default SSL context (validates against the
        # system CA bundle).  Passing None here means "default".
        return None

    ctx = ssl.create_default_context()
    if verify is False:
        # Disable all certificate checking.  This is intentionally a separate
        # branch from the CA-path branch so the decision is explicit.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        # verify is a path to a CA bundle.
        ctx.load_verify_locations(cafile=verify)  # type: ignore[arg-type]
    return ctx


def _build_response(
    request: PreparedEdgarRequest,
    http_resp: Any,  # http.client.HTTPResponse
) -> Response:
    """Convert a urllib HTTP response object to a Response.

    Reads the body eagerly and closes the connection immediately.  This keeps
    the Response self-contained and avoids holding sockets open.
    """
    try:
        body = http_resp.read()
    finally:
        # Always close, even if read() raises (e.g. incomplete transfer).
        http_resp.close()

    # http.client.HTTPResponse.headers is email.message.Message; dict() gives
    # us a plain {str: str} mapping with the last value for duplicate keys.
    raw_headers: dict[str, str] = dict(http_resp.headers)

    # Detect encoding from Content-Type header.
    content_type = raw_headers.get("Content-Type", "") or raw_headers.get("content-type", "")
    encoding = _extract_charset(content_type)

    return Response(
        status_code=http_resp.status,
        body=body,
        url=getattr(http_resp, "url", None) or request.url or "",
        headers=raw_headers,
        encoding=encoding,
        request=request,
    )


def _raise_for_http_status(
    status_code: int,
    url: str,
    response: Response,
) -> None:
    """Raise the appropriate EdgarError subclass for the given HTTP status.

    404 → EdgarNotFoundError
    429 → EdgarRateLimitError
    everything else → EdgarHTTPError

    Design note: we raise here rather than in Response.raise_for_status()
    so that the adapter — which has the urllib exception in scope — controls
    the mapping.  Response.raise_for_status() is kept as a compat shim.
    """
    if status_code == 404:
        raise EdgarNotFoundError(
            f"Not found (HTTP 404): {url!r}",
            response=response,
        )
    if status_code == 429:
        raise EdgarRateLimitError(
            f"Rate limited (HTTP 429): {url!r}. "
            "Back off and retry, or reduce request frequency.",
            response=response,
        )
    raise EdgarHTTPError(
        f"HTTP {status_code} for {url!r}",
        response=response,
    )


def _extract_charset(content_type: str) -> str:
    """Parse the charset from a Content-Type header value.

    Returns "utf-8" when charset is absent — EDGAR produces only UTF-8 JSON.
    """
    if "charset=" in content_type:
        return content_type.split("charset=")[-1].split(";")[0].strip().lower()
    return "utf-8"


def _host(url: str | None) -> str:
    """Extract the host from a URL for use in error messages."""
    if not url:
        return "<unknown host>"
    from urllib.parse import urlparse
    return urlparse(url).netloc or url
