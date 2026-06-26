"""
test_rvol.py — Test real premarket RVOL + optional AI analysis for one ticker.

Reuses:
  ibkr_connector  — connect, get_premarket_volume_ibkr, get_avg_premarket_volume_ibkr
  data_loader     — load_premarket_session (cached _pm.csv files)
  premarket_data  — fetch_market_context, enrich_ticker (with ib connection)
  day_orchestrator — _build_agents, _run_round1, _run_cross_pollination, _aggregate

Steps
─────
  1  IBKR connection
  2  Today's premarket volume  (useRTH=False, 04:00–09:30 ET)
  3  20-day avg premarket vol  (cached _pm.csv files; fetches from IBKR if missing)
  4  RVOL = today / avg20d
  5  Full enrich_ticker() — gap, ATR, news, EUR/USD, all prompt fields
  6  AI analysis (--ai flag) — 5 bulk calls × Claude + Mistral → LONG/SHORT/NOTHING

Usage
─────
  # RVOL + data enrichment only (fast, ~20 s):
  python test_rvol.py
  python test_rvol.py --ticker TSLA

  # RVOL + full AI analysis:
  python test_rvol.py --ticker NVDA --ai

  # Show every AI prompt and response:
  python test_rvol.py --ticker NVDA --ai --verbose
"""
import argparse
import os
import sys

import config

os.environ.setdefault("MISTRAL_API_KEY", getattr(config, "MISTRAL_API_KEY", ""))

import ibkr_connector
import data_loader
from premarket_data import fetch_market_context, enrich_ticker


def _section(n: int, title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  STEP {n}: {title}")
    print(f"{'─'*60}")


def run(ticker: str, run_ai: bool = False, verbose: bool = False) -> bool:

    print(f"\n{'='*60}")
    print(f"  RVOL Test — {ticker}")
    print(f"{'='*60}")

    # ── STEP 1: IBKR connection ──────────────────────────────────────────────
    _section(1, "IBKR connection")
    try:
        ib = ibkr_connector.connect()
        print(f"  Connected ✓  |  accounts: {ib.managedAccounts()}")
    except Exception as e:
        print(f"  FAIL: {e}")
        print(f"  → Is TWS/Gateway running on port {config.IBKR_PORT}?")
        return False

    contract = ibkr_connector.get_contract(ticker)
    ib.qualifyContracts(contract)
    print(f"  Contract: {contract.symbol}  conId={contract.conId}")

    # ── STEP 2: Today's premarket volume ────────────────────────────────────
    _section(2, "Today's premarket volume (04:00–09:30 ET, useRTH=False)")
    try:
        today_pm_vol = ibkr_connector.get_premarket_volume_ibkr(ib, contract)
        if today_pm_vol > 0:
            print(f"  Today's premarket volume: {today_pm_vol:,} shares")
        else:
            print(f"  Today's premarket volume: 0")
            print(f"  Note: Step 2 only returns data during US premarket hours")
            print(f"        (04:00–09:30 ET = 10:00–15:30 NL). Run then for live RVOL.")
    except Exception as e:
        print(f"  Error: {e}")
        today_pm_vol = 0

    # ── STEP 3: 20-day avg premarket volume ─────────────────────────────────
    _section(3, f"20-day avg premarket volume  (cached _pm.csv → IBKR on miss)")
    params = config.INTRADAY_PARAMS
    lookback = getattr(params, "rvol_lookback_days", 20)

    # Show which dates are already cached vs need to be fetched
    from datetime import date, timedelta
    today = date.today()
    days = []
    d = today - timedelta(days=1)
    while len(days) < lookback:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)

    cached_count = sum(
        1 for day in days
        if data_loader._pm_cache_path(ticker, day).exists()
    )
    print(f"  Lookback: {lookback} trading days")
    print(f"  Cache hits: {cached_count}/{lookback} days already have _pm.csv")
    if cached_count < lookback:
        print(f"  Fetching {lookback - cached_count} missing day(s) from IBKR...")

    try:
        avg_pm_vol = ibkr_connector.get_avg_premarket_volume_ibkr(
            ib, contract, lookback_days=lookback
        )
        print(f"  20-day avg premarket volume: {avg_pm_vol:,.0f} shares")
    except Exception as e:
        print(f"  Error: {e}")
        avg_pm_vol = 0.0

    # ── STEP 4: RVOL ────────────────────────────────────────────────────────
    _section(4, "RVOL = today / avg20d")
    if today_pm_vol > 0 and avg_pm_vol > 0:
        rvol = round(today_pm_vol / avg_pm_vol, 2)
        floor = getattr(params, "rvol_hard_floor", 1.5)
        full  = getattr(params, "rvol_full_conviction", 3.0)

        tier = (
            "BELOW FLOOR — hard NOTHING veto" if rvol < floor
            else "MID TIER — conviction capped at 60" if rvol < full
            else "IN PLAY — full conviction"
        )
        print(f"  RVOL: {rvol}x  →  {tier}")
        print(f"  (today {today_pm_vol:,} ÷ avg20d {avg_pm_vol:,.0f})")
    elif today_pm_vol == 0:
        print(f"  Today's premarket volume = 0 — market not open yet or premarket closed")
        print(f"  avg20d baseline: {avg_pm_vol:,.0f}")
        rvol = 0.0
    else:
        print(f"  avg20d baseline = 0 — no premarket history cached/available")
        rvol = 0.0

    # ── STEP 5: Full premarket data enrichment ───────────────────────────────
    _section(5, "Full premarket data enrichment (enrich_ticker)")
    try:
        eurusd  = ibkr_connector.get_eurusd_rate()
        mkt_ctx = fetch_market_context()
        data    = enrich_ticker(ticker, mkt_ctx, [], eurusd, params, ib=ib)

        print(f"  Prior close      : ${data.get('prior_close', 'n/a')}")
        print(f"  Premarket price  : ${data.get('premarket_price', 'n/a')}")
        print(f"  Gap              : {data.get('premarket_gap_pct', 'n/a')}%")
        print(f"  RVOL (enriched)  : {data.get('rvol_premarket', 'n/a')}x")
        print(f"  ATR14            : {data.get('atr14', 'n/a')}")
        print(f"  Float            : {data.get('float_shares', 'n/a')}")
        print(f"  Short %          : {data.get('short_pct_float', 'n/a')}%")
        print(f"  Sector           : {data.get('sector', 'n/a')}")
        print(f"  EUR/USD          : {eurusd:.4f}")
        print(f"  USD capital      : ${data.get('usd_capital', 'n/a')}")
        print(f"  Risk/trade       : ${data.get('risk_per_trade_usd', 'n/a')}")
        print(f"  SPY              : {mkt_ctx.get('spy_premarket_pct', 'n/a')}"
              f"   VIX: {mkt_ctx.get('vix', 'n/a')} ({mkt_ctx.get('vix_change', 'n/a')})")
        print(f"  Catalyst         : {data.get('catalyst_summary', 'n/a')[:80]}")
        enrichment_ok = True
    except Exception as e:
        import traceback
        print(f"  Error: {e}")
        if verbose:
            traceback.print_exc()
        data = {}
        enrichment_ok = False

    # ── STEP 6: AI analysis (optional) ──────────────────────────────────────
    if run_ai:
        _section(6, "AI analysis — 5 bulk calls × Claude + Mistral")
        if not enrichment_ok:
            print("  Skipped — premarket data unavailable.")
        else:
            try:
                from day_trading_prompts import rvol_conviction_cap, INTRADAY_PARAMS as _DTP
                from day_orchestrator import (
                    _build_agents, _run_round1,
                    _run_cross_pollination, _aggregate,
                )

                pm_rvol = float(data.get("rvol_premarket", 0))
                cap     = rvol_conviction_cap(pm_rvol, _DTP)

                if cap == 0:
                    print(f"  RVOL {pm_rvol:.1f}x below floor {_DTP['rvol_hard_floor']}x"
                          f" — NOTHING veto, skipping AI")
                else:
                    agents = _build_agents(verbose=verbose)
                    print(f"  Agents: {list(agents.keys())}  RVOL cap: {cap}")
                    print(f"  Running...")
                    round1  = _run_round1(data, agents)
                    revised = _run_cross_pollination(data, round1, agents)
                    agg     = _aggregate(round1, pm_rvol)

                    direction = agg["direction"]
                    net       = agg["net"]

                    cp_signals = [
                        r.get("revised_signal", direction)
                        for r in revised.values()
                        if "error" not in r
                    ]
                    if cp_signals and direction != "NOTHING" and all(s == "NOTHING" for s in cp_signals):
                        print(f"  Cross-poll override: all agents → NOTHING")
                        direction = "NOTHING"
                        net = 0.0

                    thr        = _DTP["direction_threshold"]
                    conviction = min(max(abs(net) - thr, 0) / thr, 1.0) if thr > 0 else 0.0
                    print(f"\n  ─────────────────────────────────")
                    print(f"  Result   : {direction}")
                    print(f"  Net score: {net:+.4f}")
                    print(f"  Conviction: {conviction:.0%}")
                    print(f"  ─────────────────────────────────")
            except Exception as e:
                import traceback
                print(f"  Error: {e}")
                if verbose:
                    traceback.print_exc()

    ib.disconnect()
    print(f"\n  Disconnected. Test complete.")
    return True


def main() -> None:
    p = argparse.ArgumentParser(
        description="Test premarket RVOL + optional AI analysis for one ticker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--ticker",   default="NVDA", help="Stock symbol (default: NVDA)")
    p.add_argument("--ai",       action="store_true", help="Run AI analysis after RVOL")
    p.add_argument("--verbose",  action="store_true", help="Print AI prompts and responses")
    args = p.parse_args()

    ok = run(ticker=args.ticker, run_ai=args.ai, verbose=args.verbose)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
