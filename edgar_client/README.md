# edgar-client — A Python Library for SEC Financial Data

`edgar-client` lets you fetch financial information about publicly traded companies from the U.S. Securities and Exchange Commission (SEC) with a few lines of Python. Get company profiles, annual revenue, balance sheets, and search for companies by name — all without worrying about API rate limits, technical details, or writing raw HTTP requests.

## Installation

Install via pip:

```bash
pip install edgar-client
```

Requires Python 3.11 or later. No additional dependencies — uses Python's built-in libraries.

## Quick Start

```python
from edgar_client import EdgarClient

# Create a client (the SEC requires your name and email)
client = EdgarClient(user_agent="Your Name your@email.com")

# Fetch Apple's revenue history
revenue = client.get_revenue_history("AAPL")
print(revenue.latest_value)  # Most recent annual revenue
```

That's it! The `EdgarClient` handles everything: looking up the company, fetching data from the SEC, parsing it, and returning clean Python objects.

## How It Works

When you call `client.get_revenue_history("AAPL")`, the client:

1. **Resolves the input** — recognises "AAPL" as Apple's ticker symbol
2. **Converts to a CIK** — finds the SEC's internal company ID (0000320193)
3. **Fetches from EDGAR** — downloads the company's financial data
4. **Parses the data** — extracts the revenue figure using the correct XBRL concept name
5. **Returns a Python object** — gives you a clean `FinancialSeries` with annual values

All of this happens automatically, and the client respects SEC rate limits (max 10 requests per second) without you having to think about it.

---

## Complete API Reference

### `EdgarClient(user_agent, base_url, timeout)`

Create a client to fetch SEC data.

**Parameters:**
- `user_agent` (required): Your identity. Format: `"Your Name your@email.com"`. The SEC uses this to identify your requests.
- `base_url` (optional): Base URL for the SEC API. Default: `"https://data.sec.gov"` (you'll rarely need to change this).
- `timeout` (optional): Seconds to wait for a response before giving up. Default: `10.0`. Use a larger value (e.g., `30.0`) if the SEC is slow.

**Example:**

```python
from edgar_client import EdgarClient

client = EdgarClient(
    user_agent="Alice alice@example.com",
    timeout=15.0
)
```

---

### `client.get_company(ticker_or_cik)`

Fetch a company's profile, including recent SEC filings.

**Parameters:**
- `ticker_or_cik`: Can be any of:
  - A **ticker symbol**: `"AAPL"`, `"MSFT"`, `"TSLA"` (case-insensitive)
  - A **numeric CIK**: `"0000320193"` or `"320193"` (zero-padded or short form)
  - A **company name**: `"Apple"`, `"Apple Inc."` (performs a name search)

**Returns:** A `Company` object with these attributes:

| Attribute | Type | Example |
|---|---|---|
| `name` | string | `"Apple Inc."` |
| `cik` | string (10 digits) | `"0000320193"` |
| `ticker` | string or None | `"AAPL"` |
| `sic_code` | string or None | `"7372"` (industry code) |
| `sic_description` | string or None | `"Prepackaged Software"` |
| `state_of_incorporation` | string or None | `"CA"` |
| `fiscal_year_end` | string or None | `"0930"` (Sep 30) |
| `filings` | list of Filing objects | Recent 10-K, 10-Q, 8-K filings |

Each `Filing` in the `filings` list has:

| Attribute | Type | Example |
|---|---|---|
| `accession_number` | string | `"0000320193-24-000001"` |
| `form_type` | string | `"10-K"` |
| `filing_date` | date or None | `2024-11-01` |
| `report_date` | date or None | `2024-09-28` |
| `file_url` | string | URL to the filing on sec.gov |
| `description` | string or None | `"FORM 10-K"` |
| `is_xbrl` | bool | `True` (has structured data) |
| `size` | int or None | `12000000` (bytes) |

**Example:**

```python
company = client.get_company("AAPL")
print(company.name)            # "Apple Inc."
print(company.ticker)          # "AAPL"
print(len(company.filings))    # 20 (most recent filings)

# View the most recent filing
filing = company.filings[0]
print(filing.form_type)        # "10-K"
print(filing.filing_date)      # 2024-11-01
```

---

### `client.get_facts(ticker_or_cik)`

Fetch **all** XBRL financial data for a company. This is the raw data behind 10-K and 10-Q filings — revenue, net income, assets, liabilities, and hundreds of other metrics.

**Parameters:**
- `ticker_or_cik`: Same as `get_company()` — ticker, CIK, or company name

**Returns:** A `CompanyFacts` object with:

| Attribute | Type | Example |
|---|---|---|
| `cik` | string (10 digits) | `"0000320193"` |
| `entity_name` | string | `"Apple Inc."` |
| `facts` | nested dict | `facts["us-gaap"]["Revenues"]` → `FinancialSeries` |

**Example:**

```python
facts = client.get_facts("AAPL")

# View available data sources (usually "us-gaap" for U.S. companies)
print(facts.taxonomies())  # ["us-gaap", "dei"]

# View all available concepts within a taxonomy
print(facts.concepts("us-gaap"))  # ["Revenues", "NetIncomeLoss", ...]

# Fetch a specific concept (raw revenue data)
revenue_series = facts.get("us-gaap", "Revenues")
print(revenue_series.latest_value)  # 383,285,000,000 (Decimal, exact)
```

---

### `client.get_revenue_history(ticker_or_cik)`

Convenience method: fetch a company's annual revenue without needing to know which XBRL concept name it uses.

Different companies use different names for revenue (Revenues, RevenueFromContractWithCustomer, etc.). This method tries them in order and returns the first match.

**Parameters:**
- `ticker_or_cik`: Ticker, CIK, or company name

**Returns:** A `FinancialSeries` object (see below)

**Example:**

```python
revenue = client.get_revenue_history("AAPL")

# Get the most recent annual revenue (10-K filing)
print(revenue.latest_value)          # Decimal('383285000000')

# Get all annual values as a dict
history = revenue.as_dict()          # {2023: Decimal(...), 2022: Decimal(...), ...}
for year, amount in sorted(history.items()):
    print(f"{year}: ${amount:,}")

# Access raw values
for value in revenue.values:
    print(value.end, value.value, value.form)  # date, amount, form type
```

---

### `FinancialSeries` — One metric across multiple periods

Represents a single financial metric (e.g., revenue) reported over multiple years.

| Attribute | Type | Example |
|---|---|---|
| `concept_name` | string | `"Revenues"` |
| `unit` | string | `"USD"` or `"shares"` |
| `values` | list | All reported values (newest first) |
| `latest_value` | Decimal or None | Most recent 10-K value |
| `annual_values` | list | Filtered to 10-K filings only |

| Method | Returns | Description |
|---|---|---|
| `as_dict()` | dict[int, Decimal] | `{year: value, ...}` mapping |

**Example:**

```python
revenue = client.get_revenue_history("AAPL")

# Latest value from the most recent 10-K
print(revenue.latest_value)
# Decimal('383285000000')

# All annual values as {year: amount}
data = revenue.as_dict()
# {2023: Decimal('383285000000'), 2022: Decimal('394328000000'), ...}

# Raw access to individual value objects
for value in revenue.annual_values:
    print(f"{value.end.year}: ${value.value:,} ({value.form})")
    # 2023: $383,285,000,000 (10-K)
```

**Note:** Financial values use Python's `Decimal` type (not `float`) to avoid precision loss on large numbers. Apple's quarterly revenue is ~$89 billion; a float would lose the last few digits.

---

### `client.search(company_name)`

Search for companies by name. Useful when you don't know the ticker symbol.

**Parameters:**
- `company_name`: Any part of the company name. Search is case-insensitive and can find substring matches.

**Returns:** A list of `SearchResult` objects

Each result has:

| Attribute | Type | Example |
|---|---|---|
| `name` | string | `"Apple Inc."` |
| `ticker` | string or None | `"AAPL"` |
| `cik` | string (10 digits) | `"0000320193"` |

**Example:**

```python
results = client.search("Apple")
for r in results:
    print(f"{r.name:30} {r.ticker:5} {r.cik}")

# Output:
# Apple Inc.                     AAPL   0000320193
# Apple REIT Ten                 APRE   0001569429
# ... (more results)

# Use the first result to fetch data
top_match = results[0]
company = client.get_company(top_match.cik)
```

---

### `client.invalidate_cache()`

Force a fresh download of the company name/ticker map.

The client caches the SEC's ticker list (~500 KB) in memory after the first lookup. If a company recently changed its ticker or IPO'd, call this to refresh.

**Example:**

```python
# First call: fetches and caches the ticker map
company = client.get_company("AAPL")

# Subsequent calls: use cached map (instant)
company = client.get_company("MSFT")

# Clear cache (e.g., after a company IPO'd)
client.invalidate_cache()

# Next call: re-fetches the map
company = client.get_company("NEWCO")
```

---

### `client.cache_size`

Property that returns the number of companies in the cached ticker map.

**Returns:** Integer (0 before first lookup, ~10,000 after)

**Example:**

```python
print(client.cache_size)  # 0 (cache not loaded yet)
client.get_company("AAPL")
print(client.cache_size)  # 10241 (now cached)
```

---

### Context Manager

Use `EdgarClient` as a context manager to automatically close connections:

```python
with EdgarClient(user_agent="Your Name your@email.com") as client:
    company = client.get_company("AAPL")
    revenue = client.get_revenue_history("AAPL")
# Connections automatically closed
```

---

## Error Handling

The library raises specific exceptions for different failure modes. Catch them to handle errors gracefully.

### `InvalidUserAgentError`

**When:** You create an `EdgarClient` with an invalid User-Agent.

**Cause:** The SEC requires `"Name email@domain.com"` format.

**How to fix:**
```python
# ❌ Wrong
client = EdgarClient(user_agent="alice@example.com")

# ✓ Correct
client = EdgarClient(user_agent="Alice alice@example.com")
```

### `InvalidTickerError`

**When:** You pass a ticker, CIK, or company name that doesn't exist.

**Cause:** The ticker isn't in the SEC's database, or the name search returned no matches.

**How to handle:**
```python
try:
    company = client.get_company("ZZZZZ")
except InvalidTickerError as e:
    print(f"Company not found: {e}")
    # Suggest using search()
    results = client.search("your company name")
```

### `EdgarNotFoundError`

**When:** The company exists but has no data on the SEC (HTTP 404).

**Cause:** Very rare. The company may not file with the SEC (e.g., foreign private issuers) or the CIK may be inactive.

**How to handle:**
```python
try:
    facts = client.get_facts("0000000001")
except EdgarNotFoundError as e:
    print(f"No data found: {e}")
```

### `EdgarRateLimitError`

**When:** You're sending requests too fast (HTTP 429).

**Cause:** You've exceeded 10 requests per second. The client normally prevents this automatically, but it can happen if:
- You're using multiple clients simultaneously
- The SEC's rate limit changes
- You bypassed the rate limiter

**How to handle:**
```python
import time
try:
    company = client.get_company("AAPL")
except EdgarRateLimitError as e:
    print("Rate limited. Waiting 5 seconds...")
    time.sleep(5)
    # Retry
    company = client.get_company("AAPL")
```

### `EdgarTimeoutError`

**When:** A request took too long and timed out.

**Cause:** The SEC servers are slow, or your internet connection is slow.

**How to handle:**
```python
try:
    facts = client.get_facts("AAPL")  # Can be slow for large companies
except EdgarTimeoutError as e:
    print("Request timed out. Retrying with longer timeout...")
    client = EdgarClient(user_agent="...", timeout=30.0)
    facts = client.get_facts("AAPL")
```

### `EdgarConnectionError`

**When:** A network error occurs (DNS failure, refused connection, etc.).

**Cause:** Your internet is down, the SEC servers are down, or there's a firewall issue.

**How to handle:**
```python
try:
    company = client.get_company("AAPL")
except EdgarConnectionError as e:
    print(f"Network error: {e}")
    print("Check your internet connection or try again later.")
```

### `DataParseError`

**When:** The SEC returns unexpected data (malformed JSON, missing fields).

**Cause:** The SEC API changed, or there's a bug in the library.

**How to handle:**
```python
try:
    company = client.get_company("AAPL")
except DataParseError as e:
    print(f"Data parsing error: {e}")
    # This is a bug; please report it: github.com/...
```

---

## Catching All Errors

All exceptions inherit from `EdgarError`, so you can catch them together:

```python
from edgar_client import EdgarClient, EdgarError

client = EdgarClient(user_agent="Alice alice@example.com")

try:
    company = client.get_company("AAPL")
except EdgarError as e:
    print(f"API error: {e}")
    # Handle any SEC-related error
```

---

## Rate Limiting — What It Means

The SEC has a **fair-use policy**: no more than 10 requests per second per user-agent.

### What the client does automatically

The client **enforces this limit on your behalf**. When you make requests faster than 10 per second, it pauses between them:

```python
# Send 20 requests as fast as your code allows
for ticker in tickers:
    company = client.get_company(ticker)
    # The client sleeps between calls to stay under 10 req/s
    # Total time: ~2 seconds (not ~0.1 seconds)
```

### What you don't need to do

- ❌ Call `time.sleep()` between requests — the client handles it
- ❌ Create separate clients for parallel requests — one client works fine
- ❌ Worry about accidentally hitting the rate limit

### Why it matters

If you exceed 10 requests per second, the SEC's servers return HTTP 429 ("Too Many Requests") and your request fails. The client prevents this by spacing out requests automatically.

### How it works (technical details, optional)

The client uses a token-bucket algorithm:
1. Records the time of the last request
2. Before the next request, checks if 0.1 seconds (100 ms) have passed
3. If not, sleeps for the difference
4. Then sends the request

This ensures requests are spaced at least 0.1 seconds apart, never exceeding 10 per second.

---

## Validation — Check Inputs Early

You can validate inputs before making requests:

```python
from edgar_client import EdgarClient

client = EdgarClient(user_agent="Alice alice@example.com")

# Check multiple inputs at once
errors = client.validate_inputs(
    cik="notdigits",
    ticker="TOOLONG",
    company_name=""
)

if errors:
    for error in errors:
        print(f"❌ {error}")
    # Fix the errors and try again
else:
    # All inputs are valid; safe to proceed
    company = client.get_company("AAPL")
```

---

## Using the Library with Click (CLI)

Example: build a command-line tool with Click.

```python
import click
from edgar_client import EdgarClient

@click.command()
@click.argument("ticker")
def main(ticker):
    """Fetch a company's revenue."""
    client = EdgarClient(user_agent="YourName your@email.com")
    try:
        revenue = client.get_revenue_history(ticker)
        print(f"Latest revenue: ${revenue.latest_value:,}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
```

Run it:
```bash
python script.py AAPL
# Latest revenue: $383,285,000,000
```

---

## Data Attribution and Usage

### What is EDGAR?

EDGAR (Electronic Data Gathering, Organization, and Retrieval) is the SEC's database of public company filings. All U.S. publicly traded companies must file financial statements, quarterly reports, and other disclosures here. The data is public and free.

### Using this library means you're using SEC data

When you fetch data via `edgar-client`, you're downloading from:
- **Source:** U.S. Securities and Exchange Commission (SEC)
- **URL:** https://www.sec.gov/cgi-bin/browse-edgar
- **License:** Public domain (U.S. government work)

### Attribution

If you publish analysis or reports using this data, cite it:

```
Data source: U.S. Securities and Exchange Commission (SEC)
Retrieved from: https://www.sec.gov/
```

### Terms of use

The SEC allows free access to EDGAR data, but requests that you:
- **Respect the rate limit:** Don't exceed 10 requests per second (the client does this for you)
- **Don't redistribute copies:** Link to SEC data instead of copying it
- **Follow SEC terms:** See https://www.sec.gov/tos.shtml

---

## Troubleshooting

### "Invalid User-Agent: ..."

You passed an invalid User-Agent. The SEC requires the format `"Your Name your@email.com"`.

```python
# ❌ Wrong
client = EdgarClient(user_agent="alice")

# ✓ Correct
client = EdgarClient(user_agent="Alice alice@example.com")
```

### "Unknown ticker: AAPL"

This shouldn't happen — AAPL is Apple. You might have misspelled it or hit a network error.

```python
# Try searching instead
results = client.search("Apple")
for r in results:
    print(r.ticker, r.name)
```

### "Request timed out"

The SEC servers were slow. Increase the timeout and retry:

```python
client = EdgarClient(
    user_agent="Your Name your@email.com",
    timeout=30.0  # 30 seconds instead of 10
)
company = client.get_company("AAPL")
```

### "Connection error"

Your internet is down or the SEC servers are unreachable. Check:
- Your internet connection
- Whether https://www.sec.gov is reachable
- Try again in a few moments

### "No revenue data found"

The company uses a non-standard XBRL concept for revenue, or doesn't file with the SEC.

```python
# Inspect all available concepts
facts = client.get_facts("AAPL")
for concept in facts.concepts("us-gaap"):
    if "revenue" in concept.lower() or "sales" in concept.lower():
        print(concept)
```

---

## Example: Fetch and Compare Multiple Companies

```python
from edgar_client import EdgarClient
from decimal import Decimal

client = EdgarClient(user_agent="Your Name your@email.com")

# Fetch revenue for multiple companies
tickers = ["AAPL", "MSFT", "GOOGL"]
data = {}

for ticker in tickers:
    try:
        revenue = client.get_revenue_history(ticker)
        data[ticker] = revenue.latest_value
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")

# Compare
print("\nLatest Annual Revenue:")
for ticker, revenue in sorted(data.items(), key=lambda x: x[1], reverse=True):
    print(f"{ticker}: ${revenue:,}")
```

Output:
```
Latest Annual Revenue:
AAPL: $383,285,000,000
GOOGL: $307,394,000,000
MSFT: $245,122,000,000
```

---

## More Information

- **SEC EDGAR homepage:** https://www.sec.gov/cgi-bin/browse-edgar
- **XBRL (financial data format):** https://www.sec.gov/page/about-xbrl
- **Report a bug:** Open an issue on GitHub

---

**Happy analyzing!**

*edgar-client is not affiliated with the SEC. It's a community tool that makes SEC data easier to use.*
