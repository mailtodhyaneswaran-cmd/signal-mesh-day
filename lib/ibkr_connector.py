"""
ibkr_connector.py — thin ib_async wrapper for Signal Mesh Day.

Adapted from candle-scalping-bot/ibkr_connector.py.
Key additions vs the reference:
  - get_eurusd_rate()  — live EUR→USD conversion for position sizing
  - get_contract() accepts a pre-qualified contract or builds one from symbol
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import datetime as _dt
import json as _json
from collections import defaultdict as _defaultdict
from pathlib import Path as _Path

import yfinance as yf
from zoneinfo import ZoneInfo

from ib_async import IB, Stock, LimitOrder, MarketOrder, StopOrder

import config

NL = ZoneInfo("Europe/Amsterdam")
_ET = ZoneInfo("America/New_York")

# Disk cache for avg premarket volume (keyed by symbol + date + lookback days)
from setup_paths import RVOL_CACHE_DIR as _RVOL_CACHE_DIR


def connect() -> IB:
    ib = IB()
    ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=config.IBKR_CLIENT_ID)
    return ib


def get_account_summary(ib: IB, retries: int = 5, delay: float = 1.5) -> dict:
    """Return key account metrics from the connected paper/live account.

    Fetches NetLiquidation, AvailableFunds, and BuyingPower from IBKR.
    Retries several times because accountSummary() can return empty immediately
    after connect() while IBKR initialises the subscription.

    Returns a dict with float values in USD (or account base currency).
    On total failure: raises RuntimeError so callers can abort rather than
    silently sizing off a zero/garbage value.
    """
    import time as _time
    keys_wanted = {"NetLiquidation", "AvailableFunds", "BuyingPower"}
    for attempt in range(retries):
        rows = ib.accountSummary()
        found = {
            row.tag: float(row.value)
            for row in rows
            if row.tag in keys_wanted
        }
        if len(found) == len(keys_wanted) and all(v > 0 for v in found.values()):
            return {
                "net_liquidation":  found["NetLiquidation"],
                "available_funds":  found["AvailableFunds"],
                "buying_power":     found["BuyingPower"],
            }
        if attempt < retries - 1:
            print(f"  [account] summary not ready yet (attempt {attempt+1}/{retries})"
                  f" — retrying in {delay}s...")
            _time.sleep(delay)
    raise RuntimeError(
        "get_account_summary() could not retrieve valid account data after "
        f"{retries} attempts. Check TWS is connected and the account is active."
    )


def get_contract(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
    return Stock(symbol, exchange, currency)


def get_eurusd_rate() -> float:
    """Pull live EUR→USD spot rate from yfinance (free, no key).

    Falls back to 1.08 if the fetch fails so position sizing never crashes.
    """
    try:
        fi   = yf.Ticker("EURUSD=X").fast_info
        # fast_info is a FastInfo object (not a dict) — use getattr
        rate = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
        if rate and float(rate) > 0:
            return float(rate)
    except Exception:
        pass
    # Secondary fallback: download last close
    try:
        data = yf.download("EURUSD=X", period="2d", interval="1d",
                           progress=False, auto_adjust=True)
        if not data.empty:
            return float(data["Close"].dropna().iloc[-1])
    except Exception:
        pass
    print("[ibkr_connector] EURUSD fetch failed — falling back to 1.08")
    return 1.08


def get_opening_range_bar(ib: IB, contract: Stock, opening_time: str):
    """Return the 5-min bar that starts at opening_time (NL local HH:MM), or None."""
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="3600 S",
        barSizeSetting="5 mins", whatToShow="TRADES",
        useRTH=True, formatDate=1,
    )
    for bar in bars:
        if bar.date.astimezone(NL).strftime("%H:%M") == opening_time:
            return bar
    return None


def get_latest_closed_1min_bar(ib: IB, contract: Stock):
    """Return the most recently fully closed 1-min bar."""
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="1800 S",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=True, formatDate=1,
    )
    if len(bars) < 2:
        return None
    return bars[-2]


def get_rvol(ib: IB, contract: Stock, current_volume: float) -> float:
    """Intraday RVOL: current 1-min bar volume vs median of last 10 closed bars.

    Used at BREAKOUT DETECTION time (Phase 2 / orb_strategy.py).
    This is DIFFERENT from premarket RVOL (Phase 1 screener) — see
    get_premarket_volume_ibkr() and get_avg_premarket_volume_ibkr().
    Returns 1.0 on failure (neutral — passes the gate but logs clearly).
    """
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="1800 S",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=True, formatDate=1,
    )
    recent_vols = [b.volume for b in bars[-11:-1] if b.volume > 0]
    if not recent_vols:
        return 1.0
    import statistics
    avg = statistics.median(recent_vols)
    return current_volume / avg if avg > 0 else 1.0


def get_premarket_volume_ibkr(ib: IB, contract: Stock) -> int:
    """Sum of 1-min bar volumes from 04:00 to 09:30 ET today.

    Uses useRTH=False so premarket bars are included.
    Returns 0 on failure.
    """
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="23400 S",
            barSizeSetting="1 min", whatToShow="TRADES",
            useRTH=False, formatDate=1,
        )
        pm_open   = _dt.time(4, 0)
        pm_cutoff = _dt.time(9, 30)
        total = 0
        for bar in bars:
            t = bar.date.astimezone(_ET).time()
            if pm_open <= t < pm_cutoff:
                total += bar.volume
        return total
    except Exception as e:
        print(f"[ibkr_connector] get_premarket_volume_ibkr failed: {e}")
        return 0


def get_avg_premarket_volume_ibkr(
    ib:            IB,
    contract:      Stock,
    lookback_days: int = 20,
) -> float:
    """Average premarket volume (04:00–09:30 ET) over the last lookback_days trading days.

    Disk-cached per (symbol, date, lookback_days) so repeated screener runs on the
    same day skip the IBKR request entirely.
    """
    _RVOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today      = _dt.date.today().isoformat()
    cache_file = _RVOL_CACHE_DIR / f"{contract.symbol}_{today}_{lookback_days}d.json"

    if cache_file.exists():
        try:
            return float(_json.loads(cache_file.read_text())["avg_pm_vol"])
        except Exception:
            pass

    avg = _compute_avg_premarket_volume(ib, contract, lookback_days)

    try:
        cache_file.write_text(_json.dumps({"avg_pm_vol": avg}))
    except Exception:
        pass

    return avg


def _compute_avg_premarket_volume(
    ib:            IB,
    contract:      Stock,
    lookback_days: int,
) -> float:
    """Average premarket volume using data_loader's per-day cached CSV files.

    For each of the last lookback_days trading days, loads premarket bars from
    data/{symbol}/{date}_pm.csv (cached by load_premarket_session). Cache misses
    trigger a single-day IBKR fetch (useRTH=False) which is then saved, so
    subsequent screener runs need no live IBKR connection for the baseline.
    """
    import data_loader
    from datetime import timedelta

    today = _dt.date.today()
    days: list[_dt.date] = []
    d = today - timedelta(days=1)
    while len(days) < lookback_days:
        if d.weekday() < 5:   # Mon–Fri only
            days.append(d)
        d -= timedelta(days=1)

    daily_vols = []
    for day in days:
        bars = data_loader.load_premarket_session(contract.symbol, day, ib)
        if bars:
            vol = sum(b.volume for b in bars)
            if vol > 0:
                daily_vols.append(vol)

    if not daily_vols:
        return 0.0

    return sum(daily_vols) / len(daily_vols)


def place_bracket_order(ib: IB, contract: Stock, action: str, qty: int,
                        entry_price: float, take_profit: float, stop_loss: float):
    """Limit-entry bracket order. Returns (parent, tp, sl) Trade tuple."""
    bracket = ib.bracketOrder(action, qty, round(entry_price, 2),
                              round(take_profit, 2), round(stop_loss, 2))
    for order in bracket:
        order.tif = "DAY"
    return [ib.placeOrder(contract, order) for order in bracket]


def cancel_order(ib: IB, trade) -> None:
    try:
        ib.cancelOrder(trade.order)
    except Exception:
        pass


def close_position_at_market(ib: IB, contract: Stock, direction: str, qty: int):
    """Market order to flatten an open position immediately."""
    action = "SELL" if direction == "long" else "BUY"
    order  = MarketOrder(action, qty, tif="DAY")
    return ib.placeOrder(contract, order)
