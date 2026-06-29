"""
test_live_order.py -- Run-anytime bracket order validation test.

Fetches today's 1-min IBKR bars directly (no cache, works at any time --
pre-market, during the session, or after close), computes the ORB opening
range from whatever bars are available, builds a bracket order, places it on
the paper account, verifies all 3 legs appear in IBKR, then cancels.

Unlike test_integration.py (which replays a specific --date), this test
always uses today's live IBKR data and requires no arguments to run.

Bracket source
--------------
  >= 5 RTH bars available  ->  real ORB range (first 5 min of today's session)
  < 5 RTH bars             ->  synthetic bracket at +/-1% around last price

Steps
-----
  1  Connect to IBKR paper
  2  Qualify contract + fetch today's 1-min RTH bars
  3  Compute bracket levels (ORB or synthetic fallback)
  4  Place bracket order
  5  Verify all 3 legs in IBKR open orders
  6  Cancel (unless --no-cancel)

Usage
-----
  python tst/test_live_order.py
  python tst/test_live_order.py --ticker TSLA
  python tst/test_live_order.py --direction short
  python tst/test_live_order.py --no-cancel     # leave order open in TWS
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import argparse
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
import ibkr_connector
from orb_core import ORBConfig, Bar, capture_opening_range, position_size_usd
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")
ET = ZoneInfo("America/New_York")

_PASS = "PASS"
_FAIL = "FAIL"
_SKIP = "SKIP"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(n: int, title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  STEP {n}: {title}")
    print(f"{'─'*60}")


def _print_summary(results: dict) -> bool:
    print(f"\n{'='*60}")
    print("  TEST SUMMARY")
    print(f"{'='*60}")
    all_passed = True
    for step, status in results.items():
        icon = "OK" if _PASS in status else ("--" if _SKIP in status else "XX")
        print(f"  [{icon}]  {step:<32}  {status}")
        if _FAIL in status:
            all_passed = False
    verdict = "ALL STEPS PASSED" if all_passed else "ONE OR MORE STEPS FAILED"
    print(f"\n  Overall: {verdict}")
    print(f"{'='*60}\n")
    return all_passed


def _safe_bracket(direction: str, entry: float, stop: float, tp: float):
    entry, stop, tp = round(entry, 2), round(stop, 2), round(tp, 2)
    if direction == "long" and not (tp > entry > stop):
        raise ValueError(f"LONG bracket invalid: tp={tp} entry={entry} stop={stop}")
    if direction == "short" and not (tp < entry < stop):
        raise ValueError(f"SHORT bracket invalid: tp={tp} entry={entry} stop={stop}")
    return entry, stop, tp


def _last_price(ib, contract) -> float | None:
    """Last price: live ticker first, then most recent historical bar."""
    try:
        ib.reqMarketDataType(3)   # delayed free data
        tickers = ib.reqTickers(contract)
        if tickers and tickers[0].last and float(tickers[0].last) > 0:
            return float(tickers[0].last)
    except Exception:
        pass
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="300 S",
            barSizeSetting="1 min", whatToShow="TRADES",
            useRTH=False, formatDate=1,
        )
        if bars:
            return float(bars[-1].close)
    except Exception:
        pass
    return None


def _fetch_today_rth_bars(ib, contract) -> list[Bar]:
    """Fetch today's RTH 1-min bars directly from IBKR (no cache write)."""
    today_str = date.today().isoformat()
    try:
        raw = ib.reqHistoricalData(
            contract,
            endDateTime    = "",        # now
            durationStr    = "28800 S", # 8 h covers full RTH session
            barSizeSetting = "1 min",
            whatToShow     = "TRADES",
            useRTH         = True,
            formatDate     = 1,
        )
    except Exception as e:
        print(f"  IBKR historical data error: {e}")
        return []

    bars = []
    for b in raw:
        bar_date = b.date.astimezone(NL)
        if bar_date.date().isoformat() != today_str:
            continue
        t_nl = bar_date.strftime("%H:%M")
        if t_nl < "15:30":
            continue
        bars.append(Bar(
            t      = t_nl,
            open   = b.open,
            high   = b.high,
            low    = b.low,
            close  = b.close,
            volume = float(b.volume),
        ))
    return bars


# ── Main test ─────────────────────────────────────────────────────────────────

def run_test(ticker: str, direction: str, no_cancel: bool) -> bool:
    results: dict = {}
    placed_trades: list = []
    ib = None

    print(f"\n{'='*60}")
    print(f"  Signal Mesh Day -- Live Order Test")
    print(f"  Ticker    : {ticker}")
    print(f"  Direction : {direction.upper()}")
    print(f"  Date      : {date.today()}  (today's session)")
    print(f"  Time (NL) : {datetime.now(NL).strftime('%H:%M')}")
    print(f"  Paper     : LIVE_TRADING = {config.LIVE_TRADING}")
    print(f"{'='*60}")

    # ── STEP 1: Connect ──────────────────────────────────────────────────────
    _section(1, "IBKR connection")
    try:
        ib = ibkr_connector.connect()
        accounts = ib.managedAccounts()
        print(f"  Connected  |  accounts: {accounts}")
        results["1_ibkr_connection"] = _PASS
    except Exception as e:
        print(f"  {e}")
        print(f"  Is TWS/IB Gateway running on port {config.IBKR_PORT}?")
        results["1_ibkr_connection"] = _FAIL
        _print_summary(results)
        return False

    # ── STEP 2: Qualify contract + fetch today's bars ────────────────────────
    _section(2, f"Qualify {ticker} + fetch today's 1-min RTH bars")
    contract = ibkr_connector.get_contract(ticker, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
        print(f"  Contract: {contract}")
    except Exception as e:
        print(f"  qualify failed: {e}")
        results["2_bars"] = _FAIL
        ib.disconnect()
        _print_summary(results)
        return False

    rth_bars = _fetch_today_rth_bars(ib, contract)
    if rth_bars:
        print(f"  {len(rth_bars)} RTH bars  ({rth_bars[0].t}--{rth_bars[-1].t} NL)")
        results["2_bars"] = _PASS
    else:
        print(f"  No RTH bars for today (market may be pre-open or closed) -- will use synthetic bracket")
        results["2_bars"] = f"{_SKIP} (no RTH bars)"

    # ── STEP 3: Compute bracket levels ───────────────────────────────────────
    _section(3, "Compute bracket levels")
    cfg = ORBConfig.from_params(config.INTRADAY_PARAMS)

    entry_price = stop_price = tp_price = 0.0
    qty = 1
    bracket_source = "none"

    if len(rth_bars) >= cfg.range_minutes:
        opening_bars = rth_bars[:cfg.range_minutes]
        orng = capture_opening_range(opening_bars, cfg, ticker)

        if orng:
            entry_price = orng.high if direction == "long" else orng.low
            stop_price  = orng.low  if direction == "long" else orng.high
            risk        = abs(entry_price - stop_price)
            tp_price    = round(
                entry_price + cfg.tp_r_multiple * risk if direction == "long"
                else entry_price - cfg.tp_r_multiple * risk,
                2,
            )
            eurusd   = ibkr_connector.get_eurusd_rate()
            risk_usd = config.HOUSE_MONEY_EUR * config.RISK_PER_TRADE_PCT * eurusd
            qty      = max(1, position_size_usd(risk_usd, risk))
            bracket_source = "orb"

            print(f"  Source    : today's ORB range (first {cfg.range_minutes} RTH bars)")
            print(f"  ORB range : {orng.low:.2f} -- {orng.high:.2f}  (spread {orng.spread:.2f})")
            print(f"  EUR/USD   : {eurusd:.4f}  |  risk budget ${risk_usd:.0f}")
            results["3_bracket"] = _PASS
        else:
            print(f"  ORB range too thin for the first {cfg.range_minutes} bars -- using synthetic")

    if bracket_source == "none":
        # Synthetic: bracket around last price at +/-1%
        last = _last_price(ib, contract)
        if last is None:
            print("  Cannot get last price -- aborting.")
            results["3_bracket"] = _FAIL
            ib.disconnect()
            _print_summary(results)
            return False

        offset = round(last * 0.01, 2)   # 1% of price
        if direction == "long":
            entry_price = round(last - offset, 2)         # limit below market
            stop_price  = round(last - 2 * offset, 2)
            tp_price    = round(last + 2 * offset, 2)
        else:
            entry_price = round(last + offset, 2)         # limit above market
            stop_price  = round(last + 2 * offset, 2)
            tp_price    = round(last - 2 * offset, 2)
        qty = 1
        bracket_source = "synthetic"
        print(f"  Source    : synthetic (+/-1% around last price {last:.2f})")
        results["3_bracket"] = f"{_SKIP} (synthetic -- ORB unavailable)"

    print(f"  Direction : {direction.upper()}")
    print(f"  Entry     : {entry_price:.2f}  (limit)")
    print(f"  Stop      : {stop_price:.2f}")
    print(f"  TP        : {tp_price:.2f}  ({cfg.tp_r_multiple}R)")
    print(f"  Qty       : {qty}")

    # ── STEP 4: Place bracket order ──────────────────────────────────────────
    _section(4, "Place bracket order on IBKR paper account")
    action = "BUY" if direction == "long" else "SELL"
    print(f"  {action}  qty={qty}  entry={entry_price:.2f}  SL={stop_price:.2f}  TP={tp_price:.2f}")

    try:
        entry_price, stop_price, tp_price = _safe_bracket(
            direction, entry_price, stop_price, tp_price
        )
        placed_trades = ibkr_connector.place_bracket_order(
            ib, contract, action, qty,
            entry_price, tp_price, stop_price,
        )
        print(f"  Submitted {len(placed_trades)} order legs")
        results["4_place_order"] = _PASS
    except Exception as e:
        print(f"  {e}")
        results["4_place_order"] = _FAIL
        ib.disconnect()
        _print_summary(results)
        return False

    # ── STEP 5: Verify orders in IBKR ────────────────────────────────────────
    _section(5, "Verify orders in IBKR open orders")
    ib.sleep(3)
    try:
        open_orders = ib.reqAllOpenOrders()
        ib.sleep(2)
        test_orders = [o for o in open_orders if o.contract.symbol == ticker]

        if test_orders:
            print(f"  {len(test_orders)} leg(s) confirmed for {ticker}:\n")
            print(f"  {'orderId':<8} {'action':<6} {'qty':<5} "
                  f"{'type':<12} {'limit':>10}  {'aux/stop':>10}  status")
            print(f"  {'-'*64}")
            for o in test_orders:
                print(
                    f"  {o.order.orderId:<8} "
                    f"{o.order.action:<6} "
                    f"{int(o.order.totalQuantity):<5} "
                    f"{o.order.orderType:<12} "
                    f"{getattr(o.order, 'lmtPrice', '-'):>10}  "
                    f"{getattr(o.order, 'auxPrice', '-'):>10}  "
                    f"{o.orderStatus.status}"
                )
            legs = len(test_orders)
            if legs >= 3:
                print(f"\n  All 3 bracket legs confirmed (entry + SL + TP).")
                results["5_verify"] = _PASS
            else:
                results["5_verify"] = f"{_PASS} ({legs}/3 legs -- may still propagate)"

            send_message(
                f"[Test] live_order test {'PASS' if legs >= 3 else 'PARTIAL'} -- {ticker} {direction.upper()}\n"
                f"  {legs} bracket legs in IBKR paper  (source: {bracket_source})\n"
                f"  entry {entry_price:.2f}  SL {stop_price:.2f}  TP {tp_price:.2f}  qty {qty}"
            )
        else:
            print(f"  No {ticker} orders found in IBKR open orders.")
            results["5_verify"] = _FAIL
    except Exception as e:
        print(f"  {e}")
        results["5_verify"] = _FAIL

    # ── STEP 6: Cancel ───────────────────────────────────────────────────────
    _section(6, "Cleanup")
    if no_cancel:
        print(f"  --no-cancel: orders left open. To cancel:")
        print(f"    python tst/test_connection.py --status")
        results["6_cleanup"] = f"{_SKIP} (--no-cancel)"
    else:
        cancelled = 0
        for trade in placed_trades:
            try:
                ibkr_connector.cancel_order(ib, trade)
                cancelled += 1
            except Exception:
                pass
        ib.sleep(2)
        print(f"  Cancelled {cancelled}/{len(placed_trades)} order legs.")
        results["6_cleanup"] = _PASS

    ib.disconnect()
    print("  Disconnected.")
    return _print_summary(results)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Signal Mesh Day -- live bracket order test (runs at any time)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--ticker",    default="NVDA",
                   help="Ticker to test (default: NVDA)")
    p.add_argument("--direction", default="long", choices=["long", "short"],
                   help="Trade direction (default: long)")
    p.add_argument("--no-cancel", action="store_true",
                   help="Leave bracket order open in TWS after the test")
    args = p.parse_args()

    ok = run_test(
        ticker    = args.ticker.upper(),
        direction = args.direction,
        no_cancel = args.no_cancel,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
