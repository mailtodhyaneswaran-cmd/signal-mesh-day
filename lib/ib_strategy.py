"""
ib_strategy.py — Initial Balance (IB) Breakout engine.

The Initial Balance is the high/low range of the first 60 minutes after
open (9:30–10:30 ET).  Once that range is established, a close outside
it — confirmed by a retest — signals a directional move.

Differences from ORB (5-min range):
  - range_minutes = 60  (configurable via config.ib_range_minutes)
  - rvol_mode = "disabled"  — no premarket RVOL gate; all signals are post-open
  - Fires once per session after 10:30 ET (not immediately at 9:30)
  - Wider stop (opposite IB edge) → fewer shares, same dollar risk

Everything else reuses orb_core primitives identically:
  capture_opening_range, detect_breakout, confirm_retest, build_bracket

Live engine timing (NL):
  17:30 NL = 10:30 ET  — IB range closes, start watching for breakout
  18:30 NL = 12:30 ET  — suggested cutoff (FALLBACK_WINDOW_NL["IB"] = 17:00 NL)

Run via the unified launcher (live_engine.py).
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

from orb_core import (
    ORBConfig, Bar, OpeningRange,
    capture_opening_range, detect_breakout, confirm_retest, build_bracket,
)
from strategy_base import StrategySignal, STRATEGY_REGISTRY

IB_DEFAULT_RANGE_MINUTES = 60


def _ib_config(params) -> ORBConfig:
    """Build an ORBConfig suitable for IB: 60-min range, RVOL disabled."""
    cfg = ORBConfig.from_params(params)
    cfg.range_minutes = getattr(params, "ib_range_minutes", IB_DEFAULT_RANGE_MINUTES)
    cfg.rvol_mode     = "disabled"   # no premarket RVOL gate for IB
    cfg.rvol_min      = 0.0          # always passes the live breakout check
    return cfg


class IBStrategy:
    """60-minute Initial Balance Breakout.

    Implements the Strategy protocol from strategy_base.py.
    Live engine and backtester both call evaluate() only — identical logic.
    """
    name = "ib"

    def evaluate(
        self,
        bars:   list[Bar],
        bias:   str,
        params,
    ) -> StrategySignal:
        """
        Evaluate IB signal from accumulated intraday bars.

        bars[0..range_minutes-1]  = Initial Balance bars (first 60 min)
        bars[range_minutes..]     = post-IB bars to scan for breakout/retest

        Returns StrategySignal(direction="skip") when no valid setup is found.
        """
        cfg = _ib_config(params)

        if len(bars) < cfg.range_minutes + 1:
            return StrategySignal(direction="skip")

        opening = bars[:cfg.range_minutes]
        post    = bars[cfg.range_minutes:]

        orng = capture_opening_range(opening, cfg)
        if orng is None:
            return StrategySignal(direction="skip")

        direction_confirmed = None
        for bar in post:
            if direction_confirmed is None:
                if detect_breakout(orng, bar, bias):
                    direction_confirmed = bias
                continue

            retest = confirm_retest(orng, bar, cfg, direction_confirmed)
            if retest is False:
                return StrategySignal(direction="skip")
            if retest is True:
                entry  = orng.high if direction_confirmed == "long" else orng.low
                risk_usd = _ib_risk_usd(params)
                bracket  = build_bracket(orng, entry, direction_confirmed, cfg, risk_usd)
                if bracket["qty"] == 0:
                    return StrategySignal(direction="skip")
                return StrategySignal(
                    direction = direction_confirmed,
                    entry     = bracket["entry"],
                    stop      = bracket["stop"],
                    target    = bracket["take_profit"],
                    qty       = bracket["qty"],
                )

        return StrategySignal(direction="skip")


def _ib_risk_usd() -> float:
    """Per-trade risk budget in USD for IB (same formula as ORB)."""
    try:
        import config
        import ibkr_connector
        return config.HOUSE_MONEY_EUR * config.RISK_PER_TRADE_PCT * ibkr_connector.get_eurusd_rate()
    except Exception:
        return 50.0   # safe fallback: €50 at 1.0 rate


# ── Backtest simulation ────────────────────────────────────────────────────────

def simulate_ib_session(
    bars:         list[Bar],
    capital_usd:  float,
    params,
    bias:         str  = "auto",
    verbose:      bool = False,
):
    """Replay one IB session.  Thin wrapper over orb_core.simulate_session.

    bias = "auto": take whichever side breaks the IB range first.
    Returns orb_core.TradeResult or None.
    """
    from orb_core import simulate_session

    cfg = _ib_config(params)

    if len(bars) < cfg.range_minutes + 1:
        if verbose:
            print(f"      Not enough bars for {cfg.range_minutes}-min IB ({len(bars)} bars)")
        return None

    opening = bars[:cfg.range_minutes]
    post    = bars[cfg.range_minutes:]

    return simulate_session(
        opening_bars = opening,
        post_bars    = post,
        capital_usd  = capital_usd,
        cfg          = cfg,
        bias         = bias,
        verbose      = verbose,
    )


STRATEGY_REGISTRY["ib"] = IBStrategy
