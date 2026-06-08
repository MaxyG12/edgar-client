"""
Tests for edgar_client.models.Response (and EdgarResponse alias).

Covers:
  - Construction from raw bytes (no mocking needed)
  - .status_code, .content, .text, .ok
  - .json() happy path and error path
  - .raise_for_status() compat shim
  - Encoding detection and fallback
  - EdgarResponse alias works identically

Design note: Response is eagerly loaded, so every test creates it from plain
bytes — no urllib internals to mock.
"""

import json
import pytest

from edgar_client.exceptions import DataParseError, EdgarHTTPError
from edgar_client.models import EdgarResponse, PreparedEdgarRequest, Response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_response(
    *,
    status: int = 200,
    body: bytes = b"{}",
    url: str = "https://data.sec.gov/test",
    headers: dict | None = None,
    encoding: str = "utf-8",
) -> Response:
    """Factory: create a Response without any HTTP machinery."""
    return Response(
        status_code=status,
        body=body,
        url=url,
        headers=headers or {"Content-Type": "application/json"},
        encoding=encoding,
    )


# ---------------------------------------------------------------------------
# Basic attributes
# ---------------------------------------------------------------------------


class TestResponseAttributes:
    def test_status_code(self):
        resp = make_response(status=200)
        assert resp.status_code == 200

    def test_status_code_404(self):
        resp = make_response(status=404)
        assert resp.status_code == 404

    def test_url_stored(self):
        url = "https://data.sec.gov/submissions/CIK0000320193.json"
        resp = make_response(url=url)
        assert resp.url == url

    def test_headers_stored(self):
        resp = make_response(headers={"Content-Type": "application/json; charset=utf-8"})
        # CaseInsensitiveDict: both casings should work.
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
        assert resp.headers["content-type"] == "application/json; charset=utf-8"

    def test_request_stored(self):
        req = PreparedEdgarRequest()
        req.prepare(method="GET", url="https://data.sec.gov/test", headers={})
        resp = Response(status_code=200, body=b"{}", url="https://data.sec.gov/test", request=req)
        assert resp.request is req

    def test_request_defaults_to_none(self):
        resp = make_response()
        assert resp.request is None


# ---------------------------------------------------------------------------
# .content property
# ---------------------------------------------------------------------------


class TestContent:
    def test_returns_bytes(self):
        resp = make_response(body=b'{"cik": 320193}')
        assert isinstance(resp.content, bytes)

    def test_correct_bytes(self):
        body = b'{"cik": 320193}'
        resp = make_response(body=body)
        assert resp.content == body

    def test_empty_body(self):
        resp = make_response(body=b"")
        assert resp.content == b""

    def test_content_is_idempotent(self):
        # Calling .content twice returns the same object.
        resp = make_response(body=b"hello")
        assert resp.content is resp.content


# ---------------------------------------------------------------------------
# .text property
# ---------------------------------------------------------------------------


class TestText:
    def test_decodes_utf8(self):
        resp = make_response(body=b"hello world")
        assert resp.text == "hello world"

    def test_decodes_with_specified_encoding(self):
        # Latin-1 byte that is not valid UTF-8.
        body = "café".encode("latin-1")
        resp = make_response(body=body, encoding="latin-1")
        assert resp.text == "café"

    def test_defaults_to_utf8(self):
        # No encoding specified → defaults to utf-8.
        resp = make_response(body="Ångström".encode("utf-8"))
        assert resp.text == "Ångström"

    def test_replaces_bad_bytes_not_raise(self):
        # Invalid UTF-8 sequence — should use replacement character, not raise.
        bad_bytes = b"\xff\xfe"
        resp = make_response(body=bad_bytes, encoding="utf-8")
        # Should not raise; replacement characters are inserted.
        text = resp.text
        assert isinstance(text, str)

    def test_empty_body_gives_empty_string(self):
        resp = make_response(body=b"")
        assert resp.text == ""


# ---------------------------------------------------------------------------
# .ok property
# ---------------------------------------------------------------------------


class TestOk:
    @pytest.mark.parametrize("status", [200, 201, 204, 206, 299])
    def test_ok_true_for_2xx(self, status):
        resp = make_response(status=status)
        assert resp.ok is True

    @pytest.mark.parametrize("status", [300, 301, 302, 400, 401, 403, 404, 429, 500, 503])
    def test_ok_false_for_non_2xx(self, status):
        resp = make_response(status=status)
        assert resp.ok is False


# ---------------------------------------------------------------------------
# .json()
# ---------------------------------------------------------------------------


class TestJson:
    def test_parses_dict(self):
        resp = make_response(body=b'{"cik": 320193, "name": "Apple Inc."}')
        data = resp.json()
        assert data["cik"] == 320193
        assert data["name"] == "Apple Inc."

    def test_parses_list(self):
        resp = make_response(body=b'[1, 2, 3]')
        assert resp.json() == [1, 2, 3]

    def test_parses_empty_object(self):
        resp = make_response(body=b"{}")
        assert resp.json() == {}

    def test_parses_nested(self):
        payload = {"filings": {"recent": {"form": ["10-K", "10-Q"]}}}
        resp = make_response(body=json.dumps(payload).encode())
        assert resp.json()["filings"]["recent"]["form"] == ["10-K", "10-Q"]

    def test_json_decode_error_raises_data_parse_error(self):
        resp = make_response(body=b"not json at all")
        with pytest.raises(DataParseError) as exc_info:
            resp.json()
        # The raw bytes that failed to parse are attached.
        assert exc_info.value.raw_content == b"not json at all"

    def test_json_error_includes_url_in_message(self):
        resp = make_response(body=b"bad", url="https://data.sec.gov/bad")
        with pytest.raises(DataParseError) as exc_info:
            resp.json()
        assert "https://data.sec.gov/bad" in str(exc_info.value)

    def test_json_error_has_response_attached(self):
        resp = make_response(body=b"{bad}")
        with pytest.raises(DataParseError) as exc_info:
            resp.json()
        assert exc_info.value.response is resp

    def test_kwargs_passed_to_json_loads(self):
        # parse_float lets callers use Decimal for float values.
        from decimal import Decimal
        resp = make_response(body=b'{"val": 1.23}')
        data = resp.json(parse_float=Decimal)
        assert isinstance(data["val"], Decimal)


# ---------------------------------------------------------------------------
# .raise_for_status()
# ---------------------------------------------------------------------------


class TestRaiseForStatus:
    def test_does_not_raise_on_200(self):
        resp = make_response(status=200)
        resp.raise_for_status()  # no exception

    @pytest.mark.parametrize("status", [201, 204])
    def test_does_not_raise_on_2xx(self, status):
        resp = make_response(status=status)
        resp.raise_for_status()  # no exception

    def test_raises_edgar_http_error_on_500(self):
        resp = make_response(status=500)
        with pytest.raises(EdgarHTTPError):
            resp.raise_for_status()

    def test_raises_edgar_http_error_on_404(self):
        # raise_for_status is the compat shim — it raises EdgarHTTPError, not
        # EdgarNotFoundError (the adapter does the fine-grained mapping).
        resp = make_response(status=404)
        with pytest.raises(EdgarHTTPError):
            resp.raise_for_status()

    def test_error_includes_status_code(self):
        resp = make_response(status=503)
        with pytest.raises(EdgarHTTPError) as exc_info:
            resp.raise_for_status()
        assert "503" in str(exc_info.value)

    def test_error_has_response_attached(self):
        resp = make_response(status=500)
        with pytest.raises(EdgarHTTPError) as exc_info:
            resp.raise_for_status()
        assert exc_info.value.response is resp


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------


class TestRepr:
    def test_repr_contains_status(self):
        resp = make_response(status=200)
        assert "200" in repr(resp)

    def test_repr_format(self):
        resp = make_response(status=404)
        assert repr(resp) == "<Response [404]>"


# ---------------------------------------------------------------------------
# Backward-compat alias
# ---------------------------------------------------------------------------


class TestEdgarResponseAlias:
    """EdgarResponse = Response: same class, same behaviour."""

    def test_same_class(self):
        assert EdgarResponse is Response

    def test_constructable_via_alias(self):
        resp = EdgarResponse(status_code=200, body=b"{}", url="https://data.sec.gov/test")
        assert resp.status_code == 200
        assert resp.ok is True
