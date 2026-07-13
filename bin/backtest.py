"""
backtest.py — Multi-strategy backtester (ORB / IB / VWAP).

Strategies
──────────
  orb   5-min Opening Range Breakout (default)
  ib    60-min Initial Balance Breakout (no RVOL gate, fires after 10:30 ET)
  vwap  VWAP Reversion — fade price when 1.5 %+ away from VWAP

Direction modes (no AI — avoids lookahead bias):
  auto        first breakout / first overextension wins  (default)
  mechanical  pre-set from opening candle colour (green=long, red=short)
  long/short  always that side

Usage — cached NVDA data (2026-06-05 → 2026-06-24)
─────────────────────────────────────────────────────
  # ORB (5-min range):
  python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --no-ibkr

  # IB (60-min Initial Balance):
  python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --strategy ib --no-ibkr

  # VWAP reversion:
  python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --strategy vwap --no-ibkr

  # Compare other cached tickers:
  python backtest.py --ticker MU   --start 2026-06-09 --end 2026-06-24 --strategy ib   --no-ibkr
  python backtest.py --ticker GLW  --start 2026-06-09 --end 2026-06-24 --strategy vwap --no-ibkr
  python backtest.py --ticker AMAT --start 2026-06-09 --end 2026-06-24 --strategy ib   --no-ibkr

  # Per-bar trace for one day:
  python backtest.py --ticker NVDA --start 2026-06-10 --end 2026-06-10 --strategy vwap --no-ibkr --verbose

Walk-forward (run twice with different date ranges):
  python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-12 --strategy ib --no-ibkr
  python backtest.py --ticker NVDA --start 2026-06-15 --end 2026-06-24 --strategy ib --no-ibkr

Regime scenario test (auto-picks strategy per day via RVOL + gap):
  python test_scenario.py --ticker NVDA --start 2026-06-05 --end 2026-06-24
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import argparse
import csv
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import config
import data_loader
from strategy_orb import ORBConfig, Bar, simulate_session


# ── Date helpers ──────────────────────────────────────────────────────────────

def _trading_days(start: date, end: date) -> list[date]:
    """Mon–Fri dates in [start, end] inclusive."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# ── Mechanical direction ──────────────────────────────────────────────────────

def _mechanical_bias(opening_bars: list[Bar]) -> str:
    """Long if the first opening candle is green (close >= open), else short.
    Used when --bias mechanical. For true ORB (first breakout wins), use --bias auto.
    """
    if not opening_bars:
        return "long"
    b = opening_bars[0]
    return "long" if b.close >= b.open else "short"


# ── Risk budget ───────────────────────────────────────────────────────────────

def _risk_usd(equity_usd: float) -> float:
    """1 % of current equity = per-trade risk budget passed to simulate_session."""
    return equity_usd * config.RISK_PER_TRADE_PCT


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(
    trades:       list[dict],
    equity_curve: list[float],
    session_days: int,
) -> dict:
    if not trades:
        return {"error": "no trades"}

    net_pnls = [t["net_pnl"]    for t in trades]
    r_mults  = [t["r_multiple"] for t in trades]
    wins     = [p for p in net_pnls if p > 0]
    losses   = [p for p in net_pnls if p <= 0]

    initial = equity_curve[0]
    final   = equity_curve[-1]

    ann_exp     = 252 / max(session_days, 1)
    ann_return  = (final / initial) ** ann_exp - 1 if initial > 0 else 0.0

    # Sharpe: group by date; include session days with no trade as zero
    daily: dict[str, float] = {}
    for t in trades:
        daily[t["date"]] = daily.get(t["date"], 0.0) + t["net_pnl"]
    daily_pnls = list(daily.values())
    if len(daily_pnls) > 1:
        avg_d  = statistics.mean(daily_pnls)
        std_d  = statistics.stdev(daily_pnls)
        sharpe = (avg_d / std_d * math.sqrt(252)) if std_d > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown over trade-equity checkpoints
    peak, max_dd = equity_curve[0], 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    gross_wins   = sum(wins)              if wins   else 0.0
    gross_losses = abs(sum(losses))       if losses else 0.0
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else float("inf")
    )

    # Exit reason breakdown
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    return {
        "trades":            len(trades),
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate":          round(len(wins) / len(trades), 4),
        "avg_r":             round(statistics.mean(r_mults), 4),
        "profit_factor":     round(profit_factor, 3),
        "total_net_pnl":     round(sum(net_pnls), 2),
        "initial_usd":       round(initial, 2),
        "final_usd":         round(final, 2),
        "return_pct":        round((final / initial - 1) * 100, 2),
        "ann_return_pct":    round(ann_return * 100, 2),
        "sharpe":            round(sharpe, 3),
        "max_drawdown_pct":  round(max_dd * 100, 2),
        "trades_per_day":    round(len(trades) / max(session_days, 1), 3),
        "exit_reasons":      reasons,
    }


# ── CSV output ────────────────────────────────────────────────────────────────

def _save_trades_csv(trades: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(trades[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)
    print(f"  Trades CSV → {path}")


# ── Main backtest loop ────────────────────────────────────────────────────────

def _run_session(
    strategy:    str,
    bars:        list[Bar],
    bias:        str,
    capital_usd: float,
    cfg:         ORBConfig,
    verbose:     bool,
):
    """Route one session to the correct simulate function."""
    if strategy == "ib":
        from strategy_ib import simulate_ib_session
        return simulate_ib_session(bars, capital_usd, config.INTRADAY_PARAMS,
                                   bias=bias, verbose=verbose)
    elif strategy == "vwap":
        from strategy_vwap import simulate_vwap_session
        return simulate_vwap_session(bars, capital_usd, config.INTRADAY_PARAMS,
                                     bias=bias, verbose=verbose)
    else:  # orb
        if len(bars) < cfg.range_minutes + 1:
            return None
        opening = bars[:cfg.range_minutes]
        post    = bars[cfg.range_minutes:]
        return simulate_session(opening, post, capital_usd, cfg,
                                bias=bias, verbose=verbose)


def _result_to_dict(result, ticker: str, d: date, bias: str,
                    strategy: str, equity_usd: float) -> dict:
    """Normalise a TradeResult or VWAPTradeResult to a flat CSV-ready dict."""
    return {
        "date":              d.isoformat(),
        "ticker":            ticker,
        "strategy":          strategy,
        "bias":              bias,
        "direction":         result.direction,
        "entry":             round(result.entry, 4),
        "stop":              round(result.stop, 4),
        "target":            round(result.target, 4),
        "exit_price":        round(result.exit_price, 4),
        "exit_reason":       result.exit_reason,
        "qty":               result.qty,
        "r_multiple":        round(result.r_multiple, 4),
        "gross_pnl":         round(result.gross_pnl, 2),
        "commission":        round(result.commission, 2),
        "net_pnl":           round(result.net_pnl, 2),
        "equity_usd":        round(equity_usd, 2),
        "rvol_at_breakout":  round(getattr(result, "rvol_at_breakout", 0), 3),
    }


def run_backtest(
    ticker:    str,
    start:     date,
    end:       date,
    strategy:  str  = "orb",
    bias_mode: str  = "auto",
    ib              = None,
    verbose:   bool = False,
) -> dict:
    """Run a backtest and return metrics + trade list + equity curve.

    Args:
        ticker:    Stock symbol (e.g. "NVDA").
        start/end: Date range inclusive.
        strategy:  "orb" | "ib" | "vwap"
        bias_mode: "auto" | "mechanical" | "long" | "short"
        ib:        Connected ib_async IB instance, or None for cache-only.
        verbose:   Print per-bar trace.
    """
    cfg = ORBConfig.from_params(config.INTRADAY_PARAMS)

    try:
        import ibkr_connector
        eurusd = ibkr_connector.get_eurusd_rate()
    except Exception:
        eurusd = 1.08
    print(f"  EUR/USD rate (fixed for run): {eurusd:.4f}")

    initial_usd  = config.HOUSE_MONEY_EUR * eurusd
    equity_usd   = initial_usd
    equity_curve = [equity_usd]

    days     = _trading_days(start, end)
    sessions = data_loader.fetch_date_range(ib, ticker, days, verbose=verbose)

    trades: list[dict] = []

    for d in days:
        bars = sessions.get(d, [])
        if not bars:
            continue

        opening_bars_for_bias = bars[:cfg.range_minutes] if len(bars) >= cfg.range_minutes else bars
        bias = (
            _mechanical_bias(opening_bars_for_bias)
            if bias_mode == "mechanical"
            else bias_mode
        )

        if verbose:
            print(f"\n{'─'*52}\n{d}  strategy={strategy.upper()}"
                  f"  bias={bias.upper()}  equity=${equity_usd:,.2f}")

        result = _run_session(strategy, bars, bias, _risk_usd(equity_usd), cfg, verbose)

        if result is not None:
            equity_usd += result.net_pnl
            equity_curve.append(equity_usd)
            trades.append(_result_to_dict(result, ticker, d, bias, strategy, equity_usd))

    metrics = compute_metrics(trades, equity_curve, len(days))
    return {
        "metrics":      metrics,
        "trades":       trades,
        "equity_curve": equity_curve,
        "eurusd":       eurusd,
        "strategy":     strategy,
    }


# ── Print summary ─────────────────────────────────────────────────────────────

def _print_summary(
    ticker: str, start: date, end: date, bias_mode: str, result: dict,
    strategy: str = "orb",
) -> None:
    m = result["metrics"]
    print(f"\n{'='*56}")
    print(f"  {strategy.upper()} Backtest — {ticker}  {start} → {end}  bias={bias_mode}")
    print(f"{'='*56}")
    if "error" in m:
        print(f"  ⚠️  {m['error']}")
        print(f"{'='*56}\n")
        return

    print(f"  Trades:           {m['trades']}  ({m['trades_per_day']:.3f}/day)")
    print(f"  Win rate:         {m['win_rate']*100:.1f}%  "
          f"({m['wins']}W / {m['losses']}L)")
    print(f"  Avg R:            {m['avg_r']:+.3f}")
    print(f"  Profit factor:    {m['profit_factor']:.3f}")
    print(f"  Net P&L:          ${m['total_net_pnl']:+,.2f}")
    print(f"  Return:           {m['return_pct']:+.2f}%  "
          f"({m['ann_return_pct']:+.1f}% ann.)")
    print(f"  Sharpe:           {m['sharpe']:.3f}")
    print(f"  Max drawdown:     {m['max_drawdown_pct']:.2f}%")
    print(f"  Start equity:     ${m['initial_usd']:,.2f}")
    print(f"  Final equity:     ${m['final_usd']:,.2f}")

    reasons = m.get("exit_reasons", {})
    if reasons:
        reason_str = "  ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        print(f"  Exits:            {reason_str}")
    print(f"{'='*56}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Signal Mesh Day — ORB strategy backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--ticker",   required=True,              help="Stock symbol")
    p.add_argument("--start",    required=True,              help="Start date YYYY-MM-DD")
    p.add_argument("--end",      required=True,              help="End date YYYY-MM-DD")
    p.add_argument("--strategy", default="orb",
                   choices=["orb", "ib", "vwap"],
                   help="orb=5-min ORB (default); ib=60-min Initial Balance; vwap=VWAP reversion")
    p.add_argument("--bias",     default="auto",
                   choices=["auto", "mechanical", "long", "short"],
                   help="auto=first breakout/overextension (default); mechanical=opening candle; long/short=force")
    p.add_argument("--no-ibkr",  action="store_true",        help="Cache-only (no IBKR connect)")
    p.add_argument("--out",      default=str(__import__("setup_paths").RESULTS_DIR),          help="Output dir for CSV")
    p.add_argument("--verbose",  action="store_true",        help="Per-bar trace")
    args = p.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    ib = None
    if not args.no_ibkr:
        try:
            import ibkr_connector
            print("Connecting to IBKR...")
            ib = ibkr_connector.connect()
            print("Connected.")
        except Exception as e:
            print(f"⚠️  IBKR connection failed ({e}). Running in cache-only mode.")

    print(f"\nRunning backtest: {args.ticker}  {start} → {end}"
          f"  strategy={args.strategy}  bias={args.bias}\n")
    result = run_backtest(
        ticker    = args.ticker,
        start     = start,
        end       = end,
        strategy  = args.strategy,
        bias_mode = args.bias,
        ib        = ib,
        verbose   = args.verbose,
    )

    if ib is not None:
        ib.disconnect()

    _print_summary(args.ticker, start, end, args.bias, result, strategy=args.strategy)

    if result["trades"]:
        out_path = (
            Path(args.out)
            / f"{args.ticker}_{args.start}_{args.end}_{args.strategy}_{args.bias}.csv"
        )
        _save_trades_csv(result["trades"], out_path)


if __name__ == "__main__":
    main()
