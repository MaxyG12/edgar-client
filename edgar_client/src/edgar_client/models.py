"""
Data model objects for edgar_client.

Two completely separate layers share this file:

  ┌─ HTTP layer ────────────────────────────────────────────────────────────────┐
  │  EdgarRequest          user-facing, mutable request object                  │
  │  PreparedEdgarRequest  immutable, wire-ready request (headers encoded, etc) │
  │  Response              eagerly-loaded HTTP response wrapper                  │
  └─────────────────────────────────────────────────────────────────────────────┘
  ┌─ Domain layer ──────────────────────────────────────────────────────────────┐
  │  These classes represent what the API *returns*, not how we fetch it.       │
  │  Each has a from_dict(data) classmethod that accepts raw EDGAR JSON and     │
  │  converts it into a clean Python object.                                    │
  │                                                                             │
  │  FinancialValue    one reported data point for an XBRL concept              │
  │  FinancialSeries   all values for one concept (e.g. all annual Revenues)    │
  │  Filing            one SEC filing (10-K, 10-Q, 8-K, …)                     │
  │  Company           company profile + recent filings                          │
  │  CompanyFacts      full XBRL fact tree for a company                        │
  │  SearchResult      one hit from the EDGAR full-text search API              │
  └─────────────────────────────────────────────────────────────────────────────┘

EDGAR JSON quirks handled here
-------------------------------
• filings.recent is column-oriented: each field is a list of equal length.
  We convert this to row-dicts before handing them to Filing.from_dict().
• CIK appears as both integer (in the JSON body) and padded-string (in URLs).
  We always store the 10-digit zero-padded form.
• Financial values use Python Decimal, not float, to avoid precision loss on
  values > 2^53 (Apple's quarterly revenue routinely exceeds 2^48).
• The filing archive URL is deterministic: assembled from CIK, accession
  number (dashes removed), and the primary document filename.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from .exceptions import DataParseError, EdgarHTTPError
from .structures import CaseInsensitiveDict


# =============================================================================
# Shared helpers
# =============================================================================


def _pad_cik(cik: int | str) -> str:
    """Return *cik* as a 10-digit zero-padded string (SEC canonical form).

    The API returns CIK as an integer; URLs need the padded string form.
    Storing always-padded on every object avoids repeated formatting downstream.
    """
    return str(int(cik)).zfill(10)


def _parse_date(value: str | None) -> datetime.date | None:
    """Parse an ISO-8601 date string; return None on any failure.

    We use None rather than raising because optional date fields are common in
    EDGAR data (e.g. instant-point facts have no start date, some filings lack
    a reportDate).
    """
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _build_filing_url(cik: str, accession: str, primary_doc: str | None) -> str:
    """Construct the SEC Archives URL for a filing's primary document.

    URL pattern:
        https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{doc}

    Where:
        cik_int        — CIK without leading zeros (e.g. "320193" not "0000320193")
        accession_nodash — accession number with dashes stripped
                           "0000320193-24-000001" → "000032019324000001"

    Returns an empty string when any required component is missing.
    """
    if not (cik and accession and primary_doc):
        return ""
    cik_int = str(int(cik))                    # strip leading zeros
    accession_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data"
        f"/{cik_int}/{accession_nodash}/{primary_doc}"
    )


def _column_to_rows(
    recent: dict[str, list[Any]],
    cik: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Convert the column-oriented filings.recent dict to a list of row dicts.

    The EDGAR submissions endpoint stores filings as parallel arrays for
    compactness (one field name per column instead of once per row × column).
    We normalise this to the row-dict format that Filing.from_dict() expects.

    Only the first *limit* rows are returned — the most recent filings come
    first in the SEC's array, so we truncate rather than sort.
    """
    n = len(recent.get("accessionNumber", []))
    rows: list[dict[str, Any]] = []

    def _col(key: str, default: Any = None) -> Any:
        """Safely read column *key* at row *i*.

        Uses a closure over *i* (the loop variable below).  Returns *default*
        when the column is absent OR shorter than expected — both are valid in
        the wild because:
          - Optional columns (reportDate, size) may be omitted entirely.
          - Some companies have inconsistent column lengths in older submissions.
        """
        col = recent.get(key, [])
        return col[i] if i < len(col) else default

    for i in range(min(n, limit)):
        rows.append({
            "accessionNumber":       _col("accessionNumber", ""),
            "filingDate":            _col("filingDate"),
            "reportDate":            _col("reportDate"),
            "form":                  _col("form", ""),
            "primaryDocument":       _col("primaryDocument"),
            "primaryDocDescription": _col("primaryDocDescription"),
            "size":                  _col("size"),
            "isXBRL":                _col("isXBRL", 0),
            "_cik":                  cik,    # passed through for URL construction
        })
    return rows


# =============================================================================
# HTTP layer  (unchanged from previous sprint)
# =============================================================================


class EdgarRequest:
    """User-facing, mutable request object.

    Mirrors requests.Request — the object a caller constructs to express
    intent.  Not sent directly; must be prepared via EdgarSession.prepare_request().
    """

    def __init__(
        self,
        method: str | None = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.headers: dict[str, str] = headers or {}
        self.params = params

    def prepare(self) -> "PreparedEdgarRequest":
        """Produce a PreparedEdgarRequest without session-level merging."""
        p = PreparedEdgarRequest()
        p.prepare(method=self.method, url=self.url, headers=self.headers, params=self.params)
        return p

    def __repr__(self) -> str:
        return f"<EdgarRequest [{self.method}]>"


class PreparedEdgarRequest:
    """Immutable, wire-ready request.

    Mirrors requests.PreparedRequest.  Produced by EdgarSession.prepare_request()
    after merging session defaults with per-call headers and percent-encoding the
    URL.  This is the object handed to EdgarHTTPAdapter.send().
    """

    def __init__(self) -> None:
        self.method: str | None = None
        self.url: str | None = None
        self.headers: CaseInsensitiveDict[str] = CaseInsensitiveDict()
        self.body: bytes | None = None

    def prepare(
        self,
        method: str | None = None,
        url: str | None = None,
        headers: dict[str, str] | CaseInsensitiveDict[str] | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        """Run all prepare_* steps in the required order."""
        self.prepare_method(method)
        self.prepare_url(url, params)
        self.prepare_headers(headers)

    def prepare_method(self, method: str | None) -> None:
        self.method = method.upper() if method else method

    def prepare_url(self, url: str | None, params: dict[str, str] | None) -> None:
        if not url:
            raise ValueError("A URL is required.")
        parsed = urlparse(url)
        if not parsed.scheme:
            raise ValueError(f"Invalid URL {url!r}: no scheme supplied.")
        if not parsed.netloc:
            raise ValueError(f"Invalid URL {url!r}: no host supplied.")
        if params:
            parsed = parsed._replace(query=urlencode(params, doseq=True))
        self.url = urlunparse(parsed)

    def prepare_headers(
        self,
        headers: dict[str, str] | CaseInsensitiveDict[str] | None,
    ) -> None:
        self.headers = CaseInsensitiveDict(headers) if headers else CaseInsensitiveDict()

    def copy(self) -> "PreparedEdgarRequest":
        p = PreparedEdgarRequest()
        p.method = self.method
        p.url = self.url
        p.headers = self.headers.copy()
        p.body = self.body
        return p

    def __repr__(self) -> str:
        return f"<PreparedEdgarRequest [{self.method}]>"


class Response:
    """Eagerly-loaded HTTP response.

    Why eager loading?  EDGAR responses are pure JSON — there is no benefit to
    streaming.  Reading the body immediately lets us close the connection at
    the adapter boundary, making this object fully self-contained and safe to
    pass across thread boundaries.

    See adapters.py for the building logic.
    """

    def __init__(
        self,
        *,
        status_code: int,
        body: bytes,
        url: str,
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
        request: PreparedEdgarRequest | None = None,
    ) -> None:
        self.status_code: int = status_code
        self._body: bytes = body
        self.url: str = url
        self.headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(headers or {})
        self.encoding: str = encoding
        self.request: PreparedEdgarRequest | None = request

    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode(self.encoding, errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self, **kwargs: Any) -> Any:
        try:
            return json.loads(self._body, **kwargs)
        except json.JSONDecodeError as exc:
            raise DataParseError(
                f"JSON decode failed for {self.url!r}: {exc}",
                raw_content=self._body,
                response=self,
            ) from exc

    def raise_for_status(self) -> None:
        """Backward-compat shim; the adapter raises typed exceptions before returning."""
        if not self.ok:
            raise EdgarHTTPError(
                f"HTTP {self.status_code} for {self.url!r}",
                response=self,
            )

    def __repr__(self) -> str:
        return f"<Response [{self.status_code}]>"


# Backward-compat alias
EdgarResponse = Response


# =============================================================================
# Domain layer
# =============================================================================


class FinancialValue:
    """One reported data point for an XBRL financial concept.

    Maps to a single entry in the ``units[unit]`` array inside the
    companyfacts JSON, e.g.::

        {
          "start": "2022-10-01",
          "end":   "2023-09-30",
          "val":   383285000000,
          "accn":  "0000320193-23-000077",
          "form":  "10-K",
          "filed": "2023-11-03",
          "frame": "CY2023"
        }

    The ``unit`` attribute is injected by the parent FinancialSeries because
    it lives one level up (as the dict key in ``"units": {"USD": [...]}``).

    Attributes
    ----------
    value       Exact decimal representation of the reported number.
                Using Decimal rather than float because SEC values routinely
                exceed 2^53 (Apple FY2023 revenue: 383 billion) and float
                arithmetic would lose the last few digits.
    unit        Reporting unit, e.g. "USD" or "shares".
    start       Period start date for duration facts (None for instant facts).
    end         Period end date.  This is the primary sort key — "as of" date.
    form        Filing form that contained this value, e.g. "10-K" or "10-Q".
    frame       XBRL frame identifier, e.g. "CY2023" or "CY2023Q3I" (None
                when the SEC hasn't assigned one, which is common for
                non-standard fiscal years).
    """

    def __init__(self) -> None:
        self.value: Decimal = Decimal(0)
        self.unit: str = ""
        self.start: datetime.date | None = None
        self.end: datetime.date = datetime.date.min
        self.form: str = ""
        self.frame: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, unit: str = "") -> "FinancialValue":
        """Build from one entry in a ``units[unit]`` array.

        The *unit* keyword argument must be supplied by the parent
        FinancialSeries — it is not present in the per-value dict.

        Why ``str(raw_val)`` before passing to Decimal?  ``Decimal(1.5)``
        gives ``Decimal('1.4999999…')`` (float imprecision).
        ``Decimal("1.5")`` gives ``Decimal('1.5')``.  Converting via str
        handles both int and float values correctly.
        """
        fv = cls()
        raw_val = data.get("val")
        fv.value = Decimal(str(raw_val)) if raw_val is not None else Decimal(0)
        fv.unit = unit
        fv.start = _parse_date(data.get("start"))
        fv.end = _parse_date(data.get("end")) or datetime.date.min
        fv.form = data.get("form", "")
        fv.frame = data.get("frame")
        return fv

    def __repr__(self) -> str:
        period = f"{self.start} → {self.end}" if self.start else str(self.end)
        return f"<FinancialValue {self.value:,} {self.unit} [{self.form}] {period}>"


# Backward-compat alias — old name still importable
FactValue = FinancialValue


class FinancialSeries:
    """All reported values for one XBRL concept across all periods.

    Example: Apple's annual net revenues from fiscal year 2008 through present.

    Built from one entry in the ``facts`` section of the companyfacts JSON::

        {
          "label": "Revenues",
          "description": "Amount of revenue …",
          "units": {
            "USD": [
              {"start": "2022-10-01", "end": "2023-09-30", "val": 383285000000,
               "accn": "…", "form": "10-K", "filed": "2023-11-03"},
              …
            ]
          }
        }

    Attributes
    ----------
    concept_name    XBRL concept identifier, e.g. "Revenues".
    unit            Primary reporting unit, e.g. "USD" or "shares".
    description     Plain-English concept description.
    values          All reported values, sorted newest-first by end date.
    """

    def __init__(self) -> None:
        self.concept_name: str = ""
        self.unit: str = ""
        self.description: str = ""
        self.values: list[FinancialValue] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def annual_values(self) -> list[FinancialValue]:
        """10-K values only, preserving newest-first ordering.

        Why a property and not a method?  It's a pure read-only filter with
        no parameters — accessing it should feel like reading a field, not
        calling a computation.
        """
        return [fv for fv in self.values if fv.form == "10-K"]

    @property
    def latest_value(self) -> Decimal | None:
        """The most recent 10-K value, or None when no annual data exists.

        Uses period end date as the "most recent" criterion, not the filed date.
        A company can file a 10-K amendment (10-K/A) months after the original;
        both have the same end date but different filed dates.  Picking by end
        date gives the value for the most recent *fiscal year*, regardless of
        how many amendments followed.

        ``values`` is already sorted newest-first (see from_dict), so we read
        the first element of annual_values rather than searching.
        """
        av = self.annual_values
        return av[0].value if av else None

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[int, Decimal]:
        """Return ``{fiscal_year: value}`` for all annual (10-K) data points.

        The year key is ``end.year`` — i.e. the calendar year in which the
        fiscal year ended.  For Apple (fiscal year ending September 30), FY2023
        ends 2023-09-30, so its key is 2023.

        Why a method and not a property?  It allocates a new dict on every call.
        Properties should feel cheap; ``as_dict()`` makes the allocation explicit.

        Duplicate-year handling: amended 10-K filings can produce two entries
        with the same end-year.  We keep the one with the latest end date (most
        complete data).  The returned dict is sorted ascending by year.
        """
        # best[year] = (end_date, value) — we keep the entry with the latest end
        best: dict[int, tuple[datetime.date, Decimal]] = {}
        for fv in self.annual_values:
            year = fv.end.year
            if year not in best or fv.end > best[year][0]:
                best[year] = (fv.end, fv.value)
        return {year: val for year, (_, val) in sorted(best.items())}

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        concept_name: str = "",
    ) -> "FinancialSeries":
        """Build from one entry in the ``facts[taxonomy]`` dict.

        *concept_name* is the dict key one level up (e.g. "Revenues").  It is
        not present inside *data* itself, so it must be passed by the caller.
        CompanyFacts.from_dict() supplies it automatically.
        """
        fs = cls()
        fs.concept_name = concept_name
        fs.description = data.get("description", "")

        units: dict[str, list[dict[str, Any]]] = data.get("units", {})
        if units:
            # Take the first unit key. Almost all concepts have exactly one unit
            # (USD for monetary, "shares" for share counts). Multi-unit concepts
            # (rare) use only the first.
            fs.unit = next(iter(units))
            fs.values = sorted(
                [FinancialValue.from_dict(v, unit=fs.unit) for v in units[fs.unit]],
                key=lambda fv: fv.end,
                reverse=True,   # newest-first so latest_value is values[0]
            )
        return fs

    def __repr__(self) -> str:
        n_annual = len(self.annual_values)
        return (
            f"<FinancialSeries {self.concept_name!r} "
            f"unit={self.unit!r} "
            f"values={len(self.values)} annual={n_annual}>"
        )


class Filing:
    """One SEC filing — a 10-K, 10-Q, 8-K, or any other form type.

    Built from a row extracted from the ``filings.recent`` column-arrays in
    the submissions endpoint response.  See ``_column_to_rows()`` for the
    conversion logic.

    Attributes
    ----------
    accession_number    Unique filing identifier, e.g. "0000320193-24-000001".
    form_type           SEC form type, e.g. "10-K", "10-Q", "8-K".
    filing_date         Date the filing was submitted to the SEC.
    primary_document    Filename of the primary document, e.g. "aapl-20240928.htm".
    file_url            Full URL to the primary document in the SEC archives.
                        Empty string when primary_document is absent.
    description         Human-readable description, e.g. "FORM 10-K".
    report_date         Period-of-report end date (may differ from filing_date).
    is_xbrl             True when the filing contains inline XBRL data.
    size                Total filing size in bytes (None if unavailable).

    Why ``file_url`` on the model (not a method)?
    Because it's a deterministic derivation of known fields with zero I/O.
    Callers can use it immediately without any extra calls.
    """

    def __init__(self) -> None:
        self.accession_number: str = ""
        self.form_type: str = ""
        self.filing_date: datetime.date | None = None
        self.primary_document: str | None = None
        self.file_url: str = ""
        self.description: str | None = None
        # Extra fields not in the minimal spec but commonly needed
        self.report_date: datetime.date | None = None
        self.is_xbrl: bool = False
        self.size: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, cik: str = "") -> "Filing":
        """Build from a row-dict extracted from the submissions JSON.

        The *data* dict must have the shape::

            {
              "accessionNumber":       "0000320193-24-000001",
              "filingDate":            "2024-11-01",
              "reportDate":            "2024-09-28",
              "form":                  "10-K",
              "primaryDocument":       "aapl-20240928.htm",
              "primaryDocDescription": "FORM 10-K",
              "size":                  12000000,
              "isXBRL":                1,
            }

        The *cik* keyword is required for ``file_url`` construction.  It is
        supplied automatically by ``Company.from_dict()``.

        The ``_cik`` key in *data* (injected by ``_column_to_rows``) is also
        accepted so that the caller doesn't need to supply it separately when
        the row already carries it.
        """
        f = cls()
        f.accession_number = data.get("accessionNumber", "") or ""
        f.form_type = data.get("form", "") or ""
        f.filing_date = _parse_date(data.get("filingDate"))
        f.report_date = _parse_date(data.get("reportDate"))
        f.primary_document = data.get("primaryDocument") or None
        f.description = data.get("primaryDocDescription") or None
        raw_size = data.get("size")
        f.size = int(raw_size) if raw_size is not None else None
        f.is_xbrl = bool(data.get("isXBRL", 0))

        # Prefer explicitly-passed cik, fall back to the _cik sentinel
        # injected by _column_to_rows, then give up gracefully.
        effective_cik = cik or data.get("_cik", "")
        f.file_url = _build_filing_url(effective_cik, f.accession_number, f.primary_document)

        return f

    def __repr__(self) -> str:
        return (
            f"<Filing [{self.form_type}] "
            f"{self.filing_date} "
            f"{self.accession_number}>"
        )


class Company:
    """A company's profile and recent filing history.

    Built from ``GET /submissions/CIK{padded}.json``.

    The submissions endpoint returns up to 1 000 recent filings in a
    column-oriented structure.  We normalise to a list of Filing objects
    and cap at 20 — enough for most analytical use cases.  Callers needing
    deeper history can paginate using the ``filings.files`` array (not yet
    implemented in this client).

    Attributes
    ----------
    name                    Company legal name, e.g. "Apple Inc."
    cik                     10-digit zero-padded CIK, e.g. "0000320193".
    sic_code                SIC industry code as string, e.g. "7372".
    sic_description         Human-readable SIC label, e.g. "Prepackaged Software".
    state_of_incorporation  Two-letter state code, e.g. "CA".
    fiscal_year_end         Month-day of fiscal year end, e.g. "0930" (Sep 30).
    filings                 Most recent 20 filings, newest first.
    ticker                  Primary ticker symbol (extra, not in minimal spec).
    ein                     Employer Identification Number (extra).
    entity_type             SEC entity classification (extra).
    """

    #: Maximum filings returned from the submissions endpoint's recent array.
    MAX_FILINGS = 20

    def __init__(self) -> None:
        self.name: str = ""
        self.cik: str = ""
        self.sic_code: str | None = None
        self.sic_description: str | None = None
        self.state_of_incorporation: str | None = None
        self.fiscal_year_end: str | None = None
        self.filings: list[Filing] = []
        # Extra fields: not in the user's minimal spec, but high-value
        self.ticker: str | None = None
        self.ein: str | None = None
        self.entity_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Company":
        """Build from the raw submissions JSON dict.

        This is the primary constructor.  from_response() delegates here.

        Why a classmethod rather than __init__?  The JSON structure does not
        map 1:1 to our attributes — CIK needs padding, filings need a
        column→row conversion, the SIC code comes as an int but we store a
        string.  Encoding those transformations in a named constructor keeps
        __init__ clean and makes the parsing boundary explicit.

        Raises DataParseError on structurally invalid JSON (missing "cik", etc.).
        """
        try:
            company = cls()
            company.cik = _pad_cik(data["cik"])
            company.name = data.get("name", "")

            tickers: list[str] = data.get("tickers", [])
            company.ticker = tickers[0] if tickers else None

            # SIC is an integer in the JSON, but we expose it as a string to
            # avoid callers having to know the type.
            sic_raw = data.get("sic")
            company.sic_code = str(sic_raw) if sic_raw is not None else None
            company.sic_description = data.get("sicDescription")

            company.ein = data.get("ein")
            company.entity_type = data.get("entityType")
            company.state_of_incorporation = data.get("stateOfIncorporation")
            company.fiscal_year_end = data.get("fiscalYearEnd")

            # The submissions JSON nests filings.recent as column-oriented arrays.
            # _column_to_rows converts them to row dicts and caps at MAX_FILINGS.
            recent = data.get("filings", {}).get("recent", {})
            rows = _column_to_rows(recent, company.cik, limit=cls.MAX_FILINGS)
            company.filings = [Filing.from_dict(row) for row in rows]

            return company

        except (KeyError, ValueError, TypeError) as exc:
            raise DataParseError(
                f"Unexpected structure in submissions JSON: {exc}",
            ) from exc

    @classmethod
    def from_response(cls, response: "Response") -> "Company":
        """Build from an HTTP Response (backward-compat entry point).

        Delegates to from_dict() so all parsing logic lives in one place.
        Wraps DataParseError to attach the response for debugging.
        """
        try:
            data: dict[str, Any] = response.json()
        except DataParseError:
            raise
        try:
            return cls.from_dict(data)
        except DataParseError as exc:
            # Re-raise with the response attached so callers can inspect the body.
            raise DataParseError(
                str(exc),
                raw_content=response.content,
                response=response,
            ) from exc.__cause__

    def __repr__(self) -> str:
        ticker_part = f" ({self.ticker})" if self.ticker else ""
        return f"<Company {self.name!r}{ticker_part} cik={self.cik}>"


class CompanyFacts:
    """Complete XBRL financial fact tree for a company.

    Built from ``GET /api/xbrl/companyfacts/CIK{padded}.json``.  The JSON
    payload can exceed 10 MB for companies with long filing histories.

    The ``facts`` attribute is a two-level dict::

        facts["us-gaap"]["Revenues"] → FinancialSeries(...)
        facts["dei"]["EntityCommonStockSharesOutstanding"] → FinancialSeries(...)

    Attributes
    ----------
    cik             10-digit zero-padded CIK.
    entity_name     Company name from the XBRL filing metadata.
    facts           Taxonomy → concept → FinancialSeries mapping.
    """

    def __init__(self) -> None:
        self.cik: str = ""
        self.entity_name: str = ""
        self.facts: dict[str, dict[str, FinancialSeries]] = {}

    def get(self, taxonomy: str, concept: str) -> FinancialSeries:
        """Return the FinancialSeries for ``taxonomy / concept``.

        Example::

            revenue = facts.get("us-gaap", "Revenues")
            print(revenue.latest_value)

        Raises KeyError when the taxonomy or concept is absent — use
        ``taxonomies()`` and ``concepts(taxonomy)`` to discover what's
        available before calling ``get()``.
        """
        return self.facts[taxonomy][concept]

    def taxonomies(self) -> list[str]:
        """Available taxonomy strings, e.g. ``["us-gaap", "dei"]``."""
        return list(self.facts.keys())

    def concepts(self, taxonomy: str) -> list[str]:
        """All concept names within *taxonomy*.  Empty list if not found."""
        return list(self.facts.get(taxonomy, {}).keys())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanyFacts":
        """Build from the raw companyfacts JSON dict.

        Each concept entry is passed to FinancialSeries.from_dict() with
        the concept name supplied as a keyword argument — the concept key
        in the JSON dict is the canonical XBRL concept name.
        """
        try:
            cf = cls()
            cf.cik = _pad_cik(data["cik"])
            cf.entity_name = data.get("entityName", "")

            raw_facts: dict[str, dict[str, Any]] = data.get("facts", {})
            for taxonomy, concepts in raw_facts.items():
                cf.facts[taxonomy] = {
                    concept: FinancialSeries.from_dict(concept_data, concept_name=concept)
                    for concept, concept_data in concepts.items()
                }
            return cf

        except (KeyError, ValueError, TypeError) as exc:
            raise DataParseError(
                f"Unexpected structure in companyfacts JSON: {exc}",
            ) from exc

    @classmethod
    def from_response(cls, response: "Response") -> "CompanyFacts":
        """Build from an HTTP Response (backward-compat entry point)."""
        try:
            data: dict[str, Any] = response.json()
        except DataParseError:
            raise
        try:
            return cls.from_dict(data)
        except DataParseError as exc:
            raise DataParseError(
                str(exc),
                raw_content=response.content,
                response=response,
            ) from exc.__cause__

    def __repr__(self) -> str:
        n_taxonomies = len(self.facts)
        n_concepts = sum(len(v) for v in self.facts.values())
        return (
            f"<CompanyFacts {self.entity_name!r} cik={self.cik} "
            f"taxonomies={n_taxonomies} concepts={n_concepts}>"
        )


class SearchResult:
    """One hit from the EDGAR EFTS full-text search endpoint."""

    def __init__(self) -> None:
        self.cik: str = ""
        self.name: str = ""
        self.ticker: str | None = None
        self.form: str | None = None
        self.filed_at: datetime.date | None = None
        self.description: str | None = None

    @classmethod
    def from_hit(cls, hit: dict[str, Any]) -> "SearchResult":
        """Build from one EFTS full-text-search hit dict."""
        source: dict[str, Any] = hit.get("_source", {})
        r = cls()
        ciks: list[str] = source.get("ciks", [])
        r.cik = _pad_cik(ciks[0]) if ciks else ""
        names: list[str] = source.get("display_names", [])
        r.name = names[0] if names else source.get("entity_name", "")
        tickers: list[str] = source.get("tickers", [])
        r.ticker = tickers[0] if tickers else None
        r.form = source.get("form_type")
        r.filed_at = _parse_date(source.get("file_date"))
        r.description = source.get("period_of_report")
        return r

    @classmethod
    def from_ticker_map_entry(cls, entry: dict[str, str]) -> "SearchResult":
        """Build from a TickerCache entry dict.

        The entry format is::

            {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}

        The fields ``form``, ``filed_at``, and ``description`` are not present
        in the ticker map, so they are left as None.  This is intentional —
        the ticker map is a CIK directory, not a filing index.
        """
        r = cls()
        r.cik = entry.get("cik", "")
        r.name = entry.get("title", "")
        r.ticker = entry.get("ticker") or None
        # Ticker map has no filing-level data
        r.form = None
        r.filed_at = None
        r.description = None
        return r

    def __repr__(self) -> str:
        return f"<SearchResult [{self.ticker or self.cik}] {self.name!r}>"
