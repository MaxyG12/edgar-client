"""
Tests for EdgarClient — the primary user-facing class.

Strategy
--------
No real HTTP calls.  We control the network at two levels:

  1. Pre-populate TickerCache directly (bypasses HTTP entirely for
     ticker/name resolution).
  2. Inject a FakeAdapter into the session for tests that exercise
     get_company() / get_facts() HTTP paths.

This keeps tests fast (< 1 ms each) and deterministic.

Layout
------
  TestEdgarClientInit         user_agent validation, session creation
  TestResolveCik              all three phases of _resolve_cik()
  TestGetCompany              happy path, ticker/CIK/name inputs
  TestGetFacts                delegation and URL construction
  TestSearch                  name matching, ranking, empty results
  TestGetRevenueHistory       concept fallback order, missing concept error
  TestRateLimiting            min_interval configured, shared across calls
  TestCaching                 single fetch, invalidate, cache_size property
  TestContextManager          __enter__ / __exit__ close connections
  TestRepr                    readable string representation
"""

import json
import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest

from edgar_client import EdgarClient
from edgar_client.adapters import BaseEdgarAdapter
from edgar_client.exceptions import (
    DataParseError,
    EdgarNotFoundError,
    InvalidTickerError,
    InvalidUserAgentError,
)
from edgar_client.models import (
    Company,
    CompanyFacts,
    FinancialSeries,
    Response,
    SearchResult,
    _pad_cik,
)
from edgar_client.sessions import EdgarSession

# =============================================================================
# Fixtures and helpers
# =============================================================================

VALID_UA = "Test Bot test@example.com"

# A small but realistic ticker map
_TICKER_ENTRIES = [
    {"cik": "0000320193", "ticker": "AAPL",  "title": "Apple Inc."},
    {"cik": "0001018724", "ticker": "AMZN",  "title": "Amazon.com, Inc."},
    {"cik": "0000789019", "ticker": "MSFT",  "title": "Microsoft Corporation"},
    {"cik": "0001326797", "ticker": "APLD",  "title": "Applied Digital Corporation"},
    {"cik": "0001090012", "ticker": "SNAP",  "title": "Snap Inc."},
    # Two "Apple" entries to test disambiguation
    {"cik": "0001569429", "ticker": "APRE",  "title": "Apple Reit Ten"},
]


def _make_client(
    adapter: BaseEdgarAdapter | None = None,
    ticker_entries: list[dict] | None = None,
) -> EdgarClient:
    """Create an EdgarClient with rate-limiting disabled and optional fixtures."""
    client = EdgarClient(user_agent=VALID_UA)
    # Disable rate limiting so tests don't sleep for 0.1 s per call.
    client._session._rate_limiter.min_interval = 0.0

    if adapter is not None:
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)

    if ticker_entries is not None:
        _inject_cache(client, ticker_entries)

    return client


def _inject_cache(client: EdgarClient, entries: list[dict]) -> None:
    """Pre-populate the TickerCache without an HTTP call."""
    cache = client._session._ticker_cache
    cache._cache = {e["ticker"].upper(): e["cik"] for e in entries}
    cache._entries = list(entries)
    cache._loaded = True


class FakeAdapter(BaseEdgarAdapter):
    """Returns pre-programmed Responses; records every PreparedEdgarRequest sent."""

    def __init__(self, response: Response) -> None:
        super().__init__()
        self._response = response
        self.sent_requests: list = []

    def send(self, request, *, timeout=None, verify=True, **kwargs) -> Response:
        self.sent_requests.append(request)
        return self._response

    def close(self) -> None:
        pass


def _make_json_response(data: dict, url: str = "https://data.sec.gov/test") -> Response:
    return Response(status_code=200, body=json.dumps(data).encode(), url=url)


# Canned API payloads --------------------------------------------------------

_SUBMISSIONS_DATA = {
    "cik": 320193,
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "sic": 7372,
    "sicDescription": "Prepackaged Software",
    "ein": "94-2404110",
    "entityType": "operating",
    "stateOfIncorporation": "CA",
    "fiscalYearEnd": "0930",
    "filings": {
        "recent": {
            "accessionNumber":       ["0000320193-24-000001"],
            "filingDate":            ["2024-11-01"],
            "reportDate":            ["2024-09-28"],
            "form":                  ["10-K"],
            "primaryDocument":       ["aapl-20240928.htm"],
            "primaryDocDescription": ["FORM 10-K"],
            "size":                  [12000000],
            "isXBRL":                [1],
        }
    },
}

_FACTS_DATA = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "label": "Revenue",
                "description": "Revenue from contracts with customers",
                "units": {
                    "USD": [
                        {"start": "2022-10-01", "end": "2023-09-30",
                         "val": 383285000000, "accn": "xxx", "form": "10-K",
                         "filed": "2023-11-03", "frame": "CY2023"},
                    ]
                },
            },
            "NetIncomeLoss": {
                "label": "Net Income",
                "description": "Net income (loss) attributable to parent",
                "units": {
                    "USD": [
                        {"start": "2022-10-01", "end": "2023-09-30",
                         "val": 96995000000, "accn": "xxx", "form": "10-K",
                         "filed": "2023-11-03"},
                    ]
                },
            },
        }
    },
}


# =============================================================================
# TestEdgarClientInit
# =============================================================================


class TestEdgarClientInit:
    def test_valid_user_agent_accepted(self):
        client = EdgarClient(user_agent=VALID_UA)
        assert client._session.headers["User-Agent"] == VALID_UA

    @pytest.mark.parametrize("bad_ua", ["", "NoEmail", "noemail@nodomain", "   "])
    def test_invalid_user_agent_raises(self, bad_ua):
        with pytest.raises(InvalidUserAgentError):
            EdgarClient(user_agent=bad_ua)

    def test_creates_internal_session(self):
        client = EdgarClient(user_agent=VALID_UA)
        assert isinstance(client._session, EdgarSession)

    def test_default_base_url(self):
        client = EdgarClient(user_agent=VALID_UA)
        assert client._session.base_url == "https://data.sec.gov"

    def test_custom_base_url(self):
        client = EdgarClient(user_agent=VALID_UA, base_url="http://localhost:8080")
        assert client._session.base_url == "http://localhost:8080"

    def test_default_timeout(self):
        client = EdgarClient(user_agent=VALID_UA)
        assert client._session.timeout == 10.0

    def test_custom_timeout(self):
        client = EdgarClient(user_agent=VALID_UA, timeout=30.0)
        assert client._session.timeout == 30.0


# =============================================================================
# TestResolveCik  — the three-phase smart resolution
# =============================================================================


class TestResolveCik:
    """_resolve_cik() must handle numeric CIKs, ticker symbols, and names."""

    def test_numeric_string_used_as_cik(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("320193") == "0000320193"

    def test_padded_numeric_also_works(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("0000320193") == "0000320193"

    def test_short_numeric_padded(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("12345") == "0000012345"

    def test_ticker_uppercase_resolves(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("AAPL") == "0000320193"

    def test_ticker_lowercase_resolves(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("aapl") == "0000320193"

    def test_ticker_mixed_case_resolves(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("Aapl") == "0000320193"

    def test_name_fallback_on_unknown_ticker(self):
        # "Apple" is not a ticker; falls back to name search → "Apple Inc."
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("Apple") == "0000320193"

    def test_name_fallback_case_insensitive(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("apple inc") == "0000320193"

    def test_name_fallback_multiple_matches_uses_best(self):
        # "apple" matches both "Apple Inc." and "Apple Reit Ten".
        # "Apple Inc." is a prefix match ranked higher than "Apple Reit Ten".
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._resolve_cik("Apple") == "0000320193"

    def test_completely_unknown_input_raises(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        with pytest.raises(InvalidTickerError):
            client._resolve_cik("ZZZZZNOMATCH")

    def test_empty_string_raises(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        with pytest.raises(InvalidTickerError):
            client._resolve_cik("")

    def test_spaces_only_raises(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        with pytest.raises(InvalidTickerError):
            client._resolve_cik("   ")

    def test_numeric_phase_does_not_hit_cache(self):
        """All-digit input must return a CIK even with an empty (unloaded) cache."""
        client = EdgarClient(user_agent=VALID_UA)
        client._session._rate_limiter.min_interval = 0.0
        # Cache is NOT pre-populated — numeric path must not touch it.
        assert client._resolve_cik("320193") == "0000320193"
        assert not client._session._ticker_cache._loaded  # cache untouched


# =============================================================================
# TestGetCompany
# =============================================================================


class TestGetCompany:
    def _client_with_apple(self) -> tuple[EdgarClient, FakeAdapter]:
        resp = _make_json_response(_SUBMISSIONS_DATA,
                                   url="https://data.sec.gov/submissions/CIK0000320193.json")
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        return client, adapter

    def test_returns_company_object(self):
        client, _ = self._client_with_apple()
        company = client.get_company("AAPL")
        assert isinstance(company, Company)

    def test_company_fields(self):
        client, _ = self._client_with_apple()
        company = client.get_company("AAPL")
        assert company.name == "Apple Inc."
        assert company.cik == "0000320193"
        assert company.sic_code == "7372"

    def test_accepts_ticker(self):
        client, adapter = self._client_with_apple()
        client.get_company("AAPL")
        assert "/submissions/CIK0000320193.json" in (adapter.sent_requests[-1].url or "")

    def test_accepts_padded_cik(self):
        client, adapter = self._client_with_apple()
        client.get_company("0000320193")
        assert "/CIK0000320193.json" in (adapter.sent_requests[-1].url or "")

    def test_accepts_unpadded_cik(self):
        client, adapter = self._client_with_apple()
        client.get_company("320193")
        assert "/CIK0000320193.json" in (adapter.sent_requests[-1].url or "")

    def test_accepts_company_name(self):
        client, adapter = self._client_with_apple()
        company = client.get_company("Apple")
        assert company.name == "Apple Inc."

    def test_filings_returned(self):
        client, _ = self._client_with_apple()
        company = client.get_company("AAPL")
        assert len(company.filings) == 1
        assert company.filings[0].form_type == "10-K"


# =============================================================================
# TestGetFacts
# =============================================================================


class TestGetFacts:
    def _client_with_facts(self) -> tuple[EdgarClient, FakeAdapter]:
        resp = _make_json_response(_FACTS_DATA,
                                   url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        return client, adapter

    def test_returns_company_facts(self):
        client, _ = self._client_with_facts()
        facts = client.get_facts("AAPL")
        assert isinstance(facts, CompanyFacts)

    def test_correct_url_called(self):
        client, adapter = self._client_with_facts()
        client.get_facts("0000320193")
        url = adapter.sent_requests[-1].url or ""
        assert "/api/xbrl/companyfacts/CIK0000320193.json" in url

    def test_financial_series_accessible(self):
        client, _ = self._client_with_facts()
        facts = client.get_facts("AAPL")
        revenue = facts.get("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")
        assert isinstance(revenue, FinancialSeries)
        assert revenue.latest_value == Decimal("383285000000")

    def test_accepts_lowercase_ticker(self):
        client, adapter = self._client_with_facts()
        client.get_facts("aapl")
        url = adapter.sent_requests[-1].url or ""
        assert "CIK0000320193" in url


# =============================================================================
# TestSearch  — ticker-map name matching
# =============================================================================


class TestSearch:
    def test_returns_list_of_search_results(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("Apple")
        assert isinstance(results, list)
        assert all(isinstance(r, SearchResult) for r in results)

    def test_matches_by_name(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("Apple")
        names = [r.name for r in results]
        assert "Apple Inc." in names

    def test_case_insensitive(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        upper = client.search("APPLE")
        lower = client.search("apple")
        assert {r.cik for r in upper} == {r.cik for r in lower}

    def test_no_match_returns_empty_list(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("ZZZZZNOMATCHCOMPANY")
        assert results == []

    def test_empty_query_returns_empty_list(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("")
        assert results == []

    def test_search_result_has_cik(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("Apple")
        for r in results:
            assert len(r.cik) == 10       # always 10-digit padded
            assert r.cik.isdigit()

    def test_search_result_has_ticker(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("Apple")
        aapl = next(r for r in results if r.cik == "0000320193")
        assert aapl.ticker == "AAPL"

    # -- Ranking tests --------------------------------------------------------

    def test_exact_match_ranked_first(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        # "Apple Inc." is the exact name; "Apple Reit Ten" is a prefix match.
        results = client.search("apple inc.")
        assert results[0].name == "Apple Inc."

    def test_prefix_match_before_contains(self):
        # "snap" is a prefix of "Snap Inc." and also contained in "Snapple" (not in our data).
        # "Microsoft Corporation" contains "soft" but doesn't start with "soft".
        # Let's test that "Apple Inc." (starts with "apple") comes before entries
        # that merely contain "apple" in the middle.
        entries = [
            {"cik": "0000000001", "ticker": "ZA",  "title": "Pineapple Corp"},
            {"cik": "0000000002", "ticker": "ZB",  "title": "Apple Inc."},
        ]
        client = _make_client(ticker_entries=entries)
        results = client.search("apple")
        # "Apple Inc." starts with "apple" → rank 1 (prefix)
        # "Pineapple Corp" contains "apple" → rank 2 (contains)
        assert results[0].name == "Apple Inc."
        assert results[1].name == "Pineapple Corp"

    def test_multiple_results_returned(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        results = client.search("Apple")
        # Both "Apple Inc." and "Apple Reit Ten" should match
        assert len(results) >= 2

    def test_substring_match_middle_of_name(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        # "Applied" contains "app" — but so does "Apple"
        results = client.search("Applied")
        names = [r.name for r in results]
        assert "Applied Digital Corporation" in names

    def test_no_http_call_when_cache_loaded(self):
        """search() must not make an HTTP call when the ticker cache is hot."""
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        # Inject a fake adapter that would raise if called.
        boom = MagicMock(side_effect=RuntimeError("should not be called"))
        client._session.mount("https://", FakeAdapter.__new__(FakeAdapter))
        # The cache is pre-loaded, so search must not touch the network.
        results = client.search("Apple")   # no RuntimeError = cache was used
        assert len(results) > 0


# =============================================================================
# TestGetRevenueHistory
# =============================================================================


class TestGetRevenueHistory:
    def _facts_with_concept(
        self,
        concept: str,
        taxonomy: str = "us-gaap",
    ) -> CompanyFacts:
        """Build a minimal CompanyFacts with one specific revenue concept."""
        data = {
            "cik": 320193,
            "entityName": "Test Co",
            "facts": {
                taxonomy: {
                    concept: {
                        "label": concept,
                        "description": "",
                        "units": {
                            "USD": [
                                {"start": "2022-01-01", "end": "2022-12-31",
                                 "val": 100_000_000, "accn": "x", "form": "10-K",
                                 "filed": "2023-03-01"},
                            ]
                        },
                    }
                }
            },
        }
        return CompanyFacts.from_dict(data)

    def test_returns_financial_series_for_modern_concept(self):
        resp = _make_json_response(_FACTS_DATA)
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        series = client.get_revenue_history("AAPL")
        assert isinstance(series, FinancialSeries)

    def test_first_concept_used_when_present(self):
        resp = _make_json_response(_FACTS_DATA)
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        series = client.get_revenue_history("AAPL")
        # _FACTS_DATA has RevenueFromContractWithCustomerExcludingAssessedTax
        assert series.concept_name == "RevenueFromContractWithCustomerExcludingAssessedTax"

    def test_falls_back_to_revenues_concept(self):
        # Build facts with only the legacy "Revenues" concept.
        facts_data = {
            "cik": 320193,
            "entityName": "Legacy Co",
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "label": "Revenues",
                        "description": "",
                        "units": {
                            "USD": [
                                {"start": "2018-01-01", "end": "2018-12-31",
                                 "val": 50_000_000, "accn": "a", "form": "10-K",
                                 "filed": "2019-02-01"},
                            ]
                        },
                    }
                }
            },
        }
        resp = _make_json_response(facts_data)
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        series = client.get_revenue_history("AAPL")
        assert series.concept_name == "Revenues"

    def test_raises_data_parse_error_when_no_revenue_concept(self):
        # Facts with no revenue concept at all.
        facts_data = {
            "cik": 320193,
            "entityName": "No Revenue Co",
            "facts": {
                "us-gaap": {
                    "NetIncomeLoss": {
                        "label": "Net Income",
                        "description": "",
                        "units": {"USD": [{"end": "2022-12-31", "val": 1000,
                                           "accn": "a", "form": "10-K", "filed": "2023-01-01"}]},
                    }
                }
            },
        }
        resp = _make_json_response(facts_data)
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        with pytest.raises(DataParseError) as exc_info:
            client.get_revenue_history("AAPL")
        assert "revenue" in str(exc_info.value).lower()

    def test_revenue_history_latest_value(self):
        resp = _make_json_response(_FACTS_DATA)
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        series = client.get_revenue_history("AAPL")
        assert series.latest_value == Decimal("383285000000")

    def test_revenue_history_as_dict(self):
        resp = _make_json_response(_FACTS_DATA)
        adapter = FakeAdapter(resp)
        client = _make_client(adapter=adapter, ticker_entries=_TICKER_ENTRIES)
        series = client.get_revenue_history("AAPL")
        d = series.as_dict()
        assert 2023 in d
        assert d[2023] == Decimal("383285000000")


# =============================================================================
# TestRateLimiting
# =============================================================================


class TestRateLimiting:
    """Verify the rate limiter is correctly configured and used."""

    def test_rate_limiter_configured_at_10_per_second(self):
        client = EdgarClient(user_agent=VALID_UA)
        limiter = client._session._rate_limiter
        assert limiter.max_per_second == 10.0
        assert limiter.min_interval == pytest.approx(0.1)

    def test_rate_limiter_shared_across_calls(self):
        """The same RateLimiter instance must be used for every request.

        If a new limiter were created per call it would always see
        last_request_time = 0 and never throttle.
        """
        client = EdgarClient(user_agent=VALID_UA)
        limiter1 = client._session._rate_limiter
        limiter2 = client._session._rate_limiter
        assert limiter1 is limiter2  # same object

    def test_acquire_sleeps_when_called_too_fast(self):
        """acquire() calls time.sleep with the appropriate delay."""
        from edgar_client.rate_limiter import RateLimiter
        limiter = RateLimiter(max_per_second=10)

        # Simulate a request that happened 20 ms ago (80 ms gap still needed).
        limiter._last_request_time = time.monotonic() - 0.020

        with patch("time.sleep") as mock_sleep:
            limiter.acquire()

        mock_sleep.assert_called_once()
        sleep_duration = mock_sleep.call_args[0][0]
        # Should sleep for approximately 80 ms (0.1 - 0.02 = 0.08)
        assert 0.07 < sleep_duration < 0.09

    def test_acquire_does_not_sleep_when_enough_time_passed(self):
        """acquire() must NOT sleep if min_interval has already elapsed."""
        from edgar_client.rate_limiter import RateLimiter
        limiter = RateLimiter(max_per_second=10)

        # Simulate a request that happened 200 ms ago — more than 100 ms gap.
        limiter._last_request_time = time.monotonic() - 0.200

        with patch("time.sleep") as mock_sleep:
            limiter.acquire()

        mock_sleep.assert_not_called()

    def test_min_interval_settable_to_zero_for_tests(self):
        """Setting min_interval = 0.0 disables throttling (used in test helpers)."""
        from edgar_client.rate_limiter import RateLimiter
        limiter = RateLimiter(max_per_second=10)
        limiter.min_interval = 0.0
        limiter._last_request_time = time.monotonic()  # just happened

        with patch("time.sleep") as mock_sleep:
            limiter.acquire()

        mock_sleep.assert_not_called()

    def test_concurrent_threads_serialised(self):
        """Two threads sharing a RateLimiter must not fire simultaneously.

        We verify that the total elapsed time for N concurrent calls is at
        least (N-1) * min_interval, proving they serialised rather than
        firing in a burst.
        """
        from edgar_client.rate_limiter import RateLimiter
        n = 5
        interval = 0.01  # 10 ms — short enough for a fast test
        limiter = RateLimiter(max_per_second=1 / interval)
        timestamps: list[float] = []

        def _call():
            limiter.acquire()
            timestamps.append(time.monotonic())

        threads = [threading.Thread(target=_call) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        timestamps.sort()
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            assert gap >= interval * 0.8, (  # 20% tolerance for scheduling jitter
                f"Gap between consecutive requests was {gap * 1000:.1f} ms, "
                f"expected ≥ {interval * 1000 * 0.8:.1f} ms"
            )

    def test_repr(self):
        from edgar_client.rate_limiter import RateLimiter
        r = repr(RateLimiter(max_per_second=10))
        assert "10" in r
        assert "100ms" in r


# =============================================================================
# TestCaching
# =============================================================================


class TestCaching:
    def test_cache_size_zero_before_any_lookup(self):
        client = EdgarClient(user_agent=VALID_UA)
        assert client.cache_size == 0

    def test_cache_size_reflects_entries(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client.cache_size == len(_TICKER_ENTRIES)

    def test_ticker_map_fetched_only_once(self):
        """Multiple lookups must trigger only one HTTP call for the ticker map."""
        tickers_resp = _make_json_response(
            {"0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc."}},
            url="https://www.sec.gov/files/company_tickers.json",
        )
        submissions_resp = _make_json_response(_SUBMISSIONS_DATA)

        call_count = [0]

        class CountingAdapter(BaseEdgarAdapter):
            def send(self, request, *, timeout=None, verify=True, **kwargs):
                call_count[0] += 1
                if "company_tickers" in (request.url or ""):
                    return tickers_resp
                return submissions_resp

            def close(self):
                pass

        adapter = CountingAdapter()
        client = EdgarClient(user_agent=VALID_UA)
        client._session._rate_limiter.min_interval = 0.0
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)

        client.get_company("AAPL")   # 1 ticker fetch + 1 submissions fetch = 2 calls
        client.get_company("AAPL")   # cache hot: only 1 submissions fetch = 1 more call
        client.get_company("AAPL")   # same

        # Ticker map should be fetched exactly once across all three calls.
        ticker_calls = sum(
            1 for req in adapter.__class__.__mro__
            # We can't inspect individual requests on CountingAdapter easily,
            # so we verify via the loaded flag instead.
        )
        assert client._session._ticker_cache._loaded is True
        # Total calls: 1 ticker map + 3 submissions = 4
        assert call_count[0] == 4

    def test_invalidate_cache_resets_loaded_flag(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client._session._ticker_cache._loaded is True
        client.invalidate_cache()
        assert client._session._ticker_cache._loaded is False

    def test_invalidate_cache_resets_cache_size(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        assert client.cache_size > 0
        client.invalidate_cache()
        assert client.cache_size == 0

    def test_invalidate_cache_forces_refetch(self):
        """After invalidate(), the next lookup must re-fetch the ticker map."""
        tickers_payload = {
            "0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc."}
        }
        fetch_count = [0]

        class TrackingAdapter(BaseEdgarAdapter):
            def send(self, request, *, timeout=None, verify=True, **kwargs):
                if "company_tickers" in (request.url or ""):
                    fetch_count[0] += 1
                return _make_json_response(tickers_payload)

            def close(self):
                pass

        client = EdgarClient(user_agent=VALID_UA)
        client._session._rate_limiter.min_interval = 0.0
        client._session.mount("https://", TrackingAdapter())
        client._session.mount("http://", TrackingAdapter())

        client._resolve_cik("AAPL")     # first fetch
        client._resolve_cik("AAPL")     # cache hit, no fetch
        client.invalidate_cache()
        client._resolve_cik("AAPL")     # second fetch after invalidate

        assert fetch_count[0] == 2


# =============================================================================
# TestContextManager
# =============================================================================


class TestContextManager:
    def test_enter_returns_self(self):
        client = EdgarClient(user_agent=VALID_UA)
        with client as c:
            assert c is client

    def test_exit_closes_adapters(self):
        """__exit__ must call close() which clears the connection pool."""
        client = EdgarClient(user_agent=VALID_UA)
        closed = []
        original_close = client._session.close

        def _mock_close():
            closed.append(True)
            original_close()

        client._session.close = _mock_close
        with client:
            pass
        assert closed == [True]

    def test_with_statement_pattern(self):
        with EdgarClient(user_agent=VALID_UA) as client:
            assert isinstance(client, EdgarClient)
        # No exception means __exit__ ran cleanly


# =============================================================================
# TestRepr
# =============================================================================


class TestRepr:
    def test_repr_before_cache_loaded(self):
        client = EdgarClient(user_agent=VALID_UA)
        r = repr(client)
        assert "cache not loaded" in r

    def test_repr_after_cache_loaded(self):
        client = _make_client(ticker_entries=_TICKER_ENTRIES)
        r = repr(client)
        assert "tickers cached" in r
        assert str(len(_TICKER_ENTRIES)) in r
