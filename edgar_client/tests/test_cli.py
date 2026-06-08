"""
Tests for edgar_client.cli — the Click command-line interface.

Strategy
--------
Every test uses Click's CliRunner, which captures stdout/stderr without
requiring a real terminal and prevents sys.exit() from killing the test
process.  No real HTTP calls: EdgarClient is patched at the module level.

Click 8.4 CliRunner notes
--------------------------
• CliRunner() separates stdout/stderr automatically.
• result.stdout  — text written to stdout only
• result.stderr  — text written to stderr only
• result.output  — mixed stdout+stderr (legacy alias; we avoid it for clarity)
• No mix_stderr= constructor argument in this version.

Why patch edgar_client.cli.EdgarClient (not edgar_client.api.EdgarClient)?
cli.py imports EdgarClient at the top and uses that binding.  Patching the
name in the cli module replaces the local reference that cli.py actually uses.

Layout
------
  TestCliGroup            --user-agent validation, env-var, --help
  TestCompanyCommand      table output, json output, errors
  TestFinancialsCommand   metric selection, --years, json, missing data
  TestSearchCommand       name search, EFTS fallback, --limit, json
  TestCompareCommand      multi-ticker, partial failures, json output
  TestOutputFormat        every command produces valid JSON
  TestErrorHandling       every EdgarError → exit code 1
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from edgar_client.cli import cli
from edgar_client.exceptions import (
    EdgarConnectionError,
    EdgarNotFoundError,
    EdgarRateLimitError,
    EdgarTimeoutError,
    InvalidTickerError,
    InvalidUserAgentError,
)
from edgar_client.models import (
    Company,
    CompanyFacts,
    FinancialSeries,
    SearchResult,
)

# =============================================================================
# Canned domain objects (built with from_dict() for realism)
# =============================================================================

_SUBMISSIONS = {
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
            "accessionNumber":       ["0000320193-24-000001", "0000320193-23-000077"],
            "filingDate":            ["2024-11-01",           "2023-11-03"],
            "reportDate":            ["2024-09-28",           "2023-09-30"],
            "form":                  ["10-K",                 "10-K"],
            "primaryDocument":       ["aapl-20240928.htm",    "aapl-20230930.htm"],
            "primaryDocDescription": ["FORM 10-K",            "FORM 10-K"],
            "size":                  [12000000,                11500000],
            "isXBRL":                [1,                       1],
        }
    },
}

_FACTS_FULL = {
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
                         "val": 383285000000, "accn": "x", "form": "10-K",
                         "filed": "2023-11-03", "frame": "CY2023"},
                        {"start": "2021-10-01", "end": "2022-09-30",
                         "val": 394328000000, "accn": "y", "form": "10-K",
                         "filed": "2022-10-28", "frame": "CY2022"},
                        {"start": "2020-10-01", "end": "2021-09-30",
                         "val": 365817000000, "accn": "z", "form": "10-K",
                         "filed": "2021-10-29", "frame": "CY2021"},
                    ]
                },
            },
            "NetIncomeLoss": {
                "label": "Net Income",
                "description": "Net income attributable to parent",
                "units": {
                    "USD": [
                        {"start": "2022-10-01", "end": "2023-09-30",
                         "val": 96995000000, "accn": "x", "form": "10-K",
                         "filed": "2023-11-03"},
                    ]
                },
            },
        }
    },
}

_MSFT_FACTS_DATA = {
    "cik": 789019,
    "entityName": "Microsoft Corporation",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "label": "Revenue",
                "description": "Revenue",
                "units": {
                    "USD": [
                        {"start": "2022-07-01", "end": "2023-06-30",
                         "val": 211915000000, "accn": "a", "form": "10-K",
                         "filed": "2023-07-28"},
                        {"start": "2021-07-01", "end": "2022-06-30",
                         "val": 198270000000, "accn": "b", "form": "10-K",
                         "filed": "2022-07-28"},
                    ]
                },
            }
        }
    },
}

_APPLE       = Company.from_dict(_SUBMISSIONS)
_FACTS       = CompanyFacts.from_dict(_FACTS_FULL)
_MSFT_FACTS  = CompanyFacts.from_dict(_MSFT_FACTS_DATA)

_SEARCH_RESULTS = [
    SearchResult.from_ticker_map_entry(
        {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}
    ),
    SearchResult.from_ticker_map_entry(
        {"cik": "0001569429", "ticker": "APRE", "title": "Apple Reit Ten"}
    ),
]

UA = "Test Bot test@example.com"


# =============================================================================
# Helpers
# =============================================================================


def _default_mock() -> MagicMock:
    """Return a mock EdgarClient with Apple as the default response."""
    m = MagicMock()
    m.get_company.return_value         = _APPLE
    m.get_facts.return_value           = _FACTS
    m.search.return_value              = _SEARCH_RESULTS
    m._session.search.return_value     = _SEARCH_RESULTS
    return m


def _run(args: list[str], client: MagicMock | None = None):
    """Invoke the CLI via CliRunner, patching EdgarClient.

    Returns a Click Result with:
      result.stdout    — text sent to stdout
      result.stderr    — text sent to stderr
      result.exit_code — integer exit status
    """
    if client is None:
        client = _default_mock()
    runner = CliRunner()          # Click 8.4: stdout/stderr always separated
    with patch("edgar_client.cli.EdgarClient", return_value=client):
        return runner.invoke(
            cli,
            ["--user-agent", UA] + args,
            catch_exceptions=False,
        )


# =============================================================================
# TestCliGroup
# =============================================================================


class TestCliGroup:
    def test_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "EDGAR" in result.stdout

    def test_short_help_flag(self):
        result = CliRunner().invoke(cli, ["-h"])
        assert result.exit_code == 0

    def test_missing_user_agent_exits_nonzero(self):
        mock = _default_mock()
        runner = CliRunner()
        with patch("edgar_client.cli.EdgarClient", return_value=mock):
            result = runner.invoke(cli, ["company", "AAPL"])
        assert result.exit_code != 0

    def test_user_agent_from_env_var(self):
        mock = _default_mock()
        runner = CliRunner()
        with patch("edgar_client.cli.EdgarClient", return_value=mock):
            result = runner.invoke(
                cli,
                ["company", "AAPL"],
                env={"EDGAR_USER_AGENT": UA},
                catch_exceptions=False,
            )
        assert result.exit_code == 0

    def test_invalid_user_agent_shows_param_hint(self):
        runner = CliRunner()
        # EdgarClient raises InvalidUserAgentError; cli converts to BadParameter
        with patch("edgar_client.cli.EdgarClient", side_effect=InvalidUserAgentError("bad")):
            result = runner.invoke(cli, ["--user-agent", "bad", "company", "AAPL"])
        assert result.exit_code != 0
        combined = result.stdout + result.stderr
        assert "--user-agent" in combined

    def test_subcommand_help(self):
        runner = CliRunner()
        for cmd in ["company", "financials", "search", "compare"]:
            result = runner.invoke(cli, ["--user-agent", UA, cmd, "--help"])
            assert result.exit_code == 0, f"{cmd} --help failed: {result.output}"

    def test_client_closed_after_command(self):
        """ctx.call_on_close must call client.close() after the command exits."""
        mock = _default_mock()
        _run(["company", "AAPL"], client=mock)
        mock.close.assert_called_once()


# =============================================================================
# TestCompanyCommand
# =============================================================================


class TestCompanyCommand:
    def test_exit_zero(self):
        assert _run(["company", "AAPL"]).exit_code == 0

    def test_shows_company_name(self):
        assert "Apple Inc." in _run(["company", "AAPL"]).stdout

    def test_shows_cik(self):
        assert "0000320193" in _run(["company", "AAPL"]).stdout

    def test_shows_sic(self):
        assert "7372" in _run(["company", "AAPL"]).stdout

    def test_shows_filing_form_type(self):
        assert "10-K" in _run(["company", "AAPL"]).stdout

    def test_shows_filing_date(self):
        assert "2024-11-01" in _run(["company", "AAPL"]).stdout

    def test_json_output_is_valid_json(self):
        result = _run(["company", "AAPL", "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, dict)

    def test_json_output_has_required_keys(self):
        data = json.loads(_run(["company", "AAPL", "--output", "json"]).stdout)
        assert data["name"]   == "Apple Inc."
        assert data["cik"]    == "0000320193"
        assert data["ticker"] == "AAPL"
        assert isinstance(data["filings"], list)

    def test_json_filings_have_expected_shape(self):
        data   = json.loads(_run(["company", "AAPL", "--output", "json"]).stdout)
        filing = data["filings"][0]
        for key in ("accession_number", "form_type", "filing_date", "file_url"):
            assert key in filing, f"Missing key: {key}"

    def test_short_output_flag(self):
        result = _run(["company", "AAPL", "-o", "json"])
        assert result.exit_code == 0
        json.loads(result.stdout)

    def test_invalid_ticker_exits_nonzero(self):
        mock = _default_mock()
        mock.get_company.side_effect = InvalidTickerError("Unknown: ZZZZ")
        assert _run(["company", "ZZZZ"], client=mock).exit_code != 0

    def test_not_found_exits_nonzero(self):
        mock = _default_mock()
        mock.get_company.side_effect = EdgarNotFoundError("404")
        assert _run(["company", "ZZZZ"], client=mock).exit_code != 0

    def test_delegated_to_client(self):
        mock = _default_mock()
        _run(["company", "AAPL"], client=mock)
        mock.get_company.assert_called_once_with("AAPL")


# =============================================================================
# TestFinancialsCommand
# =============================================================================


class TestFinancialsCommand:
    def test_exit_zero(self):
        assert _run(["financials", "AAPL"]).exit_code == 0

    def test_shows_revenue_label(self):
        assert "Revenue" in _run(["financials", "AAPL"]).stdout

    def test_shows_years(self):
        assert "2023" in _run(["financials", "AAPL"]).stdout

    def test_shows_formatted_value(self):
        out = _run(["financials", "AAPL"]).stdout
        assert "$" in out and "B" in out   # $383.29B style

    def test_years_limit_1(self):
        data = json.loads(
            _run(["financials", "AAPL", "--years", "1", "--output", "json"]).stdout
        )
        assert len(data["data"]) <= 1

    def test_all_years_for_large_n(self):
        # Fixture has 3 annual values; --years 10 should return all 3
        data = json.loads(
            _run(["financials", "AAPL", "--years", "10", "--output", "json"]).stdout
        )
        assert len(data["data"]) == 3

    def test_metric_net_income(self):
        result = _run(["financials", "AAPL", "--metric", "net_income"])
        assert result.exit_code == 0
        assert "Net Income" in result.stdout

    def test_json_output_structure(self):
        data = json.loads(
            _run(["financials", "AAPL", "--output", "json"]).stdout
        )
        assert data["ticker"] == "AAPL"
        assert data["metric"] == "revenue"
        assert data["unit"]   == "USD"
        assert "data"         in data
        # Values must be strings (lossless Decimal precision)
        for v in data["data"].values():
            assert isinstance(v, str)

    def test_json_years_ascending(self):
        data  = json.loads(_run(["financials", "AAPL", "--output", "json"]).stdout)
        years = [int(k) for k in data["data"]]
        assert years == sorted(years)

    def test_invalid_metric_rejected_by_click(self):
        runner = CliRunner()
        mock   = _default_mock()
        with patch("edgar_client.cli.EdgarClient", return_value=mock):
            result = runner.invoke(
                cli,
                ["--user-agent", UA, "financials", "AAPL", "--metric", "notametric"],
            )
        assert result.exit_code != 0

    def test_metric_not_present_exits_nonzero(self):
        mock = _default_mock()
        empty = CompanyFacts.from_dict(
            {"cik": 12345, "entityName": "No Revenue Co", "facts": {}}
        )
        mock.get_facts.return_value = empty
        assert _run(["financials", "AAPL", "--metric", "revenue"], client=mock).exit_code != 0

    def test_calls_get_company_and_get_facts(self):
        mock = _default_mock()
        _run(["financials", "AAPL"], client=mock)
        mock.get_company.assert_called_once()
        mock.get_facts.assert_called_once()


# =============================================================================
# TestSearchCommand
# =============================================================================


class TestSearchCommand:
    def test_exit_zero(self):
        assert _run(["search", "Apple"]).exit_code == 0

    def test_shows_company_name(self):
        assert "Apple Inc." in _run(["search", "Apple"]).stdout

    def test_shows_ticker(self):
        assert "AAPL" in _run(["search", "Apple"]).stdout

    def test_shows_cik(self):
        assert "0000320193" in _run(["search", "Apple"]).stdout

    def test_json_output_is_valid(self):
        result = _run(["search", "Apple", "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_json_result_has_required_keys(self):
        data = json.loads(_run(["search", "Apple", "--output", "json"]).stdout)
        assert len(data) > 0
        r = data[0]
        for key in ("name", "ticker", "cik"):
            assert key in r

    def test_no_results_shows_message(self):
        mock = _default_mock()
        mock.search.return_value          = []
        mock._session.search.return_value = []
        result = _run(["search", "ZZZZNOMATCH"], client=mock)
        assert result.exit_code == 0
        assert "No results" in result.stdout

    def test_efts_fallback_when_name_search_empty(self):
        """When name search returns [], EFTS should be tried next."""
        mock = _default_mock()
        mock.search.return_value          = []             # name search empty
        mock._session.search.return_value = _SEARCH_RESULTS
        result = _run(["search", "electric vehicles"], client=mock)
        assert result.exit_code == 0
        mock._session.search.assert_called_once()

    def test_limit_applied(self):
        mock = _default_mock()
        mock.search.return_value = [_SEARCH_RESULTS[0]] * 50
        data = json.loads(
            _run(["search", "Apple", "--limit", "5", "--output", "json"], client=mock).stdout
        )
        assert len(data) == 5

    def test_limit_hint_shown_when_at_max(self):
        mock = _default_mock()
        mock.search.return_value = [_SEARCH_RESULTS[0]] * 20
        result = _run(["search", "Apple"], client=mock)
        assert "--limit" in result.stdout or "limit" in result.stdout.lower()


# =============================================================================
# TestCompareCommand
# =============================================================================


class TestCompareCommand:
    def _compare_mock(self) -> MagicMock:
        mock = _default_mock()
        def _facts(ticker_or_cik, **kw):
            return _MSFT_FACTS if "MSFT" in ticker_or_cik.upper() else _FACTS
        mock.get_facts.side_effect = _facts
        return mock

    def test_exit_zero(self):
        assert _run(["compare", "AAPL", "MSFT"], client=self._compare_mock()).exit_code == 0

    def test_shows_both_tickers(self):
        out = _run(["compare", "AAPL", "MSFT"], client=self._compare_mock()).stdout
        assert "AAPL" in out
        assert "MSFT" in out

    def test_shows_shared_years(self):
        out = _run(["compare", "AAPL", "MSFT"], client=self._compare_mock()).stdout
        assert "2022" in out
        assert "2023" in out

    def test_years_option(self):
        result = _run(["compare", "AAPL", "MSFT", "--years", "1"],
                      client=self._compare_mock())
        assert result.exit_code == 0

    def test_json_output_structure(self):
        data = json.loads(
            _run(["compare", "AAPL", "MSFT", "--output", "json"],
                 client=self._compare_mock()).stdout
        )
        for key in ("metric", "tickers", "data"):
            assert key in data
        assert len(data["tickers"]) == 2

    def test_json_data_has_year_keys(self):
        data  = json.loads(
            _run(["compare", "AAPL", "MSFT", "--output", "json"],
                 client=self._compare_mock()).stdout
        )
        years = list(data["data"].keys())
        assert all(y.isdigit() for y in years)

    def test_single_ticker_fails(self):
        runner = CliRunner()
        mock   = _default_mock()
        with patch("edgar_client.cli.EdgarClient", return_value=mock):
            result = runner.invoke(
                cli,
                ["--user-agent", UA, "compare", "AAPL"],
                catch_exceptions=False,
            )
        assert result.exit_code != 0

    def test_one_bad_ticker_continues(self):
        """If one ticker errors, compare still succeeds for the others."""
        mock = _default_mock()
        def _facts(ticker_or_cik, **kw):
            if "ZZZZ" in ticker_or_cik.upper():
                raise InvalidTickerError("Unknown: ZZZZ")
            return _FACTS
        mock.get_facts.side_effect = _facts
        result = _run(["compare", "AAPL", "ZZZZ"], client=mock)
        assert result.exit_code == 0
        assert "AAPL" in result.stdout

    def test_all_bad_tickers_exits_nonzero(self):
        mock = _default_mock()
        mock.get_facts.side_effect = InvalidTickerError("Unknown")
        assert _run(["compare", "XXX1", "XXX2"], client=mock).exit_code != 0


# =============================================================================
# TestOutputFormat — all commands produce valid JSON
# =============================================================================


class TestOutputFormat:
    @pytest.mark.parametrize("args", [
        ["company",    "AAPL",         "--output", "json"],
        ["financials", "AAPL",         "--output", "json"],
        ["search",     "Apple",        "--output", "json"],
        ["compare",    "AAPL", "MSFT", "--output", "json"],
    ])
    def test_json_is_parseable(self, args: list[str]):
        mock = _default_mock()
        # compare needs per-ticker facts
        def _facts(t, **kw):
            return _MSFT_FACTS if "MSFT" in t.upper() else _FACTS
        mock.get_facts.side_effect = _facts

        result = _run(args, client=mock)
        assert result.exit_code == 0, f"Exit {result.exit_code} for {args}: {result.stdout}"
        json.loads(result.stdout)  # raises if invalid

    def test_table_output_is_not_json(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads(_run(["company", "AAPL"]).stdout)


# =============================================================================
# TestErrorHandling — every EdgarError → exit 1, error on stderr
# =============================================================================


class TestErrorHandling:
    @pytest.mark.parametrize("exc_class", [
        InvalidTickerError,
        EdgarNotFoundError,
        EdgarRateLimitError,
        EdgarTimeoutError,
        EdgarConnectionError,
    ])
    def test_edgar_error_exits_one(self, exc_class):
        mock = _default_mock()
        mock.get_company.side_effect = exc_class("test error")
        assert _run(["company", "ZZZZ"], client=mock).exit_code == 1

    def test_error_message_goes_to_stderr_not_stdout(self):
        """Errors must land on stderr so JSON piping stays clean."""
        mock = _default_mock()
        mock.get_company.side_effect = InvalidTickerError("Unknown: ZZZZ")
        result = _run(["company", "ZZZZ"], client=mock)
        # stdout must be empty (no partial output after an error)
        assert result.stdout.strip() == ""
        # Error message must appear on stderr
        assert len(result.stderr.strip()) > 0

    def test_rate_limit_message_mentions_wait(self):
        mock = _default_mock()
        mock.get_company.side_effect = EdgarRateLimitError("429")
        result = _run(["company", "AAPL"], client=mock)
        assert "wait" in result.stderr.lower() or "429" in result.stderr

    def test_not_found_message_shows_404(self):
        mock = _default_mock()
        mock.get_company.side_effect = EdgarNotFoundError("HTTP 404")
        result = _run(["company", "AAPL"], client=mock)
        assert "404" in result.stderr or "Not found" in result.stderr

    def test_invalid_ticker_suggests_search(self):
        mock = _default_mock()
        mock.get_company.side_effect = InvalidTickerError("Unknown: ZZZZ")
        result = _run(["company", "ZZZZ"], client=mock)
        # Should suggest using edgar search
        assert "search" in result.stderr.lower()
