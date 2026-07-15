"""
ibkr_connector.py — the ONLY module that talks to IBKR.

Every strategy (lib/strategy_orb.py, strategy_ib.py, strategy_vwap.py) and
bin/live_engine.py must go through this module for anything IBKR-related:
connecting, contracts, historical bars, account data, and order placement.
No other file should import ib_async or call ib.reqHistoricalData directly.

Adapted from candle-scalping-bot/ibkr_connector.py.
Key additions vs the reference:
  - Thread-safe access model: ib_async's event loop runs on the owner (main)
    thread via pump_until()/run_threads_pumping(); worker threads MARSHAL their
    IBKR calls onto it (_submit/_call_ib). This is what makes the per-ticker
    threaded live engine actually work — a worker calling ib.* directly hangs.
  - get_historical_bars()  — single retried/auto-qualified fetch point.
  - get_eurusd_rate()  — live EUR→USD conversion for position sizing
  - get_contract() accepts a pre-qualified contract or builds one from symbol
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import asyncio as _asyncio
import datetime as _dt
import json as _json
import threading as _threading
import time as _time
from collections import defaultdict as _defaultdict
from pathlib import Path as _Path

import yfinance as yf
from zoneinfo import ZoneInfo

from ib_async import IB, Stock, LimitOrder, MarketOrder, StopOrder, util as _ibutil

import config

NL = ZoneInfo("Europe/Amsterdam")
_ET = ZoneInfo("America/New_York")

# Disk cache for avg premarket volume (keyed by symbol + date + lookback days)
from setup_paths import RVOL_CACHE_DIR as _RVOL_CACHE_DIR

# ── ib_async threading model ──────────────────────────────────────────────────
# ib_async drives ONE asyncio event loop, and the TWS socket reader lives on that
# loop. Only the thread that owns the loop may run it. The live engine spawns one
# thread per ticker; a worker thread that calls a sync ib method (reqHistoricalData,
# qualifyContracts, ...) tries to run the loop itself while the socket's responses
# are serviced on the OWNER loop — so the call blocks forever. That is exactly what
# wedged the 2026-07-14 session (and why every prior session silently got empty
# bars: reqHistoricalData timed out to [], qualifyContracts — no timeout — hung).
#
# Correct pattern: the loop keeps running on the OWNER thread (the main thread,
# via pump_until()), and worker threads MARSHAL their IB calls onto it with
# asyncio.run_coroutine_threadsafe(). _submit() / _call_ib() below do that; on the
# owner thread they just call through directly (screener / pre-worker / post-join).
_LOOP = None                       # the ib_async event loop (set on connect)
_LOOP_OWNER_IDENT: int | None = None


def _on_loop_thread() -> bool:
    """True if the current thread owns/runs the ib_async loop (or none set yet)."""
    return _LOOP is None or _threading.get_ident() == _LOOP_OWNER_IDENT


def _submit(coro, timeout: float):
    """Run an ib_async *coroutine* on the owner loop and return its result.

    Owner thread: run directly. Worker thread: marshal via run_coroutine_threadsafe
    (the owner thread must be pumping the loop, e.g. via pump_until)."""
    if _on_loop_thread():
        return _ibutil.run(coro)
    fut = _asyncio.run_coroutine_threadsafe(coro, _LOOP)
    try:
        return fut.result(timeout=timeout)
    except Exception:
        fut.cancel()
        raise


def _call_ib(fn, timeout: float = 30):
    """Run a *sync* callable that touches ib on the owner loop thread.

    Used for non-coroutine ib ops (placeOrder, cancelOrder, positions, ...) so
    they execute on the loop-owner thread instead of a worker thread."""
    if _on_loop_thread():
        return fn()

    async def _wrap():
        return fn()

    fut = _asyncio.run_coroutine_threadsafe(_wrap(), _LOOP)
    try:
        return fut.result(timeout=timeout)
    except Exception:
        fut.cancel()
        raise


def pump(ib: IB, seconds: float) -> None:
    """Advance `seconds` of time. On the loop-owner thread this pumps the event
    loop (ib.sleep); on a worker thread it is a plain sleep (the owner thread
    keeps the loop running, so fills/data still arrive)."""
    if _on_loop_thread():
        ib.sleep(seconds)
    else:
        _time.sleep(seconds)


def pump_until(ib: IB, is_done, tick: float = 0.5) -> None:
    """Run the ib_async loop on the CURRENT (owner) thread until is_done() is True.

    The live engine / test harness call this on the main thread while worker
    threads run, so worker IB calls marshalled via _submit()/_call_ib() are
    serviced. Returns once is_done() returns True."""
    while not is_done():
        ib.sleep(tick)


def run_threads_pumping(ib: IB, threads: list) -> None:
    """Start worker threads and keep the ib loop running on this (owner) thread
    until they all finish. Replaces `for t: t.start(); for t: t.join()` — a bare
    join() would block the owner thread and starve the loop, hanging every
    worker's marshalled IB call."""
    done = _threading.Event()

    def _joiner():
        for t in threads:
            t.join()
        done.set()

    for t in threads:
        t.start()
    _threading.Thread(target=_joiner, name="pump-joiner", daemon=True).start()
    pump_until(ib, done.is_set)


def connect(client_id: int | None = None) -> IB:
    """Connect to IBKR TWS/Gateway.

    client_id defaults to config.IBKR_CLIENT_ID (the live engine). The premarket
    screener passes config.IBKR_CLIENT_ID_SCREENER so an overrunning screener and
    the live engine never collide on the same client id (which IBKR rejects).

    The calling thread becomes the loop owner — it must be the thread that later
    runs pump_until()/run_threads_pumping() while worker threads marshal onto it.
    """
    global _LOOP, _LOOP_OWNER_IDENT
    cid = config.IBKR_CLIENT_ID if client_id is None else client_id
    ib  = IB()
    ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=cid)
    _LOOP = _ibutil.getLoop()
    _LOOP_OWNER_IDENT = _threading.get_ident()
    return ib


def ensure_connected(ib: IB, client_id: int | None = None, attempts: int = 3) -> bool:
    """Reconnect a dropped IBKR session in place (owner thread only).

    Returns True if connected. Reconnecting drives the loop, so it is only
    attempted on the loop-owner thread; a worker that finds the session down
    returns False and skips this cycle rather than corrupting the loop.
    """
    if ib.isConnected():
        return True
    if not _on_loop_thread():
        return False
    cid = config.IBKR_CLIENT_ID if client_id is None else client_id
    for attempt in range(attempts):
        try:
            ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=cid)
            if ib.isConnected():
                print(f"[ibkr_connector] reconnected (attempt {attempt+1}/{attempts})")
                return True
        except Exception as e:
            print(f"[ibkr_connector] reconnect attempt {attempt+1}/{attempts} failed: {e}")
        _time.sleep(2 ** attempt)
    return False


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
        rows = _call_ib(lambda: ib.accountSummary())
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


def get_historical_bars(
    ib:            IB,
    contract:      Stock,
    duration_str:  str,
    bar_size:      str,
    use_rth:       bool           = True,
    end_date_time: str            = "",   # "" = now; or "YYYYMMDD HH:MM:SS" for a specific date
    retries:       int | None     = None,
    retry_delay:   float | None   = None,
):
    """The ONE place every strategy/helper fetches historical bars from.

    Qualifies the contract (skipped if already qualified), then fetches
    under `_HIST_LOCK` so concurrent per-ticker threads never overlap their
    reqHistoricalData calls — IBKR pacing rules return Error 366 / timeouts
    for overlapping requests, regardless of which strategy is running.
    Retries on an empty result (IBKR sometimes returns [] transiently while
    a subscription warms up) before giving up.

    Returns the raw ib_async BarDataList, or [] if every attempt was empty.
    """
    retries     = config.IBKR_HIST_RETRIES      if retries     is None else retries
    retry_delay = config.IBKR_HIST_RETRY_DELAY_SEC if retry_delay is None else retry_delay

    if not ensure_connected(ib):
        print(f"[ibkr_connector] get_historical_bars({contract.symbol}): not connected")
        return []

    async def _fetch():
        # Runs ON the loop-owner thread (directly on the owner, marshalled from a
        # worker). qualify + fetch as one coroutine so a worker makes a single hop.
        if not contract.conId:
            try:
                await ib.qualifyContractsAsync(contract)
            except Exception as e:
                print(f"[ibkr_connector] qualifyContracts({contract.symbol}) failed: {e}")
        return await ib.reqHistoricalDataAsync(
            contract, endDateTime=end_date_time,
            durationStr=duration_str, barSizeSetting=bar_size,
            whatToShow="TRADES", useRTH=use_rth, formatDate=1,
        )

    for attempt in range(retries):
        try:
            bars = _submit(_fetch(), timeout=60)
        except Exception as e:
            print(f"[ibkr_connector] reqHistoricalData({contract.symbol}) "
                  f"attempt {attempt+1}/{retries} EXCEPTION: {type(e).__name__}: {e}")
            bars = []
        if bars:
            return bars
        if attempt < retries - 1:
            _time.sleep(retry_delay)
    return []


# Fast-poll helpers below use retries=1: inside a 60s poll loop an empty result
# just means "no new bar yet", so we must return immediately instead of blocking
# the thread for the bulk-fetch default (IBKR_HIST_RETRIES × IBKR_HIST_RETRY_DELAY_SEC).
# Only the IB 60-min range bulk fetch (strategy_ib) wants the aggressive retry.
_POLL_RETRIES = 1


def get_opening_range_bar(ib: IB, contract: Stock, opening_time: str):
    """Return the 5-min bar that starts at opening_time (NL local HH:MM), or None."""
    bars = get_historical_bars(ib, contract, "3600 S", "5 mins", use_rth=True,
                               retries=_POLL_RETRIES)
    for bar in bars:
        if bar.date.astimezone(NL).strftime("%H:%M") == opening_time:
            return bar
    return None


def get_latest_closed_1min_bar(ib: IB, contract: Stock):
    """Return the most recently fully closed 1-min bar."""
    bars = get_historical_bars(ib, contract, "1800 S", "1 min", use_rth=True,
                               retries=_POLL_RETRIES)
    if len(bars) < 2:
        return None
    return bars[-2]


def get_rvol(ib: IB, contract: Stock, current_volume: float) -> float:
    """Intraday RVOL: current 1-min bar volume vs median of last 10 closed bars.

    Used at BREAKOUT DETECTION time by the live strategies.
    This is DIFFERENT from premarket RVOL (Phase 1 screener) — see
    get_premarket_volume_ibkr() and get_avg_premarket_volume_ibkr().
    Returns 1.0 on failure (neutral — passes the gate but logs clearly).
    """
    bars = get_historical_bars(ib, contract, "1800 S", "1 min", use_rth=True,
                               retries=_POLL_RETRIES)
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
        bars = get_historical_bars(ib, contract, "23400 S", "1 min", use_rth=False)
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
    def _place():
        bracket = ib.bracketOrder(action, qty, round(entry_price, 2),
                                  round(take_profit, 2), round(stop_loss, 2))
        for order in bracket:
            order.tif = "DAY"
        return [ib.placeOrder(contract, order) for order in bracket]
    return _call_ib(_place)


def cancel_order(ib: IB, trade) -> None:
    try:
        _call_ib(lambda: ib.cancelOrder(trade.order))
    except Exception:
        pass


def close_position_at_market(ib: IB, contract: Stock, direction: str, qty: int):
    """Market order to flatten an open position immediately."""
    action = "SELL" if direction == "long" else "BUY"
    order  = MarketOrder(action, qty, tif="DAY")
    return _call_ib(lambda: ib.placeOrder(contract, order))


def positions(ib: IB) -> list:
    """ib.positions() executed on the loop-owner thread."""
    return _call_ib(lambda: ib.positions())


def open_trades(ib: IB) -> list:
    """ib.openTrades() executed on the loop-owner thread."""
    return _call_ib(lambda: ib.openTrades())
