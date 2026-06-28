"""
test_scenario.py — Regime-aware scenario backtest.

For each trading day in the date range, this test:
  1. Loads cached premarket bars → computes today's RVOL vs 20-day avg
  2. Loads cached RTH bars → computes gap (first bar vs prior close)
  3. Builds market context (gap, RVOL; SPY/VIX optional via yfinance)
  4. Runs regime scoring → picks the best strategy for that day
  5. Simulates ALL 3 strategies (ORB / IB / VWAP) + the regime-picked one
  6. Prints a comparison table and totals

This answers the key question:
  "Does automatically picking the strategy based on market conditions
   outperform always running ORB?"

Uses cached data only (--no-ibkr flag on backtest; all data in data/).

Usage
─────
  # NVDA — full cached range:
  python test_scenario.py --ticker NVDA --start 2026-06-05 --end 2026-06-24

  # Other cached tickers:
  python test_scenario.py --ticker MU   --start 2026-06-09 --end 2026-06-24
  python test_scenario.py --ticker GLW  --start 2026-06-09 --end 2026-06-24
  python test_scenario.py --ticker AMAT --start 2026-06-09 --end 2026-06-24
  python test_scenario.py --ticker SNDK --start 2026-06-09 --end 2026-06-24

  # Verbose: show per-bar trace for the regime-picked strategy:
  python test_scenario.py --ticker NVDA --start 2026-06-10 --end 2026-06-12 --verbose

  # Include live SPY/VIX from yfinance in regime scoring (historical):
  python test_scenario.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --live-context
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import config
import data_loader
from orb_core import ORBConfig
from regime import pick_strategy, RegimeScore
from backtest import (
    _trading_days, _risk_usd, _run_session,
    _result_to_dict, compute_metrics, _save_trades_csv, _mechanical_bias,
)


# ── RVOL from cached premarket files ─────────────────────────────────────────

def _historical_rvol(
    ticker:       str,
    test_date:    date,
    lookback:     int = 20,
) -> tuple[int, float, float]:
    """Compute RVOL for test_date from cached _pm.csv files (no IBKR needed).

    Returns (today_pm_vol, avg_20d_pm_vol, rvol).
    RVOL = 0 when cache has insufficient data (reported in the table).
    """
    today_bars = data_loader.load_premarket_session(ticker, test_date)
    today_vol  = int(sum(b.volume for b in today_bars))

    days: list[date] = []
    d = test_date - timedelta(days=1)
    while len(days) < lookback:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)

    daily_vols = []
    for day in days:
        bars = data_loader.load_premarket_session(ticker, day)  # cache only
        if bars:
            vol = sum(b.volume for b in bars)
            if vol > 0:
                daily_vols.append(vol)

    avg_vol = sum(daily_vols) / len(daily_vols) if daily_vols else 0.0
    rvol    = round(today_vol / avg_vol, 2) if avg_vol > 0 and today_vol > 0 else 0.0
    return today_vol, avg_vol, rvol


# ── Market context for one historical day ────────────────────────────────────

def _build_context(
    ticker:     str,
    d:          date,
    bars:       list,
    prev_close: float,
    rvol:       float,
    live:       bool = False,
) -> dict:
    """Build a regime-compatible market context dict for one historical day.

    gap_pct: computed from first RTH bar vs prior close.
    RVOL:    passed in from cached premarket bars.
    SPY/VIX: live (yfinance historical) if --live-context, else neutral.
    """
    gap_pct = 0.0
    if bars and prev_close > 0:
        gap_pct = round((bars[0].open - prev_close) / prev_close * 100, 2)

    ctx = {
        "spy_premarket_pct": "n/a",
        "vix":               "n/a",
    }

    if live:
        try:
            import yfinance as yf
            spy = yf.download("SPY", start=d - timedelta(days=5), end=d + timedelta(days=1),
                              interval="1d", progress=False, auto_adjust=True)
            vix = yf.download("^VIX", start=d - timedelta(days=5), end=d + timedelta(days=1),
                              interval="1d", progress=False, auto_adjust=True)
            spy_closes = spy["Close"].dropna()
            vix_closes = vix["Close"].dropna()
            if len(spy_closes) >= 2:
                idx = spy_closes.index.get_loc(str(d), method="nearest") if str(d) in spy_closes.index.astype(str).tolist() else -1
                if idx >= 1:
                    pct = (spy_closes.iloc[idx] / spy_closes.iloc[idx-1] - 1) * 100
                    ctx["spy_premarket_pct"] = f"{pct:+.2f}%"
            if len(vix_closes) >= 1:
                ctx["vix"] = f"{vix_closes.iloc[-1]:.2f}"
        except Exception:
            pass

    return ctx, gap_pct


# ── Main scenario loop ────────────────────────────────────────────────────────

def run_scenario(
    ticker:  str,
    start:   date,
    end:     date,
    verbose: bool = False,
    live:    bool = False,
) -> None:
    params = config.INTRADAY_PARAMS
    cfg    = ORBConfig.from_params(params)

    try:
        import ibkr_connector
        eurusd = ibkr_connector.get_eurusd_rate()
    except Exception:
        eurusd = 1.08

    initial_usd = config.HOUSE_MONEY_EUR * eurusd

    print(f"\n{'='*80}")
    print(f"  Regime Scenario Test — {ticker}  {start} → {end}")
    print(f"  Capital: ${initial_usd:,.0f}  Risk/trade: 1%  EUR/USD: {eurusd:.4f}")
    print(f"{'='*80}\n")

    days = _trading_days(start, end)

    # Load all RTH sessions up front (cache-only)
    sessions: dict[date, list] = {}
    for d in days:
        sessions[d] = data_loader.load_session(ticker, d)

    # Equity trackers per strategy + regime-adaptive
    equity = {"orb": initial_usd, "ib": initial_usd, "vwap": initial_usd, "regime": initial_usd}
    all_trades: dict[str, list] = {"orb": [], "ib": [], "vwap": [], "regime": []}

    # Table header
    hdr = (f"{'Date':<12} {'Gap%':>6} {'RVOL':>6} {'Days':>4} "
           f"{'Regime':<6} │ {'ORB':>10} │ {'IB':>10} │ {'VWAP':>10} │ {'Regime':>10}")
    print(hdr)
    print("─" * len(hdr))

    prev_close = 0.0

    for d in days:
        bars = sessions.get(d, [])
        if not bars:
            continue

        # Compute gap from first bar vs prior close
        first_bar_open = bars[0].open
        gap_pct = round((first_bar_open - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0.0
        prev_close = bars[-1].close  # update for next iteration

        # RVOL from cached premarket bars
        today_vol, avg_vol, rvol = _historical_rvol(ticker, d, lookback=20)
        cache_days = min(20, sum(
            1 for i in range(1, 21)
            for day in [d - timedelta(days=i * 2)]  # rough
            if data_loader._pm_cache_path(ticker, day).exists()
        ))
        # Simpler count
        premarket_days = sum(
            1 for day in _trading_days(d - timedelta(days=30), d - timedelta(days=1))
            if data_loader._pm_cache_path(ticker, day).exists()
        )

        # Build market context
        candidate = {"gap_pct": gap_pct, "rvol": rvol if rvol > 0 else None}
        mkt_ctx, _ = _build_context(ticker, d, bars, prev_close, rvol, live=live)
        strategy_today, score = pick_strategy(mkt_ctx, [candidate], params)

        # Run all 3 strategies + regime-adaptive
        results = {}
        for strat in ("orb", "ib", "vwap"):
            bias = "auto"
            r = _run_session(strat, bars, bias, _risk_usd(equity[strat]), cfg, verbose=verbose and strat == strategy_today.lower())
            results[strat] = r
            if r is not None:
                equity[strat] += r.net_pnl
                all_trades[strat].append(_result_to_dict(r, ticker, d, bias, strat, equity[strat]))

        # Regime-adaptive result
        regime_strat = strategy_today.lower() if strategy_today != "SIT_OUT" else None
        r_regime = results.get(regime_strat) if regime_strat else None
        if r_regime is not None:
            equity["regime"] += r_regime.net_pnl
            all_trades["regime"].append(_result_to_dict(r_regime, ticker, d, "auto", f"regime:{regime_strat}", equity["regime"]))

        # Format row
        def _fmt(r):
            if r is None:
                return "  no setup"
            sign = "+" if r.net_pnl >= 0 else ""
            return f"{sign}${r.net_pnl:.0f} ({r.exit_reason[:2]})"

        print(
            f"{d}  {gap_pct:>+6.1f}%  {rvol:>5.1f}x  {premarket_days:>4}d"
            f"  {strategy_today:<6} │ {_fmt(results['orb']):>10}"
            f" │ {_fmt(results['ib']):>10}"
            f" │ {_fmt(results['vwap']):>10}"
            f" │ {_fmt(r_regime):>10}"
        )

    # Summary totals
    print(f"\n{'─'*80}")
    print(f"  {'TOTALS':}")
    for label, strat_key in [("Always ORB", "orb"), ("Always IB", "ib"),
                              ("Always VWAP", "vwap"), ("Regime-adaptive", "regime")]:
        trades = all_trades[strat_key]
        final  = equity[strat_key]
        pnl    = final - initial_usd
        sign   = "+" if pnl >= 0 else ""
        n      = len(trades)
        wins   = sum(1 for t in trades if t["net_pnl"] > 0)
        wr     = f"{wins/n*100:.0f}%" if n > 0 else "n/a"
        print(f"  {label:<18}  {n:>2} trades  {wr:>4} WR  "
              f"net {sign}${pnl:,.0f}  final ${final:,.0f}")

    # Save CSVs
    out = Path("results")
    for strat_key in ("orb", "ib", "vwap", "regime"):
        if all_trades[strat_key]:
            path = out / f"{ticker}_{start}_{end}_scenario_{strat_key}.csv"
            _save_trades_csv(all_trades[strat_key], path)

    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Regime-aware scenario backtest — compare ORB / IB / VWAP per day",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--ticker",        default="NVDA")
    p.add_argument("--start",         default="2026-06-05", help="YYYY-MM-DD")
    p.add_argument("--end",           default="2026-06-24", help="YYYY-MM-DD")
    p.add_argument("--verbose",       action="store_true",
                   help="Per-bar trace for the regime-picked strategy each day")
    p.add_argument("--live-context",  action="store_true",
                   help="Fetch historical SPY/VIX from yfinance for richer regime scoring")
    args = p.parse_args()

    run_scenario(
        ticker  = args.ticker,
        start   = date.fromisoformat(args.start),
        end     = date.fromisoformat(args.end),
        verbose = args.verbose,
        live    = args.live_context,
    )


if __name__ == "__main__":
    main()
