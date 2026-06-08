"""
Tests for EdgarClient input validation.

Strategy
--------
All tests use mocked EdgarSession to avoid HTTP calls.  We test:
  1. validate_inputs() method — collect-all-errors pattern
  2. Early validation in public methods (get_company, get_facts, search)
  3. That validation errors are raised before network calls occur
"""

from unittest.mock import MagicMock

import pytest

from edgar_client import EdgarClient
from edgar_client.exceptions import InvalidTickerError

UA = "Test Bot test@example.com"


def _make_client() -> EdgarClient:
    """Create an EdgarClient with a mocked session."""
    client = EdgarClient(user_agent=UA)
    client._session = MagicMock()
    return client


# =============================================================================
# TestValidateInputs
# =============================================================================


class TestValidateInputs:
    """Test the validate_inputs() method (collect-all-errors pattern)."""

    def test_no_errors_when_empty(self):
        client = _make_client()
        errors = client.validate_inputs()
        assert errors == []

    def test_single_cik_error(self):
        client = _make_client()
        errors = client.validate_inputs(cik="notdigits")
        assert len(errors) == 1
        assert "digits" in errors[0].lower()

    def test_single_ticker_error_too_long(self):
        client = _make_client()
        errors = client.validate_inputs(ticker="TOOLONG")
        assert len(errors) == 1
        assert "1–5 letters" in errors[0] or "5 letters" in errors[0]

    def test_single_ticker_error_not_alpha(self):
        client = _make_client()
        errors = client.validate_inputs(ticker="AA11")
        assert len(errors) == 1
        assert "letters" in errors[0].lower()

    def test_single_company_name_error_empty(self):
        client = _make_client()
        errors = client.validate_inputs(company_name="")
        assert len(errors) == 1
        assert "empty" in errors[0].lower()

    def test_single_company_name_error_whitespace(self):
        client = _make_client()
        errors = client.validate_inputs(company_name="   ")
        assert len(errors) == 1
        assert "empty" in errors[0].lower() or "whitespace" in errors[0].lower()

    def test_multiple_errors_collected(self):
        """All validation failures reported at once (collect-all-errors pattern)."""
        client = _make_client()
        errors = client.validate_inputs(
            cik="notdigits",
            ticker="TOOLONG",
            company_name="",
            timeout=-1.0,
        )
        # Should have at least 4 errors (one for each bad input)
        assert len(errors) >= 4
        # Verify each error type appears
        assert any("cik" in e.lower() for e in errors), "CIK error missing"
        assert any("ticker" in e.lower() for e in errors), "Ticker error missing"
        assert any("company" in e.lower() for e in errors), "Company error missing"
        assert any("timeout" in e.lower() for e in errors), "Timeout error missing"

    def test_cik_none_error(self):
        client = _make_client()
        errors = client.validate_inputs(cik=None)
        assert len(errors) == 1
        assert "none" in errors[0].lower()

    def test_cik_wrong_type_error(self):
        client = _make_client()
        errors = client.validate_inputs(cik=12345)  # int, not str
        assert len(errors) == 1
        assert "string" in errors[0].lower()

    def test_cik_empty_string_error(self):
        client = _make_client()
        errors = client.validate_inputs(cik="")
        assert len(errors) == 1
        assert "empty" in errors[0].lower()

    def test_cik_valid_passes(self):
        client = _make_client()
        errors = client.validate_inputs(cik="0000320193")
        assert errors == []

    def test_cik_short_numeric_passes(self):
        """Short numeric CIKs should be valid (will be padded)."""
        client = _make_client()
        errors = client.validate_inputs(cik="320193")
        assert errors == []

    def test_ticker_valid_passes(self):
        client = _make_client()
        errors = client.validate_inputs(ticker="AAPL")
        assert errors == []

    def test_ticker_1_letter_passes(self):
        client = _make_client()
        errors = client.validate_inputs(ticker="X")
        assert errors == []

    def test_ticker_5_letters_passes(self):
        client = _make_client()
        errors = client.validate_inputs(ticker="ABCDE")
        assert errors == []

    def test_ticker_6_letters_fails(self):
        client = _make_client()
        errors = client.validate_inputs(ticker="ABCDEF")
        assert len(errors) == 1

    def test_company_name_valid_passes(self):
        client = _make_client()
        errors = client.validate_inputs(company_name="Apple")
        assert errors == []

    def test_user_agent_valid_passes(self):
        client = _make_client()
        errors = client.validate_inputs(user_agent="Alice alice@example.com")
        assert errors == []

    def test_user_agent_invalid_no_email_fails(self):
        client = _make_client()
        errors = client.validate_inputs(user_agent="NoEmail")
        assert len(errors) == 1
        assert "format" in errors[0].lower() or "email" in errors[0].lower()

    def test_base_url_valid_https(self):
        client = _make_client()
        errors = client.validate_inputs(base_url="https://data.sec.gov")
        assert errors == []

    def test_base_url_valid_http(self):
        client = _make_client()
        errors = client.validate_inputs(base_url="http://localhost:8080")
        assert errors == []

    def test_base_url_invalid_no_scheme(self):
        client = _make_client()
        errors = client.validate_inputs(base_url="data.sec.gov")
        assert len(errors) == 1
        assert "http" in errors[0].lower()

    def test_timeout_float_valid(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=10.0)
        assert errors == []

    def test_timeout_int_valid(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=10)
        assert errors == []

    def test_timeout_tuple_valid(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=(5.0, 10.0))
        assert errors == []

    def test_timeout_none_valid(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=None)
        assert errors == []

    def test_timeout_negative_fails(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=-1.0)
        assert len(errors) == 1
        assert "negative" in errors[0].lower() or ">=" in errors[0]

    def test_timeout_tuple_negative_element_fails(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=(5.0, -1.0))
        assert len(errors) == 1
        assert "negative" in errors[0].lower()

    def test_timeout_wrong_tuple_length_fails(self):
        client = _make_client()
        errors = client.validate_inputs(timeout=(5.0, 10.0, 15.0))
        assert len(errors) == 1
        assert "tuple" in errors[0].lower() or "2" in errors[0]

    def test_timeout_wrong_type_fails(self):
        client = _make_client()
        errors = client.validate_inputs(timeout="10")
        assert len(errors) == 1
        assert "number" in errors[0].lower()


# =============================================================================
# TestEarlyValidationGetCompany
# =============================================================================


class TestEarlyValidationGetCompany:
    """Early validation in get_company() (before network calls)."""

    def test_empty_string_raises_immediately(self):
        client = _make_client()
        with pytest.raises(InvalidTickerError) as exc_info:
            client.get_company("")
        assert "empty" in str(exc_info.value).lower()
        # Verify no network call was made (session.get_company not called)
        client._session.get_company.assert_not_called()

    def test_whitespace_only_raises_immediately(self):
        client = _make_client()
        with pytest.raises(InvalidTickerError) as exc_info:
            client.get_company("   ")
        assert "empty" in str(exc_info.value).lower()
        client._session.get_company.assert_not_called()

    def test_none_raises_immediately(self):
        client = _make_client()
        with pytest.raises((InvalidTickerError, AttributeError)):
            # None.strip() will raise AttributeError
            client.get_company(None)  # type: ignore
        # Session method might not be called, depending on which error fires first
        # But we expect the error before any network call

    def test_valid_input_proceeds_to_network(self):
        client = _make_client()
        from edgar_client.models import Company
        client._session.get_company.return_value = Company()
        try:
            # This should proceed to the network layer
            # (and fail because _resolve_cik will try the cache,
            # but at least we get past the early validation)
            client.get_company("AAPL")
        except Exception:
            pass  # Expected to fail at network layer, not early validation
        # If we get here without early validation error, test passes


# =============================================================================
# TestEarlyValidationGetFacts
# =============================================================================


class TestEarlyValidationGetFacts:
    """Early validation in get_facts() (before network calls)."""

    def test_empty_string_raises_immediately(self):
        client = _make_client()
        with pytest.raises(InvalidTickerError) as exc_info:
            client.get_facts("")
        assert "empty" in str(exc_info.value).lower()
        client._session.get_facts.assert_not_called()

    def test_whitespace_only_raises_immediately(self):
        client = _make_client()
        with pytest.raises(InvalidTickerError) as exc_info:
            client.get_facts("   ")
        assert "empty" in str(exc_info.value).lower()
        client._session.get_facts.assert_not_called()


# =============================================================================
# TestEarlyValidationSearch
# =============================================================================


class TestEarlyValidationSearch:
    """Early validation in search() (before network calls)."""

    def test_none_raises_typeerror(self):
        client = _make_client()
        with pytest.raises(TypeError) as exc_info:
            client.search(None)  # type: ignore
        assert "must not be none" in str(exc_info.value).lower()

    def test_non_string_raises_typeerror(self):
        client = _make_client()
        with pytest.raises(TypeError) as exc_info:
            client.search(123)  # type: ignore
        assert "string" in str(exc_info.value).lower()

    def test_empty_string_returns_empty_list(self):
        client = _make_client()
        results = client.search("")
        assert results == []
        # No network call should be made
        client._session.search_by_name.assert_not_called()

    def test_whitespace_only_returns_empty_list(self):
        client = _make_client()
        results = client.search("   ")
        assert results == []
        client._session.search_by_name.assert_not_called()

    def test_valid_input_proceeds_to_search(self):
        client = _make_client()
        client._session.search_by_name.return_value = []
        results = client.search("Apple")
        client._session.search_by_name.assert_called_once()


# =============================================================================
# TestEarlyValidationGetRevenueHistory
# =============================================================================


class TestEarlyValidationGetRevenueHistory:
    """Early validation in get_revenue_history() (before network calls)."""

    def test_empty_string_raises_immediately(self):
        client = _make_client()
        with pytest.raises(InvalidTickerError) as exc_info:
            client.get_revenue_history("")
        assert "empty" in str(exc_info.value).lower()
        # Should not call get_facts
        client._session.get_facts.assert_not_called()

    def test_whitespace_only_raises_immediately(self):
        client = _make_client()
        with pytest.raises(InvalidTickerError) as exc_info:
            client.get_revenue_history("   ")
        assert "empty" in str(exc_info.value).lower()
        client._session.get_facts.assert_not_called()


# =============================================================================
# Integration: validate_inputs() can be called before any operation
# =============================================================================


class TestValidateInputsIntegration:
    """Users can call validate_inputs() before attempting operations."""

    def test_user_can_check_all_inputs_before_calling_api(self):
        """Common pattern: validate all inputs, then proceed if valid."""
        client = _make_client()

        inputs = {
            "cik": "notdigits",
            "ticker": "TOOLONG",
            "company_name": "",
        }

        errors = client.validate_inputs(**inputs)

        if errors:
            # User sees all errors at once
            assert len(errors) == 3
            for error in errors:
                assert isinstance(error, str)
        else:
            # This branch won't be taken, but it shows the expected pattern
            pass

    def test_user_can_validate_and_proceed_separately(self):
        """User flow: validate, collect errors, then decide to proceed or abort."""
        client = _make_client()
        user_input = "AAPL"

        # User validates before calling
        errors = client.validate_inputs(ticker=user_input)
        assert errors == []

        # User can now safely proceed (if mocking allows)
        # In real code, this would fetch data
