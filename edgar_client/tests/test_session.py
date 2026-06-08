"""
Tests for edgar_client.sessions.EdgarSession (and by extension EdgarHTTPAdapter).

Strategy
--------
We never touch the real network. Instead we replace EdgarHTTPAdapter.send()
with a fake that returns a hand-crafted Response.  This exercises:
  - EdgarSession.get(path) URL construction
  - EdgarSession.prepare_request() header merging
  - EdgarSession.send() rate-limit + adapter dispatch
  - EdgarSession.get_company() / get_facts() parsing
  - EdgarSession.resolve_cik() ticker → CIK lookup

The adapter swap is done via session.mount(), which is the intended extension
point — the same mechanism users would use to inject a custom transport.

We also test the adapter's urllib exception translation in isolation (without
going through EdgarSession) to keep the per-class tests focused.
"""

import json
import socket
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from edgar_client.exceptions import (
    EdgarConnectTimeoutError,
    EdgarConnectionError,
    EdgarHTTPError,
    EdgarNotFoundError,
    EdgarRateLimitError,
    EdgarReadTimeoutError,
    EdgarTimeoutError,
    InvalidSchemaError,
    InvalidUserAgentError,
)
from edgar_client.models import PreparedEdgarRequest, Response
from edgar_client.sessions import EdgarSession
from edgar_client.adapters import BaseEdgarAdapter, EdgarHTTPAdapter


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

VALID_USER_AGENT = "Test Bot test@example.com"


def make_response(
    status: int = 200,
    body: bytes = b"{}",
    url: str = "https://data.sec.gov/test",
) -> Response:
    return Response(status_code=status, body=body, url=url)


class FakeAdapter(BaseEdgarAdapter):
    """Adapter that returns pre-programmed Responses without network I/O.

    Usage::

        adapter = FakeAdapter(Response(status_code=200, body=b'...', url='...'))
        session.mount("https://", adapter)
    """

    def __init__(self, response: Response) -> None:
        super().__init__()
        self._response = response
        # Track every PreparedEdgarRequest sent through us for assertions.
        self.sent_requests: list[PreparedEdgarRequest] = []

    def send(
        self,
        request: PreparedEdgarRequest,
        *,
        timeout=None,
        verify=True,
        **kwargs,
    ) -> Response:
        self.sent_requests.append(request)
        return self._response

    def close(self) -> None:
        pass


def make_session(adapter: BaseEdgarAdapter | None = None) -> EdgarSession:
    """Create a session with rate-limiting disabled and an optional fake adapter."""
    session = EdgarSession(user_agent=VALID_USER_AGENT)
    # Disable rate limiting for tests — we don't want 0.1 s sleeps per call.
    session._rate_limiter.min_interval = 0.0
    if adapter is not None:
        session.mount("https://", adapter)
    return session


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------


class TestEdgarSessionConstruction:
    def test_stores_user_agent_in_headers(self):
        session = EdgarSession(user_agent=VALID_USER_AGENT)
        assert session.headers["User-Agent"] == VALID_USER_AGENT

    def test_default_base_url(self):
        session = EdgarSession(user_agent=VALID_USER_AGENT)
        assert session.base_url == "https://data.sec.gov"

    def test_custom_base_url(self):
        session = EdgarSession(user_agent=VALID_USER_AGENT, base_url="http://localhost:8080")
        assert session.base_url == "http://localhost:8080"

    def test_trailing_slash_stripped_from_base_url(self):
        session = EdgarSession(user_agent=VALID_USER_AGENT, base_url="https://data.sec.gov/")
        assert session.base_url == "https://data.sec.gov"

    def test_default_timeout(self):
        session = EdgarSession(user_agent=VALID_USER_AGENT)
        assert session.timeout == 10.0

    def test_custom_timeout(self):
        session = EdgarSession(user_agent=VALID_USER_AGENT, timeout=30.0)
        assert session.timeout == 30.0

    @pytest.mark.parametrize("bad_ua", [
        "",
        "NoEmailHere",
        "missingemail@nodomain",
        "   ",
    ])
    def test_invalid_user_agent_raises(self, bad_ua):
        with pytest.raises(InvalidUserAgentError):
            EdgarSession(user_agent=bad_ua)

    @pytest.mark.parametrize("good_ua", [
        "Alice alice@example.com",
        "My Bot v2 bot@company.org",
        "Research research@university.edu",
    ])
    def test_valid_user_agents_accepted(self, good_ua):
        session = EdgarSession(user_agent=good_ua)  # no exception
        assert session.headers["User-Agent"] == good_ua

    def test_context_manager(self):
        with EdgarSession(user_agent=VALID_USER_AGENT) as session:
            assert session.base_url == "https://data.sec.gov"
        # After __exit__, adapters are closed — no exception expected.


# ---------------------------------------------------------------------------
# get(path) URL construction
# ---------------------------------------------------------------------------


class TestGetPathConstruction:
    """Verify that get() builds the right full URL before sending."""

    def _url_sent(self, session: EdgarSession, path_or_url: str) -> str:
        """Helper: call get() and return the URL that reached the adapter."""
        adapter = FakeAdapter(make_response(body=b"{}"))
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.get(path_or_url)
        return adapter.sent_requests[-1].url or ""

    def test_relative_path_prepends_base_url(self):
        session = make_session()
        url = self._url_sent(session, "/submissions/CIK0000320193.json")
        assert url == "https://data.sec.gov/submissions/CIK0000320193.json"

    def test_path_without_leading_slash(self):
        session = make_session()
        url = self._url_sent(session, "submissions/CIK0000320193.json")
        assert url == "https://data.sec.gov/submissions/CIK0000320193.json"

    def test_full_https_url_used_as_is(self):
        session = make_session()
        full_url = "https://www.sec.gov/files/company_tickers.json"
        url = self._url_sent(session, full_url)
        assert url == full_url

    def test_full_http_url_used_as_is(self):
        session = make_session()
        full_url = "http://localhost:8080/test"
        adapter = FakeAdapter(make_response())
        session.mount("http://", adapter)
        session.get(full_url)
        assert adapter.sent_requests[-1].url == full_url

    def test_params_appear_in_url(self):
        session = make_session()
        adapter = FakeAdapter(make_response())
        session.mount("https://", adapter)
        session.get("/test", params={"q": "Apple"})
        sent_url = adapter.sent_requests[-1].url or ""
        assert "q=Apple" in sent_url


# ---------------------------------------------------------------------------
# Header merging
# ---------------------------------------------------------------------------


class TestHeaderMerging:
    def test_user_agent_always_present(self):
        adapter = FakeAdapter(make_response())
        session = make_session(adapter=adapter)
        session.get("/test")
        headers = adapter.sent_requests[-1].headers
        assert "User-Agent" in headers
        assert headers["user-agent"] == VALID_USER_AGENT

    def test_default_headers_sent(self):
        adapter = FakeAdapter(make_response())
        session = make_session(adapter=adapter)
        session.get("/test")
        headers = adapter.sent_requests[-1].headers
        assert "Accept" in headers
        assert "Accept-Encoding" in headers

    def test_per_request_header_merged(self):
        adapter = FakeAdapter(make_response())
        session = make_session(adapter=adapter)
        session.request("GET", "https://data.sec.gov/test", headers={"X-Custom": "yes"})
        headers = adapter.sent_requests[-1].headers
        assert headers["X-Custom"] == "yes"
        # Session defaults also present.
        assert "User-Agent" in headers

    def test_per_request_header_overrides_session(self):
        adapter = FakeAdapter(make_response())
        session = make_session(adapter=adapter)
        session.request(
            "GET",
            "https://data.sec.gov/test",
            headers={"User-Agent": "Override override@test.com"},
        )
        headers = adapter.sent_requests[-1].headers
        assert headers["user-agent"] == "Override override@test.com"


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


class TestAdapterSelection:
    def test_https_adapter_selected(self):
        session = make_session()
        adapter = session.get_adapter("https://data.sec.gov/test")
        assert isinstance(adapter, EdgarHTTPAdapter)

    def test_http_adapter_selected(self):
        session = make_session()
        adapter = session.get_adapter("http://localhost/test")
        assert isinstance(adapter, EdgarHTTPAdapter)

    def test_unknown_scheme_raises(self):
        session = make_session()
        with pytest.raises(InvalidSchemaError):
            session.get_adapter("ftp://ftp.sec.gov/test")

    def test_mounted_adapter_takes_precedence(self):
        fake = FakeAdapter(make_response())
        session = make_session()
        session.mount("https://data.sec.gov/", fake)
        chosen = session.get_adapter("https://data.sec.gov/submissions/CIK0000320193.json")
        assert chosen is fake


# ---------------------------------------------------------------------------
# get_company / get_facts domain parsing
# ---------------------------------------------------------------------------

_SUBMISSIONS_PAYLOAD = {
    "cik": 320193,
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "sic": 7372,
    "sicDescription": "Prepackaged Software",
    "ein": "94-2404110",
    "entityType": "operating",
    "stateOfIncorporation": "CA",
    "fiscalYearEnd": "09",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-24-000001"],
            "filingDate": ["2024-11-01"],
            "reportDate": ["2024-09-28"],
            "form": ["10-K"],
            "primaryDocument": ["aapl-20240928.htm"],
            "primaryDocDescription": ["FORM 10-K"],
            "size": [12000000],
            "isXBRL": [1],
        }
    },
}

_FACTS_PAYLOAD = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "description": "Amount of revenue",
                "units": {
                    "USD": [
                        {
                            "start": "2022-10-01",
                            "end": "2023-09-30",
                            "val": 383285000000,
                            "accn": "xxx",
                            "form": "10-K",
                            "filed": "2023-11-03",
                        }
                    ]
                },
            }
        }
    },
}

_TICKERS_PAYLOAD = {
    "0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc."}
}


class TestGetCompany:
    def test_returns_company_object(self):
        body = json.dumps(_SUBMISSIONS_PAYLOAD).encode()
        adapter = FakeAdapter(make_response(body=body, url="https://data.sec.gov/submissions/CIK0000320193.json"))
        session = make_session(adapter=adapter)
        # Provide CIK directly so we skip the ticker-map fetch.
        company = session.get_company("0000320193")
        assert company.cik == "0000320193"
        assert company.name == "Apple Inc."
        assert company.ticker == "AAPL"

    def test_correct_url_called(self):
        body = json.dumps(_SUBMISSIONS_PAYLOAD).encode()
        adapter = FakeAdapter(make_response(body=body))
        session = make_session(adapter=adapter)
        session.get_company("0000320193")
        url = adapter.sent_requests[-1].url or ""
        assert "/submissions/CIK0000320193.json" in url

    def test_filings_parsed(self):
        body = json.dumps(_SUBMISSIONS_PAYLOAD).encode()
        adapter = FakeAdapter(make_response(body=body))
        session = make_session(adapter=adapter)
        company = session.get_company("0000320193")
        assert len(company.filings) == 1
        assert company.filings[0].form_type == "10-K"   # renamed: form → form_type
        assert company.filings[0].is_xbrl is True

    def test_ticker_lookup_triggers_tickers_fetch(self):
        """Passing a ticker (not a numeric CIK) should trigger the ticker map fetch."""
        tickers_body = json.dumps(_TICKERS_PAYLOAD).encode()
        submissions_body = json.dumps(_SUBMISSIONS_PAYLOAD).encode()

        call_count = [0]

        class SequencedAdapter(BaseEdgarAdapter):
            """Returns tickers JSON on first call, submissions JSON on second."""

            def send(self, request, *, timeout=None, verify=True, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return make_response(body=tickers_body, url=str(request.url))
                return make_response(body=submissions_body, url=str(request.url))

            def close(self):
                pass

        session = make_session(adapter=SequencedAdapter())
        company = session.get_company("AAPL")
        assert call_count[0] == 2  # tickers + submissions
        assert company.name == "Apple Inc."


class TestGetFacts:
    def test_returns_company_facts(self):
        body = json.dumps(_FACTS_PAYLOAD).encode()
        adapter = FakeAdapter(make_response(body=body))
        session = make_session(adapter=adapter)
        facts = session.get_facts("0000320193")
        assert facts.entity_name == "Apple Inc."
        assert facts.cik == "0000320193"

    def test_correct_url_called(self):
        body = json.dumps(_FACTS_PAYLOAD).encode()
        adapter = FakeAdapter(make_response(body=body))
        session = make_session(adapter=adapter)
        session.get_facts("0000320193")
        url = adapter.sent_requests[-1].url or ""
        assert "/api/xbrl/companyfacts/CIK0000320193.json" in url

    def test_financial_series_accessible(self):
        from decimal import Decimal
        body = json.dumps(_FACTS_PAYLOAD).encode()
        adapter = FakeAdapter(make_response(body=body))
        session = make_session(adapter=adapter)
        facts = session.get_facts("0000320193")
        revenue = facts.get("us-gaap", "Revenues")
        assert revenue.unit == "USD"
        assert revenue.latest_value == Decimal("383285000000")


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------


class TestResolveCik:
    def test_numeric_string_padded(self):
        session = make_session()
        assert session.resolve_cik("320193") == "0000320193"

    def test_already_padded_unchanged(self):
        session = make_session()
        assert session.resolve_cik("0000320193") == "0000320193"


# ---------------------------------------------------------------------------
# EdgarHTTPAdapter: urllib exception → edgar exception translation
# ---------------------------------------------------------------------------


class TestAdapterExceptionTranslation:
    """These tests replace urllib.request.urlopen with a mock that raises
    various urllib exceptions, then verify the adapter maps them correctly."""

    def _make_prep(self, url: str = "https://data.sec.gov/test") -> PreparedEdgarRequest:
        p = PreparedEdgarRequest()
        p.prepare(method="GET", url=url, headers={"User-Agent": VALID_USER_AGENT})
        return p

    def _adapter(self) -> EdgarHTTPAdapter:
        return EdgarHTTPAdapter(max_retries=0)

    # -- 404 / 429 / generic HTTP errors --

    def test_404_raises_not_found(self):
        adapter = self._adapter()
        prep = self._make_prep()

        http_error = urllib.error.HTTPError(
            url=prep.url, code=404, msg="Not Found",
            hdrs=MagicMock(), fp=MagicMock(read=MagicMock(return_value=b"")),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(EdgarNotFoundError) as exc_info:
                adapter.send(prep)
            assert exc_info.value.response is not None
            assert exc_info.value.response.status_code == 404

    def test_429_raises_rate_limit(self):
        adapter = self._adapter()
        prep = self._make_prep()

        http_error = urllib.error.HTTPError(
            url=prep.url, code=429, msg="Too Many Requests",
            hdrs=MagicMock(), fp=MagicMock(read=MagicMock(return_value=b"")),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(EdgarRateLimitError):
                adapter.send(prep)

    def test_500_raises_http_error(self):
        adapter = self._adapter()
        prep = self._make_prep()

        http_error = urllib.error.HTTPError(
            url=prep.url, code=500, msg="Internal Server Error",
            hdrs=MagicMock(), fp=MagicMock(read=MagicMock(return_value=b"")),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(EdgarHTTPError):
                adapter.send(prep)

    # -- Network errors --

    def test_url_error_raises_connection_error(self):
        adapter = self._adapter()
        prep = self._make_prep()

        url_error = urllib.error.URLError(reason="Name or service not known")
        with patch("urllib.request.urlopen", side_effect=url_error):
            with pytest.raises(EdgarConnectionError):
                adapter.send(prep)

    def test_url_error_with_timeout_reason_raises_connect_timeout(self):
        adapter = self._adapter()
        prep = self._make_prep()

        timeout_reason = socket.timeout("timed out")
        url_error = urllib.error.URLError(reason=timeout_reason)
        with patch("urllib.request.urlopen", side_effect=url_error):
            with pytest.raises(EdgarConnectTimeoutError):
                adapter.send(prep)

    def test_socket_timeout_raises_edgar_timeout(self):
        # socket.timeout is an alias for TimeoutError since Python 3.3, so the
        # except TimeoutError branch in the adapter fires.  We verify that *some*
        # EdgarTimeoutError is raised (connect vs read distinction is not reliable
        # at the socket.timeout level when urllib doesn't wrap it into URLError).
        adapter = self._adapter()
        prep = self._make_prep()

        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with pytest.raises(EdgarTimeoutError):
                adapter.send(prep)

    def test_timeout_error_raises_edgar_timeout(self):
        adapter = self._adapter()
        prep = self._make_prep()

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(EdgarTimeoutError):
                adapter.send(prep)

    def test_os_error_raises_connection_error(self):
        adapter = self._adapter()
        prep = self._make_prep()

        with patch("urllib.request.urlopen", side_effect=OSError("broken pipe")):
            with pytest.raises(EdgarConnectionError):
                adapter.send(prep)

    # -- Successful response --

    def test_200_returns_response(self):
        adapter = self._adapter()
        prep = self._make_prep()

        mock_http = MagicMock()
        mock_http.status = 200
        mock_http.read.return_value = b'{"test": true}'
        mock_http.headers = {"Content-Type": "application/json"}
        mock_http.url = prep.url
        mock_http.close = MagicMock()

        with patch("urllib.request.urlopen", return_value=mock_http):
            response = adapter.send(prep)

        assert response.status_code == 200
        assert response.json() == {"test": True}
        assert response.ok is True

    def test_response_has_correct_url(self):
        adapter = self._adapter()
        prep = self._make_prep("https://data.sec.gov/submissions/CIK0000320193.json")

        mock_http = MagicMock()
        mock_http.status = 200
        mock_http.read.return_value = b"{}"
        mock_http.headers = {}
        mock_http.url = prep.url
        mock_http.close = MagicMock()

        with patch("urllib.request.urlopen", return_value=mock_http):
            response = adapter.send(prep)

        assert "CIK0000320193" in response.url


# ---------------------------------------------------------------------------
# Retry behaviour in EdgarHTTPAdapter
# ---------------------------------------------------------------------------


class TestAdapterRetry:
    """Verify that the adapter retries 5xx errors and gives up after max_retries."""

    def _make_prep(self) -> PreparedEdgarRequest:
        p = PreparedEdgarRequest()
        p.prepare(method="GET", url="https://data.sec.gov/test", headers={"User-Agent": VALID_USER_AGENT})
        return p

    def test_retries_500_and_succeeds(self):
        """First call returns 500; second returns 200. Should succeed."""
        adapter = EdgarHTTPAdapter(max_retries=1)
        prep = self._make_prep()

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError(
                    url=prep.url, code=500, msg="Internal Server Error",
                    hdrs=MagicMock(), fp=MagicMock(read=MagicMock(return_value=b"")),
                )
            mock_http = MagicMock()
            mock_http.status = 200
            mock_http.read.return_value = b'{"ok": true}'
            mock_http.headers = {}
            mock_http.url = prep.url
            mock_http.close = MagicMock()
            return mock_http

        with patch("urllib.request.urlopen", side_effect=side_effect):
            with patch("time.sleep"):  # suppress back-off delay in tests
                response = adapter.send(prep)

        assert response.status_code == 200
        assert call_count[0] == 2

    def test_gives_up_after_max_retries(self):
        """All calls return 500; should raise EdgarHTTPError after exhausting retries."""
        adapter = EdgarHTTPAdapter(max_retries=2)
        prep = self._make_prep()

        call_count = [0]

        def always_500(*args, **kwargs):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url=prep.url, code=500, msg="Internal Server Error",
                hdrs=MagicMock(), fp=MagicMock(read=MagicMock(return_value=b"")),
            )

        with patch("urllib.request.urlopen", side_effect=always_500):
            with patch("time.sleep"):
                with pytest.raises(EdgarHTTPError):
                    adapter.send(prep)

        assert call_count[0] == 3  # 1 initial + 2 retries

    def test_does_not_retry_404(self):
        """404 is not retryable — should raise immediately without retrying."""
        adapter = EdgarHTTPAdapter(max_retries=3)
        prep = self._make_prep()

        call_count = [0]

        def always_404(*args, **kwargs):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url=prep.url, code=404, msg="Not Found",
                hdrs=MagicMock(), fp=MagicMock(read=MagicMock(return_value=b"")),
            )

        with patch("urllib.request.urlopen", side_effect=always_404):
            with pytest.raises(EdgarNotFoundError):
                adapter.send(prep)

        assert call_count[0] == 1  # no retries
