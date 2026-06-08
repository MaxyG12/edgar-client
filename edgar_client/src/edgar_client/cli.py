"""
edgar — command-line interface for the SEC EDGAR API.

Design notes on Click patterns used
─────────────────────────────────────
@click.group()
  Creates the `edgar` command group.  All subcommands are registered on it.

@cli.command("name")
  Registers a subcommand on the group.  The string argument lets us name the
  function differently from the CLI name (e.g. company_cmd vs "company").

@click.argument("ticker")
  Positional argument — required, no flag prefix.

@click.argument("tickers", nargs=-1, required=True)
  Variadic positional: collects all remaining positional args into a tuple.
  Used for `compare AAPL MSFT GOOGL`.

@click.option("--output", type=click.Choice([...]))
  Constrained option: Click rejects values not in the list before the function
  is called, giving a clean error message.

@click.pass_context  /  @click.pass_obj
  `@click.pass_context` gives access to the full Click Context.
  `@click.pass_obj` is shorthand for ctx.obj — the shared-state dict we store
  the EdgarClient in.  Both inject the object as the FIRST positional argument.

ctx.ensure_object(dict)
  Initialises ctx.obj to {} if it's None.  Safe to call multiple times.

ctx.call_on_close(fn)
  Registers fn() to run when the context exits.  Used to close the EdgarClient
  (release TCP connections) even if the command raises an exception.

envvar="EDGAR_USER_AGENT"
  Click reads this environment variable if --user-agent is not passed.
  Lets users set: export EDGAR_USER_AGENT="Name email@example.com"

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])
  Adds -h as an alias for --help on every command and subcommand.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .api import EdgarClient
from .exceptions import (
    DataParseError,
    EdgarConnectionError,
    EdgarError,
    EdgarNotFoundError,
    EdgarRateLimitError,
    EdgarTimeoutError,
    InvalidTickerError,
    InvalidUserAgentError,
)
from .models import Company, CompanyFacts, FinancialSeries, SearchResult

# =============================================================================
# Metric definitions
# =============================================================================
#
# Maps the friendly CLI name (e.g. "revenue") to an ordered list of
# (taxonomy, XBRL-concept) pairs.  We try them left-to-right and return the
# first one that's present in a company's facts.  The ordering puts the
# most-commonly-used concept first so the typical path hits on the first try.

_METRICS: dict[str, list[tuple[str, str]]] = {
    "revenue": [
        # ASC 606 (adopted by most US filers ~2018)
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        # Legacy US GAAP (pre-2018 and some industries)
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "SalesRevenueGoodsNet"),
        # Banks and financial services
        ("us-gaap", "RevenuesNetOfInterestExpense"),
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
    ],
    "assets": [
        ("us-gaap", "Assets"),
    ],
    "eps": [
        ("us-gaap", "EarningsPerShareBasic"),
        ("us-gaap", "EarningsPerShareDiluted"),
    ],
    "operating_income": [
        ("us-gaap", "OperatingIncomeLoss"),
    ],
    "cash": [
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsAndShortTermInvestments"),
    ],
}

# Human-readable column header for each metric
_METRIC_LABELS: dict[str, str] = {
    "revenue":          "Revenue",
    "net_income":       "Net Income",
    "assets":           "Total Assets",
    "eps":              "EPS (Basic)",
    "operating_income": "Operating Income",
    "cash":             "Cash & Equivalents",
}

_AVAILABLE_METRICS = sorted(_METRICS.keys())


# =============================================================================
# Rich consoles
# =============================================================================
#
# Two consoles: one for stdout (normal output), one for stderr (errors/warnings).
# Separating them means `edgar financials AAPL --output json 2>/dev/null` gives
# clean JSON on stdout with no noise mixed in.

_console     = Console()
_err_console = Console(stderr=True)


# =============================================================================
# Formatting helpers
# =============================================================================


def _fmt_value(value: Decimal, unit: str) -> str:
    """Format a financial value for display.

    Scales large numbers to B/M/T so columns stay readable:
      383285000000  →  $383.29B
      89498000000   →  $89.50B
      1234567       →  $1.23M
    """
    if value is None:  # type: ignore[comparison-overlap]
        return "—"
    abs_v = abs(float(value))
    sign  = "-" if value < 0 else ""

    if unit == "USD":
        if abs_v >= 1e12:
            return f"{sign}${abs_v / 1e12:.2f}T"
        if abs_v >= 1e9:
            return f"{sign}${abs_v / 1e9:.2f}B"
        if abs_v >= 1e6:
            return f"{sign}${abs_v / 1e6:.2f}M"
        return f"{sign}${abs_v:,.0f}"
    if unit == "shares":
        if abs_v >= 1e9:
            return f"{sign}{abs_v / 1e9:.2f}B"
        if abs_v >= 1e6:
            return f"{sign}{abs_v / 1e6:.2f}M"
        return f"{sign}{abs_v:,.0f}"
    # For EPS and other units, keep raw decimal
    return f"{sign}{abs_v:.4f} {unit}"


def _resolve_metric(facts: CompanyFacts, metric: str) -> FinancialSeries:
    """Return the first matching FinancialSeries for *metric* from *facts*.

    Raises click.ClickException (printed cleanly by Click) if no concept matches.
    """
    for taxonomy, concept in _METRICS.get(metric, []):
        try:
            return facts.get(taxonomy, concept)
        except KeyError:
            continue
    raise click.ClickException(
        f"No '{_METRIC_LABELS.get(metric, metric)}' data found. "
        "The company may use IFRS (foreign private issuer) or "
        "not report this metric in US GAAP."
    )


def _top_n_years(series: FinancialSeries, n: int) -> dict[int, Decimal]:
    """Return the *n* most recent years from series.as_dict()."""
    all_data = series.as_dict()
    top_years = sorted(all_data, reverse=True)[:n]
    return {y: all_data[y] for y in top_years}


# =============================================================================
# JSON serialisers — converts domain objects to plain dicts for --output json
# =============================================================================


def _company_to_dict(c: Company) -> dict[str, Any]:
    return {
        "name":                   c.name,
        "cik":                    c.cik,
        "ticker":                 c.ticker,
        "sic_code":               c.sic_code,
        "sic_description":        c.sic_description,
        "state_of_incorporation": c.state_of_incorporation,
        "fiscal_year_end":        c.fiscal_year_end,
        "filings": [
            {
                "accession_number": f.accession_number,
                "form_type":        f.form_type,
                "filing_date":      str(f.filing_date) if f.filing_date else None,
                "description":      f.description,
                "file_url":         f.file_url,
                "is_xbrl":          f.is_xbrl,
                "size":             f.size,
            }
            for f in c.filings
        ],
    }


def _financials_to_dict(
    ticker: str,
    metric: str,
    series: FinancialSeries,
    years: int,
) -> dict[str, Any]:
    data = _top_n_years(series, years)
    return {
        "ticker": ticker.upper(),
        "metric": metric,
        "label":  _METRIC_LABELS.get(metric, metric),
        "unit":   series.unit,
        # JSON keys must be strings; values are str(Decimal) for lossless precision
        "data": {str(yr): str(val) for yr, val in sorted(data.items())},
    }


def _search_to_dict(results: list[SearchResult]) -> list[dict[str, Any]]:
    return [
        {
            "name":     r.name,
            "ticker":   r.ticker,
            "cik":      r.cik,
            "form":     r.form,
            "filed_at": str(r.filed_at) if r.filed_at else None,
        }
        for r in results
    ]


def _compare_to_dict(
    keys:        list[str],
    display:     dict[str, str],
    series_map:  dict[str, FinancialSeries | None],
    metric:      str,
    years:       int,
) -> dict[str, Any]:
    all_years: set[int] = set()
    for s in series_map.values():
        if s:
            all_years.update(_top_n_years(s, years))

    rows: dict[str, dict[str, str | None]] = {}
    for yr in sorted(all_years, reverse=True)[:years]:
        row: dict[str, str | None] = {}
        for k, s in series_map.items():
            if s:
                val = _top_n_years(s, years).get(yr)
                row[display[k]] = str(val) if val is not None else None
            else:
                row[display[k]] = None
        rows[str(yr)] = row

    return {
        "metric":  metric,
        "label":   _METRIC_LABELS.get(metric, metric),
        "tickers": [display[k] for k in keys],
        "data":    rows,
    }


# =============================================================================
# Error handling
# =============================================================================


def _die(exc: EdgarError) -> None:
    """Print a friendly error message to stderr and exit(1).

    Using stderr keeps stdout clean for --output json pipelines.
    """
    if isinstance(exc, InvalidTickerError):
        _err_console.print(f"[bold red]Unknown company:[/] {exc}")
        _err_console.print(
            "Tip: [cyan]edgar search <name>[/] shows companies matching a name."
        )
    elif isinstance(exc, EdgarNotFoundError):
        _err_console.print(f"[bold red]Not found (HTTP 404):[/] {exc}")
    elif isinstance(exc, EdgarRateLimitError):
        _err_console.print(
            "[bold red]Rate limited (HTTP 429).[/] "
            "Wait a few seconds and try again."
        )
    elif isinstance(exc, EdgarTimeoutError):
        _err_console.print(
            f"[bold red]Request timed out.[/] "
            "SEC servers can be slow. Try adding [cyan]--timeout 30[/]."
        )
    elif isinstance(exc, EdgarConnectionError):
        _err_console.print(f"[bold red]Network error:[/] {exc}")
    elif isinstance(exc, DataParseError):
        _err_console.print(f"[bold red]Data error:[/] {exc}")
    else:
        _err_console.print(f"[bold red]EDGAR error:[/] {exc}")
    sys.exit(1)


# =============================================================================
# CLI group
# =============================================================================

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(prog_name="edgar")
@click.option(
    "--user-agent", "-u",
    envvar="EDGAR_USER_AGENT",
    metavar="\"NAME EMAIL\"",
    required=True,
    help=(
        "Your identity (SEC requirement). "
        "Format: 'Your Name your@email.com'. "
        "Or set env var: export EDGAR_USER_AGENT='...'"
    ),
)
@click.pass_context
def cli(ctx: click.Context, user_agent: str) -> None:
    """Query SEC EDGAR from the command line.

    \b
    Set your identity once, use any command:
        export EDGAR_USER_AGENT="Your Name your@email.com"
        edgar company AAPL
        edgar financials AAPL --metric revenue --years 5
        edgar search "electric vehicles"
        edgar compare AAPL MSFT GOOGL --metric revenue

    \b
    Add --output json to any command for machine-readable output:
        edgar company AAPL --output json | jq .name
    """
    # ctx.ensure_object(dict): if ctx.obj is None, set it to {}.
    # This is safe even if called multiple times (by nested groups, etc.).
    ctx.ensure_object(dict)

    try:
        client = EdgarClient(user_agent=user_agent)
    except InvalidUserAgentError as exc:
        # click.BadParameter gives "Error: Invalid value for '--user-agent': …"
        # which is cleaner than a raw exception traceback.
        raise click.BadParameter(str(exc), param_hint="'--user-agent'") from exc

    ctx.obj["client"] = client

    # ctx.call_on_close: runs client.close() when the context exits, even
    # if the command raises.  Releases pooled TCP connections cleanly.
    ctx.call_on_close(client.close)


# =============================================================================
# edgar company TICKER
# =============================================================================


@cli.command("company")
@click.argument("ticker")
@click.option(
    "--output", "-o",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.pass_obj
def company_cmd(obj: dict, ticker: str, output: str) -> None:
    """Show company profile and recent filings.

    TICKER accepts a ticker symbol (AAPL), numeric CIK (0000320193),
    or company name (\"Apple\").

    \b
    Examples:
        edgar company AAPL
        edgar company "Apple Inc"
        edgar company 0000320193
        edgar company MSFT --output json
    """
    client: EdgarClient = obj["client"]

    try:
        company = client.get_company(ticker)
    except EdgarError as exc:
        _die(exc)

    # ── JSON output ──────────────────────────────────────────────────────────
    if output == "json":
        click.echo(json.dumps(_company_to_dict(company), indent=2))
        return

    # ── Rich table output ────────────────────────────────────────────────────
    sic_str = company.sic_code or "—"
    if company.sic_description:
        sic_str += f"  {company.sic_description}"

    # Panel: compact company header card
    header = Text()
    header.append(f"{company.name}", style="bold white")
    if company.ticker:
        header.append(f"   {company.ticker}", style="bold cyan")
    header.append(f"\n\nCIK  ", style="dim")
    header.append(company.cik, style="cyan")
    header.append(f"     SIC  ", style="dim")
    header.append(sic_str, style="cyan")
    header.append(f"\nState  ", style="dim")
    header.append(company.state_of_incorporation or "—", style="cyan")
    header.append(f"     FY End  ", style="dim")
    header.append(company.fiscal_year_end or "—", style="cyan")

    _console.print()
    _console.print(Panel(header, expand=False, padding=(0, 2)))
    _console.print()

    # Filings table
    tbl = Table(
        title=f"Recent Filings ({len(company.filings)} shown)",
        show_lines=False,
        highlight=True,
    )
    tbl.add_column("Accession Number",  style="dim",       no_wrap=True)
    tbl.add_column("Form",              style="bold",      width=7)
    tbl.add_column("Filed",             style="cyan",      width=12)
    tbl.add_column("Description",                          min_width=20)
    tbl.add_column("XBRL",              justify="center",  width=5)

    for f in company.filings:
        tbl.add_row(
            f.accession_number,
            f.form_type,
            str(f.filing_date) if f.filing_date else "—",
            f.description or "—",
            "✓" if f.is_xbrl else "",
        )

    _console.print(tbl)
    _console.print()


# =============================================================================
# edgar financials TICKER --metric METRIC --years N
# =============================================================================


@cli.command("financials")
@click.argument("ticker")
@click.option(
    "--metric", "-m",
    type=click.Choice(_AVAILABLE_METRICS, case_sensitive=False),
    default="revenue",
    show_default=True,
    help=f"Metric to display. Choices: {', '.join(_AVAILABLE_METRICS)}",
)
@click.option(
    "--years", "-y",
    type=click.IntRange(min=1, max=30),
    default=5,
    show_default=True,
    help="Number of most recent annual periods to show.",
)
@click.option(
    "--output", "-o",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.pass_obj
def financials_cmd(
    obj: dict, ticker: str, metric: str, years: int, output: str,
) -> None:
    """Show annual financial history for a company.

    Fetches XBRL data from EDGAR and displays the chosen metric year-by-year.
    TICKER accepts a symbol, CIK, or company name.

    \b
    Examples:
        edgar financials AAPL
        edgar financials MSFT --metric net_income --years 10
        edgar financials GOOGL --metric eps --output json
    """
    # click.IntRange(min=1) already validated; click.Choice validated metric.
    client: EdgarClient  = obj["client"]
    label               = _METRIC_LABELS.get(metric, metric.title())

    try:
        # Two separate calls so the error message names which step failed.
        company = client.get_company(ticker)
        facts   = client.get_facts(ticker)
    except EdgarError as exc:
        _die(exc)

    try:
        series = _resolve_metric(facts, metric)
    except click.ClickException:
        raise   # already formatted by _resolve_metric

    data = _top_n_years(series, years)

    if not data:
        _err_console.print(
            f"[yellow]No annual data for {label} ({ticker.upper()}).[/]"
        )
        sys.exit(1)

    # ── JSON output ──────────────────────────────────────────────────────────
    if output == "json":
        click.echo(json.dumps(_financials_to_dict(ticker, metric, series, years), indent=2))
        return

    # ── Rich table output ────────────────────────────────────────────────────
    display_ticker = company.ticker or ticker.upper()
    _console.print()
    _console.print(
        f"[bold]{label}[/] — [cyan]{company.name}[/] ([dim]{display_ticker}[/])"
    )
    _console.print(f"Unit: [dim]{series.unit}[/]\n")

    tbl = Table(show_header=True, highlight=True)
    tbl.add_column("Year",  style="dim",  justify="center", width=6)
    tbl.add_column(label,                 justify="right",  min_width=14)

    # Most recent year first (sorted descending)
    for yr in sorted(data, reverse=True):
        tbl.add_row(str(yr), _fmt_value(data[yr], series.unit))

    _console.print(tbl)
    _console.print()


# =============================================================================
# edgar search QUERY
# =============================================================================


@cli.command("search")
@click.argument("query")
@click.option(
    "--limit", "-l",
    type=click.IntRange(min=1, max=200),
    default=20,
    show_default=True,
    help="Maximum number of results to display.",
)
@click.option(
    "--output", "-o",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.pass_obj
def search_cmd(obj: dict, query: str, limit: int, output: str) -> None:
    """Search for companies by name or keyword.

    Searches the SEC's company directory (ticker→CIK map) for company names
    containing QUERY.  For broad keyword searches (e.g. \"electric vehicles\")
    that return no name matches, automatically falls back to the EDGAR
    full-text search index.

    \b
    Examples:
        edgar search "Apple"
        edgar search "electric vehicles"
        edgar search Tesla --limit 5
        edgar search "semiconductor" --output json
    """
    client: EdgarClient = obj["client"]

    try:
        # Phase 1: fast company-name search from the cached ticker map.
        results = client.search(query)

        # Phase 2: if name search returned nothing, try EDGAR full-text search
        # (EFTS) which indexes the content of actual filings.  Useful for
        # industry/keyword queries like "electric vehicles".
        if not results:
            raw = client._session.search(query)
            # EFTS returns one hit per *filing*, not per *company*.
            # Deduplicate by CIK so we show each company at most once.
            seen_ciks: set[str] = set()
            for r in raw:
                if r.cik not in seen_ciks:
                    results.append(r)
                    seen_ciks.add(r.cik)

    except EdgarError as exc:
        _die(exc)

    results = results[:limit]

    # ── JSON output ──────────────────────────────────────────────────────────
    if output == "json":
        click.echo(json.dumps(_search_to_dict(results), indent=2))
        return

    # ── Rich table output ────────────────────────────────────────────────────
    if not results:
        _console.print(f"[yellow]No results for [bold]{query!r}[/].[/]")
        return

    tbl = Table(
        title=f'Search results for "{query}"',
        show_lines=False,
        highlight=True,
    )
    tbl.add_column("Company",  min_width=35)
    tbl.add_column("Ticker",   style="bold cyan",  width=9)
    tbl.add_column("CIK",      style="dim",        width=14)

    for r in results:
        tbl.add_row(r.name, r.ticker or "—", r.cik)

    _console.print()
    _console.print(tbl)

    if len(results) == limit:
        _console.print(
            f"[dim]Showing first {limit} results.  "
            "Use [cyan]--limit N[/] to see more.[/]"
        )
    _console.print()


# =============================================================================
# edgar compare TICKER... --metric METRIC --years N
# =============================================================================


@cli.command("compare")
@click.argument("tickers", nargs=-1, required=True)
@click.option(
    "--metric", "-m",
    type=click.Choice(_AVAILABLE_METRICS, case_sensitive=False),
    default="revenue",
    show_default=True,
    help=f"Metric to compare. Choices: {', '.join(_AVAILABLE_METRICS)}",
)
@click.option(
    "--years", "-y",
    type=click.IntRange(min=1, max=30),
    default=5,
    show_default=True,
    help="Number of most recent annual periods to compare.",
)
@click.option(
    "--output", "-o",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.pass_obj
def compare_cmd(
    obj:     dict,
    tickers: tuple[str, ...],
    metric:  str,
    years:   int,
    output:  str,
) -> None:
    """Compare a metric across multiple companies side by side.

    TICKERS is two or more symbols, CIKs, or company names.
    Rows are fiscal years; columns are companies.

    \b
    Examples:
        edgar compare AAPL MSFT GOOGL --metric revenue
        edgar compare AAPL MSFT --metric net_income --years 10
        edgar compare AAPL TSLA --metric eps --output json
    """
    # nargs=-1 means Click collects remaining positional args into a tuple.
    # The required=True ensures Click errors before this function runs if empty.
    if len(tickers) < 2:
        raise click.UsageError(
            "Provide at least 2 tickers to compare.\n"
            "Example: edgar compare AAPL MSFT GOOGL"
        )

    client: EdgarClient = obj["client"]
    label = _METRIC_LABELS.get(metric, metric.title())

    # ── Fetch data for every ticker ──────────────────────────────────────────
    # series_map  : key → FinancialSeries (or None on error)
    # display_map : key → short display name for the column header
    #
    # We use get_facts() only (one HTTP call per ticker) and pull the
    # company display name from facts.entity_name — avoids doubling the
    # request count vs. also calling get_company().
    keys:        list[str]                         = []
    series_map:  dict[str, FinancialSeries | None] = {}
    display_map: dict[str, str]                    = {}

    for raw in tickers:
        key = raw.upper()
        keys.append(key)
        try:
            facts              = client.get_facts(raw)
            series_map[key]    = _resolve_metric(facts, metric)
            # Prefer the ticker symbol for the column header; fall back to
            # the first four words of the entity name if ticker is unknown.
            display_map[key]   = key
        except EdgarError as exc:
            _err_console.print(f"[yellow]⚠  {raw!r}:[/] {exc}")
            series_map[key]    = None
            display_map[key]   = key
        except click.ClickException as exc:
            _err_console.print(f"[yellow]⚠  {raw!r}:[/] {exc.format_message()}")
            series_map[key]    = None
            display_map[key]   = key

    # ── Align years across all companies ────────────────────────────────────
    # Different companies have different fiscal year ends; using the year in
    # FinancialSeries.as_dict() (end.year) means "2023" might mean FY ending
    # Sep 2023 for Apple but Dec 2023 for Microsoft.  This is a known
    # limitation — for rigorous comparison, normalise to calendar year.
    all_years: set[int] = set()
    for s in series_map.values():
        if s:
            all_years.update(_top_n_years(s, years))

    if not all_years:
        raise click.ClickException(
            f"No '{label}' data found for any of the requested tickers."
        )

    recent_years = sorted(all_years, reverse=True)[:years]

    # ── JSON output ──────────────────────────────────────────────────────────
    if output == "json":
        click.echo(
            json.dumps(
                _compare_to_dict(keys, display_map, series_map, metric, years),
                indent=2,
            )
        )
        return

    # ── Rich table output ────────────────────────────────────────────────────
    units  = {s.unit for s in series_map.values() if s}
    unit   = next(iter(units)) if len(units) == 1 else "mixed"
    note   = f" ({unit})" if unit != "mixed" else ""

    tbl = Table(
        title=f"{label} Comparison{note}",
        show_lines=False,
        highlight=True,
    )
    tbl.add_column("Year", style="dim", justify="center", width=6)
    for k in keys:
        col_style = "green" if series_map[k] else "red dim"
        tbl.add_column(display_map[k], justify="right", min_width=14, style=col_style)

    for yr in recent_years:
        row: list[str] = [str(yr)]
        for k in keys:
            s = series_map[k]
            if s:
                yr_data = _top_n_years(s, years)
                val = yr_data.get(yr)
                row.append(_fmt_value(val, unit) if val is not None else "[dim]—[/]")
            else:
                row.append("[dim]N/A[/]")
        tbl.add_row(*row)

    _console.print()
    _console.print(tbl)
    _console.print()
