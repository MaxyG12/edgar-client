"""
Tests for the edgar_client domain model layer.

Scope: everything in models.py that represents what the API *returns* —
FinancialValue, FinancialSeries, Filing, Company, CompanyFacts.

Strategy
--------
Every test constructs objects from raw dicts (mirroring the real EDGAR JSON),
not from HTTP responses.  This keeps tests fast and focused on parsing logic,
not network mechanics.  The from_response() path is covered in test_session.py.

Layout
------
  TestFinancialValue      from_dict, Decimal precision, dates, repr
  TestFinancialSeries     from_dict, properties, as_dict, repr
  TestFiling              from_dict, field mapping, file_url construction, repr
  TestCompany             from_dict, sic_code rename, filing limit, repr
  TestCompanyFacts        from_dict, get/taxonomies/concepts, repr
  TestBackwardCompat      FactValue alias, from_response still works
  TestEdgeCases           missing fields, empty arrays, malformed data
"""

import datetime
import json
from decimal import Decimal

import pytest

from edgar_client.exceptions import DataParseError
from edgar_client.models import (
    Company,
    CompanyFacts,
    FactValue,
    Filing,
    FinancialSeries,
    FinancialValue,
    Response,
    _build_filing_url,
    _column_to_rows,
    _pad_cik,
    _parse_date,
)


# =============================================================================
# Shared test data — real-world-like EDGAR payloads
# =============================================================================

# One entry from a companyfacts units[unit] array
_REVENUE_VALUE_DICT = {
    "start": "2022-10-01",
    "end":   "2023-09-30",
    "val":   383285000000,
    "accn":  "0000320193-23-000077",
    "form":  "10-K",
    "filed": "2023-11-03",
    "frame": "CY2023",
}

_QUARTERLY_VALUE_DICT = {
    "start": "2023-07-01",
    "end":   "2023-09-30",
    "val":   89498000000,
    "accn":  "0000320193-23-000077",
    "form":  "10-Q",
    "filed": "2023-11-03",
    "frame": "CY2023Q3",
}

# One concept entry from facts[taxonomy]
_REVENUES_CONCEPT = {
    "label":       "Revenues",
    "description": "Amount of revenue recognised from contracts with customers",
    "units": {
        "USD": [
            {   # FY2023 10-K
                "start": "2022-10-01", "end": "2023-09-30",
                "val": 383285000000, "accn": "xxx", "form": "10-K",
                "filed": "2023-11-03", "frame": "CY2023",
            },
            {   # FY2022 10-K
                "start": "2021-10-01", "end": "2022-09-30",
                "val": 394328000000, "accn": "yyy", "form": "10-K",
                "filed": "2022-10-28", "frame": "CY2022",
            },
            {   # Q3 FY2023 10-Q
                "start": "2023-07-01", "end": "2023-09-30",
                "val": 89498000000, "accn": "zzz", "form": "10-Q",
                "filed": "2023-11-03",
            },
        ]
    },
}

# Raw submissions JSON (what the API actually returns)
_SUBMISSIONS_JSON = {
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
            "accessionNumber":      ["0000320193-24-000001", "0000320193-23-000077"],
            "filingDate":           ["2024-11-01",           "2023-11-03"],
            "reportDate":           ["2024-09-28",           "2023-09-30"],
            "form":                 ["10-K",                 "10-K"],
            "primaryDocument":      ["aapl-20240928.htm",   "aapl-20230930.htm"],
            "primaryDocDescription":["FORM 10-K",           "FORM 10-K"],
            "size":                 [12000000,               11500000],
            "isXBRL":               [1,                      1],
        }
    },
}

# Raw companyfacts JSON
_FACTS_JSON = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": _REVENUES_CONCEPT,
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Entity Common Stock, Shares Outstanding",
                "description": "Outstanding shares.",
                "units": {
                    "shares": [
                        {"end": "2023-09-30", "val": 15552752000,
                         "accn": "aaa", "form": "10-K", "filed": "2023-11-03"},
                    ]
                },
            }
        },
    },
}


# =============================================================================
# TestFinancialValue
# =============================================================================


class TestFinancialValue:
    """FinancialValue.from_dict() parses one entry from a units[unit] array."""

    def test_value_parsed_as_decimal(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert isinstance(fv.value, Decimal)
        assert fv.value == Decimal("383285000000")

    def test_large_int_exact_precision(self):
        # Values > 2^53 would lose digits as float.
        large = {"val": 999999999999999, "end": "2023-09-30", "form": "10-K"}
        fv = FinancialValue.from_dict(large, unit="USD")
        assert fv.value == Decimal("999999999999999")

    def test_float_val_no_imprecision(self):
        # val=1.5 must not become Decimal('1.4999…')
        fv = FinancialValue.from_dict({"val": 1.5, "end": "2023-09-30", "form": "10-K"}, unit="USD")
        assert fv.value == Decimal("1.5")

    def test_unit_stored(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert fv.unit == "USD"

    def test_unit_shares(self):
        fv = FinancialValue.from_dict({"val": 15552752000, "end": "2023-09-30", "form": "10-K"}, unit="shares")
        assert fv.unit == "shares"

    def test_start_date_parsed(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert fv.start == datetime.date(2022, 10, 1)

    def test_end_date_parsed(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert fv.end == datetime.date(2023, 9, 30)

    def test_start_optional_for_instant_facts(self):
        # Instant-point facts (e.g. balance sheet items) have no start date.
        instant = {"val": 1000, "end": "2023-09-30", "form": "10-K"}
        fv = FinancialValue.from_dict(instant, unit="USD")
        assert fv.start is None

    def test_form_stored(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert fv.form == "10-K"

    def test_frame_stored(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert fv.frame == "CY2023"

    def test_frame_optional(self):
        no_frame = {**_REVENUE_VALUE_DICT}
        del no_frame["frame"]
        fv = FinancialValue.from_dict(no_frame, unit="USD")
        assert fv.frame is None

    def test_missing_val_defaults_to_zero(self):
        fv = FinancialValue.from_dict({"end": "2023-09-30", "form": "10-K"}, unit="USD")
        assert fv.value == Decimal(0)

    def test_repr_contains_value(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        r = repr(fv)
        assert "383,285,000,000" in r or "383285000000" in r

    def test_repr_contains_form(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert "10-K" in repr(fv)

    def test_repr_contains_end_date(self):
        fv = FinancialValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert "2023-09-30" in repr(fv)

    def test_fact_value_alias(self):
        # FactValue is a backward-compat alias for FinancialValue.
        assert FactValue is FinancialValue
        fv = FactValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert isinstance(fv, FinancialValue)


# =============================================================================
# TestFinancialSeries
# =============================================================================


class TestFinancialSeries:
    """FinancialSeries.from_dict() wraps all values for one XBRL concept."""

    def _revenues(self) -> FinancialSeries:
        return FinancialSeries.from_dict(_REVENUES_CONCEPT, concept_name="Revenues")

    # -- Construction --

    def test_concept_name_stored(self):
        fs = self._revenues()
        assert fs.concept_name == "Revenues"

    def test_concept_name_defaults_empty(self):
        fs = FinancialSeries.from_dict(_REVENUES_CONCEPT)
        assert fs.concept_name == ""

    def test_description_stored(self):
        fs = self._revenues()
        assert "revenue" in fs.description.lower()

    def test_unit_detected(self):
        fs = self._revenues()
        assert fs.unit == "USD"

    def test_values_all_parsed(self):
        fs = self._revenues()
        # 2 annual + 1 quarterly = 3 total
        assert len(fs.values) == 3

    def test_all_values_are_financial_value(self):
        fs = self._revenues()
        assert all(isinstance(v, FinancialValue) for v in fs.values)

    def test_values_sorted_newest_first(self):
        fs = self._revenues()
        ends = [v.end for v in fs.values]
        assert ends == sorted(ends, reverse=True)

    def test_unit_passed_to_values(self):
        fs = self._revenues()
        assert all(v.unit == "USD" for v in fs.values)

    def test_empty_units_gives_empty_values(self):
        fs = FinancialSeries.from_dict({"label": "X", "description": "Y", "units": {}})
        assert fs.values == []
        assert fs.unit == ""

    # -- annual_values property --

    def test_annual_values_returns_only_10k(self):
        fs = self._revenues()
        av = fs.annual_values
        assert all(v.form == "10-K" for v in av)

    def test_annual_values_count(self):
        fs = self._revenues()
        assert len(fs.annual_values) == 2   # FY2023 and FY2022

    def test_annual_values_excludes_quarterly(self):
        fs = self._revenues()
        forms = {v.form for v in fs.annual_values}
        assert "10-Q" not in forms

    def test_annual_values_is_subset_of_values(self):
        fs = self._revenues()
        assert set(id(v) for v in fs.annual_values).issubset(
            set(id(v) for v in fs.values)
        )

    def test_annual_values_empty_when_no_10k(self):
        only_quarterly = {
            "label": "Q-only",
            "description": "",
            "units": {"USD": [
                {"val": 100, "end": "2023-09-30", "form": "10-Q"},
            ]},
        }
        fs = FinancialSeries.from_dict(only_quarterly)
        assert fs.annual_values == []

    # -- latest_value property --

    def test_latest_value_is_most_recent_annual(self):
        fs = self._revenues()
        # FY2023 (end 2023-09-30) is more recent than FY2022 (end 2022-09-30)
        assert fs.latest_value == Decimal("383285000000")

    def test_latest_value_none_when_no_annual(self):
        only_quarterly = {
            "label": "Q", "description": "",
            "units": {"USD": [{"val": 1, "end": "2023-09-30", "form": "10-Q"}]},
        }
        fs = FinancialSeries.from_dict(only_quarterly)
        assert fs.latest_value is None

    def test_latest_value_none_when_empty(self):
        fs = FinancialSeries.from_dict({"label": "X", "description": "", "units": {}})
        assert fs.latest_value is None

    # -- as_dict() method --

    def test_as_dict_returns_annual_only(self):
        fs = self._revenues()
        d = fs.as_dict()
        # Only 10-K years; the Q3 value should not appear
        assert set(d.keys()) == {2023, 2022}

    def test_as_dict_correct_values(self):
        fs = self._revenues()
        d = fs.as_dict()
        assert d[2023] == Decimal("383285000000")
        assert d[2022] == Decimal("394328000000")

    def test_as_dict_sorted_ascending_by_year(self):
        fs = self._revenues()
        years = list(fs.as_dict().keys())
        assert years == sorted(years)

    def test_as_dict_empty_when_no_annual(self):
        only_quarterly = {
            "label": "Q", "description": "",
            "units": {"USD": [{"val": 1, "end": "2023-09-30", "form": "10-Q"}]},
        }
        fs = FinancialSeries.from_dict(only_quarterly)
        assert fs.as_dict() == {}

    def test_as_dict_amended_10k_uses_latest_end_date(self):
        """When two 10-K entries share the same year, keep the one with the
        later end date (e.g. an amended filing covering a longer period)."""
        amended = {
            "label": "X", "description": "",
            "units": {"USD": [
                {"val": 100, "end": "2023-09-28", "form": "10-K"},   # original
                {"val": 200, "end": "2023-09-30", "form": "10-K"},   # amended — later end
            ]},
        }
        fs = FinancialSeries.from_dict(amended)
        assert fs.as_dict() == {2023: Decimal("200")}

    # -- repr --

    def test_repr_contains_concept_name(self):
        fs = self._revenues()
        assert "Revenues" in repr(fs)

    def test_repr_contains_unit(self):
        fs = self._revenues()
        assert "USD" in repr(fs)

    def test_repr_contains_value_count(self):
        fs = self._revenues()
        r = repr(fs)
        assert "values=3" in r
        assert "annual=2" in r


# =============================================================================
# TestFiling
# =============================================================================


class TestFiling:
    """Filing.from_dict() maps a row-dict to a Filing object."""

    _ROW = {
        "accessionNumber":       "0000320193-24-000001",
        "filingDate":            "2024-11-01",
        "reportDate":            "2024-09-28",
        "form":                  "10-K",
        "primaryDocument":       "aapl-20240928.htm",
        "primaryDocDescription": "FORM 10-K",
        "size":                  12000000,
        "isXBRL":                1,
    }

    def test_accession_number(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.accession_number == "0000320193-24-000001"

    def test_form_type_field_name(self):
        # The field is called form_type (not form) to avoid collision with the
        # Python built-in and to be explicit about the SEC nomenclature.
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.form_type == "10-K"

    def test_filing_date_parsed(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.filing_date == datetime.date(2024, 11, 1)

    def test_report_date_parsed(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.report_date == datetime.date(2024, 9, 28)

    def test_primary_document(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.primary_document == "aapl-20240928.htm"

    def test_description(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.description == "FORM 10-K"

    def test_size(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.size == 12000000

    def test_is_xbrl_true(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert f.is_xbrl is True

    def test_is_xbrl_false_when_zero(self):
        row = {**self._ROW, "isXBRL": 0}
        f = Filing.from_dict(row, cik="0000320193")
        assert f.is_xbrl is False

    # -- file_url construction --

    def test_file_url_constructed(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        expected = (
            "https://www.sec.gov/Archives/edgar/data"
            "/320193/000032019324000001/aapl-20240928.htm"
        )
        assert f.file_url == expected

    def test_file_url_cik_without_leading_zeros(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        # CIK in URL must be the integer form (no leading zeros)
        assert "/320193/" in f.file_url
        assert "/0000320193/" not in f.file_url

    def test_file_url_accession_without_dashes(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert "/000032019324000001/" in f.file_url
        assert "0000320193-24-000001" not in f.file_url

    def test_file_url_empty_when_no_primary_doc(self):
        row = {**self._ROW, "primaryDocument": None}
        f = Filing.from_dict(row, cik="0000320193")
        assert f.file_url == ""

    def test_file_url_empty_when_no_cik(self):
        # If CIK is missing we return empty string gracefully.
        f = Filing.from_dict(self._ROW, cik="")
        assert f.file_url == ""

    def test_file_url_uses_cik_from_data_sentinel(self):
        # _column_to_rows injects _cik into each row dict.
        row = {**self._ROW, "_cik": "0000320193"}
        f = Filing.from_dict(row)   # no explicit cik kwarg
        assert "320193" in f.file_url

    def test_missing_report_date_is_none(self):
        row = {**self._ROW, "reportDate": None}
        f = Filing.from_dict(row, cik="0000320193")
        assert f.report_date is None

    def test_missing_size_is_none(self):
        row = {k: v for k, v in self._ROW.items() if k != "size"}
        f = Filing.from_dict(row, cik="0000320193")
        assert f.size is None

    # -- repr --

    def test_repr_contains_form_type(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert "10-K" in repr(f)

    def test_repr_contains_filing_date(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert "2024-11-01" in repr(f)

    def test_repr_contains_accession(self):
        f = Filing.from_dict(self._ROW, cik="0000320193")
        assert "0000320193-24-000001" in repr(f)


# =============================================================================
# TestCompany
# =============================================================================


class TestCompany:
    """Company.from_dict() parses the full submissions JSON."""

    def _company(self) -> Company:
        return Company.from_dict(_SUBMISSIONS_JSON)

    # -- Core fields --

    def test_name(self):
        assert self._company().name == "Apple Inc."

    def test_cik_padded(self):
        # CIK comes as integer 320193 in JSON; must be stored zero-padded.
        assert self._company().cik == "0000320193"

    def test_sic_code_field_name(self):
        # The old field was 'sic'; it's now 'sic_code' per the user's spec.
        c = self._company()
        assert c.sic_code == "7372"
        assert not hasattr(c, "sic")    # old name must not exist

    def test_sic_code_is_string(self):
        # SIC is an integer in the JSON but we store it as a string.
        c = self._company()
        assert isinstance(c.sic_code, str)

    def test_sic_description(self):
        assert self._company().sic_description == "Prepackaged Software"

    def test_state_of_incorporation(self):
        assert self._company().state_of_incorporation == "CA"

    def test_fiscal_year_end(self):
        assert self._company().fiscal_year_end == "0930"

    def test_ticker(self):
        assert self._company().ticker == "AAPL"

    def test_ein(self):
        assert self._company().ein == "94-2404110"

    # -- Filings --

    def test_filings_are_filing_objects(self):
        c = self._company()
        assert all(isinstance(f, Filing) for f in c.filings)

    def test_filing_form_type(self):
        c = self._company()
        assert c.filings[0].form_type == "10-K"

    def test_filings_count(self):
        # _SUBMISSIONS_JSON has 2 entries in the column arrays.
        assert len(self._company().filings) == 2

    def test_filing_file_url_built(self):
        c = self._company()
        assert c.filings[0].file_url.startswith(
            "https://www.sec.gov/Archives/edgar/data/320193/"
        )

    def test_filings_capped_at_20(self):
        """Generate 30 filings in the JSON; verify only 20 are parsed."""
        n = 30
        big = {
            **_SUBMISSIONS_JSON,
            "filings": {
                "recent": {
                    "accessionNumber":       [f"0000320193-24-{i:06d}" for i in range(n)],
                    "filingDate":            ["2024-11-01"] * n,
                    "reportDate":            ["2024-09-28"] * n,
                    "form":                  ["10-K"] * n,
                    "primaryDocument":       [f"doc-{i}.htm" for i in range(n)],
                    "primaryDocDescription": ["FORM 10-K"] * n,
                    "size":                  [12000000] * n,
                    "isXBRL":                [1] * n,
                }
            },
        }
        c = Company.from_dict(big)
        assert len(c.filings) == 20

    def test_filings_order_preserved(self):
        # First filing in the JSON (index 0) must be first in the list.
        c = self._company()
        assert c.filings[0].accession_number == "0000320193-24-000001"
        assert c.filings[1].accession_number == "0000320193-23-000077"

    def test_empty_filings(self):
        no_filings = {**_SUBMISSIONS_JSON, "filings": {"recent": {}}}
        c = Company.from_dict(no_filings)
        assert c.filings == []

    def test_missing_optional_fields_default_to_none(self):
        minimal = {"cik": 12345, "name": "Minimal Co."}
        c = Company.from_dict(minimal)
        assert c.sic_code is None
        assert c.ticker is None
        assert c.fiscal_year_end is None

    def test_missing_cik_raises_data_parse_error(self):
        with pytest.raises(DataParseError):
            Company.from_dict({"name": "No CIK"})

    # -- repr --

    def test_repr_contains_name(self):
        assert "Apple Inc." in repr(self._company())

    def test_repr_contains_ticker(self):
        assert "AAPL" in repr(self._company())

    def test_repr_contains_cik(self):
        assert "0000320193" in repr(self._company())


# =============================================================================
# TestCompanyFacts
# =============================================================================


class TestCompanyFacts:
    """CompanyFacts.from_dict() parses the full companyfacts JSON."""

    def _facts(self) -> CompanyFacts:
        return CompanyFacts.from_dict(_FACTS_JSON)

    # -- Construction --

    def test_cik_padded(self):
        assert self._facts().cik == "0000320193"

    def test_entity_name(self):
        assert self._facts().entity_name == "Apple Inc."

    def test_taxonomies_present(self):
        f = self._facts()
        assert "us-gaap" in f.taxonomies()
        assert "dei" in f.taxonomies()

    def test_concepts_present(self):
        f = self._facts()
        assert "Revenues" in f.concepts("us-gaap")
        assert "EntityCommonStockSharesOutstanding" in f.concepts("dei")

    def test_concepts_unknown_taxonomy_returns_empty(self):
        assert self._facts().concepts("nonexistent") == []

    # -- get() method --

    def test_get_returns_financial_series(self):
        fs = self._facts().get("us-gaap", "Revenues")
        assert isinstance(fs, FinancialSeries)

    def test_get_concept_name_set(self):
        fs = self._facts().get("us-gaap", "Revenues")
        assert fs.concept_name == "Revenues"

    def test_get_unit(self):
        fs = self._facts().get("us-gaap", "Revenues")
        assert fs.unit == "USD"

    def test_get_shares_unit(self):
        fs = self._facts().get("dei", "EntityCommonStockSharesOutstanding")
        assert fs.unit == "shares"

    def test_get_latest_value(self):
        fs = self._facts().get("us-gaap", "Revenues")
        assert fs.latest_value == Decimal("383285000000")

    def test_get_missing_taxonomy_raises_key_error(self):
        with pytest.raises(KeyError):
            self._facts().get("nonexistent", "Revenues")

    def test_get_missing_concept_raises_key_error(self):
        with pytest.raises(KeyError):
            self._facts().get("us-gaap", "NonExistentConcept")

    def test_as_dict_via_get(self):
        d = self._facts().get("us-gaap", "Revenues").as_dict()
        assert 2023 in d
        assert 2022 in d

    def test_missing_cik_raises_data_parse_error(self):
        with pytest.raises(DataParseError):
            CompanyFacts.from_dict({"entityName": "No CIK"})

    # -- repr --

    def test_repr_contains_entity_name(self):
        assert "Apple Inc." in repr(self._facts())

    def test_repr_contains_cik(self):
        assert "0000320193" in repr(self._facts())

    def test_repr_contains_taxonomy_count(self):
        assert "taxonomies=2" in repr(self._facts())

    def test_repr_contains_concept_count(self):
        # 1 us-gaap + 1 dei = 2 concepts total
        assert "concepts=2" in repr(self._facts())


# =============================================================================
# TestBackwardCompat
# =============================================================================


class TestBackwardCompat:
    """Old names and old calling conventions must still work."""

    def test_fact_value_alias(self):
        assert FactValue is FinancialValue

    def test_fact_value_from_dict(self):
        fv = FactValue.from_dict(_REVENUE_VALUE_DICT, unit="USD")
        assert isinstance(fv, FinancialValue)

    def test_company_from_response(self):
        body = json.dumps(_SUBMISSIONS_JSON).encode()
        resp = Response(status_code=200, body=body,
                        url="https://data.sec.gov/submissions/CIK0000320193.json")
        c = Company.from_response(resp)
        assert c.name == "Apple Inc."

    def test_company_facts_from_response(self):
        body = json.dumps(_FACTS_JSON).encode()
        resp = Response(status_code=200, body=body,
                        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
        cf = CompanyFacts.from_response(resp)
        assert cf.entity_name == "Apple Inc."

    def test_from_response_bad_json_raises_data_parse_error(self):
        resp = Response(status_code=200, body=b"not json",
                        url="https://data.sec.gov/test")
        with pytest.raises(DataParseError):
            Company.from_response(resp)


# =============================================================================
# TestEdgeCases
# =============================================================================


class TestEdgeCases:
    """Edge cases: missing fields, malformed data, boundary conditions."""

    def test_financial_value_missing_end_date(self):
        fv = FinancialValue.from_dict({"val": 100, "form": "10-K"}, unit="USD")
        # end should default to datetime.date.min, not raise
        assert fv.end == datetime.date.min

    def test_financial_series_multi_unit_uses_first(self):
        multi_unit = {
            "label": "X", "description": "",
            "units": {
                "USD":    [{"val": 1, "end": "2023-09-30", "form": "10-K"}],
                "shares": [{"val": 2, "end": "2023-09-30", "form": "10-K"}],
            },
        }
        fs = FinancialSeries.from_dict(multi_unit)
        # First key in dict is used; dict ordering is stable in Python 3.7+
        assert fs.unit in ("USD", "shares")
        assert len(fs.values) > 0

    def test_company_no_tickers(self):
        no_tickers = {**_SUBMISSIONS_JSON, "tickers": []}
        c = Company.from_dict(no_tickers)
        assert c.ticker is None

    def test_company_sic_none(self):
        no_sic = {k: v for k, v in _SUBMISSIONS_JSON.items() if k != "sic"}
        c = Company.from_dict(no_sic)
        assert c.sic_code is None

    def test_filing_empty_accession(self):
        # A row with only the form field — all other fields absent.
        f = Filing.from_dict({"form": "10-K"})
        assert f.accession_number == ""
        assert f.file_url == ""

    def test_company_facts_empty_facts(self):
        cf = CompanyFacts.from_dict({"cik": 12345, "entityName": "X", "facts": {}})
        assert cf.taxonomies() == []

    def test_pad_cik_helper(self):
        assert _pad_cik(320193) == "0000320193"
        assert _pad_cik("320193") == "0000320193"
        assert _pad_cik("0000320193") == "0000320193"

    def test_parse_date_valid(self):
        assert _parse_date("2023-09-30") == datetime.date(2023, 9, 30)

    def test_parse_date_none_input(self):
        assert _parse_date(None) is None

    def test_parse_date_empty_string(self):
        assert _parse_date("") is None

    def test_parse_date_invalid_format(self):
        assert _parse_date("not-a-date") is None

    def test_build_filing_url_correct(self):
        url = _build_filing_url("0000320193", "0000320193-24-000001", "doc.htm")
        assert url == (
            "https://www.sec.gov/Archives/edgar/data"
            "/320193/000032019324000001/doc.htm"
        )

    def test_build_filing_url_no_doc(self):
        assert _build_filing_url("0000320193", "0000320193-24-000001", None) == ""

    def test_build_filing_url_empty_cik(self):
        assert _build_filing_url("", "0000320193-24-000001", "doc.htm") == ""

    def test_column_to_rows_respects_limit(self):
        recent = {
            "accessionNumber": [f"acc-{i}" for i in range(30)],
            "form":            ["10-K"] * 30,
            "filingDate":      ["2024-01-01"] * 30,
        }
        rows = _column_to_rows(recent, cik="0000320193", limit=5)
        assert len(rows) == 5

    def test_column_to_rows_injects_cik(self):
        recent = {"accessionNumber": ["acc-0"], "form": ["10-K"], "filingDate": ["2024-01-01"]}
        rows = _column_to_rows(recent, cik="0000320193", limit=10)
        assert rows[0]["_cik"] == "0000320193"

    def test_column_to_rows_empty(self):
        assert _column_to_rows({}, cik="0000320193", limit=20) == []
