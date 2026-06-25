"""
data_loader.py — IBKR 1-min bar fetcher with local CSV cache.

Cache layout:  data/{TICKER}/{YYYY-MM-DD}.csv
Each CSV has a header: t,open,high,low,close,volume  (RTH bars only)

IBKR pacing limits: ~60 historical requests per 10 min.
  - _fetch_from_ibkr sleeps PACING_SLEEP_SEC after each live fetch.
  - fetch_date_range adds a longer pause every BULK_BATCH_SIZE fetches.
"""
import csv
import time
from datetime import date
from pathlib import Path
from typing import Optional

from orb_core import Bar

CACHE_DIR       = Path("data")
PACING_SLEEP_SEC = 11          # between individual IBKR requests (avoids 60-req/10-min)
BULK_BATCH_SIZE  = 50          # hard pause after this many live fetches in one run
BULK_PAUSE_SEC   = 650         # ~10 min 50 s — resets the IBKR pacing window


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_path(ticker: str, d: date) -> Path:
    return CACHE_DIR / ticker / f"{d.isoformat()}.csv"


def _save_bars(bars: list[Bar], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.t, b.open, b.high, b.low, b.close, b.volume])


def _load_bars_csv(path: Path) -> list[Bar]:
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bars.append(Bar(
                t      = row["t"],
                open   = float(row["open"]),
                high   = float(row["high"]),
                low    = float(row["low"]),
                close  = float(row["close"]),
                volume = float(row["volume"]),
            ))
    return bars


# ── IBKR fetch ────────────────────────────────────────────────────────────────

def _fetch_from_ibkr(ib, ticker: str, d: date) -> list[Bar]:
    """Fetch RTH 1-min bars from IBKR for one date. Sleeps PACING_SLEEP_SEC after."""
    from zoneinfo import ZoneInfo
    import ibkr_connector

    NL       = ZoneInfo("Europe/Amsterdam")
    contract = ibkr_connector.get_contract(ticker)
    end_dt   = f"{d.strftime('%Y%m%d')} 22:00:00"

    try:
        raw = ib.reqHistoricalData(
            contract,
            endDateTime    = end_dt,
            durationStr    = "1 D",
            barSizeSetting = "1 min",
            whatToShow     = "TRADES",
            useRTH         = True,
            formatDate     = 1,
        )
    except Exception as e:
        print(f"  [data_loader] IBKR fetch failed {ticker} {d}: {e}")
        time.sleep(PACING_SLEEP_SEC)
        return []

    bars = [
        Bar(
            t      = b.date.astimezone(NL).strftime("%H:%M"),
            open   = b.open,
            high   = b.high,
            low    = b.low,
            close  = b.close,
            volume = float(b.volume),
        )
        for b in raw
    ]
    time.sleep(PACING_SLEEP_SEC)
    return bars


# ── Public API ────────────────────────────────────────────────────────────────

def load_session(ticker: str, session_date: date, ib=None) -> list[Bar]:
    """Return RTH 1-min bars for ticker on session_date.

    Loads from CSV cache when available; otherwise fetches from IBKR.
    Returns an empty list if data is unavailable (holiday, no connection, etc.).
    """
    path = _cache_path(ticker, session_date)
    if path.exists():
        return _load_bars_csv(path)
    if ib is None:
        return []
    bars = _fetch_from_ibkr(ib, ticker, session_date)
    if bars:
        _save_bars(bars, path)
    return bars


def fetch_date_range(
    ib,
    ticker: str,
    dates:  list[date],
    verbose: bool = False,
) -> dict[date, list[Bar]]:
    """Batch-load bars for multiple dates, respecting IBKR pacing limits.

    Cache hits are free; live fetches are counted and a long pause is inserted
    every BULK_BATCH_SIZE requests to stay within IBKR's 60-req/10-min window.

    Returns {date: [Bar, ...]}; empty list for dates with no data (holidays, etc.).
    """
    result: dict[date, list[Bar]] = {}
    live_fetches = 0

    for d in dates:
        was_cached = _cache_path(ticker, d).exists()
        bars = load_session(ticker, d, ib)
        result[d] = bars

        if not was_cached and bars:
            live_fetches += 1
            if verbose:
                print(f"  [data_loader] fetched {ticker} {d}  ({len(bars)} bars)")
            if live_fetches % BULK_BATCH_SIZE == 0:
                print(f"  [data_loader] bulk pause {BULK_PAUSE_SEC}s "
                      f"(after {live_fetches} IBKR fetches)...")
                time.sleep(BULK_PAUSE_SEC)
        elif not bars and verbose:
            print(f"  [data_loader] no data  {ticker} {d} (holiday/weekend?)")

    return result
