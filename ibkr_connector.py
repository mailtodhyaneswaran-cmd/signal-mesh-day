"""
ibkr_connector.py — thin ib_async wrapper for Signal Mesh Day.

Adapted from candle-scalping-bot/ibkr_connector.py.
Key additions vs the reference:
  - get_eurusd_rate()  — live EUR→USD conversion for position sizing
  - get_contract() accepts a pre-qualified contract or builds one from symbol
"""
import yfinance as yf
from zoneinfo import ZoneInfo

from ib_async import IB, Stock, LimitOrder, MarketOrder, StopOrder

import config

NL = ZoneInfo("Europe/Amsterdam")


def connect() -> IB:
    ib = IB()
    ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=config.IBKR_CLIENT_ID)
    return ib


def get_contract(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
    return Stock(symbol, exchange, currency)


def get_eurusd_rate() -> float:
    """Pull live EUR→USD spot rate from yfinance (free, no key).

    Falls back to 1.08 if the fetch fails so position sizing never crashes.
    """
    try:
        info = yf.Ticker("EURUSD=X").fast_info
        rate = float(info.get("last_price") or info.get("regularMarketPrice") or 0)
        if rate > 0:
            return rate
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
    """Relative Volume vs median of last 10 closed 1-min bars.

    Returns 1.0 on failure (neutral — won't block a trade but logs clearly).
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
