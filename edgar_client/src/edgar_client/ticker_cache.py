"""
Ticker-to-CIK lookup cache for edgar_client.

The SEC publishes company_tickers.json — a ~500 KB file mapping every
exchange-listed ticker symbol to its numeric CIK.  TickerCache fetches
this file once per session lifetime and serves all subsequent queries
from two in-memory data structures:

    _cache   : dict[str, str]       ticker.upper() → 10-digit padded CIK
    _entries : list[dict]           all {cik, ticker, title} dicts for name search

Why on EdgarSession (not EdgarClient)?
  Every HTTP request — including the ticker-map fetch — must pass through
  the session's rate limiter and connection pool.  Placing the cache on the
  session ensures that automatically.  See sessions.py for the full argument.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from .exceptions import DataParseError, InvalidTickerError

if TYPE_CHECKING:
    from .sessions import EdgarSession

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _pad_cik(cik: int | str) -> str:
    """Zero-pad a CIK to exactly 10 digits (SEC canonical form)."""
    return str(int(cik)).zfill(10)


class TickerCache:
    """Lazy-loaded, thread-safe cache of the SEC company tickers map.

    The cache is populated on the first lookup (ticker or name) and then
    kept for the lifetime of the session.  Call ``invalidate()`` to discard
    it and force a fresh download on the next request.

    Internals
    ---------
    _cache   : fast O(1) ticker → CIK lookup (used by ``lookup()``)
    _entries : ordered list of all entries for linear name search (``search_by_name()``)

    Thread safety: ``_load()`` is guarded by a lock so concurrent callers race
    exactly once — the first caller fetches the map, the rest wait at the lock
    and then find ``_loaded = True`` on entry and return immediately.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}            # TICKER_UPPER → padded_cik
        self._entries: list[dict[str, str]] = []    # [{cik, ticker, title}, ...]
        self._loaded: bool = False
        self._tickers_url: str = _TICKERS_URL
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def lookup(self, ticker: str, session: "EdgarSession") -> str:
        """Return the 10-digit padded CIK for *ticker*.

        Loads the tickers map on the first call; subsequent calls are O(1).

        Raises InvalidTickerError when the ticker is not in the map.  Note that
        the map only covers exchange-listed companies.  Unlisted filers (e.g.
        non-reporting foreign private issuers) may have a CIK but no ticker.
        """
        if not self._loaded:
            self._load(session)
        key = ticker.upper()
        if key not in self._cache:
            raise InvalidTickerError(
                f"Unknown ticker: {ticker!r}. "
                "If the company is not exchange-listed, pass its 10-digit CIK "
                "directly, or use search_by_name() to find it."
            )
        return self._cache[key]

    def search_by_name(
        self,
        name: str,
        session: "EdgarSession",
    ) -> list[dict[str, str]]:
        """Return all ticker-map entries whose title contains *name*.

        Matching is case-insensitive substring search.  Results are ranked so
        the best matches come first:

            Rank 0 — Exact match:    query == title (case-folded)
            Rank 1 — Prefix match:   title starts with query
            Rank 2 — Contains match: query anywhere in title

        Within the same rank, entries are sorted alphabetically by title.

        Returns an empty list when nothing matches or *name* is empty.

        Why linear search?
        The tickers map has ~10 000 entries; a linear scan takes < 1 ms in
        CPython.  Building an index (inverted index, trie, etc.) would speed
        repeated queries but add complexity for negligible real-world gain —
        callers who need sub-millisecond lookup should build their own index
        on top of ``all_entries()``.
        """
        if not self._loaded:
            self._load(session)

        query = name.strip().lower()
        if not query:
            return []

        matches = [
            entry for entry in self._entries
            if query in entry["title"].lower()
        ]

        def _rank(entry: dict[str, str]) -> tuple[int, str]:
            title_lower = entry["title"].lower()
            if title_lower == query:
                return (0, entry["title"])
            if title_lower.startswith(query):
                return (1, entry["title"])
            return (2, entry["title"])

        return sorted(matches, key=_rank)

    def invalidate(self) -> None:
        """Discard the cache; force a fresh download on the next call.

        Useful when you suspect the in-memory map is stale — the SEC updates
        company_tickers.json when companies list, delist, or change tickers.
        """
        with self._lock:
            self._loaded = False
            self._cache = {}
            self._entries = []

    def all_entries(self, session: "EdgarSession") -> list[dict[str, str]]:
        """Return every entry in the tickers map as a list of dicts.

        Useful for bulk analysis or building custom indexes.  The map is loaded
        on the first call if not already cached.
        """
        if not self._loaded:
            self._load(session)
        return list(self._entries)

    def __len__(self) -> int:
        """Number of ticker symbols in the cache (0 until first load)."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self, session: "EdgarSession") -> None:
        """Fetch and parse the SEC company-tickers JSON.

        The double-checked locking pattern (check ``_loaded`` before AND after
        acquiring the lock) prevents a second fetch when multiple threads race
        to the first call:

          Thread A sees _loaded=False → enters lock → fetches → sets _loaded=True
          Thread B sees _loaded=False → blocks at lock → resumes after A → sees
            _loaded=True inside the lock → returns without fetching again
        """
        with self._lock:
            if self._loaded:           # re-check under the lock
                return

            response = session.get(self._tickers_url)
            try:
                raw: dict[str, dict[str, Any]] = response.json()
            except Exception as exc:
                raise DataParseError(
                    "Failed to parse SEC company-tickers JSON. "
                    "The file may be temporarily unavailable.",
                    raw_content=response.content,
                    response=response,
                ) from exc

            cache: dict[str, str] = {}
            entries: list[dict[str, str]] = []

            for item in raw.values():
                # The SEC JSON uses "cik_str" for the string form of the CIK
                # and may also expose "cik" as an integer in some variants.
                raw_cik = item.get("cik_str") or item.get("cik")
                raw_ticker = item.get("ticker")
                raw_title = item.get("title", "")
                if raw_cik is None or raw_ticker is None:
                    continue
                padded = _pad_cik(raw_cik)
                ticker_upper = str(raw_ticker).upper()
                cache[ticker_upper] = padded
                entries.append({
                    "cik":    padded,
                    "ticker": str(raw_ticker),
                    "title":  str(raw_title),
                })

            self._cache = cache
            self._entries = entries
            self._loaded = True
