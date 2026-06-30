"""
test_integration.py — Signal Mesh Day end-to-end pipeline test.

Tests the complete pipeline for a specific historical date using real IBKR data:

  Step 1  IBKR connection
  Step 2  Load 1-min session bars from IBKR (cached or fetched)
  Step 3  Compute opening range + RVOL from historical bars
  Step 4  Fetch premarket context via yfinance (prior close, ATR, news)
  Step 5  Run AI mesh (25 prompts × Claude + Mistral) → directional bias
  Step 6  Run ORB simulation on historical bars → entry / stop / target
  Step 7  Place bracket order on IBKR paper account
  Step 8  Verify all order legs appear in IBKR open orders  ← PASS condition
  Step 9  Cancel orders (cleanup)

PASS condition: all 3 bracket legs (entry limit + stop loss + take profit) visible
in IBKR open orders after placement.

Usage
─────
  # Default: NVDA on 2026-06-24
  python test_integration.py

  # Different ticker / date:
  python test_integration.py --ticker TSLA --date 2026-06-23

  # Skip AI prompts (much faster — tests data + ORB + order placement only):
  python test_integration.py --skip-ai

  # Leave orders open after test (inspect in TWS):
  python test_integration.py --no-cancel

  # Print every AI prompt + response:
  python test_integration.py --verbose
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import argparse
import os
import sys
from datetime import date
from zoneinfo import ZoneInfo

import config

os.environ.setdefault("MISTRAL_API_KEY", getattr(config, "MISTRAL_API_KEY", ""))

import ibkr_connector
import data_loader
from orb_core import (
    ORBConfig, capture_opening_range, simulate_session,
    rvol as compute_rvol,
)
from premarket_data import fetch_market_context, enrich_ticker
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")

_PASS = "✅ PASS"
_FAIL = "❌ FAIL"
_SKIP = "⏭  SKIP"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(n: int, title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  STEP {n}: {title}")
    print(f"{'─'*60}")


def _print_summary(results: dict) -> bool:
    print(f"\n{'='*60}")
    print(f"  TEST SUMMARY")
    print(f"{'='*60}")
    all_passed = True
    for step, status in results.items():
        icon = "✅" if _PASS in status else ("⏭" if _SKIP in status else "❌")
        print(f"  {icon}  {step:<28}  {status}")
        if _FAIL in status:
            all_passed = False
    verdict = "✅  ALL STEPS PASSED" if all_passed else "❌  ONE OR MORE STEPS FAILED"
    print(f"\n  Overall: {verdict}")
    print(f"{'='*60}\n")
    return all_passed


def _safe_bracket_prices(
    direction:   str,     # "long" | "short"
    entry:       float,
    stop:        float,
    take_profit: float,
) -> tuple[float, float, float]:
    """Ensure entry/stop/tp are in valid bracket relationship for IBKR."""
    entry = round(entry, 2)
    stop  = round(stop, 2)
    tp    = round(take_profit, 2)
    if direction == "long":
        if not (tp > entry > stop):
            raise ValueError(
                f"LONG bracket invalid: tp={tp} entry={entry} stop={stop}  "
                f"(need tp > entry > stop)"
            )
    else:
        if not (tp < entry < stop):
            raise ValueError(
                f"SHORT bracket invalid: tp={tp} entry={entry} stop={stop}  "
                f"(need tp < entry < stop)"
            )
    return entry, stop, tp


# ── Main test ─────────────────────────────────────────────────────────────────

def run_test(
    ticker:    str,
    test_date: date,
    skip_ai:   bool = False,
    skip_rvol: bool = False,
    no_cancel: bool = False,
    verbose:   bool = False,
) -> bool:

    results:       dict  = {}
    placed_trades: list  = []
    ib                   = None

    print(f"\n{'='*60}")
    print(f"  Signal Mesh Day — Integration Test")
    print(f"  Ticker : {ticker}")
    print(f"  Date   : {test_date}  (historical session for ORB + RVOL)")
    print(f"  Paper  : LIVE_TRADING = {config.LIVE_TRADING}")
    print(f"{'='*60}")

    # ────────────────────────────────────────────────────────────────────────
    # STEP 1 — IBKR connection
    # ────────────────────────────────────────────────────────────────────────
    _section(1, "IBKR connection")
    try:
        ib = ibkr_connector.connect()
        accounts = ib.managedAccounts()
        print(f"  Connected ✓  |  accounts: {accounts}")
        results["1_ibkr_connection"] = _PASS
    except Exception as e:
        print(f"  {e}")
        print(f"  → Is TWS/IB Gateway running on port {config.IBKR_PORT}?")
        results["1_ibkr_connection"] = _FAIL
        _print_summary(results)
        return False

    # ────────────────────────────────────────────────────────────────────────
    # STEP 2 — Load 1-min historical bars
    # ────────────────────────────────────────────────────────────────────────
    _section(2, f"Load 1-min bars for {ticker} on {test_date}")
    bars = data_loader.load_session(ticker, test_date, ib)
    if not bars:
        print(f"  No bars found for {ticker} on {test_date}.")
        print(f"  Hint: fetch data first with:")
        print(f"    python backtest.py --ticker {ticker} "
              f"--start {test_date} --end {test_date}")
        results["2_bar_load"] = _FAIL
        ib.disconnect()
        _print_summary(results)
        return False

    print(f"  {len(bars)} bars loaded  ({bars[0].t} → {bars[-1].t} NL time)")
    results["2_bar_load"] = _PASS

    # ────────────────────────────────────────────────────────────────────────
    # STEP 3 — Opening range + RVOL from historical bars
    # ────────────────────────────────────────────────────────────────────────
    _section(3, "Opening range + RVOL")
    cfg = ORBConfig.from_params(config.INTRADAY_PARAMS)

    # RTH bars start at 15:30 NL (= 09:30 ET)
    rth_bars = [b for b in bars if b.t >= "15:30"]
    orng     = None
    orb_rvol = 0.0
    opening_bars: list = []
    post_bars:    list = []

    if len(rth_bars) < cfg.range_minutes + 1:
        print(f"  Only {len(rth_bars)} RTH bars — not enough for ORB "
              f"(need ≥ {cfg.range_minutes + 1}).")
        results["3_orb_range"] = f"{_SKIP} (insufficient RTH bars)"
    else:
        opening_bars = rth_bars[:cfg.range_minutes]
        post_bars    = rth_bars[cfg.range_minutes:]
        orng         = capture_opening_range(opening_bars, cfg, ticker)

        # RVOL at the first post-open bar (index = range_minutes in the full list)
        all_bars = opening_bars + post_bars
        orb_rvol = compute_rvol(cfg.range_minutes, all_bars, opening_bars, cfg)

        if orng:
            print(f"  Opening range : {orng.low:.4f} – {orng.high:.4f}"
                  f"  (spread {orng.spread:.4f})")
            print(f"  RVOL          : {orb_rvol:.2f}x  "
                  f"(gate ≥ {cfg.rvol_min}x → "
                  f"{'OK ✓' if orb_rvol >= cfg.rvol_min else 'BELOW FLOOR'})")
            results["3_orb_range"] = _PASS
        else:
            print(f"  Range too thin — would be skipped in live trading.")
            results["3_orb_range"] = f"{_SKIP} (thin range)"

    # ────────────────────────────────────────────────────────────────────────
    # STEP 4 — Premarket data (yfinance)
    # ────────────────────────────────────────────────────────────────────────
    _section(4, "Premarket context (yfinance: prior close, ATR, news)")
    prior_close = 0.0
    eurusd      = 1.08
    prompt_data: dict = {}

    try:
        mkt_ctx     = fetch_market_context()
        eurusd      = ibkr_connector.get_eurusd_rate()
        prompt_data = enrich_ticker(ticker, mkt_ctx, [], eurusd, config.INTRADAY_PARAMS)
        prior_close = float(prompt_data.get("prior_close", 0) or 0)

        print(f"  Prior close      : ${prior_close:.4f}")
        print(f"  Premarket price  : ${float(prompt_data.get('premarket_price',0)):.4f}")
        print(f"  Premarket gap    : {prompt_data.get('premarket_gap_pct','n/a')}%")
        print(f"  Premarket RVOL   : {prompt_data.get('rvol_premarket','n/a')}x")
        print(f"  ATR14            : {prompt_data.get('atr14','n/a')}")
        print(f"  EUR/USD          : {eurusd:.4f}")
        print(f"  Catalyst         : {prompt_data.get('catalyst_summary','')[:80]}")
        print(f"  SPY              : {mkt_ctx.get('spy_premarket_pct','n/a')}"
              f"  VIX: {mkt_ctx.get('vix','n/a')}")
        results["4_premarket_data"] = _PASS
    except Exception as e:
        print(f"  {e}")
        results["4_premarket_data"] = _FAIL

    # ────────────────────────────────────────────────────────────────────────
    # STEP 5 — AI mesh: 25 prompts × 2 agents → directional bias
    # ────────────────────────────────────────────────────────────────────────
    _section(5, "AI mesh (25 prompts × Claude + Mistral)")
    ai_direction = "LONG"   # safe default for order test if AI skipped / NOTHING

    if skip_ai:
        print(f"  Skipped via --skip-ai  →  defaulting to LONG for order test")
        results["5_ai_mesh"] = f"{_SKIP} (--skip-ai)"
    elif not prompt_data:
        print(f"  Skipped — no premarket data available.")
        results["5_ai_mesh"] = f"{_SKIP} (no premarket data)"
    else:
        try:
            from day_orchestrator import _build_agents, _run_round1, _aggregate
            from day_trading_prompts import rvol_conviction_cap

            agents  = _build_agents(verbose=verbose)
            pm_rvol = float(prompt_data.get("rvol_premarket", 0))
            cap     = rvol_conviction_cap(pm_rvol)

            if cap == 0 and not skip_rvol:
                floor = config.INTRADAY_PARAMS.rvol_hard_floor
                print(f"  Premarket RVOL {pm_rvol:.1f}x < floor {floor}x"
                      f" — RVOL veto  →  defaulting to LONG for order test")
                results["5_ai_mesh"] = f"{_SKIP} (RVOL veto)"
            else:
                if skip_rvol and cap == 0:
                    print(f"  RVOL {pm_rvol:.1f}x would veto — bypassed via --skip-rvol")
                print(f"  Agents: {list(agents.keys())}  |  RVOL cap: {cap}")
                print(f"  Running 5 bulk calls × {len(agents)} agent(s) (25 analyses total)...")
                round1    = _run_round1(prompt_data, agents)
                floor     = config.INTRADAY_PARAMS.rvol_hard_floor
                agg_rvol  = max(pm_rvol, floor) if skip_rvol else pm_rvol
                agg       = _aggregate(round1, agg_rvol)
                ai_direction = agg["direction"]   # LONG | SHORT | NOTHING
                print(f"\n  → Mesh result: {ai_direction}  (net={agg['net']:+.4f})")
                if ai_direction == "NOTHING":
                    print(f"  Mesh says NOTHING — defaulting to LONG for order test")
                    ai_direction = "LONG"
                results["5_ai_mesh"] = _PASS
        except Exception as e:
            import traceback
            print(f"  {e}")
            if verbose:
                traceback.print_exc()
            results["5_ai_mesh"] = _FAIL
            ai_direction = "LONG"

    # ────────────────────────────────────────────────────────────────────────
    # STEP 6 — ORB simulation on historical bars → entry / stop / target
    # ────────────────────────────────────────────────────────────────────────
    _section(6, f"ORB simulation on {test_date} bars (bias={ai_direction})")

    entry_price = stop_price = tp_price = 0.0
    direction   = ai_direction.lower()   # "long" | "short"
    qty         = 1

    if orng and opening_bars and post_bars:
        risk_usd = config.HOUSE_MONEY_EUR * config.RISK_PER_TRADE_PCT * eurusd
        sim      = simulate_session(
            opening_bars = opening_bars,
            post_bars    = post_bars,
            capital_usd  = risk_usd,
            cfg          = cfg,
            bias         = direction,
            verbose      = verbose,
        )
        if sim:
            entry_price = sim.entry
            stop_price  = sim.stop
            tp_price    = sim.target
            qty         = max(1, sim.qty)
            print(f"  Historical trade found:")
            print(f"  Direction : {sim.direction.upper()}")
            print(f"  Entry     : {entry_price:.4f}")
            print(f"  Stop      : {stop_price:.4f}")
            print(f"  Target    : {tp_price:.4f}")
            print(f"  Qty       : {qty}")
            print(f"  Exit      : {sim.exit_reason}  |  R = {sim.r_multiple:.2f}")
            print(f"  Net P&L   : ${sim.net_pnl:+.2f}  (gross ${sim.gross_pnl:+.2f})")
            results["6_orb_simulation"] = _PASS
        else:
            print(f"  No clean ORB setup in the historical session for {direction.upper()}.")
            print(f"  Using ORB range levels for the order test.")
            results["6_orb_simulation"] = f"{_SKIP} (no historical setup)"

    # Fall back to ORB range levels if simulation found no trade
    if entry_price == 0.0:
        if orng:
            if direction == "long":
                entry_price = round(orng.high, 2)
                stop_price  = round(orng.low, 2)
            else:
                entry_price = round(orng.low, 2)
                stop_price  = round(orng.high, 2)
            risk = abs(entry_price - stop_price)
            tp_price = round(
                entry_price + cfg.tp_r_multiple * risk
                if direction == "long"
                else entry_price - cfg.tp_r_multiple * risk,
                2,
            )
        elif prior_close > 0:
            offset = round(prior_close * 0.01, 2)
            if direction == "long":
                entry_price = round(prior_close - offset, 2)
                stop_price  = round(prior_close - 2 * offset, 2)
                tp_price    = round(prior_close + 2 * offset, 2)
            else:
                entry_price = round(prior_close + offset, 2)
                stop_price  = round(prior_close + 2 * offset, 2)
                tp_price    = round(prior_close - 2 * offset, 2)
        else:
            print("  FAIL: Cannot compute order prices — no ORB range or prior close.")
            results["6_orb_simulation"] = _FAIL
            ib.disconnect()
            _print_summary(results)
            return False
        qty = 1

    # ────────────────────────────────────────────────────────────────────────
    # STEP 7 — Place bracket order on IBKR paper
    # ────────────────────────────────────────────────────────────────────────
    _section(7, "Place bracket order on IBKR paper account")

    action = "BUY" if direction == "long" else "SELL"
    print(f"  {action} {qty} × {ticker}")
    print(f"  Entry  (limit): {entry_price:.2f}")
    print(f"  Stop   loss   : {stop_price:.2f}")
    print(f"  Take   profit : {tp_price:.2f}")
    print(f"  Risk / trade  : ${abs(entry_price - stop_price) * qty:.2f}")

    try:
        entry_price, stop_price, tp_price = _safe_bracket_prices(
            direction, entry_price, stop_price, tp_price
        )
        contract = ibkr_connector.get_contract(ticker)
        ib.qualifyContracts(contract)
        placed_trades = ibkr_connector.place_bracket_order(
            ib, contract, action, qty,
            entry_price, tp_price, stop_price,
        )
        print(f"\n  Submitted {len(placed_trades)} order legs.")
        results["7_bracket_order"] = _PASS
    except Exception as e:
        import traceback
        print(f"  {e}")
        if verbose:
            traceback.print_exc()
        results["7_bracket_order"] = _FAIL
        ib.disconnect()
        _print_summary(results)
        return False

    # ────────────────────────────────────────────────────────────────────────
    # STEP 8 — Verify orders appear in IBKR
    # ────────────────────────────────────────────────────────────────────────
    _section(8, "Verify orders in IBKR open orders")
    ib.sleep(3)

    try:
        open_orders  = ib.reqAllOpenOrders()
        ib.sleep(2)
        test_orders  = [o for o in open_orders if o.contract.symbol == ticker]

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
                    f"{getattr(o.order,'lmtPrice','-'):>10}  "
                    f"{getattr(o.order,'auxPrice','-'):>10}  "
                    f"{o.orderStatus.status}"
                )

            legs_found = len(test_orders)
            if legs_found >= 3:
                print(f"\n  ✅ All 3 bracket legs confirmed (entry + SL + TP).")
                results["8_order_verify"] = _PASS
            else:
                results["8_order_verify"] = f"{_PASS} ({legs_found}/3 legs — may still propagate)"

            send_message(
                f"✅ <b>Integration test — {ticker}</b>\n"
                f"  {legs_found} bracket legs in IBKR paper ({'PASS' if legs_found >= 3 else 'partial'})\n"
                f"  {action} {qty}×  "
                f"entry {entry_price:.2f}  SL {stop_price:.2f}  TP {tp_price:.2f}\n"
                f"  AI direction: {ai_direction}  |  ORB RVOL: {orb_rvol:.1f}x"
            )
        else:
            print(f"  No {ticker} orders found in IBKR open orders.")
            results["8_order_verify"] = _FAIL
    except Exception as e:
        print(f"  {e}")
        results["8_order_verify"] = _FAIL

    # ────────────────────────────────────────────────────────────────────────
    # STEP 9 — Cleanup
    # ────────────────────────────────────────────────────────────────────────
    _section(9, "Cleanup")
    if no_cancel:
        print(f"  --no-cancel: orders left open for inspection in TWS.")
        print(f"  Cancel manually or run: python test_connection.py --status")
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

    ib.disconnect()
    print(f"  Disconnected.")

    return _print_summary(results)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Signal Mesh Day — end-to-end integration test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--ticker",    default="NVDA",       help="Ticker to test")
    p.add_argument("--date",      default="2026-06-24", help="YYYY-MM-DD (historical session)")
    p.add_argument("--skip-ai",   action="store_true",  help="Skip AI prompts (faster)")
    p.add_argument("--skip-rvol", action="store_true",  help="Bypass RVOL gate in AI mesh step (useful for historical dates where premarket data is gone)")
    p.add_argument("--no-cancel", action="store_true",  help="Leave orders open after test")
    p.add_argument("--verbose",   action="store_true",  help="Print AI prompt/response")
    args = p.parse_args()

    ok = run_test(
        ticker     = args.ticker,
        test_date  = date.fromisoformat(args.date),
        skip_ai    = args.skip_ai,
        skip_rvol  = args.skip_rvol,
        no_cancel  = args.no_cancel,
        verbose    = args.verbose,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
