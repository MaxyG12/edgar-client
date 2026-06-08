"""
Tests for edgar_client.exceptions

Covers:
  - Exception hierarchy (isinstance checks confirm subclass relationships)
  - Attribute storage (response, request, raw_content)
  - Backward-compatible aliases
  - Auto-population of .request from .response

Design note: we don't touch the network here. These tests validate the pure
Python exception classes in isolation.
"""

import pytest

from edgar_client.exceptions import (
    CompanyNotFoundError,
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
    NetworkError,
    RateLimitedError,
)
from edgar_client.models import PreparedEdgarRequest, Response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status: int = 200) -> Response:
    """Return a minimal Response for exception attribute tests."""
    req = PreparedEdgarRequest()
    req.prepare(method="GET", url="https://data.sec.gov/test", headers={})
    return Response(
        status_code=status,
        body=b"{}",
        url="https://data.sec.gov/test",
        request=req,
    )


# ---------------------------------------------------------------------------
# Hierarchy: every exception IS-A EdgarError IS-A IOError
# ---------------------------------------------------------------------------


class TestHierarchy:
    """Every edgar_client exception must be catchable as EdgarError."""

    def test_edgar_error_is_io_error(self):
        # Inherits from IOError so except IOError catches it.
        assert issubclass(EdgarError, IOError)

    def test_connection_error(self):
        exc = EdgarConnectionError("test")
        assert isinstance(exc, EdgarError)
        assert isinstance(exc, IOError)

    def test_timeout_error(self):
        exc = EdgarTimeoutError("test")
        assert isinstance(exc, EdgarError)

    def test_connect_timeout_is_timeout(self):
        exc = EdgarConnectTimeoutError("test")
        assert isinstance(exc, EdgarTimeoutError)
        assert isinstance(exc, EdgarError)

    def test_read_timeout_is_timeout(self):
        exc = EdgarReadTimeoutError("test")
        assert isinstance(exc, EdgarTimeoutError)
        assert isinstance(exc, EdgarError)

    def test_not_found_error(self):
        exc = EdgarNotFoundError("test")
        assert isinstance(exc, EdgarError)

    def test_rate_limit_error(self):
        exc = EdgarRateLimitError("test")
        assert isinstance(exc, EdgarError)

    def test_http_error(self):
        exc = EdgarHTTPError("test")
        assert isinstance(exc, EdgarError)

    def test_invalid_user_agent(self):
        exc = InvalidUserAgentError("test")
        assert isinstance(exc, EdgarError)

    def test_invalid_schema(self):
        exc = InvalidSchemaError("test")
        assert isinstance(exc, EdgarError)
        assert isinstance(exc, ValueError)  # also ValueError per design

    def test_invalid_ticker(self):
        exc = InvalidTickerError("test")
        assert isinstance(exc, EdgarError)

    def test_data_parse_error(self):
        exc = DataParseError("test")
        assert isinstance(exc, EdgarError)


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------


class TestAliases:
    """Old names must still resolve to the same class or a subclass."""

    def test_network_error_is_connection_error(self):
        assert NetworkError is EdgarConnectionError

    def test_company_not_found_is_not_found_error(self):
        assert CompanyNotFoundError is EdgarNotFoundError

    def test_rate_limited_is_rate_limit_error(self):
        assert RateLimitedError is EdgarRateLimitError

    def test_old_name_catchable_via_new_name(self):
        # Raising via alias, catching via canonical name — must work.
        with pytest.raises(EdgarNotFoundError):
            raise CompanyNotFoundError("company gone")

    def test_new_name_catchable_via_old_name(self):
        with pytest.raises(CompanyNotFoundError):
            raise EdgarNotFoundError("company gone")


# ---------------------------------------------------------------------------
# Attribute storage on EdgarError
# ---------------------------------------------------------------------------


class TestEdgarErrorAttributes:
    """EdgarError stores response and request; DataParseError also stores raw_content."""

    def test_stores_response(self):
        resp = _make_response(404)
        exc = EdgarNotFoundError("not found", response=resp)
        assert exc.response is resp

    def test_stores_request_explicitly(self):
        req = PreparedEdgarRequest()
        req.prepare(method="GET", url="https://data.sec.gov/test", headers={})
        exc = EdgarError("test", request=req)
        assert exc.request is req

    def test_auto_populates_request_from_response(self):
        # When response has a .request, EdgarError should copy it.
        resp = _make_response(404)
        exc = EdgarNotFoundError("not found", response=resp)
        # resp.request was set in _make_response
        assert exc.request is resp.request

    def test_request_none_by_default(self):
        exc = EdgarError("test")
        assert exc.request is None

    def test_response_none_by_default(self):
        exc = EdgarError("test")
        assert exc.response is None

    def test_message_preserved(self):
        exc = EdgarRateLimitError("slow down!")
        assert "slow down!" in str(exc)

    def test_data_parse_error_stores_raw_content(self):
        raw = b'{"broken": true'
        exc = DataParseError("bad JSON", raw_content=raw)
        assert exc.raw_content is raw

    def test_data_parse_error_raw_content_none_by_default(self):
        exc = DataParseError("bad JSON")
        assert exc.raw_content is None

    def test_data_parse_error_inherits_response(self):
        resp = _make_response(200)
        exc = DataParseError("oops", response=resp, raw_content=b"garbage")
        assert exc.response is resp

    def test_exception_args_preserved(self):
        exc = EdgarError("message", "extra arg")
        assert exc.args == ("message", "extra arg")


# ---------------------------------------------------------------------------
# raise / catch semantics
# ---------------------------------------------------------------------------


class TestRaiseCatch:
    """Verify that callers can catch by base class or specific subclass."""

    def test_catch_base_catches_all(self):
        for ExcClass in (
            EdgarConnectionError,
            EdgarTimeoutError,
            EdgarNotFoundError,
            EdgarRateLimitError,
            EdgarHTTPError,
            InvalidUserAgentError,
            InvalidTickerError,
            DataParseError,
        ):
            with pytest.raises(EdgarError):
                raise ExcClass("test")

    def test_does_not_catch_sibling(self):
        # EdgarNotFoundError should NOT be caught by EdgarConnectionError.
        with pytest.raises(EdgarNotFoundError):
            try:
                raise EdgarNotFoundError("not found")
            except EdgarConnectionError:
                pytest.fail("Wrong except branch taken")

    def test_timeout_subclasses_catchable_as_timeout(self):
        for SubClass in (EdgarConnectTimeoutError, EdgarReadTimeoutError):
            with pytest.raises(EdgarTimeoutError):
                raise SubClass("timed out")
