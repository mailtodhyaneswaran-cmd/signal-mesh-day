"""
sp500_universe.py — S&P 500 constituent list with weekly cache.

Cache: data/sp500_tickers.json  (auto-refreshed every CACHE_DAYS days)
Source: Wikipedia S&P 500 constituents table (via pandas.read_html)
"""
import json
from datetime import date
from pathlib import Path

CACHE_FILE = Path("data/sp500_tickers.json")
CACHE_DAYS = 7


def get_sp500_tickers() -> list[str]:
    """Return S&P 500 tickers, refreshing the cache if older than CACHE_DAYS."""
    if CACHE_FILE.exists():
        try:
            saved = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            age   = (date.today() - date.fromisoformat(saved["date"])).days
            if age < CACHE_DAYS:
                return saved["tickers"]
        except Exception:
            pass  # corrupted cache — re-fetch

    tickers = _fetch_from_wikipedia()
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"date": date.today().isoformat(), "tickers": tickers}),
        encoding="utf-8",
    )
    print(f"[sp500] Cached {len(tickers)} tickers → {CACHE_FILE}")
    return tickers


def _fetch_from_wikipedia() -> list[str]:
    """Scrape the S&P 500 list from Wikipedia.

    Wikipedia blocks the default Python urllib User-Agent with HTTP 403.
    We send a browser-like UA and pass the downloaded HTML to pd.read_html
    so no browser or external dependency is needed.
    """
    import urllib.request
    from io import StringIO
    import pandas as pd

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; signal-mesh-day/1.0)"},
    )
    print("[sp500] Fetching constituents from Wikipedia...")
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8")

    tables = pd.read_html(StringIO(html), attrs={"id": "constituents"})
    # Fix BRK.B → BRK-B etc. for yfinance compatibility
    tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    print(f"[sp500] {len(tickers)} constituents loaded.")
    return tickers
