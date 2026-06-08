# EdgarClient Error Handling Audit

## Executive Summary

**Status:** 7/10 — Good HTTP error mapping; gaps in input validation and JSON parsing.

The library handles network-level errors well (timeouts, connection failures, 404s, 429s) but has three classes of gaps:
1. **JSON parsing errors** are not consistently wrapped as `DataParseError`
2. **Input validation** happens late (at HTTP layer) instead of early (before expensive operations)
3. **No bulk validation method** — users cannot collect all validation failures at once

---

## Audit by Public Method

### `EdgarClient.__init__(user_agent, base_url, timeout)`

| Failure Mode | Caught? | Error Type | Actionable? |
|---|---|---|---|
| Invalid user_agent format | ✓ | `InvalidUserAgentError` | ✓ Explains regex rule |
| Invalid base_url (not a URL) | ✗ | Would raise `ValueError` from urllib | ✗ Generic error |
| Invalid timeout (negative) | ✗ | Would raise `ValueError` from `float()` | ✗ Generic error |

**Gap:** No validation of base_url or timeout before creating session.

---

### `EdgarClient.get_company(ticker_or_cik)`

| Failure Mode | Caught? | Error Type | Actionable? |
|---|---|---|---|
| Empty string input | ✗ | `InvalidTickerError` | ✓ But late — happens at lookup |
| Whitespace-only input | ✗ | `InvalidTickerError` | ✓ But late |
| Invalid CIK format (e.g. "ABC") | ✓ | `InvalidTickerError` | ✓ Falls through to name search |
| Unknown ticker | ✓ | `InvalidTickerError` | ✓ Suggests search() |
| Unknown company name | ✓ | `InvalidTickerError` | ✓ Suggests search() |
| Network timeout | ✓ | `EdgarTimeoutError` | ✓ |
| HTTP 404 | ✓ | `EdgarNotFoundError` | ✓ |
| HTTP 429 (rate limit) | ✓ | `EdgarRateLimitError` | ✓ |
| JSON parsing error | ✗ | `json.JSONDecodeError` | ✗ Raw exception escapes |
| Missing required key | ✗ | `KeyError` | ✗ Raw exception escapes |

**Gaps:** 
- JSON/KeyError exceptions not wrapped
- Empty input accepted (wasted network call)

---

### `EdgarClient.get_facts(ticker_or_cik)`

Same as `get_company()`, plus:
- Response payload can be > 10 MB (no streaming); no out-of-memory check

---

### `EdgarClient.search(company_name)`

| Failure Mode | Caught? | Error Type | Actionable? |
|---|---|---|---|
| Empty string | ✓ | Returns `[]` | ✓ But inefficient |
| None | ✗ | `TypeError` or `AttributeError` | ✗ |
| URL encoding error (unlikely) | ✗ | `UnicodeEncodeError` | ✗ |

**Gap:** No validation; accepts None, which could bubble `TypeError`.

---

### `EdgarClient.get_revenue_history(ticker_or_cik)`

Same as `get_facts()`, plus:
- Tries 6 concept names; if all absent, raises `DataParseError` ✓

---

### `EdgarClient._resolve_cik(ticker_or_cik)`

| Failure Mode | Caught? | Error Type | Actionable? |
|---|---|---|---|
| Input is numeric CIK | ✓ | Returns padded CIK | ✓ |
| Input is known ticker | ✓ | Returns CIK from cache | ✓ |
| Input is unknown ticker | ✓ | Falls to name search | ✓ |
| Input is company name | ✓ | Name search + returns first | ✓ |
| Input is completely unknown | ✓ | `InvalidTickerError` | ✓ |
| Empty string | ✓ | `InvalidTickerError` | ✓ But wastes cache lookup |
| None | ✗ | `AttributeError` on `.strip()` | ✗ Raw exception escapes |

**Gap:** No pre-flight validation before expensive cache lookup.

---

## Audit by Component

### `EdgarHTTPAdapter.send()` 

**Good:**
- ✓ Wraps `socket.timeout` and `urllib.error.URLError` correctly
- ✓ Maps status codes to typed exceptions (404→`EdgarNotFoundError`, 429→`EdgarRateLimitError`)
- ✓ Retries 5xx with backoff
- ✓ Handles both platform timeout behaviours

**Gaps:** None significant.

---

### `Company.from_dict()` and other `from_dict()` methods

**Current:**
```python
@classmethod
def from_dict(cls, data: dict[str, Any]) -> "Company":
    # If data["cik"] is missing, KeyError escapes to caller
    # If data["name"] is missing, KeyError escapes to caller
    cik = _pad_cik(data["cik"])
    name = data["name"]
    ...
```

**Gap:** Missing keys raise `KeyError`, not `DataParseError`.

**Scenario:**
```python
client.get_company("AAPL")
# If SEC returns malformed JSON (missing "name" key):
# → KeyError: 'name'  ← User sees raw exception
# Instead of:
# → DataParseError("Missing required key 'name' in company data")
```

---

### `EdgarSession.request()` and `.send()`

**Good:**
- ✓ All network exceptions mapped to `EdgarError` subclasses
- ✓ Response status validated via `.raise_for_status()`

**Gaps:** None significant.

---

## Validation Gaps Summary

| Input | Validated? | When? | Gap |
|---|---|---|---|
| `user_agent` | ✓ | Init | Regex checks at construction — good |
| `base_url` | ✗ | — | No validation; could be non-URL |
| `timeout` | ✗ | — | No range check (negative values) |
| `ticker_or_cik` | Partial | Late | Format not checked until network call |
| `company_name` in search | ✗ | — | None is accepted; KeyError would escape |
| CIK (when passed directly) | ✓ | Late | `_pad_cik()` validates, but errors if non-digit |

---

## Error Message Quality

| Exception | Message | Actionable? |
|---|---|---|
| `InvalidUserAgentError` | "Invalid User-Agent 'X'. The SEC requires the format: 'Your Name your@email.com'" | ✓ Yes |
| `InvalidTickerError` | "Unknown ticker: ZZZZ. Tried name search." | ✓ Yes |
| `EdgarNotFoundError` | "HTTP 404" | ~ Minimal but OK |
| `EdgarRateLimitError` | "HTTP 429" | ~ Minimal |
| `DataParseError` | "No revenue data found for 'AAPL'. Tried: ..." | ✓ Yes |
| `KeyError` (from from_dict) | "'name'" | ✗ No — too terse |

---

## Recommended Fixes (Priority Order)

### 1. **Wrap JSON/KeyError exceptions** (HIGH)
Add try/except in all `from_dict()` methods:
```python
try:
    cik = _pad_cik(data["cik"])
except KeyError as e:
    raise DataParseError(f"Missing required key {e} in {cls.__name__} data")
except (ValueError, TypeError) as e:
    raise DataParseError(f"Invalid data type: {e}")
```

### 2. **Add validate_inputs() method** (MEDIUM)
On `EdgarClient`, collect all validation errors before any network call:
```python
def validate_inputs(self, **kwargs) -> list[str]:
    """Validate input parameters; return list of error messages (empty if valid)."""
    errors = []
    if "cik" in kwargs and not _is_valid_cik_format(kwargs["cik"]):
        errors.append("CIK must be 10 digits; got {kwargs['cik']!r}")
    if "ticker" in kwargs and not _is_valid_ticker(kwargs["ticker"]):
        errors.append("Ticker must be 1-5 uppercase letters; got {kwargs['ticker']!r}")
    if "company_name" in kwargs and not kwargs["company_name"]:
        errors.append("Company name must not be empty")
    return errors
```

### 3. **Early validation in public methods** (MEDIUM)
Before expensive operations, validate inputs:
```python
def get_company(self, ticker_or_cik: str) -> Company:
    # Pre-flight check: empty input wastes a cache lookup
    if not ticker_or_cik or not ticker_or_cik.strip():
        raise InvalidTickerError("Ticker or CIK must not be empty")
    cik = self._resolve_cik(ticker_or_cik.strip())
    return self._session.get_company(cik)
```

### 4. **Validate base_url and timeout** (LOW)
In `EdgarClient.__init__()`:
```python
if not isinstance(timeout, (int, float, tuple, type(None))):
    raise TypeError(f"timeout must be int, float, tuple, or None; got {type(timeout)}")
if isinstance(timeout, (int, float)) and timeout < 0:
    raise ValueError(f"timeout must be >= 0; got {timeout}")
```

---

## Implementation Plan

1. Add helper functions: `_validate_cik_format()`, `_validate_ticker_format()`, `_is_empty_or_whitespace()`
2. Wrap json.JSONDecodeError and KeyError in all `from_dict()` methods
3. Add early validation to `get_company()`, `get_facts()`, `search()` methods
4. Add validate_inputs() method to EdgarClient (public API for explicit validation)
5. Update docstrings with new error conditions
6. Add tests for new validation paths

---

## Tests Needed

```python
# New test cases
def test_empty_ticker_rejected_early():
    client = EdgarClient(user_agent=...)
    with pytest.raises(InvalidTickerError):
        client.get_company("")

def test_whitespace_only_rejected_early():
    with pytest.raises(InvalidTickerError):
        client.get_company("   ")

def test_invalid_json_wrapped():
    # Mock adapter to return malformed JSON
    with pytest.raises(DataParseError):
        client.get_company("AAPL")

def test_missing_required_key_wrapped():
    # Mock adapter to return valid JSON but missing "name"
    with pytest.raises(DataParseError):
        client.get_company("AAPL")

def test_validate_inputs_collects_errors():
    errors = client.validate_inputs(
        cik="not10digits",
        ticker="TOOLONG",
        company_name=""
    )
    assert len(errors) == 3  # All three errors collected
    assert all(isinstance(e, str) for e in errors)
```

---

## Conclusion

The library has **solid HTTP-layer error handling** but **needs input validation** before expensive network calls. The main improvements are:
1. Wrap JSON parsing errors
2. Validate inputs early and collect all errors at once
3. Provide a public `validate_inputs()` method for explicit validation

All three are low-risk changes with high user benefit.
