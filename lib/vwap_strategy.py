"""
vwap_strategy.py — VWAP Reversion engine.

VWAP (Volume Weighted Average Price) is the average price of all trades
today, weighted by volume.  It resets at 9:30 ET.  Price gravitates back
to VWAP — overextensions above/below VWAP tend to mean-revert.

Strategy logic
──────────────
  1. Compute running VWAP from all 1-min bars since open.
  2. When price closes more than vwap_min_deviation % away from VWAP,
     flag a potential reversion entry.
  3. Optionally wait for a reversal candle (close moving back toward VWAP).
  4. Entry at current close; stop at the recent extreme; target at VWAP
     (or tp_r_multiple × risk if VWAP is too close).

Works best on: choppy / range-bound days — exactly when ORB and IB fail.
Does NOT need premarket RVOL data; all signals come from live session bars.

Key differences from ORB/IB
────────────────────────────
  - Mean-reversion (fade the extension), not breakout (follow the move)
  - No fixed opening range; VWAP is computed continuously from bar 1
  - Can fire multiple times per session (not capped to 1 per the code,
    but MAX_TRADES_PER_SYMBOL_PER_DAY = 1 in config still applies)
  - Narrower average stop → more shares per trade at the same dollar risk

VWAP formula (typical/price weighting):
  VWAP = sum((H + L + C) / 3 × V) / sum(V)
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

from orb_core import Bar, TradeResult, position_size_usd
from strategy_base import StrategySignal, STRATEGY_REGISTRY


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class VWAPConfig:
    """All tunable VWAP-reversion knobs.  Read from config.INTRADAY_PARAMS."""
    min_deviation:       float = 0.015   # 1.5 % from VWAP to trigger
    tp_r_multiple:       float = 1.5     # more conservative than ORB (2R)
    require_reversal:    bool  = True     # wait for a bar closing back toward VWAP
    warmup_bars:         int   = 15       # bars before looking for trades
    stop_atr_mult:       float = 1.0     # stop = extreme ± atr_mult × bar range
    min_atr_bars:        int   = 10       # bars for ATR estimate
    slippage_pct:        float = 0.0002
    commission_per_share: float = 0.005

    @classmethod
    def from_params(cls, p) -> "VWAPConfig":
        return cls(
            min_deviation        = getattr(p, "vwap_min_deviation",      0.015),
            tp_r_multiple        = getattr(p, "vwap_tp_r_multiple",      1.5),
            require_reversal     = getattr(p, "vwap_require_reversal",   True),
            warmup_bars          = getattr(p, "vwap_warmup_bars",         15),
            stop_atr_mult        = getattr(p, "vwap_stop_atr_mult",      1.0),
            min_atr_bars         = getattr(p, "vwap_min_atr_bars",        10),
            slippage_pct         = getattr(p, "slippage_pct",             0.0002),
            commission_per_share = getattr(p, "commission_per_share",     0.005),
        )


# ── VWAP computation ──────────────────────────────────────────────────────────

def compute_vwap(bars: Sequence[Bar]) -> float:
    """Compute VWAP from a sequence of 1-min bars (typical-price weighting).

    VWAP = sum((H + L + C) / 3 × V) / sum(V)
    Returns 0.0 if bars are empty or total volume is zero.
    """
    cum_pv  = sum((b.high + b.low + b.close) / 3.0 * b.volume for b in bars)
    cum_vol = sum(b.volume for b in bars)
    return cum_pv / cum_vol if cum_vol > 0 else 0.0


def _atr_estimate(bars: Sequence[Bar]) -> float:
    """Simple ATR estimate: mean of last N bar high-low ranges."""
    ranges = [b.high - b.low for b in bars if b.high > b.low]
    return statistics.mean(ranges) if ranges else 0.01


# ── Signal detection ──────────────────────────────────────────────────────────

def detect_vwap_setup(
    bars:  Sequence[Bar],
    cfg:   VWAPConfig,
    bias:  str = "auto",
) -> tuple[Optional[str], float, float]:
    """
    Check if the latest bar is a valid VWAP reversion entry.

    Args:
        bars:  All bars from open up to and including the current bar.
        cfg:   VWAPConfig.
        bias:  "long" | "short" | "auto"
               "auto" fades whichever direction is overextended.
               "long" / "short" only enter in that direction
               (used when the premarket screener set a directional bias).

    Returns:
        (direction, entry_price, vwap)  if a setup is found, else (None, 0, 0).
    """
    if len(bars) < cfg.warmup_bars + 1:
        return None, 0.0, 0.0

    vwap = compute_vwap(bars[:-1])   # VWAP excludes the triggering bar
    bar  = bars[-1]

    if vwap <= 0:
        return None, 0.0, 0.0

    deviation = (bar.close - vwap) / vwap

    if bias == "auto":
        if deviation <= -cfg.min_deviation:
            direction = "long"
        elif deviation >= cfg.min_deviation:
            direction = "short"
        else:
            return None, 0.0, 0.0
    else:
        if abs(deviation) < cfg.min_deviation:
            return None, 0.0, 0.0
        # Direction-gated: only fade if price moved the right way
        if bias == "long"  and deviation >= 0:
            return None, 0.0, 0.0
        if bias == "short" and deviation <= 0:
            return None, 0.0, 0.0
        direction = bias

    # Reversal confirmation: last bar must be moving back toward VWAP
    if cfg.require_reversal and len(bars) >= 2:
        prev = bars[-2]
        if direction == "long"  and bar.close <= prev.close:
            return None, 0.0, 0.0
        if direction == "short" and bar.close >= prev.close:
            return None, 0.0, 0.0

    return direction, bar.close, vwap


# ── Strategy Protocol implementation ─────────────────────────────────────────

class VWAPStrategy:
    """VWAP Reversion strategy.  Implements the Strategy protocol."""
    name = "vwap"

    def evaluate(
        self,
        bars:   list[Bar],
        bias:   str,
        params,
    ) -> StrategySignal:
        """
        Evaluate VWAP setup from accumulated intraday bars.

        Called by the live engine on every new 1-min bar.
        Returns StrategySignal(direction="skip") if no setup is present.
        """
        cfg = VWAPConfig.from_params(params)

        direction, entry, vwap = detect_vwap_setup(bars, cfg, bias)
        if direction is None:
            return StrategySignal(direction="skip")

        atr = _atr_estimate(bars[-cfg.min_atr_bars:])
        slip = cfg.slippage_pct * entry

        if direction == "long":
            entry_filled = entry + slip
            stop         = entry_filled - cfg.stop_atr_mult * atr
            risk         = entry_filled - stop
            target       = entry_filled + cfg.tp_r_multiple * risk
        else:
            entry_filled = entry - slip
            stop         = entry_filled + cfg.stop_atr_mult * atr
            risk         = stop - entry_filled
            target       = entry_filled - cfg.tp_r_multiple * risk

        if risk <= 0:
            return StrategySignal(direction="skip")

        return StrategySignal(
            direction = direction,
            entry     = round(entry_filled, 4),
            stop      = round(stop, 4),
            target    = round(target, 4),
        )


STRATEGY_REGISTRY["vwap"] = VWAPStrategy


# ── Backtest simulation ────────────────────────────────────────────────────────

@dataclass
class VWAPTradeResult:
    direction:      str
    entry:          float
    stop:           float
    target:         float
    qty:            int
    exit_price:     float
    exit_reason:    str     # "tp" | "sl" | "vwap_touch" | "session_end"
    risk_per_share: float
    r_multiple:     float
    gross_pnl:      float
    commission:     float
    net_pnl:        float
    entry_bar_idx:  int = 0
    vwap_at_entry:  float = 0.0


def simulate_vwap_session(
    bars:        list[Bar],
    capital_usd: float,
    params,
    bias:        str  = "auto",
    verbose:     bool = False,
) -> Optional[VWAPTradeResult]:
    """
    Replay one session and return the first VWAP trade taken, or None.

    Args:
        bars:        RTH 1-min bars from 9:30 ET.
        capital_usd: Risk budget per trade in USD.
        params:      config.INTRADAY_PARAMS.
        bias:        "auto" | "long" | "short".
        verbose:     Print per-bar trace.
    """
    cfg = VWAPConfig.from_params(params)

    for i in range(cfg.warmup_bars, len(bars)):
        window = bars[:i + 1]
        direction, entry, vwap = detect_vwap_setup(window, cfg, bias)
        if direction is None:
            continue

        atr  = _atr_estimate(bars[max(0, i - cfg.min_atr_bars):i])
        slip = cfg.slippage_pct * entry

        if direction == "long":
            entry_filled = entry + slip
            stop         = entry_filled - cfg.stop_atr_mult * atr
            risk         = entry_filled - stop
            target       = entry_filled + cfg.tp_r_multiple * risk
        else:
            entry_filled = entry - slip
            stop         = entry_filled + cfg.stop_atr_mult * atr
            risk         = stop - entry_filled
            target       = entry_filled - cfg.tp_r_multiple * risk

        if risk <= 0:
            continue

        qty = position_size_usd(capital_usd, risk)
        if qty == 0:
            continue

        if verbose:
            print(f"      [{bars[i].t}] VWAP setup: {direction.upper()}"
                  f"  entry={entry_filled:.4f}  stop={stop:.4f}"
                  f"  target={target:.4f}  qty={qty}"
                  f"  VWAP={vwap:.4f}")

        result = _manage_vwap_trade(
            bars, i + 1, direction, vwap,
            entry_filled, stop, target, qty, risk, cfg, verbose,
        )
        result.entry_bar_idx = i
        result.vwap_at_entry = vwap
        return result

    if verbose:
        print("      No VWAP setup found in session.")
    return None


def _manage_vwap_trade(
    bars:      list[Bar],
    start:     int,
    direction: str,
    vwap:      float,
    entry:     float,
    stop:      float,
    target:    float,
    qty:       int,
    risk:      float,
    cfg:       VWAPConfig,
    verbose:   bool,
) -> VWAPTradeResult:
    exit_price, reason = entry, "session_end"

    for bar in bars[start:]:
        # Update running VWAP (only bars up to and including current)
        running_vwap = compute_vwap(bars[:bars.index(bar) + 1])

        if direction == "long":
            hit_sl   = bar.low  <= stop
            hit_tp   = bar.high >= target
            hit_vwap = bar.close >= running_vwap   # price returned to VWAP
        else:
            hit_sl   = bar.high >= stop
            hit_tp   = bar.low  <= target
            hit_vwap = bar.close <= running_vwap

        if hit_sl:
            exit_price, reason = stop,           "sl"
            break
        if hit_tp:
            exit_price, reason = target,         "tp"
            break
        if hit_vwap and reason != "tp":
            exit_price, reason = running_vwap,   "vwap_touch"
            break

    gross      = (exit_price - entry) * qty if direction == "long" else (entry - exit_price) * qty
    commission = cfg.commission_per_share * qty * 2
    net        = gross - commission
    r_multiple = gross / (risk * qty) if risk * qty else 0.0

    if verbose:
        icon = "✅" if net > 0 else "❌"
        print(f"      RESULT: {reason}  gross={gross:+.2f}  net={net:+.2f}"
              f"  R={r_multiple:.2f}  {icon}")

    return VWAPTradeResult(
        direction, entry, stop, target, qty,
        exit_price, reason, risk, r_multiple, gross, commission, net,
    )
